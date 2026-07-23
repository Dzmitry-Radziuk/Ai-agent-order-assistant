from restaurant_bot.domain.models import (
    ConversationState,
    ExtractedItem,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    TelegramEvent,
)
from restaurant_bot.services.engine import ConversationEngine
from restaurant_bot.services.parser import infer_intent


def _voice(text: str) -> TelegramEvent:
    return TelegramEvent(
        update_id=1, chat_id="voice-quantity", input_type=InputKind.VOICE, text=text
    )


def _multiple_state(engine: ConversationEngine) -> ConversationState:
    item = engine._build_item(ExtractedItem(product_query="Глазной мускул", quantity=5, unit="кг"))
    item.id = "multiple"
    item.status = ItemStatus.MATCHED
    item.catalog_product_id = "beef"
    item.catalog_name = "Глазной мускул"
    item.catalog_unit = "кг"
    item.minimum_multiple = 20
    item.existing_quantity = 20
    item.suggested_quantity = 20
    return ConversationState(current_issue_item_id=item.id, cart=[item])


def _unit_mismatch_state(engine: ConversationEngine) -> ConversationState:
    item = engine._build_item(ExtractedItem(product_query="Курица", quantity=4, unit="шт"))
    item.id = "unit"
    item.status = ItemStatus.UNIT_MISMATCH
    item.catalog_product_id = "chicken"
    item.catalog_name = "Курица"
    item.catalog_unit = "кг"
    return ConversationState(current_issue_item_id=item.id, cart=[item])


def test_voice_generic_correction_repeats_fix_quantity_button(settings) -> None:  # type: ignore[no-untyped-def]
    engine = ConversationEngine(settings)

    result = engine.handle(
        _voice("давайте исправим количество"),
        ParsedCommand(intent=Intent.UNKNOWN),
        _multiple_state(engine),
        [],
    )

    assert result.state.stage.value == "review"
    assert result.state.cart[0].quantity == 20


def test_voice_explicit_other_quantity_opens_manual_input(settings) -> None:  # type: ignore[no-untyped-def]
    engine = ConversationEngine(settings)

    result = engine.handle(
        _voice("давайте укажем другое количество"),
        ParsedCommand(intent=Intent.ENTER_OTHER_QUANTITY),
        _multiple_state(engine),
        [],
    )

    assert result.state.stage.value == "await_multiple_quantity"
    assert result.state.cart[0].quantity == 5
    assert "Укажите другое количество" in result.reply.text


def test_voice_explicit_number_changes_only_current_multiple(settings) -> None:  # type: ignore[no-untyped-def]
    engine = ConversationEngine(settings)
    state = _multiple_state(engine)
    second = engine._build_item(ExtractedItem(product_query="Другой товар", quantity=7, unit="кг"))
    second.id = "other"
    second.status = ItemStatus.MATCHED
    state.cart.append(second)

    result = engine.handle(
        _voice("исправь на 40 килограммов"), ParsedCommand(intent=Intent.UNKNOWN), state, []
    )

    assert result.state.cart[0].quantity == 40
    assert result.state.cart[1].quantity == 7


def test_voice_add_without_product_word_accepts_current_multiple_recommendation(settings) -> None:  # type: ignore[no-untyped-def]
    engine = ConversationEngine(settings)

    result = engine.handle(
        _voice("добавь недостающее"),
        ParsedCommand(intent=Intent.ADD_MORE),
        _multiple_state(engine),
        [],
    )

    assert result.state.cart[0].quantity == 20
    assert result.state.cart[0].status is ItemStatus.MATCHED


def test_voice_add_more_products_is_not_consumed_as_quantity_correction(settings) -> None:  # type: ignore[no-untyped-def]
    engine = ConversationEngine(settings)

    result = engine.handle(
        _voice("добавить еще товары"),
        ParsedCommand(intent=Intent.ADD_MORE),
        _multiple_state(engine),
        [],
    )

    assert result.state.cart[0].quantity == 5
    assert result.state.stage.value == "collecting"


def test_voice_increment_phrase_adds_to_current_multiple_quantity(settings) -> None:  # type: ignore[no-untyped-def]
    engine = ConversationEngine(settings)
    state = _multiple_state(engine)

    result = engine.handle(
        _voice("Добавить еще 15 килограмм"),
        infer_intent("Добавить еще 15 килограмм"),
        state,
        [],
    )

    assert result.state.cart[0].quantity == 20
    assert result.state.cart[0].status is ItemStatus.MATCHED


def test_voice_bare_contextual_add_accepts_missing_multiple(settings) -> None:  # type: ignore[no-untyped-def]
    engine = ConversationEngine(settings)
    state = _multiple_state(engine)

    result = engine.handle(
        _voice("Добавить недостающее"),
        infer_intent("Добавить недостающее"),
        state,
        [],
    )

    assert result.state.cart[0].quantity == 20


def test_voice_unit_mismatch_accepts_catalog_unit_and_spoken_quantity(settings) -> None:  # type: ignore[no-untyped-def]
    engine = ConversationEngine(settings)

    accepted = engine.handle(
        _voice("переведи в килограммы"),
        ParsedCommand(intent=Intent.UNKNOWN),
        _unit_mismatch_state(engine),
        [],
    )
    assert accepted.state.cart[0].status is ItemStatus.MATCHED
    assert accepted.state.cart[0].unit == "кг"

    changed = engine.handle(
        _voice("поставь 5 кг"),
        ParsedCommand(intent=Intent.UNKNOWN),
        _unit_mismatch_state(engine),
        [],
    )
    assert changed.state.cart[0].status is ItemStatus.MATCHED
    assert changed.state.cart[0].quantity == 5
    assert changed.state.cart[0].unit == "кг"
