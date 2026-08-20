"""Проверяет распознавание вопросов о текущем заведении."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from restaurant_bot.conversation.routing.state_compatibility import StateCompatibilityPolicy
from restaurant_bot.domain.models import ConversationState, InputKind, Intent, TelegramEvent
from restaurant_bot.input.telegram_interpretation import TelegramInputInterpreter
from restaurant_bot.integrations.venue_access_registry import VenueAccessEntry
from restaurant_bot.parsing.commands.api import infer_intent
from restaurant_bot.parsing.semantic_routing import classify_bot_conversation
from restaurant_bot.parsing.venue_query import is_venue_status_query
from restaurant_bot.services.venue_registration import VenueContext, VenueRegistrationService


@pytest.mark.parametrize(
    "phrase",
    (
        "К какому заведению я подключен?",
        "К какому ресторану мы сейчас привязаны?",
        "Где я сейчас работаю?",
        "Покажи моё текущее заведение",
        "С каким кафе я связан?",
    ),
)
def test_venue_questions_have_a_read_only_intent(phrase: str) -> None:
    """Распознаёт разные вопросы о текущем заведении без товарной позиции."""
    command = infer_intent(phrase)

    assert is_venue_status_query(phrase)
    assert command.intent is Intent.VENUE_STATUS
    assert command.items == []
    assert command.history_query is None


@pytest.mark.parametrize(
    "phrase",
    (
        "А что мне делать дальше?",
        "Что мне делать дальше?",
        "Как вообще пользоваться ботом?",
    ),
)
def test_help_questions_do_not_go_to_history(phrase: str) -> None:
    """Не направляет вопрос о следующем шаге в историю поставок."""
    command = infer_intent(phrase)

    assert classify_bot_conversation(phrase) is Intent.HELP
    assert command.intent is Intent.HELP
    assert command.history_query is None
    assert command.items == []


@pytest.mark.parametrize("phrase", ("добавь кафе 5 кг", "закажи ресторан 2 шт"))
def test_order_request_keeps_product_semantics(phrase: str) -> None:
    """Не подменяет явную товарную команду вопросом о заведении."""
    command = infer_intent(phrase)

    assert command.intent is Intent.ADD_ITEMS
    assert command.items
    assert not is_venue_status_query(phrase)


class _VoiceTranscript:
    """Передаёт транскрипт голоса в тот же интерпретатор, что и текст."""

    def recognize_media(self, event, state, parse_text, processing_message_id=None):  # type: ignore[no-untyped-def]
        """Использует общий semantic-путь после получения транскрипта."""
        del state, processing_message_id
        return parse_text(event.text, ConversationState())


@pytest.mark.parametrize("input_kind", (InputKind.TEXT, InputKind.VOICE))
def test_text_and_voice_venue_questions_share_the_same_boundary(
    input_kind: InputKind,
) -> None:
    """Связывает текстовый вопрос и его голосовую транскрипцию с одним intent."""
    phrase = "К какому ресторану я сейчас привязан?"
    provider = MagicMock()
    provider.parse_text.return_value = infer_intent(phrase)
    interpreter = TelegramInputInterpreter(
        provider,
        lambda: _VoiceTranscript(),
        StateCompatibilityPolicy(),
    )

    command = interpreter.interpret(
        TelegramEvent(
            update_id=2,
            chat_id="chat-1",
            telegram_user_id="user-1",
            chat_type="private",
            input_type=input_kind,
            text=phrase,
        ),
        ConversationState(),
    )

    assert command.intent is Intent.VENUE_STATUS
    assert command.items == []
    assert command.history_query is None


def _event() -> TelegramEvent:
    """Создаёт личное событие для проверки ответа о заведении."""
    return TelegramEvent(
        update_id=1,
        chat_id="chat-1",
        telegram_user_id="user-1",
        chat_type="private",
        input_type=InputKind.TEXT,
        text="К какому заведению я подключен?",
    )


def _context(name: str = "Качели", code: str = "kach") -> VenueContext:
    """Создаёт текущий контекст заведения для теста."""
    return VenueContext(
        venue_code=code,
        venue_name=name,
        spreadsheet_id="sheet-1",
        spreadsheet_url="https://example.test/sheet-1",
        telegram_user_id="user-1",
        telegram_chat_id="chat-1",
    )


def _service(settings) -> VenueRegistrationService:  # type: ignore[no-untyped-def]
    """Создаёт сервис с изолированным реестром доступа."""
    service = VenueRegistrationService(settings, MagicMock(), MagicMock(), directory=MagicMock())
    service.access_registry = MagicMock()
    return service


def test_current_venue_reply_uses_fresh_active_registry(settings) -> None:  # type: ignore[no-untyped-def]
    """Показывает выбранное заведение по свежей активной привязке."""
    service = _service(settings)
    service.access_registry.entries_for_identity.return_value = [
        VenueAccessEntry("telegram", "user-1", "chat-1", "kach", True, "Качели")
    ]

    reply = service.current_venue_reply(_event(), _context())

    assert "Качели" in reply.text
    assert "Текущее заведение" in reply.text
    assert [(button.text, button.callback_data) for row in reply.rows for button in row] == [
        ("Показать черновик", "v2:cartpage:0"),
        ("Посмотреть статусы заявок", "v2:orders"),
        ("Новая заявка", "v2:new"),
    ]


def test_current_venue_reply_lists_multiple_active_venues(settings) -> None:  # type: ignore[no-untyped-def]
    """Показывает текущее и доступное для переключения заведение."""
    service = _service(settings)
    service.access_registry.entries_for_identity.return_value = [
        VenueAccessEntry("telegram", "user-1", "chat-1", "kach", True, "Качели"),
        VenueAccessEntry("telegram", "user-1", "chat-1", "test", True, "Тестовый ресторан"),
    ]

    reply = service.current_venue_reply(_event(), _context())

    assert "Качели" in reply.text
    assert "Тестовый ресторан" in reply.text
    assert "invite" in reply.text


def test_current_venue_reply_reports_disabled_access(settings) -> None:  # type: ignore[no-untyped-def]
    """Сообщает об отключённой привязке вместо старого названия."""
    service = _service(settings)
    service.access_registry.entries_for_identity.return_value = [
        VenueAccessEntry("telegram", "user-1", "chat-1", "kach", False, "Качели")
    ]

    reply = service.current_venue_reply(_event(), _context())

    assert "отключена" in reply.text
    assert "Качели" not in reply.text
