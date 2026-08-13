"""Проверяет поведение, связанное с модулем «test comment scope clarification»."""

from restaurant_bot.domain.models import (
    CartItem,
    CatalogProduct,
    ConversationState,
    ExtractedItem,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    SessionStage,
    TelegramEvent,
)
from restaurant_bot.services.engine import ConversationEngine


def _event(text: str, update_id: int = 1) -> TelegramEvent:
    """Создаёт текстовое событие для уточнения комментария."""
    return TelegramEvent(
        update_id=update_id,
        chat_id="comment-scope",
        input_type=InputKind.TEXT,
        text=text,
    )


def _catalog() -> list[CatalogProduct]:
    """Возвращает два однозначных товара для проверки изменения черновика."""
    return [
        CatalogProduct(product_id="tomato", name="Помидоры", unit="кг", supplier="Овощи"),
        CatalogProduct(product_id="cucumber", name="Огурцы", unit="кг", supplier="Овощи"),
    ]


def _ambiguous_command() -> ParsedCommand:
    """Создаёт команду с товарами и неоднозначным общим пожеланием."""
    return ParsedCommand(
        intent=Intent.ADD_ITEMS,
        items=[
            ExtractedItem(product_query="Помидоры", quantity=5, unit="кг"),
            ExtractedItem(product_query="Огурцы", quantity=4, unit="кг"),
        ],
        comment_clarification="положить отдельно",
    )


def _start_clarification(settings, state: ConversationState | None = None):  # type: ignore[no-untyped-def]
    """Сохраняет ожидающие товары без добавления их в черновик."""
    return ConversationEngine(settings).handle(
        _event("Помидоры 5 кг, огурцы 4 кг, положить отдельно"),
        _ambiguous_command(),
        state or ConversationState(),
        _catalog(),
    )


def test_ambiguous_comment_items_are_held_outside_the_draft(settings) -> None:  # type: ignore[no-untyped-def]
    """Не изменяет черновик до ответа об области комментария."""
    result = _start_clarification(settings)

    assert result.state.stage is SessionStage.AWAIT_COMMENT_SCOPE
    assert result.state.cart == []
    assert [item.product_query for item in result.state.pending_comment_items] == [
        "Помидоры",
        "Огурцы",
    ]
    assert "К каким товарам относится" in result.reply.text


def test_natural_all_items_answer_applies_comment_to_every_pending_item(settings) -> None:  # type: ignore[no-untyped-def]
    """Применяет комментарий ко всем ожидающим товарам после уверенного решения."""
    engine = ConversationEngine(settings)
    started = _start_clarification(settings)
    command = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        items=[item.model_copy(deep=True) for item in started.state.pending_comment_items],
        comment_scope_action="items",
        comment_target_indexes=[0, 1],
        confidence=0.99,
    )

    result = engine.handle(_event("Для всех товаров", 2), command, started.state, _catalog())

    assert result.state.pending_comment_items == []
    assert [item.comment for item in result.state.cart] == [
        "положить отдельно",
        "положить отдельно",
    ]


def test_named_item_answer_changes_only_selected_pending_item(settings) -> None:  # type: ignore[no-untyped-def]
    """Применяет комментарий только к однозначно названной ожидающей позиции."""
    engine = ConversationEngine(settings)
    started = _start_clarification(settings)
    command = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        items=[item.model_copy(deep=True) for item in started.state.pending_comment_items],
        comment_scope_action="items",
        comment_target_indexes=[1],
        confidence=0.98,
    )

    result = engine.handle(_event("Только для огурцов", 2), command, started.state, _catalog())

    assert [item.comment for item in result.state.cart] == ["", "положить отдельно"]


def test_order_scope_applies_pending_comment_to_existing_and_new_items(settings) -> None:  # type: ignore[no-untyped-def]
    """Распространяет комментарий на всю заявку только после явного выбора."""
    existing = CartItem(
        id="milk",
        source_query="Молоко",
        catalog_product_id="milk",
        catalog_name="Молоко",
        quantity=2,
        unit="л",
        catalog_unit="л",
        status=ItemStatus.MATCHED,
    )
    started = _start_clarification(settings, ConversationState(cart=[existing]))
    command = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        items=[item.model_copy(deep=True) for item in started.state.pending_comment_items],
        comment_scope_action="order",
        confidence=0.99,
    )

    result = ConversationEngine(settings).handle(
        _event("Для всей заявки", 2),
        command,
        started.state,
        _catalog(),
    )

    assert len(result.state.cart) == 3
    assert all("положить отдельно" in item.comment for item in result.state.cart)


def test_low_confidence_scope_keeps_pending_items_and_draft_unchanged(settings) -> None:  # type: ignore[no-untyped-def]
    """Повторяет вопрос вместо применения неуверенного решения ИИ."""
    started = _start_clarification(settings)
    command = ParsedCommand(
        intent=Intent.CLARIFY_CURRENT,
        comment_scope_action="items",
        comment_target_indexes=[0, 1],
        confidence=0.71,
    )

    result = ConversationEngine(settings).handle(
        _event("Наверное к ним", 2),
        command,
        started.state,
        _catalog(),
    )

    assert result.state.cart == []
    assert len(result.state.pending_comment_items) == 2
    assert result.state.stage is SessionStage.AWAIT_COMMENT_SCOPE


def test_scope_with_any_out_of_range_index_is_rejected_as_a_whole(settings) -> None:  # type: ignore[no-untyped-def]
    """Не применяет частично допустимый ответ с выдуманным индексом товара."""
    started = _start_clarification(settings)
    command = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        comment_scope_action="items",
        comment_target_indexes=[0, 99],
        confidence=0.99,
    )

    result = ConversationEngine(settings).handle(
        _event("Для первого и сотого товара", 2),
        command,
        started.state,
        _catalog(),
    )

    assert result.state.cart == []
    assert len(result.state.pending_comment_items) == 2
    assert "Уточните комментарий" in result.reply.text


def test_cancelled_comment_discards_pending_items_without_touching_draft(settings) -> None:  # type: ignore[no-untyped-def]
    """Отменяет спорное добавление без загрязнения существующего черновика."""
    existing = CartItem(id="milk", source_query="Молоко", status=ItemStatus.MATCHED)
    started = _start_clarification(settings, ConversationState(cart=[existing]))

    result = ConversationEngine(settings).handle(
        _event("Не добавляй комментарий", 2),
        ParsedCommand(intent=Intent.CANCEL, comment_scope_action="cancel", confidence=0.99),
        started.state,
        _catalog(),
    )

    assert result.state.pending_comment_items == []
    assert result.state.cart == [existing]
    assert "Комментарий не добавлен" in result.reply.text


def test_skipping_the_only_unresolved_item_does_not_claim_it_was_added(settings) -> None:  # type: ignore[no-untyped-def]
    """Не показывает успешное добавление после явного пропуска единственного товара."""
    engine = ConversationEngine(settings)
    added = engine.handle(
        _event("Несуществующий товар 2 штуки"),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[ExtractedItem(product_query="Несуществующий товар", quantity=2, unit="шт")],
        ),
        ConversationState(),
        [],
    )

    skipped = engine.handle(
        _event("Не добавлять", 2),
        ParsedCommand(intent=Intent.SKIP_CURRENT),
        added.state,
        [],
    )

    assert skipped.state.cart[0].status is ItemStatus.SKIPPED
    assert "Товар добавлен в черновик заказа" not in skipped.reply.text
    assert "Товаров пока нет" in skipped.reply.text
