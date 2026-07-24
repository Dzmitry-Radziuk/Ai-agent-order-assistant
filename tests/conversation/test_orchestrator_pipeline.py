from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock

import pytest

from restaurant_bot.domain.models import (
    BotReply,
    CatalogProduct,
    ConversationState,
    EngineResult,
    ExtractedItem,
    Intent,
    ParsedCommand,
)
from restaurant_bot.services import orchestrator as orchestrator_module
from restaurant_bot.services.orchestrator import ClaimedUpdate, UpdateOrchestrator
from restaurant_bot.services.venue_registration import RegistrationResult, VenueContext


def _authorized_orchestrator(mocker) -> UpdateOrchestrator:  # type: ignore[no-untyped-def]
    """Создаёт оркестратор с изолированными внешними зависимостями."""
    service = object.__new__(UpdateOrchestrator)
    service.redis = MagicMock()
    service.telegram = MagicMock()
    service.openai = MagicMock()
    service.sheets = MagicMock()
    service.catalog = MagicMock()
    service.engine = MagicMock()
    service.tracer = MagicMock()
    trace = MagicMock()
    service.tracer.observation.return_value = nullcontext(trace)
    service.tracer.anonymized_chat_id.return_value = "chat-hash"
    service.registration = MagicMock()
    service.registration.handle.return_value = RegistrationResult(handled=False)
    service.registration.context_for.return_value = VenueContext(
        venue_code="12345",
        venue_name="Тестовое кафе",
        spreadsheet_id="venue-sheet",
        spreadsheet_url="https://example.test/venue-sheet",
        telegram_user_id="7",
        telegram_chat_id="7",
    )
    service._ensure_venue_session = MagicMock()  # type: ignore[method-assign]
    service._checkpoint_state = MagicMock()  # type: ignore[method-assign]
    service._checkpoint_reply = MagicMock()  # type: ignore[method-assign]
    service._checkpoint_tasks = MagicMock()  # type: ignore[method-assign]
    service._enqueue_side_effects = MagicMock()  # type: ignore[method-assign]
    service._finish = MagicMock()  # type: ignore[method-assign]
    mocker.patch.object(
        orchestrator_module,
        "chat_lock",
        lambda *_args, **_kwargs: nullcontext(),
    )
    session_local = mocker.patch.object(orchestrator_module, "SessionLocal")
    session_local.return_value.__enter__.return_value = MagicMock()
    repository = mocker.patch.object(orchestrator_module, "SessionRepository")
    repository.return_value.get_for_update.return_value = (None, ConversationState())
    return service


def _claim(text: str) -> ClaimedUpdate:
    """Создаёт захваченное текстовое обновление Telegram."""
    return ClaimedUpdate(
        payload={
            "update_id": 1,
            "message": {
                "chat": {"id": 7, "type": "private"},
                "from": {"id": 7, "username": "cook"},
                "text": text,
            },
        },
        result=None,
        state_applied=False,
        reply_sent=False,
        tasks_enqueued=False,
    )


def _voice_claim() -> ClaimedUpdate:
    """Создаёт захваченное голосовое обновление Telegram."""
    return ClaimedUpdate(
        payload={
            "update_id": 2,
            "message": {
                "chat": {"id": 7, "type": "private"},
                "from": {"id": 7, "username": "cook"},
                "voice": {"file_id": "voice-1", "mime_type": "audio/ogg"},
            },
        },
        result=None,
        state_applied=False,
        reply_sent=False,
        tasks_enqueued=False,
    )


def _photo_claim() -> ClaimedUpdate:
    """Создаёт захваченное обновление с фотографией."""
    return ClaimedUpdate(
        payload={
            "update_id": 3,
            "message": {
                "chat": {"id": 7, "type": "private"},
                "from": {"id": 7, "username": "cook"},
                "photo": [{"file_id": "photo-1", "file_size": 1000}],
            },
        },
        result=None,
        state_applied=False,
        reply_sent=False,
        tasks_enqueued=False,
    )


def _search_all_claim() -> ClaimedUpdate:
    """Создаёт нажатие кнопки поиска у всех поставщиков."""
    return ClaimedUpdate(
        payload={
            "update_id": 4,
            "callback_query": {
                "id": "callback-1",
                "from": {"id": 7, "username": "cook"},
                "message": {
                    "message_id": 44,
                    "chat": {"id": 7, "type": "private"},
                },
                "data": "v2:searchall:0",
            },
        },
        result=None,
        state_applied=False,
        reply_sent=False,
        tasks_enqueued=False,
    )


