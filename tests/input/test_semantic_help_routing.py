"""Проверяет семантическую маршрутизацию просьб о помощи для текста и голоса."""

from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from restaurant_bot.conversation.routing.state_compatibility import StateCompatibilityPolicy
from restaurant_bot.domain.models import (
    ConversationState,
    ExtractedItem,
    InputKind,
    Intent,
    ParsedCommand,
    TelegramEvent,
)
from restaurant_bot.input.telegram_interpretation import TelegramInputInterpreter
from restaurant_bot.integrations.openai_client import OpenAIService
from restaurant_bot.parsing.ai.reconciliation import recover_omitted_explicit_items
from restaurant_bot.parsing.ai.schemas import ParsedInputSchema
from restaurant_bot.parsing.semantic_routing import classify_bot_conversation


class TranscriptRecognizer:
    """Передаёт расшифрованный голосовой текст в общий интерпретатор."""

    def recognize_media(self, event, state, parse_text, processing_message_id=None):  # type: ignore[no-untyped-def]
        """Использует тот же semantic parser, что и текстовый канал."""
        return parse_text(event.text, state)


def _interpreter(provider: Mock) -> TelegramInputInterpreter:
    """Создаёт интерпретатор с общим голосовым маршрутом."""
    return TelegramInputInterpreter(
        provider,
        lambda: TranscriptRecognizer(),
        StateCompatibilityPolicy(),
    )


@pytest.mark.parametrize(
    "phrase",
    (
        "Что ты умеешь?",
        "Мне нужна помощь",
        "Как с тобой работать",
        "Скажи, что ты умеешь",
        "Как вообще бот работает?",
    ),
)
def test_free_help_phrases_are_delegated_to_structured_semantics(phrase: str) -> None:
    """Маршрутизирует свободные просьбы о помощи в структурированный AI-контракт."""
    provider = Mock()
    provider.parse_text.return_value = ParsedCommand(intent=Intent.HELP, text=phrase)
    interpreter = _interpreter(provider)

    command = interpreter.interpret(
        TelegramEvent(update_id=1, chat_id="help", input_type=InputKind.TEXT, text=phrase),
        ConversationState(),
    )

    assert command.intent is Intent.HELP
    assert command.items == []
    provider.parse_text.assert_called_once_with(phrase)


@pytest.mark.parametrize("phrase", ["Как с тобой работать", "Как вообще бот работает?"])
def test_voice_help_uses_the_same_semantic_boundary_as_text(phrase: str) -> None:
    """Сводит голосовую расшифровку к тому же результату, что и текст."""
    provider = Mock()
    provider.parse_text.return_value = ParsedCommand(intent=Intent.HELP, text=phrase)
    interpreter = _interpreter(provider)
    state = ConversationState()

    text_command = interpreter.interpret(
        TelegramEvent(update_id=1, chat_id="help", input_type=InputKind.TEXT, text=phrase),
        state,
    )
    voice_command = interpreter.interpret(
        TelegramEvent(update_id=2, chat_id="help", input_type=InputKind.VOICE, text=phrase),
        state,
    )

    assert voice_command.intent is text_command.intent is Intent.HELP
    assert voice_command.items == text_command.items == []
    assert provider.parse_text.call_count == 2


def test_voice_help_cannot_become_catalog_search_when_ai_returns_an_item() -> None:
    """Защищает голосовой вопрос о работе бота от ошибочного item AI."""
    phrase = "Как вообще бот работает?"
    provider = Mock()
    provider.parse_text.return_value = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        text=phrase,
        items=[ExtractedItem(product_query="Вообще бот работает")],
    )
    command = _interpreter(provider).interpret(
        TelegramEvent(update_id=3, chat_id="help", input_type=InputKind.VOICE, text=phrase),
        ConversationState(),
    )

    assert command.intent is Intent.HELP
    assert command.items == []


