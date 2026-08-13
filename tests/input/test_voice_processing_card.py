from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
from openai import APITimeoutError

from restaurant_bot.conversation.routing.state_compatibility import StateCompatibilityPolicy
from restaurant_bot.domain.models import (
    CartItem,
    ConversationState,
    ExtractedItem,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    SessionStage,
    TelegramEvent,
)
from restaurant_bot.input.telegram_interpretation import TelegramInputInterpreter
from restaurant_bot.input.voice_transcript_policy import (
    has_supported_voice_letters,
    select_transcription_result,
)
from restaurant_bot.services.orchestrator import UpdateOrchestrator


def _interpreter(orchestrator: UpdateOrchestrator) -> TelegramInputInterpreter:
    """Создаёт интерпретатор с тем же lazy media recognizer, что и orchestrator."""
    return TelegramInputInterpreter(
        orchestrator.openai,
        orchestrator._recognizer,
        StateCompatibilityPolicy(),
    )


def test_voice_processing_card_matches_the_transient_n8n_reply() -> None:
    """Проверяет, что голос обработка карточка соответствует transient n8n ответ."""
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
    """Проверяет, что голос транскрипция отклоняет несвязанные script."""
    assert not has_supported_voice_letters("روبطرخون")
    assert has_supported_voice_letters("сироп роза")
    assert has_supported_voice_letters("соус Heinz")


def test_generic_voice_prompt_explicitly_preserves_navigation_commands() -> None:
    """Проверяет, что общий голос инструкция модели explicitly сохраняет навигация команды."""
    prompt = UpdateOrchestrator._voice_transcription_prompt(
        type("State", (), {"current_item": lambda self: None})()
    )

    assert "добавить товары" in prompt
    assert "не является названием товара" in prompt


def test_placeholder_transcription_is_retried_with_high_accuracy_model() -> None:
    """Проверяет, что заглушка распознавание голоса является повторяется with повышенная accuracy модель."""
    state = type("State", (), {"current_item": lambda self: None})()

    assert UpdateOrchestrator._requires_high_accuracy_transcription("Тестовый товар", state)


def test_suspicious_voice_action_is_retried_against_visible_buttons() -> None:
    """Повторно распознаёт команду, похожую на видимую кнопку."""
    state = ConversationState(
        visible_actions=[
            {"label": "Выбрать количество", "action_id": "v2:mulone:r4"},
        ]
    )

    assert UpdateOrchestrator._requires_high_accuracy_transcription(
        "Убрать количество",
        state,
    )


def test_exact_visible_action_with_terminal_punctuation_does_not_retry_transcription() -> None:
    """Не отправляет точную голосовую команду на лишнюю повторную расшифровку."""
    state = ConversationState(
        visible_actions=[
            {"label": "К черновику", "action_id": "v2:back:r4"},
        ]
    )

    assert not UpdateOrchestrator._requires_high_accuracy_transcription(
        "К черновику.",
        state,
    )


def test_same_transcription_model_disables_quality_retry() -> None:
    """Не повторяет успешное распознавание той же самой моделью."""
    orchestrator = object.__new__(UpdateOrchestrator)
    orchestrator.openai = SimpleNamespace(
        settings=SimpleNamespace(
            openai_transcribe_model="gpt-4o-transcribe",
            openai_transcribe_fallback_model="gpt-4o-transcribe",
        )
    )

    assert not orchestrator._has_distinct_transcription_fallback()
    orchestrator.openai.settings.openai_transcribe_fallback_model = "gpt-4o-mini-transcribe"

    assert not orchestrator._has_distinct_transcription_fallback()


def _awaiting_kilograms_state() -> ConversationState:
    """Создаёт состояние ожидания количества текущего товара в килограммах."""
    item = CartItem(
        id="squid",
        source_query="Кальмар командорский",
        catalog_product_id="squid-product",
        catalog_name="Кальмар командорский",
        quantity=1,
        unit="шт",
        catalog_unit="кг",
        status=ItemStatus.UNIT_MISMATCH,
    )
    return ConversationState(
        stage=SessionStage.AWAIT_UNIT_QUANTITY,
        current_issue_item_id=item.id,
        cart=[item],
    )


