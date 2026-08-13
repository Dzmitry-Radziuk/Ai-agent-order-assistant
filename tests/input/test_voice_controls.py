"""Проверяет поведение, связанное с модулем «test voice controls»."""

import pytest

from restaurant_bot.domain.models import (
    Candidate,
    CartItem,
    CatalogProduct,
    ConversationState,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    SessionStage,
    TelegramEvent,
)
from restaurant_bot.parsing.commands.api import infer_intent
from restaurant_bot.services.engine import ConversationEngine


def _voice(text: str) -> TelegramEvent:
    """Создаёт тестовое голосовое событие Telegram."""
    return TelegramEvent(
        update_id=1, chat_id="voice-controls", input_type=InputKind.VOICE, text=text
    )


def _candidates() -> list[Candidate]:
    """Создаёт тестовый набор кандидатов каталога."""
    return [
        Candidate(product_id="rose", name="Сироп Роза", supplier="Сиропы", unit="шт"),
        Candidate(product_id="tarhun", name="Сироп Тархун", supplier="Сиропы", unit="шт"),
    ]


@pytest.mark.parametrize("phrase", ["давай второй", "вариант два", "беру второй вариант"])
def test_voice_selects_candidate_by_natural_number(settings, phrase: str) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что голос выбирает кандидата по естественно произнесённому номеру."""
    item = CartItem(
        id="choice",
        source_query="сироп",
        quantity=2,
        unit="шт",
        status=ItemStatus.AMBIGUOUS,
        candidates=_candidates(),
    )
    result = ConversationEngine(settings).handle(
        _voice(phrase),
        ParsedCommand(intent=Intent.ADD_ITEMS, text=phrase),
        ConversationState(cart=[item], current_issue_item_id="choice"),
        [
            CatalogProduct(product_id="rose", name="Сироп Роза", supplier="Сиропы", unit="шт"),
            CatalogProduct(product_id="tarhun", name="Сироп Тархун", supplier="Сиропы", unit="шт"),
        ],
    )

    assert result.state.cart[0].catalog_product_id == "tarhun"
    assert result.state.cart[0].status is ItemStatus.MATCHED


@pytest.mark.parametrize(
    ("phrase", "intent"),
    [
        ("название товара изменить", Intent.MANUAL_CURRENT),
        ("не добавляй этот товар", Intent.SKIP_CURRENT),
    ],
)
def test_voice_controls_not_found_card(settings, phrase: str, intent: Intent) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что голос controls не found карточка."""
    item = CartItem(id="missing", source_query="редкий соус", status=ItemStatus.NOT_FOUND)
    state = ConversationState(cart=[item], current_issue_item_id="missing")
    result = ConversationEngine(settings).handle(
        _voice(phrase), ParsedCommand(intent=Intent.UNKNOWN, text=phrase), state, []
    )

    if intent is Intent.MANUAL_CURRENT:
        assert result.state.stage is SessionStage.AWAIT_MANUAL_DETAILS
    else:
        assert result.state.cart[0].status is ItemStatus.SKIPPED


