from restaurant_bot.domain.models import (
    CartItem,
    ConversationState,
    InputKind,
    Intent,
    ItemStatus,
    TelegramEvent,
)
from restaurant_bot.services.engine import ConversationEngine
from restaurant_bot.services.parser import parse_callback


def test_callback_actions_have_explicit_state_machine_meanings() -> None:
    assert parse_callback("v2:cart").intent is Intent.SHOW_FINAL_REVIEW
    assert parse_callback("v2:back").intent is Intent.BACK
    assert parse_callback("v2:submit").intent is Intent.SUBMIT_AS_IS
    assert parse_callback("v2:addreq:2").intent is Intent.PRODUCT_ADD
    assert parse_callback("v2:dupmerge:3:0").intent is Intent.MERGE_DUPLICATE


def test_candidate_callbacks_use_human_numbering_and_keep_revision() -> None:
    command = parse_callback("v2:sel:4:0:r7")

    assert command.intent is Intent.SELECT_CANDIDATE
    assert command.callback_target == "4"
    assert command.selected_index == 1
    assert command.callback_revision == 7


def test_stale_callback_cannot_mutate_current_draft(settings) -> None:  # type: ignore[no-untyped-def]
    engine = ConversationEngine(settings)
    state = ConversationState(
        ui_revision=3,
        cart=[
            CartItem(
                id="rose",
                source_query="Сироп Роза",
                catalog_name="Сироп Роза",
                quantity=5,
                unit="шт",
                status=ItemStatus.MATCHED,
            )
        ],
    )
    event = TelegramEvent(update_id=1, chat_id="123456", input_type=InputKind.CALLBACK)

    result = engine.handle(event, parse_callback("v2:clear:r2"), state, [])

    assert result.state.stage.value == "collecting"
    assert len(result.state.cart) == 1
    assert "Сироп Роза" in result.reply.text
