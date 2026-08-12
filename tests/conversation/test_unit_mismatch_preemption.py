from __future__ import annotations

import pytest

from restaurant_bot.conversation.routing.contracts import (
    CompatibilityAction,
    CompatibilityContext,
)
from restaurant_bot.conversation.routing.state_compatibility import (
    StateCompatibilityPolicy,
)
from restaurant_bot.domain.models import (
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


def _event(text: str, input_kind: InputKind = InputKind.TEXT) -> TelegramEvent:
    """Создаёт событие для проверки unit mismatch routing."""
    return TelegramEvent(update_id=1, chat_id="1", input_type=input_kind, text=text)


def _catalog() -> list[CatalogProduct]:
    """Возвращает каталог новых позиций для тестов."""
    return [
        CatalogProduct(product_id="parmesan", name="Пармезан", unit="кг"),
        CatalogProduct(product_id="dill", name="Укроп", unit="кг"),
    ]


def _unit_state(engine: ConversationEngine) -> ConversationState:
    """Создаёт draft с активной карточкой несовпадающей единицы."""
    item = engine._build_item(
        ExtractedItem(
            product_query="Курица",
            quantity=4,
            unit="шт",
            comment="без кожи",
        )
    )
    item.id = "unit"
    item.catalog_product_id = "chicken"
    item.catalog_name = "Курица охлаждённая"
    item.catalog_unit = "кг"
    item.supplier = "Птица"
    item.status = ItemStatus.UNIT_MISMATCH
    return ConversationState(current_issue_item_id=item.id, cart=[item])


def _add_command(text: str, query: str, quantity: float = 3) -> ParsedCommand:
    """Создаёт concrete ADD_ITEMS для независимого товара."""
    return ParsedCommand(
        intent=Intent.ADD_ITEMS,
        text=text,
        items=[ExtractedItem(product_query=query, quantity=quantity, unit="кг")],
    )


def test_unit_mismatch_policy_interrupts_concrete_add_items(settings) -> None:  # type: ignore[no-untyped-def]
    """Прерывает unit flow для нового конкретного товара."""
    engine = ConversationEngine(settings)
    decision = StateCompatibilityPolicy().evaluate(
        _add_command("Пармезан 3 кг", "Пармезан"),
        _unit_state(engine),
        CompatibilityContext.UNIT_MISMATCH,
    )

    assert decision.action is CompatibilityAction.INTERRUPT


def test_named_add_without_quantity_interrupts_unit_flow(settings) -> None:  # type: ignore[no-untyped-def]
    """Считает явное добавление товара новой командой даже без количества."""
    engine = ConversationEngine(settings)
    command = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        text="добавь укроп",
        items=[ExtractedItem(product_query="укроп")],
    )

    decision = StateCompatibilityPolicy().evaluate(
        command,
        _unit_state(engine),
        CompatibilityContext.UNIT_MISMATCH,
    )

    assert decision.action is CompatibilityAction.INTERRUPT


@pytest.mark.parametrize("input_kind", [InputKind.TEXT, InputKind.VOICE])
def test_independent_add_does_not_rewrite_old_unit_item(
    settings,
    input_kind: InputKind,
) -> None:  # type: ignore[no-untyped-def]
    """Добавляет новый товар отдельно для текста и голоса."""
    engine = ConversationEngine(settings)
    state = _unit_state(engine)
    phrase = "Пармезан 3 кг" if input_kind is InputKind.TEXT else "Пармезан три килограмма"
    result = engine.handle(
        _event(phrase, input_kind),
        _add_command(phrase, "Пармезан"),
        state,
        _catalog(),
    )

    assert len(result.state.cart) == 2
    old, new = result.state.cart
    assert old.id == "unit"
    assert old.status is ItemStatus.UNIT_MISMATCH
    assert (old.quantity, old.unit, old.catalog_unit) == (4, "шт", "кг")
    assert old.comment == "без кожи"
    assert old.catalog_product_id == "chicken"
    assert new.source_query == "Пармезан"
    assert (new.quantity, new.unit) == (3, "кг")
    assert new.status is ItemStatus.MATCHED
    assert new.comment == ""
    assert new.issue_message == ""
    assert result.state.current_issue_item_id == "unit"
    assert result.state.current_issue_kind is not None


def test_unit_mismatch_adds_another_product_without_old_quantity(settings) -> None:  # type: ignore[no-untyped-def]
    """Не переносит единицу старого товара в новую позицию."""
    engine = ConversationEngine(settings)
    result = engine.handle(
        _event("добавь укроп 2 кг"),
        _add_command("добавь укроп 2 кг", "Укроп", quantity=2),
        _unit_state(engine),
        _catalog(),
    )

    assert [(item.source_query, item.quantity, item.unit) for item in result.state.cart] == [
        ("Курица", 4, "шт"),
        ("Укроп", 2, "кг"),
    ]


@pytest.mark.parametrize(
    "intent",
    [Intent.USE_CATALOG_UNIT, Intent.UNIT_OK, Intent.UNIT_EDIT, Intent.ENTER_OTHER_QUANTITY],
)
def test_unit_mismatch_existing_actions_continue(
    settings,
    intent: Intent,
) -> None:  # type: ignore[no-untyped-def]
    """Оставляет реальные unit actions в текущем modal flow."""
    engine = ConversationEngine(settings)
    decision = StateCompatibilityPolicy().evaluate(
        ParsedCommand(intent=intent),
        _unit_state(engine),
        CompatibilityContext.UNIT_MISMATCH,
    )

    assert decision.action is CompatibilityAction.CONTINUE


