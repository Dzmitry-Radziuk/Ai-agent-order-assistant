"""Проверяет поведение, связанное с модулем «test quantity state preemption»."""

from types import SimpleNamespace

from restaurant_bot.conversation.progression import ProgressionKind, advance
from restaurant_bot.conversation.routing.contracts import CompatibilityAction
from restaurant_bot.conversation.routing.state_compatibility import (
    StateCompatibilityPolicy,
)
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
from restaurant_bot.input.telegram_interpretation import TelegramInputInterpreter
from restaurant_bot.parsing.commands.api import infer_intent
from restaurant_bot.services.engine import ConversationEngine
from restaurant_bot.services.orchestrator import UpdateOrchestrator


def _event(text: str, input_type: InputKind = InputKind.TEXT) -> TelegramEvent:
    """Создаёт событие для проверки прерывания modal-ожидания количества."""
    return TelegramEvent(
        update_id=1, chat_id="quantity-preemption", input_type=input_type, text=text
    )


def _missing_quantity_state() -> ConversationState:
    """Создаёт черновик с незавершённой курицей."""
    item = CartItem(
        id="chicken",
        source_query="Курица",
        status=ItemStatus.MISSING_QTY,
        catalog_product_id="chicken-id",
        catalog_name="Курица",
        catalog_unit="кг",
    )
    return ConversationState(
        cart=[item],
        current_issue_item_id=item.id,
        stage=SessionStage.AWAIT_UNIT_QUANTITY,
    )


def test_quantity_policy_interrupts_for_a_concrete_new_product() -> None:
    """Не считает новую товарную позицию ответом на количество."""
    state = _missing_quantity_state()
    command = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        text="пармезан 3 кг",
        items=[ExtractedItem(product_query="пармезан", quantity=3, unit="кг")],
    )

    decision = StateCompatibilityPolicy().evaluate(command, state)

    assert decision.action is CompatibilityAction.INTERRUPT


def test_quantity_state_keeps_incomplete_item_when_text_adds_new_product(settings) -> None:  # type: ignore[no-untyped-def]
    """Добавляет новый товар, не перенося в него контекст незавершённой курицы."""
    engine = ConversationEngine(settings)
    state = _missing_quantity_state()
    command = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        text="пармезан 3 кг",
        items=[ExtractedItem(product_query="пармезан", quantity=3, unit="кг")],
    )

    result = engine.handle(_event(command.text), command, state, [])

    chicken, parmesan = result.state.cart
    assert (chicken.source_query, chicken.quantity, chicken.status) == (
        "Курица",
        None,
        ItemStatus.MISSING_QTY,
    )
    assert (parmesan.source_query, parmesan.quantity, parmesan.unit) == ("пармезан", 3, "кг")


