"""Проверяет границу между разговором и вопросом о поставках."""

from unittest.mock import MagicMock

import pytest

from restaurant_bot.conversation.routing.state_compatibility import StateCompatibilityPolicy
from restaurant_bot.domain.models import ConversationState, Intent, ParsedCommand
from restaurant_bot.input.telegram_interpretation import TelegramInputInterpreter
from restaurant_bot.parsing.history import parse_history_query
from restaurant_bot.parsing.semantic_routing import classify_bot_conversation


@pytest.mark.parametrize(
    "phrase",
    (
        "А если я с тобой буду разговаривать на какие-то свободные темы?",
        "Если я что-нибудь скажу про машины, что будет?",
        "Что ты будешь делать сегодня?",
        "Когда машины приедут?",
        "Если я буду какую-нибудь другую речь говорить, что делать?",
    ),
)
def test_hypothetical_chat_does_not_enter_history(phrase: str) -> None:
    """Не отправляет гипотетический флуд в историю поставок."""
    assert classify_bot_conversation(phrase) is Intent.SMALL_TALK
    assert parse_history_query(phrase) is None


def test_interpreter_protects_hypothetical_chat_before_ai_history_payload() -> None:
    """Не принимает ошибочный AI history-payload для разговорной фразы."""
    provider = MagicMock()
    provider.parse_text.return_value = ParsedCommand(intent=Intent.HISTORY_QUERY)
    interpreter = TelegramInputInterpreter(
        provider,
        lambda: MagicMock(),
        StateCompatibilityPolicy(),
    )

    command = interpreter.interpret_text(
        "А если я с тобой буду разговаривать на какие-то свободные темы?",
        ConversationState(),
    )

    assert command.intent is Intent.SMALL_TALK
    assert command.history_query is None
