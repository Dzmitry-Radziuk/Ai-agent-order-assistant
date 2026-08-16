"""Проверяет общий text/voice и modal-safe вход history-запроса."""

from __future__ import annotations

from unittest.mock import MagicMock

from restaurant_bot.conversation.routing.state_compatibility import StateCompatibilityPolicy
from restaurant_bot.domain.history import HistoryQuery, HistoryQuestionType
from restaurant_bot.domain.models import (
    CartItem,
    ConversationState,
    InputKind,
    Intent,
    ParsedCommand,
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


def test_natural_history_phrases_are_classified_before_generic_ai() -> None:
    """Ключевые разговорные вопросы не попадают в добавление товаров."""
    provider = MagicMock()
    provider.parse_text.return_value = ParsedCommand(intent=Intent.ADD_ITEMS)
    interpreter = TelegramInputInterpreter(
        provider, lambda: MagicMock(), StateCompatibilityPolicy()
    )

    phrases = (
        "Говядина приедет?",
        "Мороженое вообще приедет?",
        "Говядина уже приехала?",
        "Хлеб ожидается?",
        "Подскажи по говядине",
        "Что там по говядине?",
        "Мне говядину ждать?",
        "Говядина будет или нет?",
        "Поставка по говядине в силе?",
        "Говядина задерживается?",
        "Когда уже эта говядина будет?",
    )

    commands = [interpreter.interpret_text(phrase, ConversationState()) for phrase in phrases]

    assert all(command.intent is Intent.HISTORY_QUERY for command in commands)
    assert all(command.history_query is not None for command in commands)
    provider.parse_text.assert_not_called()


def test_pronoun_history_question_requires_one_safe_context_product() -> None:
    """Не выдумывает товар для местоимения без однозначного контекста."""
    provider = MagicMock()
    provider.parse_text.return_value = ParsedCommand(intent=Intent.ADD_ITEMS)
    interpreter = TelegramInputInterpreter(
        provider, lambda: MagicMock(), StateCompatibilityPolicy()
    )
    one_item = ConversationState(cart=[CartItem(id="1", source_query="говядина")])
    two_items = ConversationState(
        cart=[CartItem(id="1", source_query="говядина"), CartItem(id="2", source_query="хлеб")]
    )

    safe = interpreter.interpret_text("Она уже приехала?", one_item)
    unsafe = interpreter.interpret_text("Она уже приехала?", two_items)
    empty = interpreter.interpret_text("Она уже приехала?", ConversationState())

    assert safe.intent is Intent.HISTORY_QUERY
    assert safe.history_query is not None
    assert safe.history_query.product_queries == ["говядина"]
    assert unsafe.intent is Intent.UNKNOWN
    assert empty.intent is Intent.UNKNOWN
    assert provider.parse_text.call_count == 0


def test_unrecognized_history_formulation_uses_structured_ai_fallback() -> None:
    """Передаёт свободную формулировку в существующий структурированный AI-путь."""
    provider = MagicMock()
    provider.parse_text.return_value = ParsedCommand(
        intent=Intent.HISTORY_QUERY,
        history_query=HistoryQuery(
            product_queries=["говядину"],
            question_type=HistoryQuestionType.CURRENT_STATUS,
            original_text="Есть ли информация насчёт говядины",
        ),
    )
    interpreter = TelegramInputInterpreter(
        provider, lambda: MagicMock(), StateCompatibilityPolicy()
    )

    command = interpreter.interpret_text(
        "Есть ли информация насчёт говядины",
        ConversationState(),
    )

    assert command.intent is Intent.HISTORY_QUERY
    assert command.history_query is not None
    provider.parse_text.assert_called_once_with("Есть ли информация насчёт говядины")


def test_history_text_and_voice_transcript_share_boundary_route() -> None:
    """Текст и расшифрованный голос проходят один и тот же интерпретатор."""
    provider = MagicMock()
    provider.parse_text.return_value = ParsedCommand(intent=Intent.ADD_ITEMS)

    class _TranscriptRecognizer:
        """Передаёт транскрипт в общий text-путь."""

        def recognize_media(self, event, state, parse_text, processing_message_id=None):
            """Запускает общий разбор расшифрованного текста."""
            del processing_message_id
            return parse_text(event.text, state)

    interpreter = TelegramInputInterpreter(
        provider, lambda: _TranscriptRecognizer(), StateCompatibilityPolicy()
    )
    state = ConversationState()
    transcript = "Мороженое вообще приедет?"
    text_command = interpreter.interpret_text(transcript, state)
    voice_command = interpreter.interpret(
        TelegramEvent(
            update_id=1,
            chat_id="chat",
            input_type=InputKind.VOICE,
            text=transcript,
        ),
        state,
    )

    assert voice_command == text_command
    assert voice_command.intent is Intent.HISTORY_QUERY


def test_venue_history_ai_payload_is_read_only_and_has_no_items() -> None:
    """Нормализует venue-level ответ AI в историю без изменения черновика."""
    provider = MagicMock()
    provider.parse_text.return_value = ParsedCommand(
        intent=Intent.HISTORY_QUERY,
        history_query=HistoryQuery(
            product_queries=[],
            question_type=HistoryQuestionType.VENUE_DELIVERIES,
            original_text="Что по поставкам?",
        ),
    )
    interpreter = TelegramInputInterpreter(
        provider, lambda: MagicMock(), StateCompatibilityPolicy()
    )

    command = interpreter.interpret_text("Есть ли сегодня поставки вообще", ConversationState())

    assert command.intent is Intent.HISTORY_QUERY
    assert command.items == []
    assert command.explicit_add_items is False
    assert command.history_query is not None
    assert command.history_query.product_queries == []