def test_unit_mismatch_short_quantity_continues_existing_flow(settings) -> None:  # type: ignore[no-untyped-def]
    """Применяет короткое количество к открытой unit карточке."""
    engine = ConversationEngine(settings)
    result = engine.handle(
        _event("5 кг"),
        ParsedCommand(intent=Intent.UNKNOWN, text="5 кг"),
        _unit_state(engine),
        [],
    )

    assert result.state.cart[0].status is ItemStatus.MATCHED
    assert (result.state.cart[0].quantity, result.state.cart[0].unit) == (5, "кг")


def test_unit_mismatch_random_phrase_repeats_safe_prompt(settings) -> None:  # type: ignore[no-untyped-def]
    """Не меняет unit карточку на случайную разговорную фразу."""
    engine = ConversationEngine(settings)
    state = _unit_state(engine)
    result = engine.handle(
        _event("ну не знаю"),
        ParsedCommand(intent=Intent.UNKNOWN, text="ну не знаю"),
        state,
        _catalog(),
    )

    item = result.state.cart[0]
    assert item.status is ItemStatus.UNIT_MISMATCH
    assert (item.quantity, item.unit, item.catalog_unit) == (4, "шт", "кг")
    assert result.state.current_issue_item_id == "unit"
    assert "Уточните количество" in result.reply.text


@pytest.mark.parametrize(
    ("intent", "text"),
    [(Intent.SHOW_CART, "Покажи черновик"), (Intent.THANKS, "Спасибо")],
)
def test_independent_navigation_interrupts_unit_flow(
    settings,
    intent: Intent,
    text: str,
) -> None:  # type: ignore[no-untyped-def]
    """Независимая команда не меняет unit mismatch позицию."""
    engine = ConversationEngine(settings)
    state = _unit_state(engine)
    decision = StateCompatibilityPolicy().evaluate(
        ParsedCommand(intent=intent, text=text),
        state,
        CompatibilityContext.UNIT_MISMATCH,
    )
    result = engine.handle(_event(text), ParsedCommand(intent=intent, text=text), state, _catalog())

    assert decision.action is CompatibilityAction.INTERRUPT
    assert result.state.cart[0].status is ItemStatus.UNIT_MISMATCH
    assert result.state.current_issue_item_id == "unit"


def test_remove_item_target_interrupts_unit_flow(settings) -> None:  # type: ignore[no-untyped-def]
    """Удаляет явно названную unit-позицию обычным remove routing."""
    engine = ConversationEngine(settings)
    state = _unit_state(engine)
    command = ParsedCommand(
        intent=Intent.REMOVE_ITEM,
        text="Убери курицу",
        target_query="курица",
    )

    decision = StateCompatibilityPolicy().evaluate(
        command,
        state,
        CompatibilityContext.UNIT_MISMATCH,
    )
    result = engine.handle(_event(command.text), command, state, _catalog())

    assert decision.action is CompatibilityAction.INTERRUPT
    assert result.state.cart[0].status is ItemStatus.SKIPPED
    assert result.state.current_issue_item_id == ""


def test_skip_current_unit_item_clears_issue_references(settings) -> None:  # type: ignore[no-untyped-def]
    """Удаляет текущую unit mismatch позицию без битых ссылок."""
    engine = ConversationEngine(settings)
    result = engine.handle(
        _event("Не добавлять"),
        ParsedCommand(intent=Intent.SKIP_CURRENT, text="Не добавлять"),
        _unit_state(engine),
        _catalog(),
    )

    assert result.state.cart[0].status is ItemStatus.SKIPPED
    assert result.state.current_issue_item_id == ""
    assert result.state.current_issue_kind is None


def test_unit_mismatch_explicit_catalog_unit_action_updates_item(settings) -> None:  # type: ignore[no-untyped-def]
    """Применяет подтверждённую каталожную единицу."""
    engine = ConversationEngine(settings)
    result = engine.handle(
        _event("Используй единицу из каталога"),
        ParsedCommand(intent=Intent.USE_CATALOG_UNIT, text="Используй единицу из каталога"),
        _unit_state(engine),
        _catalog(),
    )

    item = result.state.cart[0]
    assert item.status is ItemStatus.MATCHED
    assert item.unit == "кг"


def test_unit_edit_opens_expected_quantity_stage(settings) -> None:  # type: ignore[no-untyped-def]
    """Открывает ввод количества в единице каталога."""
    engine = ConversationEngine(settings)
    result = engine.handle(
        _event("Ввести количество", InputKind.TEXT),
        ParsedCommand(intent=Intent.UNIT_EDIT, text="Ввести количество"),
        _unit_state(engine),
        _catalog(),
    )

    assert result.state.stage is SessionStage.AWAIT_UNIT_QUANTITY
    assert result.state.cart[0].status is ItemStatus.UNIT_MISMATCH
    assert "Укажите количество в кг" in result.reply.text