def test_product_text_pipeline_shows_progress_and_edits_it_with_result(mocker) -> None:  # type: ignore[no-untyped-def]
    """Показывает поиск сразу и заменяет его итоговой карточкой товара."""
    service = _authorized_orchestrator(mocker)
    claim = _claim("сироп роза 10 штук")
    service._claim = MagicMock(return_value=claim)  # type: ignore[method-assign]
    command = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        items=[ExtractedItem(product_query="сироп роза", quantity=10, unit="шт")],
    )
    service._parse = MagicMock(return_value=command)  # type: ignore[method-assign]
    catalog = [CatalogProduct(product_id="rose", name="Сироп Роза", unit="шт")]
    service.catalog.get.return_value = catalog
    result = EngineResult(
        state=ConversationState(spreadsheet_id="venue-sheet", ui_message_id=44),
        reply=BotReply(text="Черновик обновлён"),
    )
    service.engine.handle.return_value = result
    service._resolve_ai_pending = MagicMock(return_value=result)  # type: ignore[method-assign]
    service.telegram.send_reply.side_effect = [55, 55]

    service.process(1)

    assert service.telegram.send_reply.call_count == 2
    progress = service.telegram.send_reply.call_args_list[0].args[1]
    final = service.telegram.send_reply.call_args_list[1].args[1]
    assert progress.text == "🔎 Ищу товары в каталоге…"
    assert final.edit_message_id == 55
    service.telegram.disable_keyboard.assert_called_once_with("7", None)
    service.catalog.get.assert_called_once_with("")
    service._checkpoint_state.assert_called_once()
    service._checkpoint_reply.assert_called_once_with(1, "7", 55)
    service._checkpoint_tasks.assert_called_once_with(1)
    service._finish.assert_called_once_with(1, "done")


def test_unauthorized_update_stops_before_parser(mocker) -> None:  # type: ignore[no-untyped-def]
    """Не запускает бизнес-логику для непривязанного пользователя."""
    service = _authorized_orchestrator(mocker)
    service._claim = MagicMock(return_value=_claim("сироп роза"))  # type: ignore[method-assign]
    service.registration.context_for.return_value = None
    service._complete_unauthorized = MagicMock()  # type: ignore[method-assign]
    service._parse = MagicMock()  # type: ignore[method-assign]

    service.process(1)

    service._complete_unauthorized.assert_called_once()
    service._parse.assert_not_called()
    service._finish.assert_called_once_with(1, "done")


def test_voice_pipeline_shows_progress_before_transcription(mocker) -> None:  # type: ignore[no-untyped-def]
    """Сразу подтверждает получение голоса и заменяет карточку результатом."""
    service = _authorized_orchestrator(mocker)
    service._claim = MagicMock(return_value=_voice_claim())  # type: ignore[method-assign]
    command = ParsedCommand(intent=Intent.SHOW_CART, text="покажи черновик")
    service._parse = MagicMock(return_value=command)  # type: ignore[method-assign]
    result = EngineResult(state=ConversationState(), reply=BotReply(text="Черновик заявки"))
    service.engine.handle.return_value = result
    service._resolve_ai_pending = MagicMock(return_value=result)  # type: ignore[method-assign]
    service.telegram.send_reply.side_effect = [66, 66]

    service.process(2)

    progress = service.telegram.send_reply.call_args_list[0].args[1]
    final = service.telegram.send_reply.call_args_list[1].args[1]
    assert progress.text == "Обрабатываю голосовое сообщение..."
    assert final.edit_message_id == 66
    service.telegram.disable_keyboard.assert_called_once_with("7", None)
    service.catalog.get.assert_not_called()


