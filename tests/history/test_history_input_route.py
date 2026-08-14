"""Проверяет общий text/voice и modal-safe вход history-запроса."""

from __future__ import annotations

from unittest.mock import MagicMock

from restaurant_bot.conversation.routing.state_compatibility import StateCompatibilityPolicy
from restaurant_bot.domain.models import (
    ConversationState,
    InputKind,
    Intent,
    SessionStage,
    TelegramEvent,
)
from restaurant_bot.input.telegram_interpretation import TelegramInputInterpreter


def test_history_text_is_independent_of_pending_quantity() -> None:
    """Не превращает вопрос истории в количество или новый товар."""
    provider = MagicMock()
    interpreter = TelegramInputInterpreter(
        provider, lambda: MagicMock(), StateCompatibilityPolicy()
    )
    state = ConversationState(
        stage=SessionStage.AWAIT_UNIT_QUANTITY,
        current_issue_item_id="pending",
    )

    command = interpreter.interpret_text("Сегодня приедет говядина?", state)

    assert command.intent is Intent.HISTORY_QUERY
    assert command.history_query is not None
    provider.parse_text.assert_not_called()


def test_history_voice_transcript_uses_same_semantic_route() -> None:
    """Транскрипция голоса проходит тот же history parser, что и текст."""
    provider = MagicMock()
    interpreter = TelegramInputInterpreter(
        provider, lambda: MagicMock(), StateCompatibilityPolicy()
    )
    state = ConversationState()
    text_command = interpreter.interpret_text("Что там с говядиной?", state)
    voice_event = TelegramEvent(
        update_id=1,
        chat_id="chat",
        input_type=InputKind.VOICE,
        text="Что там с говядиной?",
    )
    recognizer = MagicMock()
    recognizer.recognize_media.return_value = text_command
    voice_interpreter = TelegramInputInterpreter(
        provider,
        lambda: recognizer,
        StateCompatibilityPolicy(),
    )

    voice_command = voice_interpreter.interpret(voice_event, state)

    assert voice_command.intent is Intent.HISTORY_QUERY
    assert voice_command.history_query == text_command.history_query