def test_voice_confirms_final_submission(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что голос подтверждает финальный отправка заявки."""
    item = CartItem(
        id="ready",
        source_query="Сироп Роза",
        catalog_product_id="rose",
        catalog_name="Сироп Роза",
        supplier="Сиропы",
        catalog_unit="шт",
        quantity=2,
        unit="шт",
        status=ItemStatus.MATCHED,
    )
    state = ConversationState(cart=[item], stage=SessionStage.AWAIT_SUBMIT_CONFIRM)
    result = ConversationEngine(settings).handle(
        _voice("да, отправляй"),
        ParsedCommand(intent=Intent.CONFIRM, text="да, отправляй"),
        state,
        [],
    )

    assert result.enqueue_submission
    assert result.state.stage is SessionStage.SUBMITTING


def test_voice_keeps_or_changes_multiple_warning_in_context(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что голос сохраняет или изменяет кратность предупреждение в контекст."""
    item = CartItem(
        id="multiple",
        source_query="Говядина",
        catalog_product_id="beef",
        catalog_name="Говядина",
        catalog_unit="кг",
        quantity=5,
        unit="кг",
        suggested_quantity=20,
        status=ItemStatus.MATCHED,
    )
    state = ConversationState(cart=[item], current_issue_item_id="multiple")
    kept = ConversationEngine(settings).handle(
        _voice("оставь как указано"),
        ParsedCommand(intent=Intent.UNKNOWN, text="оставь как указано"),
        state,
        [],
    )
    assert kept.state.cart[0].quantity == 5
    assert kept.state.cart[0].suggested_quantity is None

    item.suggested_quantity = 20
    accepted = ConversationEngine(settings).handle(
        _voice("поставь рекомендованное количество"),
        ParsedCommand(intent=Intent.UNKNOWN, text="поставь рекомендованное количество"),
        ConversationState(cart=[item], current_issue_item_id="multiple"),
        [],
    )
    assert accepted.state.cart[0].quantity == 20


def test_voice_fix_quantity_repeats_visible_button_action(settings) -> None:  # type: ignore[no-untyped-def]
    """Открывает выбор количества как одноимённая кнопка."""
    item = CartItem(
        id="multiple",
        source_query="Говядина",
        catalog_product_id="beef",
        catalog_name="Говядина",
        catalog_unit="кг",
        quantity=5,
        unit="кг",
        suggested_quantity=20,
        status=ItemStatus.MATCHED,
    )
    phrase = "Давай исправим количество"

    result = ConversationEngine(settings).handle(
        _voice(phrase),
        ParsedCommand(intent=Intent.ENTER_OTHER_QUANTITY, text=phrase),
        ConversationState(cart=[item], current_issue_item_id="multiple"),
        [],
    )

    assert result.state.cart[0].quantity == 5
    assert "Выберите количество" in result.reply.text


def test_voice_opens_and_retries_procurement_request(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что голос открывает и повторяет procurement запрос."""
    state = ConversationState(
        product_add_requests=[
            {"request_id": "failed-1", "description": "Редкий соус", "status": "write_failed"}
        ]
    )
    engine = ConversationEngine(settings)
    listed = engine.handle(
        _voice("покажи запросы снабженцу"),
        ParsedCommand(intent=Intent.UNKNOWN, text="покажи запросы снабженцу"),
        state,
        [],
    )
    assert "Запросы снабженцу" in listed.reply.text

    retried = engine.handle(
        _voice("повтори запрос номер один"),
        ParsedCommand(intent=Intent.UNKNOWN, text="повтори запрос номер один"),
        state,
        [],
    )
    assert retried.enqueue_product_add
    assert retried.state.product_add_requests[0]["status"] == "retry_pending"


@pytest.mark.parametrize("phrase", ["МБР", "эм бэ эр"])
def test_voice_selects_visible_supplier_by_name_or_spoken_acronym(settings, phrase: str) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что голос выбирает видимого поставщика по названию или произнесённой аббревиатуре."""
    item = CartItem(
        id="supplier-item",
        source_query="Сироп",
        catalog_product_id="syrup",
        catalog_name="Сироп",
        supplier="МБР",
        catalog_unit="шт",
        quantity=1,
        unit="шт",
        price=100,
        supplier_current_sum=0,
        supplier_minimum_amount=1500,
        status=ItemStatus.MATCHED,
    )
    state = ConversationState(cart=[item], stage=SessionStage.REVIEW)
    result = ConversationEngine(settings).handle(
        _voice(phrase), ParsedCommand(intent=Intent.UNKNOWN, text=phrase), state, []
    )

    assert result.state.supplier_hint_context == "МБР"
    assert result.state.supplier_search_locked


@pytest.mark.parametrize("status", [ItemStatus.DUPLICATE_PENDING, ItemStatus.UNIT_MISMATCH])
@pytest.mark.parametrize(
    "phrase",
    [
        "Ну давай добавим ещё товары, пожалуйста.",
        "Хочу добавить новые позиции",
        "Давай внесём ещё продукты",
    ],
)
def test_global_add_more_voice_command_wins_on_local_issue_cards(
    settings, status: ItemStatus, phrase: str
) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что общая голосовая команда добавления имеет приоритет над локальными карточками уточнения."""
    item = CartItem(
        id="issue",
        source_query="Сироп Роза",
        quantity=2,
        unit="шт",
        catalog_unit="шт",
        status=status,
    )
    state = ConversationState(cart=[item], current_issue_item_id="issue")

    result = ConversationEngine(settings).handle(_voice(phrase), infer_intent(phrase), state, [])

    assert result.state.stage is SessionStage.COLLECTING
    assert result.state.cart[0].status is status
    assert (
        result.reply.text == "Отправьте товары текстом, голосом или фото — я добавлю их в текущий "
        "черновик заказа."
    )


def test_voice_adds_items_for_the_only_supplier_below_minimum(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что голос добавляет позиции только для поставщика с недостигнутым минимумом."""
    item = CartItem(
        id="supplier-item",
        source_query="Сироп",
        catalog_product_id="syrup",
        catalog_name="Сироп",
        supplier="МБР",
        catalog_unit="шт",
        quantity=1,
        unit="шт",
        price=100,
        supplier_current_sum=0,
        supplier_minimum_amount=1500,
        status=ItemStatus.MATCHED,
    )
    state = ConversationState(cart=[item], stage=SessionStage.REVIEW)
    phrase = "Давай доберем до минималки"

    result = ConversationEngine(settings).handle(_voice(phrase), infer_intent(phrase), state, [])

    assert result.state.supplier_hint_context == "МБР"
    assert result.state.supplier_search_locked


def test_voice_opens_supplier_chooser_for_multiple_warnings(settings) -> None:  # type: ignore[no-untyped-def]
    """Повторяет голосом кнопку выбора поставщика."""
    items = [
        CartItem(
            id=f"item-{index}",
            source_query=f"Товар {index}",
            catalog_product_id=f"product-{index}",
            catalog_name=f"Товар {index}",
            supplier=supplier,
            catalog_unit="шт",
            quantity=1,
            unit="шт",
            price=100,
            supplier_minimum_amount=1500,
            status=ItemStatus.MATCHED,
        )
        for index, supplier in enumerate(("МБР", "Сантос"), start=1)
    ]
    phrase = "Давай выберем поставщика"

    result = ConversationEngine(settings).handle(
        _voice(phrase),
        ParsedCommand(intent=Intent.UNKNOWN, text=phrase),
        ConversationState(cart=items, stage=SessionStage.AWAIT_SUBMIT_CONFIRM),
        [],
    )

    assert "Выберите поставщика" in result.reply.text
    button_texts = [button.text for row in result.reply.rows for button in row]
    assert any("МБР" in text for text in button_texts)
    assert any("Сантос" in text for text in button_texts)