def test_unexpected_unit_in_short_quantity_voice_triggers_high_accuracy_retry() -> None:
    """Перепроверяет короткий голос, если модель спутала граммы с килограммами."""
    state = _awaiting_kilograms_state()

    assert UpdateOrchestrator._requires_high_accuracy_transcription("Один грамм.", state)
    assert not UpdateOrchestrator._requires_high_accuracy_transcription(
        "Один килограмм.",
        state,
    )


def test_quantity_voice_prompt_contains_expected_catalog_unit() -> None:
    """Передаёт модели товар и ожидаемую единицу открытой карточки."""
    prompt = UpdateOrchestrator._voice_transcription_prompt(_awaiting_kilograms_state())

    assert "Кальмар командорский" in prompt
    assert "в кг" in prompt
    assert "грамм" in prompt
    assert "килограмм" in prompt


def test_missing_quantity_voice_prompt_prioritizes_number_over_skip_command() -> None:
    """Подсказывает модели не путать короткое количество с командой пропуска."""
    item = CartItem(
        id="wine",
        source_query="Вино белое",
        catalog_product_id="wine-product",
        catalog_name="Вино белое",
        catalog_unit="шт",
        status=ItemStatus.MISSING_QTY,
    )
    state = ConversationState(
        stage=SessionStage.REVIEW,
        current_issue_item_id=item.id,
        cart=[item],
    )

    prompt = UpdateOrchestrator._voice_transcription_prompt(state)

    assert "только число" in prompt
    assert "пять" in prompt
    assert "не добавлять" in prompt
    assert "это количество" in prompt


def test_high_accuracy_retry_corrects_gram_kilogram_confusion(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Использует уточнённую расшифровку перед применением количества."""
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"voice")
    orchestrator = object.__new__(UpdateOrchestrator)
    orchestrator.telegram = MagicMock()
    orchestrator.telegram.download_file.return_value = SimpleNamespace(
        path=audio,
        mime_type="audio/ogg",
    )
    orchestrator.openai = MagicMock()
    orchestrator.openai.transcribe.side_effect = [
        "Один грамм.",
        "Один килограмм.",
    ]
    orchestrator.openai.parse_text = MagicMock(
        return_value=ParsedCommand(intent=Intent.EDIT_QUANTITY, edit_quantity=1, edit_unit="кг")
    )
    state = _awaiting_kilograms_state()
    event = TelegramEvent(
        update_id=12,
        chat_id="77",
        input_type=InputKind.VOICE,
        file_id="voice-3",
        mime_type="audio/ogg",
    )

    command = _interpreter(orchestrator).interpret(event, state)

    assert command.text == "Один килограмм."
    orchestrator.openai.parse_text.assert_called_once_with("Один килограмм.")
    assert orchestrator.openai.transcribe.call_count == 2
    assert orchestrator.openai.transcribe.call_args_list[1].kwargs["high_accuracy"] is True


def test_high_accuracy_retry_cannot_truncate_a_full_product_list() -> None:
    """Сохраняет полную первую расшифровку, если повтор потерял большую часть списка."""
    primary = "Сироп роза 10 штук, говядины пару килограмм, пару яблок, бутылка воды."
    retry = "Говядины пару килограмм."

    assert select_transcription_result(primary, retry) == primary


def test_high_accuracy_retry_replaces_known_placeholder_transcript() -> None:
    """Заменяет служебную галлюцинацию более точным результатом."""
    assert (
        select_transcription_result(
            "Тестовый товар",
            "Не добавлять",
        )
        == "Не добавлять"
    )


def test_voice_phrase_without_button_verb_matches_visible_action() -> None:
    """Понимает сокращённое название видимой кнопки без вызова ИИ."""
    state = ConversationState(
        visible_actions=[
            {
                "label": "Искать у всех поставщиков",
                "action_id": "v2:searchall:1:r3",
            },
            {"label": "Изменить название", "action_id": "v2:rename:1:r3"},
        ]
    )

    assert (
        TelegramInputInterpreter._match_visible_action("У всех поставщиков.", state)
        == "v2:searchall:1:r3"
    )


def test_voice_transcription_timeout_returns_recovery_command(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Не роняет обработчик при временном тайм-ауте расшифровки."""
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"voice")
    orchestrator = object.__new__(UpdateOrchestrator)
    orchestrator.telegram = MagicMock()
    orchestrator.telegram.download_file.return_value = SimpleNamespace(
        path=audio,
        mime_type="audio/ogg",
    )
    orchestrator.openai = MagicMock()
    orchestrator.openai.transcribe.side_effect = APITimeoutError(
        request=httpx.Request("POST", "https://api.openai.test/audio/transcriptions")
    )
    event = TelegramEvent(
        update_id=10,
        chat_id="77",
        input_type=InputKind.VOICE,
        file_id="voice-1",
        mime_type="audio/ogg",
    )

    command = _interpreter(orchestrator).interpret(event, ConversationState())

    assert command.intent is Intent.UNKNOWN
    assert command.text == ""