def test_quantity_preemption_uses_global_text_parser_boundary(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет прерывание quantity-flow через реальный text parsing boundary."""
    parser = object.__new__(UpdateOrchestrator)
    parser.openai = SimpleNamespace(parse_text=infer_intent)
    state = _missing_quantity_state()

    command = TelegramInputInterpreter(
        parser.openai,
        lambda: None,
        StateCompatibilityPolicy(),
    ).interpret_text("пармезан 3 кг", state)

    assert command.intent is Intent.ADD_ITEMS
    assert command.items[0].product_query == "пармезан"
    assert command.items[0].quantity == 3

    result = ConversationEngine(settings).handle(
        _event(command.text),
        command,
        state,
        [CatalogProduct(product_id="parmesan", name="Пармезан", unit="кг")],
    )

    assert result.state.cart[0].status is ItemStatus.MISSING_QTY
    parmesan = next(item for item in result.state.cart if item.source_query == "пармезан")
    assert (parmesan.quantity, parmesan.unit) == (3, "кг")


def test_quantity_state_keeps_incomplete_item_for_voice_addition(settings) -> None:  # type: ignore[no-untyped-def]
    """Голосовое добавление также прерывает quantity modal без изменения курицы."""
    engine = ConversationEngine(settings)
    state = _missing_quantity_state()
    command = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        text="пармезан 3 кг",
        items=[ExtractedItem(product_query="пармезан", quantity=3, unit="кг")],
    )

    result = engine.handle(_event(command.text, InputKind.VOICE), command, state, [])

    assert result.state.cart[0].status is ItemStatus.MISSING_QTY
    assert result.state.cart[0].quantity is None
    assert any(item.source_query == "пармезан" for item in result.state.cart)


def test_quantity_answer_continues_current_item(settings) -> None:  # type: ignore[no-untyped-def]
    """Количество с явным edit intent завершает текущую позицию."""
    engine = ConversationEngine(settings)
    state = _missing_quantity_state()
    command = ParsedCommand(
        intent=Intent.EDIT_QUANTITY,
        text="5 кг",
        edit_quantity=5,
        edit_unit="кг",
    )

    result = engine.handle(_event(command.text), command, state, [])

    assert result.state.cart[0].quantity == 5
    assert result.state.cart[0].unit == "кг"


def test_quantity_state_routes_global_actions_without_changing_quantity(settings) -> None:  # type: ignore[no-untyped-def]
    """Навигация, удаление и thanks не записываются как количество."""
    engine = ConversationEngine(settings)
    cases = [
        (Intent.SHOW_CART, "покажи черновик", "Курица"),
        (Intent.THANKS, "спасибо", "Курица"),
        (Intent.REMOVE_ITEM, "убери курицу", "Курица"),
    ]

    for intent, text, expected_query in cases:
        state = _missing_quantity_state()
        command = ParsedCommand(intent=intent, text=text, target_query="курица")
        result = engine.handle(_event(text), command, state, [])

        item = result.state.cart[0]
        assert item.source_query == expected_query
        assert item.quantity is None
        if intent is Intent.REMOVE_ITEM:
            assert item.status is ItemStatus.SKIPPED
        else:
            assert item.status is ItemStatus.MISSING_QTY


def test_quantity_state_ignores_unrelated_text(settings) -> None:  # type: ignore[no-untyped-def]
    """Неизвестная произвольная фраза не изменяет незавершённую позицию."""
    engine = ConversationEngine(settings)
    state = _missing_quantity_state()
    command = ParsedCommand(intent=Intent.UNKNOWN, text="я ещё подумаю")

    result = engine.handle(_event(command.text), command, state, [])

    assert result.state.cart[0].status is ItemStatus.MISSING_QTY
    assert result.state.cart[0].quantity is None


def test_new_product_without_quantity_preempts_then_resumes_old_pending_item(settings) -> None:  # type: ignore[no-untyped-def]
    """Сначала уточняет новый товар, затем возвращается к старому quantity-вопросу."""
    engine = ConversationEngine(settings)
    mustard = _missing_quantity_state().cart[0]
    mustard.source_query = "Горчица дижонская"
    mustard.catalog_product_id = "mustard"
    mustard.catalog_name = "Горчица дижонская"
    state = ConversationState(
        cart=[mustard],
        current_issue_item_id=mustard.id,
        stage=SessionStage.AWAIT_UNIT_QUANTITY,
    )
    catalog = [
        CatalogProduct(product_id="mustard", name="Горчица дижонская", unit="шт"),
        CatalogProduct(product_id="horseradish", name="Хрен столовый домашний", unit="шт"),
    ]
    add_horseradish = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        text="Хрен столовый домашний",
        items=[ExtractedItem(product_query="Хрен столовый домашний")],
    )

    pending = engine.handle(_event(add_horseradish.text), add_horseradish, state, catalog)

    assert pending.state.cart[0].status is ItemStatus.MISSING_QTY
    horseradish = next(
        item for item in pending.state.cart if item.catalog_product_id == "horseradish"
    )
    assert horseradish.status is ItemStatus.MISSING_QTY
    assert pending.state.current_issue_item_id == horseradish.id

    quantity = ParsedCommand(
        intent=Intent.EDIT_QUANTITY,
        text="4 штуки",
        edit_quantity=4,
        edit_unit="шт",
    )
    resumed = engine.handle(_event(quantity.text), quantity, pending.state, catalog)

    assert horseradish.status is ItemStatus.MATCHED
    assert horseradish.quantity == 4
    assert resumed.state.current_issue_item_id == mustard.id


def test_nested_new_items_resume_in_reverse_interruption_order() -> None:
    """Возвращает вопросы A после последовательного решения B и C."""
    first = CartItem(id="a", source_query="Курица", status=ItemStatus.MISSING_QTY)
    second = CartItem(id="b", source_query="Сыр", status=ItemStatus.MISSING_QTY)
    third = CartItem(id="c", source_query="Укроп", status=ItemStatus.MISSING_QTY)
    state = ConversationState(
        cart=[first, second, third],
        current_issue_item_id=first.id,
        stage=SessionStage.AWAIT_UNIT_QUANTITY,
    )

    result = advance(
        state,
        added_count=2,
        preferred_issue_item_id=second.id,
        issue_context_item_ids=[second.id, third.id],
    )
    assert result.kind is ProgressionKind.ISSUE
    assert result.item is second

    second.status = ItemStatus.MATCHED
    result = advance(state)
    assert result.item is third
    assert result.resumed is False

    third.status = ItemStatus.MATCHED
    result = advance(state)
    assert result.item is first
    assert result.resumed is True
    assert state.current_issue_item_id == first.id


def test_nested_interruptions_resume_latest_context_first() -> None:
    """Возвращает контексты C, B и A в обратном порядке прерывания."""
    first = CartItem(id="a", source_query="Курица", status=ItemStatus.MISSING_QTY)
    second = CartItem(id="b", source_query="Сыр", status=ItemStatus.MISSING_QTY)
    third = CartItem(id="c", source_query="Укроп", status=ItemStatus.MISSING_QTY)
    state = ConversationState(
        cart=[first, second, third],
        current_issue_item_id=first.id,
        stage=SessionStage.AWAIT_UNIT_QUANTITY,
    )

    assert (
        advance(
            state,
            preferred_issue_item_id=second.id,
            issue_context_item_ids=[second.id],
        ).item
        is second
    )
    assert (
        advance(
            state,
            preferred_issue_item_id=third.id,
            issue_context_item_ids=[third.id],
        ).item
        is third
    )

    third.status = ItemStatus.MATCHED
    result = advance(state)
    assert result.item is second
    assert result.resumed is True

    second.status = ItemStatus.MATCHED
    result = advance(state)
    assert result.item is first
    assert result.resumed is True
