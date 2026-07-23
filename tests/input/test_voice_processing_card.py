from unittest.mock import MagicMock

from restaurant_bot.domain.models import ConversationState, InputKind, SessionStage, TelegramEvent
from restaurant_bot.services.orchestrator import UpdateOrchestrator


def test_voice_processing_card_matches_the_transient_n8n_reply() -> None:
    reply = UpdateOrchestrator._voice_processing_reply()

    assert reply.text == "Обрабатываю голосовое сообщение..."
    assert reply.rows == []
    assert reply.edit_message_id is None


def test_text_product_input_shows_catalog_search_card() -> None:
    """Показывает прогресс сразу после ввода названия товара."""
    event = TelegramEvent(
        update_id=1,
        chat_id="77",
        input_type=InputKind.TEXT,
        text="креветки королевские 10 кг",
    )

    assert UpdateOrchestrator._should_show_text_processing(event, ConversationState())
    assert UpdateOrchestrator._text_processing_reply().text == "🔎 Ищу товары в каталоге…"


def test_text_processing_card_does_not_replace_quantity_or_navigation() -> None:
    """Не показывает поиск для количества и навигационных команд."""
    quantity_event = TelegramEvent(
        update_id=1,
        chat_id="77",
        input_type=InputKind.TEXT,
        text="10",
    )
    quantity_state = ConversationState(stage=SessionStage.AWAIT_MULTIPLE_QUANTITY)
    cart_event = quantity_event.model_copy(update={"text": "покажи черновик"})

    assert not UpdateOrchestrator._should_show_text_processing(quantity_event, quantity_state)
    assert not UpdateOrchestrator._should_show_text_processing(cart_event, ConversationState())


def test_voice_transcript_rejects_unrelated_script() -> None:
    assert not UpdateOrchestrator._has_supported_voice_letters("روبطرخون")
    assert UpdateOrchestrator._has_supported_voice_letters("сироп роза")
    assert UpdateOrchestrator._has_supported_voice_letters("соус Heinz")


def test_generic_voice_prompt_explicitly_preserves_navigation_commands() -> None:
    prompt = UpdateOrchestrator._voice_transcription_prompt(
        type("State", (), {"current_item": lambda self: None})()
    )

    assert "добавить товары" in prompt
    assert "не является названием товара" in prompt


def test_placeholder_transcription_is_retried_with_high_accuracy_model() -> None:
    state = type("State", (), {"current_item": lambda self: None})()

    assert UpdateOrchestrator._requires_high_accuracy_transcription("Тестовый товар", state)


def test_callback_ack_failure_does_not_abort_business_action() -> None:
    orchestrator = object.__new__(UpdateOrchestrator)
    orchestrator.telegram = MagicMock()
    orchestrator.telegram.answer_callback.side_effect = RuntimeError("network")
    log = MagicMock()

    orchestrator._answer_callback_best_effort("callback", log)

    orchestrator.telegram.answer_callback.assert_called_once_with("callback")
    log.warning.assert_called_once_with("telegram_callback_ack_failed", error_type="RuntimeError")
