from restaurant_bot.domain.models import InputKind, Intent, ParsedCommand
from restaurant_bot.services.input_normalizer import normalize_telegram_update
from restaurant_bot.services.orchestrator import UpdateOrchestrator
from restaurant_bot.services.parser import infer_intent


def test_bot_suffix_is_removed_from_slash_command_before_routing() -> None:
    event = normalize_telegram_update(
        {"update_id": 1, "message": {"chat": {"id": 7}, "text": "/draft@restaurant_order_bot"}}
    )

    assert event.input_type is InputKind.TEXT
    assert event.bot_command == "draft"
    assert infer_intent(event.text).intent is Intent.SHOW_CART


def test_product_typo_is_never_interpreted_as_skip_command() -> None:
    command = infer_intent("гонядина 5 кг")

    assert command.intent is Intent.ADD_ITEMS
    assert command.items[0].product_query == "гонядина"
    assert command.items[0].quantity == 5


def test_direct_commands_bypass_catalog_but_product_operations_read_it() -> None:
    assert not UpdateOrchestrator._needs_catalog(ParsedCommand(intent=Intent.SHOW_CART))
    assert not UpdateOrchestrator._needs_catalog(ParsedCommand(intent=Intent.CLEAR_CART))
    assert not UpdateOrchestrator._needs_catalog(ParsedCommand(intent=Intent.EDIT_QUANTITY))
    assert UpdateOrchestrator._needs_catalog(ParsedCommand(intent=Intent.ADD_ITEMS))
    assert UpdateOrchestrator._needs_catalog(ParsedCommand(intent=Intent.SELECT_CANDIDATE))


def test_callback_input_preserves_callback_data_for_engine() -> None:
    event = normalize_telegram_update(
        {
            "update_id": 2,
            "callback_query": {
                "id": "cb",
                "data": "v2:back:r3",
                "from": {"id": 7},
                "message": {"chat": {"id": 7}, "message_id": 9},
            },
        }
    )

    assert event.input_type is InputKind.CALLBACK
    assert event.callback_data == "v2:back:r3"
    assert event.callback_message_id == 9