def test_photo_pipeline_reports_download_recognition_and_catalog_stages(mocker, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Показывает каждый длительный этап обработки фотографии."""
    service = _authorized_orchestrator(mocker)
    service._claim = MagicMock(return_value=_photo_claim())  # type: ignore[method-assign]
    photo = tmp_path / "large-order.jpg"
    photo.write_bytes(b"image")
    service.telegram.download_file.return_value = SimpleNamespace(
        path=photo,
        mime_type="image/jpeg",
    )
    command = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        items=[
            ExtractedItem(product_query="картофель", quantity=5, unit="кг"),
            ExtractedItem(product_query="сливки", quantity=10, unit="шт"),
        ],
    )
    service.openai.parse_photo.return_value = command
    service.catalog.get.return_value = []
    result = EngineResult(state=ConversationState(), reply=BotReply(text="Черновик обновлён"))
    service.engine.handle.return_value = result
    service._resolve_ai_pending = MagicMock(return_value=result)  # type: ignore[method-assign]
    service.telegram.send_reply.return_value = 70

    service.process(3)

    replies = [call.args[1] for call in service.telegram.send_reply.call_args_list]
    assert [reply.text for reply in replies] == [
        "📷 <b>Фото получено</b>\n\nЗагружаю изображение…",
        "🔎 <b>Распознаю товары на фото…</b>\n\n"
        "Для большого списка это может занять до нескольких минут.",
        "📋 <b>Фото распознано</b>\n\nНайдено позиций: 2. Сверяю товары с каталогом…",
        "Черновик обновлён",
    ]
    assert all(reply.edit_message_id == 70 for reply in replies[1:])
    service.telegram.disable_keyboard.assert_called_once_with("7", None)


def test_photo_timeout_replaces_progress_with_specific_recovery(mocker, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Заменяет зависшую карточку фото понятной подсказкой после таймаута."""
    service = _authorized_orchestrator(mocker)
    service._claim = MagicMock(return_value=_photo_claim())  # type: ignore[method-assign]
    photo = tmp_path / "large-order.jpg"
    photo.write_bytes(b"image")
    service.telegram.download_file.return_value = SimpleNamespace(
        path=photo,
        mime_type="image/jpeg",
    )
    service.openai.parse_photo.side_effect = TimeoutError("vision timed out")
    service.telegram.send_reply.return_value = 71

    with pytest.raises(TimeoutError, match="vision timed out"):
        service.process(3)

    error_reply = service.telegram.send_reply.call_args_list[-1].args[1]
    assert "Не удалось распознать фото" in error_reply.text
    assert "двумя или тремя фотографиями" in error_reply.text
    assert error_reply.edit_message_id == 71


def test_search_all_suppliers_deletes_old_card_and_edits_progress_with_result(
    mocker,
) -> None:  # type: ignore[no-untyped-def]
    """Удаляет старую карточку перед полным поиском и выводит новый результат."""
    service = _authorized_orchestrator(mocker)
    service._claim = MagicMock(return_value=_search_all_claim())  # type: ignore[method-assign]
    command = ParsedCommand(intent=Intent.SEARCH_ALL_SUPPLIERS, callback_target="0")
    service._parse = MagicMock(return_value=command)  # type: ignore[method-assign]
    service.catalog.get.return_value = []
    result = EngineResult(state=ConversationState(), reply=BotReply(text="Выберите товар"))
    service.engine.handle.return_value = result
    service._resolve_ai_pending = MagicMock(return_value=result)  # type: ignore[method-assign]
    service.telegram.send_reply.return_value = 72

    service.process(4)

    service.telegram.delete_message.assert_called_once_with("7", 44)
    replies = [call.args[1] for call in service.telegram.send_reply.call_args_list]
    assert replies[0].text == "🔎 <b>Ищу товар у всех поставщиков…</b>"
    assert replies[1].text == "Выберите товар"
    assert replies[1].edit_message_id == 72


def test_registration_route_short_circuits_business_engine(mocker) -> None:  # type: ignore[no-untyped-def]
    """Завершает регистрацию до запуска товарного сценария."""
    service = _authorized_orchestrator(mocker)
    claim = _claim("12345")
    service._claim = MagicMock(return_value=claim)  # type: ignore[method-assign]
    registration = RegistrationResult(handled=True, reply=BotReply(text="Вы подключены"))
    service.registration.handle.return_value = registration
    service._complete_registration = MagicMock()  # type: ignore[method-assign]

    service.process(1)

    service._complete_registration.assert_called_once_with(1, ANY, claim, registration)
    service.engine.handle.assert_not_called()
    service._finish.assert_called_once_with(1, "done")


def test_complete_registration_persists_context_and_reply(mocker) -> None:  # type: ignore[no-untyped-def]
    """Сохраняет контекст заведения и отправляет карточку регистрации."""
    service = _authorized_orchestrator(mocker)
    claim = _claim("12345")
    event = orchestrator_module.normalize_telegram_update(claim.payload)
    context = VenueContext(
        venue_code="12345",
        venue_name="Кафе",
        spreadsheet_id="sheet-1",
        spreadsheet_url="https://example.test/sheet-1",
        telegram_user_id="7",
        telegram_chat_id="7",
    )
    registration = RegistrationResult(
        handled=True,
        reply=BotReply(text="Вы подключены"),
        context=context,
        reset_session=True,
    )
    service.telegram.send_reply.return_value = 88

    service._complete_registration(1, event, claim, registration)

    checkpoint_result = service._checkpoint_state.call_args.args[2]
    assert checkpoint_result.state.venue_code == "12345"
    assert checkpoint_result.state.venue_name == "Кафе"
    assert checkpoint_result.state.spreadsheet_id == "sheet-1"
    service._checkpoint_reply.assert_called_once_with(1, "7", 88)
    service._checkpoint_tasks.assert_called_once_with(1)


def test_pipeline_failure_is_checkpointed_and_user_gets_safe_reply(mocker) -> None:  # type: ignore[no-untyped-def]
    """Фиксирует ошибку обработки и отправляет безопасную карточку."""
    service = _authorized_orchestrator(mocker)
    claim = _claim("/draft")
    service._claim = MagicMock(return_value=claim)  # type: ignore[method-assign]
    service._parse = MagicMock(side_effect=RuntimeError("parser failed"))  # type: ignore[method-assign]
    service.telegram.send_reply.return_value = 77

    with pytest.raises(RuntimeError, match="parser failed"):
        service.process(1)

    service._finish.assert_called_once_with(1, "failed", "parser failed")
    error_reply = service.telegram.send_reply.call_args.args[1]
    assert "Не удалось обработать сообщение" in error_reply.text
    assert error_reply.rows[0][0].text == "Черновик"