def test_free_form_help_reaches_existing_structured_ai_parser() -> None:
    """Передаёт новую формулировку помощи существующему structured AI-вызову."""
    phrase = "Как с тобой работать"
    response = SimpleNamespace(
        output_parsed=ParsedInputSchema(intent=Intent.HELP, text=phrase),
        usage=None,
    )
    client = Mock()
    client.responses.parse.return_value = response
    generation = Mock()
    tracer = Mock()
    generation_context = MagicMock()
    generation_context.__enter__.return_value = generation
    tracer.generation.return_value = generation_context
    service = OpenAIService.__new__(OpenAIService)
    service.settings = SimpleNamespace(openai_text_model="test-model")
    service.client = client
    service.tracer = tracer

    command = service._parse_text_once(phrase)

    assert command.intent is Intent.HELP
    assert command.items == []
    client.responses.parse.assert_called_once()
    assert client.responses.parse.call_args.kwargs["text_format"] is ParsedInputSchema


def test_support_question_cannot_be_recovered_as_a_product() -> None:
    """Не превращает вопрос о работе бота в товар при пустом AI-ответе."""
    phrase = "Работаешь?"

    payload = recover_omitted_explicit_items(
        {"intent": Intent.UNKNOWN, "items": []},
        phrase,
    )

    assert payload["intent"] is Intent.UNKNOWN
    assert payload["items"] == []


def test_unknown_help_payload_cannot_keep_an_ai_product_hallucination() -> None:
    """Удаляет товар, если AI вернул его для неподтверждённого общего вопроса."""
    payload = recover_omitted_explicit_items(
        {
            "intent": Intent.UNKNOWN,
            "items": [
                {
                    "product_query": "Работаешь",
                    "quantity": None,
                    "unit": "",
                    "source_line": "Работаешь?",
                }
            ],
        },
        "Работаешь?",
    )

    assert payload["intent"] is Intent.UNKNOWN
    assert payload["items"] == []


def test_explicit_add_command_keeps_product_mutation_semantics() -> None:
    """Сохраняет добавление товара с явным действием и количеством."""
    provider = Mock()
    provider.parse_text.return_value = ParsedCommand(intent=Intent.UNKNOWN)
    command = _interpreter(provider).interpret_text(
        "добавь говядину 5 кг",
        ConversationState(),
    )

    assert command.intent is Intent.ADD_ITEMS
    assert command.items
    assert command.items[0].quantity == 5
    assert command.items[0].unit == "кг"


@pytest.mark.parametrize(
    ("phrase", "expected"),
    (
        (
            "А если я буду просто флудить, что ты скажешь на это?",
            Intent.SMALL_TALK,
        ),
        (
            "Ведь любые запросы, которые я говорю, они ж не должны идти в историю, правильно?",
            Intent.HELP,
        ),
        ("Как с тобой работать?", Intent.HELP),
        ("Вообще бот работает?", Intent.HELP),
        ("Как вообще бот работает?", Intent.HELP),
        ("Тобой пользоваться.", Intent.HELP),
        ("Я не понимаю, что делать", Intent.HELP),
        ("Мне ничего не понятно", Intent.HELP),
        ("Что делать?", Intent.HELP),
    ),
)
def test_bot_conversation_is_not_treated_as_history_or_product(
    phrase: str, expected: Intent
) -> None:
    """Маршрутизирует разговор о боте в помощь или бытовой ответ."""
    assert classify_bot_conversation(phrase) is expected

    provider = Mock()
    provider.parse_text.return_value = ParsedCommand(
        intent=Intent.HISTORY_QUERY,
        text=phrase,
    )
    command = _interpreter(provider).interpret_text(phrase, ConversationState())

    assert command.intent is expected
    assert command.items == []
    assert command.history_query is None


def test_product_history_question_keeps_history_semantics() -> None:
    """Не смешивает вопрос о поставке товара с разговором о работе бота."""
    phrase = "А ты можешь сказать, когда приедет говядина?"
    provider = Mock()
    provider.parse_text.return_value = ParsedCommand(intent=Intent.HISTORY_QUERY, text=phrase)

    command = _interpreter(provider).interpret_text(phrase, ConversationState())

    assert command.intent is Intent.HISTORY_QUERY
    assert command.history_query is not None
    provider.parse_text.assert_not_called()


def test_product_name_starting_with_bot_is_not_treated_as_bot_question() -> None:
    """Не принимает название «ботинок» за обращение к боту."""
    assert classify_bot_conversation("ботинок") is None
