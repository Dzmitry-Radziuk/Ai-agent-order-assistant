from unittest.mock import MagicMock

from restaurant_bot.domain.models import (
    BotReply,
    ConversationState,
    EngineResult,
    Intent,
    PendingSubmission,
)
from restaurant_bot.repositories.order_events import sanitize_audit_details
from restaurant_bot.services.orchestrator import UpdateOrchestrator


def test_audit_details_hide_content_secrets_and_bound_large_values() -> None:
    sanitized = sanitize_audit_details(
        {
            "text": "Томаты 20 кг",
            "token": "private",
            "error": "x" * 2000,
        }
    )

    assert sanitized["text"].startswith("<content sha256:")
    assert sanitized["token"] == "<redacted>"
    assert sanitized["error"].startswith("<content sha256:")
    assert "x" * 20 not in sanitized["error"]


def test_order_transition_uses_one_trace_for_start_action_and_submission() -> None:
    events = MagicMock()
    state = ConversationState(
        order_trace_id="trace-1",
        pending_submission=PendingSubmission(
            order_no="ORDER-1",
            trace_id="trace-1",
            rows=[{"product": "one"}],
        ),
    )
    result = EngineResult(state=state, reply=BotReply(text="ok"))

    UpdateOrchestrator._append_order_transition_events(
        events,
        123,
        result,
        {
            "previous_trace_id": "",
            "trace_started": True,
            "telegram_user_id": "user-1",
            "telegram_chat_id": "chat-1",
            "venue_code": "venue-1",
            "intent": "submit",
            "input_type": "callback",
            "previous_stage": "review",
            "previous_cart_count": 1,
        },
    )

    event_types = [call.kwargs["event_type"] for call in events.append_once.call_args_list]
    assert event_types == ["order_started", "user_action", "submission_requested"]
    assert {call.kwargs["trace_id"] for call in events.append_once.call_args_list} == {"trace-1"}


def test_clear_cart_records_cancelled_trace() -> None:
    events = MagicMock()
    result = EngineResult(state=ConversationState(), reply=BotReply(text="ok"))

    UpdateOrchestrator._append_order_transition_events(
        events,
        124,
        result,
        {
            "previous_trace_id": "trace-old",
            "trace_started": False,
            "telegram_user_id": "user-1",
            "telegram_chat_id": "chat-1",
            "venue_code": "venue-1",
            "intent": Intent.CLEAR_CART.value,
            "input_type": "callback",
            "previous_stage": "review",
            "previous_cart_count": 3,
        },
    )

    event_types = [call.kwargs["event_type"] for call in events.append_once.call_args_list]
    assert event_types == ["user_action", "order_cancelled"]
