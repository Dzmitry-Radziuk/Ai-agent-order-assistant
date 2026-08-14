"""Проверяет поведение, связанное с модулем «test submission guards»."""

from restaurant_bot.domain.models import (
    CatalogProduct,
    ConversationState,
    ExtractedItem,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    PendingSubmission,
    TelegramEvent,
)
from restaurant_bot.services.engine import ConversationEngine


def _event() -> TelegramEvent:
    """Создаёт тестовое событие Telegram."""
    return TelegramEvent(update_id=1, chat_id="123456", input_type=InputKind.TEXT)


def test_submit_request_stops_on_first_unresolved_item(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что отправка заявки останавливается на первой нерешённой позиции."""
    engine = ConversationEngine(settings)
    result = engine.handle(
        _event(),
        ParsedCommand(intent=Intent.ADD_ITEMS, items=[ExtractedItem(product_query="Сироп Роза")]),
        ConversationState(),
        [CatalogProduct(product_id="rose", name="Сироп Роза", supplier="Сиропы", unit="шт")],
    )

    guarded = engine.handle(_event(), ParsedCommand(intent=Intent.SUBMIT_REQUEST), result.state, [])

    assert guarded.enqueue_submission is False
    assert guarded.state.cart[0].status is ItemStatus.MISSING_QTY
    assert "Укажите количество" in guarded.reply.text


def test_submit_as_is_cannot_bypass_unresolved_item_guard(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что отправка как является не может обходит неразрешённая позиция проверка."""
    engine = ConversationEngine(settings)
    catalog = [CatalogProduct(product_id="rose", name="Сироп Роза", supplier="Сиропы", unit="шт")]
    result = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(product_query="Сироп Роза", quantity=5, unit="шт"),
                ExtractedItem(product_query="Креветки", quantity=2, unit="кг"),
            ],
        ),
        ConversationState(),
        catalog,
    )

    guarded = engine.handle(
        _event(), ParsedCommand(intent=Intent.SUBMIT_AS_IS), result.state, catalog
    )

    assert guarded.enqueue_submission is False
    assert guarded.state.pending_submission is None
    assert guarded.state.current_issue_item_id == guarded.state.cart[1].id
    assert "Товар не найден" in guarded.reply.text


def test_submission_retry_reuses_checkpoint_without_clearing_draft(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что повторная отправка использует checkpoint без очистки черновика."""
    engine = ConversationEngine(settings)
    cart_item = engine._build_item(ExtractedItem(product_query="Сироп Роза", quantity=5, unit="шт"))
    cart_item.status = ItemStatus.MATCHED
    state = ConversationState(
        cart=[cart_item],
        pending_submission=PendingSubmission(
            order_no="20260722-0001",
            rows=[{"Наименование у поставщика": "Сироп Роза", "Кол-во": 5}],
        ),
    )

    retry = engine._prepare_submission(_event(), state)

    assert retry.enqueue_submission is True
    assert retry.state.stage.value == "submitting"
    assert retry.state.pending_submission is not None
    assert retry.state.pending_submission.order_no == "20260722-0001"
    assert retry.state.cart[0].status is ItemStatus.MATCHED
    assert retry.reply.text == (
        "🔸 <b><u>Отправка не завершена</u></b>\n\n"
        "Заявка: 20260722-0001\n\n"
        "Нажмите «Повторить отправку». Уже выполненные этапы будут пропущены."
    )
    assert [[button.text, button.callback_data] for row in retry.reply.rows for button in row] == [
        ["Повторить отправку", "v2:submit"],
        ["К черновику", "v2:back"],
    ]


def test_submission_retry_is_blocked_after_uncertain_dispatch(settings) -> None:  # type: ignore[no-untyped-def]
    """Не разрешает повторный POST после неоднозначного ответа центрального скрипта."""
    engine = ConversationEngine(settings)
    state = ConversationState(
        pending_submission=PendingSubmission(
            order_no="20260722-0002",
            failed_stage="dispatch_uncertain",
            last_error="read timeout",
            rows=[{"Наименование у поставщика": "Сироп Роза", "Кол-во": 5}],
        ),
    )

    guarded = engine._prepare_submission(_event(), state)

    assert guarded.enqueue_submission is False
    assert guarded.state.pending_submission is not None
    assert guarded.state.pending_submission.order_no == "20260722-0002"
    assert "не отправляйте её повторно" in guarded.reply.text.lower()
    assert guarded.reply.rows == []


def test_disabled_external_dispatch_still_enqueues_local_table_write(settings) -> None:  # type: ignore[no-untyped-def]
    """Создаёт задачу записи в таблицу при выключенной внешней отправке."""
    disabled_settings = settings.model_copy(update={"google_order_submission_enabled": False})
    engine = ConversationEngine(disabled_settings)
    cart_item = engine._build_item(ExtractedItem(product_query="Сироп Роза", quantity=5, unit="шт"))
    cart_item.status = ItemStatus.MATCHED
    state = ConversationState(cart=[cart_item])

    guarded = engine._prepare_submission(_event(), state)

    assert guarded.enqueue_submission is True
    assert guarded.state.pending_submission is not None
    assert guarded.state.pending_submission.rows[0]["Кол-во"] == 5
    assert guarded.state.cart == [cart_item]
    assert "Подготавливаю заявку" in guarded.reply.text
    assert "Отправляю заявку" not in guarded.reply.text
