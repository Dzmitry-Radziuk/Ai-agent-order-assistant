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
from restaurant_bot.parsing.history import parse_history_query


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


def test_delivery_vocabulary_uses_intent_precedence() -> None:
    """Разделяет вопросы о поставке и пожелания к текущему заказу."""
    provider = MagicMock()
    provider.parse_text.return_value = ParsedCommand(intent=Intent.HISTORY_QUERY)
    interpreter = TelegramInputInterpreter(
        provider, lambda: MagicMock(), StateCompatibilityPolicy()
    )
    cases = (
        ("Когда привезут калькан?", Intent.HISTORY_QUERY),
        ("Калькан завтра привезут?", Intent.HISTORY_QUERY),
        ("Калькан уже привезли?", Intent.HISTORY_QUERY),
        ("Что с кальканом?", Intent.HISTORY_QUERY),
        ("Доставка вообще сегодня будет?", Intent.HISTORY_QUERY),
        ("Сегодня что-нибудь привезут?", Intent.HISTORY_QUERY),
        ("Женя сегодня чего-нибудь привезет?", Intent.HISTORY_QUERY),
        ("Привезти завтра к 20:00", Intent.UNKNOWN),
        ("Чтобы привезли завтра до восьми", Intent.UNKNOWN),
        ("Желательно привезти завтра утром", Intent.UNKNOWN),
        (
            "Добавь комментарий к горчице домашней привезти завтра к 20:00",
            Intent.EDIT_COMMENT,
        ),
        ("Добавь общий комментарий: привезти завтра к 20:00", Intent.EDIT_COMMENT),
        ("Добавь калькан 5 кг, привезти завтра к 20:00", Intent.ADD_ITEMS),
        ("Калькан 5 кг привезти завтра к 20:00", Intent.ADD_ITEMS),
        ("Женя, привези калькан завтра", Intent.UNKNOWN),
        (
            "Комментарий для всех товаров: доставить завтра утром",
            Intent.EDIT_COMMENT,
        ),
    )

    for phrase, expected_intent in cases:
        command = interpreter.interpret_text(phrase, ConversationState())
        assert command.intent is expected_intent, phrase
        if expected_intent is Intent.HISTORY_QUERY:
            assert command.history_query is not None
        else:
            assert command.history_query is None


def test_delivery_wish_is_clarified_for_active_cart_without_silent_mutation() -> None:
    """Просит выбрать область пожелания, если в черновике несколько товаров."""
    provider = MagicMock()
    provider.parse_text.return_value = ParsedCommand(intent=Intent.HISTORY_QUERY)
    interpreter = TelegramInputInterpreter(
        provider, lambda: MagicMock(), StateCompatibilityPolicy()
    )
    state = ConversationState(
        cart=[
            CartItem(id="one", source_query="Горчица домашняя"),
            CartItem(id="two", source_query="Горчица дижонская"),
        ]
    )

    command = interpreter.interpret_text("Привезти завтра к 20:00", state)

    assert command.intent is Intent.ADD_ITEMS
    assert command.comment_clarification == "Привезти завтра к 20:00"
    assert command.items == []
    assert command.global_comment == ""
    assert command.history_query is None


def test_delivery_wish_without_cart_is_safe_unknown() -> None:
    """Не создаёт товар из отдельного пожелания без черновика."""
    provider = MagicMock()
    provider.parse_text.return_value = ParsedCommand(intent=Intent.HISTORY_QUERY)
    interpreter = TelegramInputInterpreter(
        provider, lambda: MagicMock(), StateCompatibilityPolicy()
    )

    command = interpreter.interpret_text("Привезти завтра к 20:00", ConversationState())

    assert command.intent is Intent.UNKNOWN
    assert command.items == []
    assert command.history_query is None


def test_delivery_wish_history_parser_declines_instruction_shape() -> None:
    """Не считает форму пожелания самостоятельным запросом истории."""
    assert parse_history_query("Калькан 5 кг привезти завтра к 20:00") is None
    assert parse_history_query("Привезти завтра к 20:00") is None


def test_voice_delivery_wish_and_history_share_text_route() -> None:
    """Голосовая транскрипция использует ту же границу истории и заказа."""
    provider = MagicMock()
    provider.parse_text.return_value = ParsedCommand(intent=Intent.HISTORY_QUERY)

    class TranscriptRecognizer:
        """Передаёт распознанный текст в общий интерпретатор."""

        def recognize_media(self, event, state, parse_text, processing_message_id=None):
            """Вызывает тот же text-путь, что и обычное сообщение."""
            del processing_message_id
            return parse_text(event.text, state)

    interpreter = TelegramInputInterpreter(
        provider, lambda: TranscriptRecognizer(), StateCompatibilityPolicy()
    )
    state = ConversationState()
    for transcript, expected in (
        ("калькан пять килограмм привезти завтра к двадцати", Intent.ADD_ITEMS),
        ("когда привезут калькан", Intent.HISTORY_QUERY),
        ("доставка сегодня будет", Intent.HISTORY_QUERY),
    ):
        text_command = interpreter.interpret_text(transcript, state)
        voice_command = interpreter.interpret(
            TelegramEvent(update_id=1, chat_id="chat", input_type=InputKind.VOICE, text=transcript),
            state,
        )
        assert voice_command == text_command
        assert voice_command.intent is expected


def test_order_delivery_attribute_is_not_history_for_text_or_voice() -> None:
    """Сохраняет заказ, если слово «доставка» является характеристикой товара."""
    provider = MagicMock()
    provider.parse_text.return_value = ParsedCommand(intent=Intent.HISTORY_QUERY)

    class TranscriptRecognizer:
        """Передаёт расшифрованную голосовую фразу в текстовый маршрут."""

        def recognize_media(self, event, state, parse_text, processing_message_id=None):
            """Использует общий разбор текста после распознавания речи."""
            del processing_message_id
            return parse_text(event.text, state)

    interpreter = TelegramInputInterpreter(
        provider, lambda: TranscriptRecognizer(), StateCompatibilityPolicy()
    )
    state = ConversationState()
    transcript = (
        "Мне нужен лук зеленый, 5 килограмм, срез корня от 5 сантиметров, "
        "также нужно, чтобы он был пучками и доставка в коробках."
    )

    text_command = interpreter.interpret_text(transcript, state)
    voice_command = interpreter.interpret(
        TelegramEvent(
            update_id=2,
            chat_id="chat",
            input_type=InputKind.VOICE,
            text=transcript,
        ),
        state,
    )

    assert text_command.intent is Intent.ADD_ITEMS
    assert text_command.history_query is None
    assert text_command.items[0].quantity == 5
    assert text_command.items[0].unit == "кг"
    assert voice_command == text_command