def test_high_accuracy_timeout_keeps_primary_transcript(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Использует первичную расшифровку, если уточняющий запрос завис."""
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"voice")
    orchestrator = object.__new__(UpdateOrchestrator)
    orchestrator.telegram = MagicMock()
    orchestrator.telegram.download_file.return_value = SimpleNamespace(
        path=audio,
        mime_type="audio/ogg",
    )
    orchestrator.openai = MagicMock()
    orchestrator.openai.transcribe.side_effect = [
        "Убрать количество",
        APITimeoutError(
            request=httpx.Request("POST", "https://api.openai.test/audio/transcriptions")
        ),
    ]
    orchestrator.openai.parse_text = MagicMock(
        return_value=ParsedCommand(intent=Intent.ENTER_OTHER_QUANTITY)
    )
    state = ConversationState(
        visible_actions=[
            {"label": "Выбрать количество", "action_id": "v2:mulone:r4"},
        ]
    )
    event = TelegramEvent(
        update_id=11,
        chat_id="77",
        input_type=InputKind.VOICE,
        file_id="voice-2",
        mime_type="audio/ogg",
    )

    command = _interpreter(orchestrator).interpret(event, state)

    assert command.intent is Intent.ENTER_OTHER_QUANTITY
    orchestrator.openai.parse_text.assert_called_once_with("Убрать количество")


def test_visible_action_timeout_returns_unknown_instead_of_product() -> None:
    """Не превращает команду кнопки в товар при тайм-ауте смыслового маршрута."""
    orchestrator = object.__new__(UpdateOrchestrator)
    orchestrator.openai = MagicMock()
    orchestrator.openai.parse_text.return_value = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        items=[ExtractedItem(product_query="Править поставщику.")],
    )
    orchestrator.openai.choose_visible_action.side_effect = APITimeoutError(
        request=httpx.Request("POST", "https://api.openai.test/responses")
    )
    state = ConversationState(
        stage=SessionStage.AWAIT_SUBMIT_CONFIRM,
        visible_actions=[
            {"label": "Отправить в таблицу заказа", "action_id": "v2:submit:r7"},
            {"label": "К черновику", "action_id": "v2:back:r7"},
        ],
    )

    command = _interpreter(orchestrator).interpret_text("Править поставщику.", state)

    assert command.intent is Intent.UNKNOWN
    assert command.items == []


def test_free_form_visible_button_phrase_uses_exact_screen_action() -> None:
    """Понимает разговорную формулировку видимой кнопки без вызова ИИ."""
    orchestrator = object.__new__(UpdateOrchestrator)
    orchestrator.openai = MagicMock()
    state = ConversationState(
        visible_actions=[
            {"label": "Выбрать количество", "action_id": "v2:mulone:r4"},
        ]
    )

    command = _interpreter(orchestrator).interpret_text(
        "Я хочу выбрать количество",
        state,
    )

    assert command.intent is Intent.FIX_MULTIPLE
    assert command.callback_revision == 4
    orchestrator.openai.parse_text.assert_not_called()


def test_semantic_voice_action_can_only_choose_a_visible_button() -> None:
    """Выбирает смысловую команду только из кнопок текущего экрана."""
    orchestrator = object.__new__(UpdateOrchestrator)
    orchestrator.openai = MagicMock()
    orchestrator.openai.parse_text.return_value = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        items=[ExtractedItem(product_query="пусть будет как раньше")],
    )
    orchestrator.openai.choose_visible_action.return_value = "v2:keep_current:r8"
    state = ConversationState(
        stage=SessionStage.AWAIT_SUBMIT_CONFIRM,
        ui_message_text="Как поступить?",
        visible_actions=[
            {"label": "Оставить 10 кг", "action_id": "v2:keep_current:r8"},
            {"label": "Ввести другое количество", "action_id": "v2:enter_quantity:r8"},
        ],
    )

    command = _interpreter(orchestrator).interpret_text(
        "Нет, оставим всё как было",
        state,
    )

    assert command.intent is Intent.KEEP_CURRENT_QUANTITY
    orchestrator.openai.choose_visible_action.assert_called_once()


def test_real_product_with_quantity_is_not_replaced_by_visible_add_action() -> None:
    """Не заменяет распознанный товар навигационной кнопкой текущего экрана."""
    orchestrator = object.__new__(UpdateOrchestrator)
    orchestrator.openai = MagicMock()
    parsed = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        items=[
            ExtractedItem(
                product_query="Сироп роза",
                quantity=1,
                unit="шт",
                comment="желательно холодный",
            )
        ],
    )
    orchestrator.openai.parse_text.return_value = parsed
    state = ConversationState(
        stage=SessionStage.AWAIT_ADD_MORE_CONFIRM,
        visible_actions=[
            {"label": "Да, добавить товары", "action_id": "v2:add:r6"},
        ],
    )

    command = _interpreter(orchestrator).interpret_text(
        "Сироп роза, одна штука, желательно холодный.",
        state,
    )

    assert command is parsed
    orchestrator.openai.choose_visible_action.assert_not_called()


def test_semantic_action_does_not_accept_an_unavailable_callback() -> None:
    """Не выполняет действие, которого нет среди видимых кнопок."""
    orchestrator = object.__new__(UpdateOrchestrator)
    orchestrator.openai = MagicMock()
    parsed = ParsedCommand(intent=Intent.UNKNOWN)
    orchestrator.openai.parse_text.return_value = parsed
    orchestrator.openai.choose_visible_action.return_value = ""
    state = ConversationState(
        stage=SessionStage.AWAIT_SUBMIT_CONFIRM,
        visible_actions=[
            {"label": "К черновику", "action_id": "v2:back:r2"},
        ],
    )

    assert _interpreter(orchestrator).interpret_text("удали всё", state) is parsed


def test_callback_ack_failure_does_not_abort_business_action() -> None:
    """Проверяет, что callback подтверждение callback сбой выполняет не abort business действие."""
    orchestrator = object.__new__(UpdateOrchestrator)
    orchestrator.telegram = MagicMock()
    orchestrator.telegram.answer_callback.side_effect = RuntimeError("network")
    log = MagicMock()

    orchestrator._answer_callback_best_effort("callback", log)

    orchestrator.telegram.answer_callback.assert_called_once_with("callback")
    log.warning.assert_called_once_with("telegram_callback_ack_failed", error_type="RuntimeError")
