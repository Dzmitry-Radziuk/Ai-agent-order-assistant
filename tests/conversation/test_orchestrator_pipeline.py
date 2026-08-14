"""Проверяет поведение, связанное с модулем «test orchestrator pipeline»."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock

import httpx
import pytest

from restaurant_bot.conversation.routing.state_compatibility import StateCompatibilityPolicy
from restaurant_bot.domain.models import (
    BotReply,
    Button,
    CartItem,
    CatalogProduct,
    ConversationState,
    EngineResult,
    ExtractedItem,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    SessionStage,
    TelegramEvent,
)
from restaurant_bot.input.telegram_interpretation import TelegramInputInterpreter
from restaurant_bot.integrations.openai_client import CommentScopeDecision
from restaurant_bot.services import orchestrator as orchestrator_module
from restaurant_bot.services.orchestrator import ClaimedUpdate, UpdateOrchestrator
from restaurant_bot.services.venue_registration import RegistrationResult, VenueContext


def _interpreter(service: UpdateOrchestrator) -> TelegramInputInterpreter:
    """Создаёт интерпретатор для прямых проверок входного маршрута."""
    return TelegramInputInterpreter(service.openai, lambda: MagicMock(), StateCompatibilityPolicy())


def _authorized_orchestrator(mocker) -> UpdateOrchestrator:  # type: ignore[no-untyped-def]
    """Создаёт оркестратор с изолированными внешними зависимостями."""
    service = object.__new__(UpdateOrchestrator)
    service.redis = MagicMock()
    service.telegram = MagicMock()
    service.openai = MagicMock()
    service.sheets = MagicMock()
    service.catalog = MagicMock()
    service.engine = MagicMock()
    service.input_interpreter = MagicMock()
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


def _analytics_orchestrator(*, include_user_content: bool = False) -> UpdateOrchestrator:
    """Создаёт оркестратор только для проверки продуктовой аналитики."""
    service = object.__new__(UpdateOrchestrator)
    service.settings = SimpleNamespace(
        log_user_content=include_user_content,
        log_content_max_length=500,
    )
    service.tracer = MagicMock()
    service.tracer.anonymized_chat_id.return_value = "venue-hash"
    return service


@pytest.mark.parametrize(
    ("status", "expected_outcome", "expected_reason"),
    [
        (ItemStatus.NOT_FOUND, "failed", "product_not_found"),
        (ItemStatus.AMBIGUOUS, "needs_clarification", "ambiguous_product"),
        (ItemStatus.MISSING_QTY, "needs_clarification", "missing_quantity"),
    ],
)
def test_request_analytics_classifies_unsuccessful_product_searches(
    status: ItemStatus,
    expected_outcome: str,
    expected_reason: str,
) -> None:
    """Классифицирует неудачный поиск без раскрытия пользовательского текста."""
    service = _analytics_orchestrator()
    event = orchestrator_module.normalize_telegram_update(_claim("кукуруза 2 кг").payload)
    command = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        items=[ExtractedItem(product_query="кукуруза", quantity=2, unit="кг")],
    )
    item = CartItem(id="corn", source_query="кукуруза", status=status)
    state = ConversationState(
        cart=[item],
        current_issue_item_id=item.id,
        stage=SessionStage.COLLECTING,
    )

    analytics = service._request_analytics(
        event,
        command,
        state,
        previous_stage=SessionStage.COLLECTING.value,
        previous_cart_count=0,
        previous_issue_item_id="",
    )

    assert analytics["scenario"] == "draft_management"
    assert analytics["outcome"] == expected_outcome
    assert analytics["failure_reason"] == expected_reason
    assert analytics["issue_types"] == [status.value]
    assert analytics["request_fingerprint"].startswith("<content sha256:")
    assert "кукуруза" not in analytics["request_fingerprint"]


def test_request_analytics_marks_mixed_result_as_partial() -> None:
    """Отмечает частичный результат, когда найден не каждый новый товар."""
    service = _analytics_orchestrator()
    event = orchestrator_module.normalize_telegram_update(_claim("сироп роза и кукуруза").payload)
    command = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        items=[
            ExtractedItem(product_query="сироп роза"),
            ExtractedItem(product_query="кукуруза"),
        ],
    )
    state = ConversationState(
        cart=[
            CartItem(id="rose", source_query="сироп роза", status=ItemStatus.MATCHED),
            CartItem(id="corn", source_query="кукуруза", status=ItemStatus.NOT_FOUND),
        ]
    )

    analytics = service._request_analytics(
        event,
        command,
        state,
        previous_stage=SessionStage.COLLECTING.value,
        previous_cart_count=0,
        previous_issue_item_id="",
    )

    assert analytics["outcome"] == "partial"
    assert analytics["failure_reason"] == "product_not_found"
    assert analytics["item_count"] == 2
    assert analytics["issue_count"] == 1


def test_request_analytics_marks_ambiguous_comment_scope_for_insights() -> None:
    """Отмечает неясную область комментария как требующее уточнения обращение."""
    service = _analytics_orchestrator(include_user_content=True)
    event = orchestrator_module.normalize_telegram_update(
        _claim("томаты 5 кг, огурцы 4 кг, положить отдельно").payload
    )
    command = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        items=[
            ExtractedItem(product_query="томаты", quantity=5, unit="кг"),
            ExtractedItem(product_query="огурцы", quantity=4, unit="кг"),
        ],
        comment_clarification="положить отдельно",
    )

    analytics = service._request_analytics(
        event,
        command,
        ConversationState(),
        previous_stage=SessionStage.COLLECTING.value,
        previous_cart_count=0,
        previous_issue_item_id="",
    )

    assert analytics["outcome"] == "needs_clarification"
    assert analytics["failure_reason"] == "ambiguous_comment_scope"
    assert analytics["user_text"] == event.text


def test_pending_comment_answer_uses_dedicated_scope_resolver() -> None:
    """Не отправляет ответ об области комментария в обычный поиск товаров."""
    service = object.__new__(UpdateOrchestrator)
    service.openai = SimpleNamespace(
        parse_text=lambda text: ParsedCommand(intent=Intent.UNKNOWN, text=text),
        resolve_comment_scope=lambda _text, _items: CommentScopeDecision(
            action="items",
            target_item_indexes=[0, 1],
            confidence=0.99,
        ),
    )
    state = ConversationState(
        pending_comment_items=[
            ExtractedItem(product_query="Помидоры", quantity=5, unit="кг"),
            ExtractedItem(product_query="Огурцы", quantity=4, unit="кг"),
        ],
        pending_comment_text="положить отдельно",
    )

    command = _interpreter(service).interpret_text("Для всех товаров", state)

    assert command.intent is Intent.ADD_ITEMS
    assert command.comment_scope_action == "items"
    assert command.comment_target_indexes == [0, 1]
    assert [item.product_query for item in command.items] == ["Помидоры", "Огурцы"]


def test_pending_comment_callback_selects_last_item_without_ai() -> None:
    """Безошибочно обрабатывает кнопку последнего товара по сохранённым индексам."""
    service = object.__new__(UpdateOrchestrator)
    service.openai = MagicMock()
    state = ConversationState(
        pending_comment_items=[
            ExtractedItem(product_query="Помидоры"),
            ExtractedItem(product_query="Огурцы"),
        ],
        pending_comment_text="положить отдельно",
    )

    command = _interpreter(service).interpret(
        TelegramEvent(
            update_id=1,
            chat_id="chat",
            input_type=InputKind.CALLBACK,
            callback_data="v2:comment:last:r7",
        ),
        state,
    )

    assert command.comment_scope_action == "items"
    assert command.comment_target_indexes == [1]
    assert command.confidence == 1
    service.openai.resolve_comment_scope.assert_not_called()


def test_request_analytics_can_show_failed_text_when_diagnostics_are_enabled() -> None:
    """Показывает текст нераспознанной команды только по действующей настройке логов."""
    service = _analytics_orchestrator(include_user_content=True)
    event = orchestrator_module.normalize_telegram_update(_claim("привези что-нибудь").payload)

    analytics = service._request_analytics(
        event,
        ParsedCommand(intent=Intent.UNKNOWN, text=event.text),
        ConversationState(),
        previous_stage=SessionStage.COLLECTING.value,
        previous_cart_count=0,
        previous_issue_item_id="",
    )

    assert analytics["scenario"] == "unrecognized_request"
    assert analytics["outcome"] == "failed"
    assert analytics["failure_reason"] == "unrecognized_request"
    assert analytics["user_text"] == "привези что-нибудь"


@pytest.mark.parametrize(
    ("status", "expected_outcome", "expected_reason"),
    [
        (ItemStatus.MATCHED, "success", ""),
        (ItemStatus.UNIT_MISMATCH, "needs_clarification", "unit_mismatch"),
    ],
)
def test_request_analytics_recovers_contextual_voice_quantity(
    status: ItemStatus,
    expected_outcome: str,
    expected_reason: str,
) -> None:
    """Не считает обработанный короткий голосовой ответ нераспознанным."""
    service = _analytics_orchestrator()
    event = orchestrator_module.normalize_telegram_update(_voice_claim().payload).model_copy(
        update={"text": "10 килограмм"}
    )
    item = CartItem(
        id="rice",
        source_query="Рис круглый",
        quantity=10,
        unit="кг",
        status=status,
    )
    state = ConversationState(cart=[item])

    analytics = service._request_analytics(
        event,
        ParsedCommand(intent=Intent.UNKNOWN, text=event.text),
        state,
        previous_stage=SessionStage.REVIEW.value,
        previous_cart_count=1,
        previous_issue_item_id=item.id,
    )

    assert analytics["intent"] == Intent.EDIT_QUANTITY.value
    assert analytics["scenario"] == "draft_management"
    assert analytics["outcome"] == expected_outcome
    assert analytics["failure_reason"] == expected_reason


def test_record_request_outcome_updates_log_and_langfuse_metadata() -> None:
    """Пишет один и тот же понятный итог в лог и Langfuse."""
    service = _analytics_orchestrator()
    trace = MagicMock()
    log = MagicMock()
    analytics = {
        "status": "done",
        "scenario": "order_status",
        "intent": "order_status",
        "outcome": "success",
        "failure_reason": "",
        "input_type": "voice",
    }

    service._record_request_outcome(
        trace,
        log,
        analytics,
        chat_id="7",
        venue_code="venue-123",
    )

    log.info.assert_called_once_with("user_request_outcome", **analytics)
    trace.update.assert_called_once_with(
        output=analytics,
        metadata={
            "scenario": "order_status",
            "intent": "order_status",
            "outcome": "success",
            "failure_reason": "",
            "input_type": "voice",
            "chat_hash": "venue-hash",
            "venue_hash": "venue-hash",
        },
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
    service.input_interpreter.interpret = MagicMock(return_value=command)
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
    service.input_interpreter.interpret = MagicMock()

    service.process(1)

    service._complete_unauthorized.assert_called_once()
    service.input_interpreter.interpret.assert_not_called()
    service._finish.assert_called_once_with(1, "done")


def test_voice_pipeline_shows_progress_before_transcription(mocker) -> None:  # type: ignore[no-untyped-def]
    """Сразу подтверждает получение голоса и заменяет карточку результатом."""
    service = _authorized_orchestrator(mocker)
    service._claim = MagicMock(return_value=_voice_claim())  # type: ignore[method-assign]
    command = ParsedCommand(intent=Intent.SHOW_CART, text="покажи черновик")
    service.input_interpreter.interpret = MagicMock(return_value=command)
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


def test_voice_pipeline_continues_when_progress_card_times_out(mocker) -> None:  # type: ignore[no-untyped-def]
    """Не отменяет распознавание голоса из-за сбоя служебной карточки."""
    service = _authorized_orchestrator(mocker)
    service._claim = MagicMock(return_value=_voice_claim())  # type: ignore[method-assign]
    command = ParsedCommand(intent=Intent.FIX_MULTIPLE, text="выбрать количество")
    service.input_interpreter.interpret = MagicMock(return_value=command)
    result = EngineResult(
        state=ConversationState(),
        reply=BotReply(
            text="Выберите количество",
            rows=[[Button(text="Выбрать 20 кг", callback_data="v2:accept_multiple")]],
        ),
    )
    service.engine.handle.return_value = result
    service._resolve_ai_pending = MagicMock(return_value=result)  # type: ignore[method-assign]
    service.telegram.send_reply.side_effect = [TimeoutError("Telegram timeout"), 67]

    service.process(2)

    service.input_interpreter.interpret.assert_called_once()
    final = service.telegram.send_reply.call_args_list[1].args[1]
    assert final.text == "Выберите количество"
    assert final.edit_message_id is None
    checkpoint = service._checkpoint_state.call_args.args[2]
    assert checkpoint.state.visible_actions == [
        {"label": "Выбрать 20 кг", "action_id": "v2:accept_multiple:r1"}
    ]
    service._finish.assert_called_once_with(2, "done")


def test_photo_pipeline_reports_download_recognition_and_catalog_stages(mocker, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Показывает каждый длительный этап обработки фотографии."""
    service = _authorized_orchestrator(mocker)
    service._claim = MagicMock(return_value=_photo_claim())  # type: ignore[method-assign]
    service.input_interpreter = TelegramInputInterpreter(
        service.openai,
        service._recognizer,
        StateCompatibilityPolicy(),
    )
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
    service.input_interpreter = TelegramInputInterpreter(
        service.openai,
        service._recognizer,
        StateCompatibilityPolicy(),
    )
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
    """Отключает старую карточку перед полным поиском и выводит новый результат."""
    service = _authorized_orchestrator(mocker)
    service._claim = MagicMock(return_value=_search_all_claim())  # type: ignore[method-assign]
    command = ParsedCommand(intent=Intent.SEARCH_ALL_SUPPLIERS, callback_target="0")
    service.input_interpreter.interpret = MagicMock(return_value=command)
    service.catalog.get.return_value = []
    result = EngineResult(state=ConversationState(), reply=BotReply(text="Выберите товар"))
    service.engine.handle.return_value = result
    service._resolve_ai_pending = MagicMock(return_value=result)  # type: ignore[method-assign]
    service.telegram.send_reply.return_value = 72

    service.process(4)

    service.telegram.disable_keyboard.assert_called_once_with("7", 44)
    telegram_methods = [
        entry[0]
        for entry in service.telegram.mock_calls
        if entry[0] in {"answer_callback", "disable_keyboard", "send_reply"}
    ]
    assert telegram_methods[:3] == ["answer_callback", "disable_keyboard", "send_reply"]
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
    service.input_interpreter.interpret = MagicMock(side_effect=RuntimeError("parser failed"))
    service.telegram.send_reply.return_value = 77

    with pytest.raises(RuntimeError, match="parser failed"):
        service.process(1)

    service._finish.assert_called_once_with(1, "failed", "parser failed")
    error_reply = service.telegram.send_reply.call_args.args[1]
    assert "Не удалось обработать сообщение" in error_reply.text
    assert error_reply.rows[0][0].text == "Черновик"


def test_checkpointed_result_retries_only_telegram_delivery(mocker) -> None:  # type: ignore[no-untyped-def]
    """Не применяет товары повторно после временного TLS-сбоя Telegram."""
    service = _authorized_orchestrator(mocker)
    result = EngineResult(
        state=ConversationState(spreadsheet_id="venue-sheet"),
        reply=BotReply(text="Черновик обновлён", edit_message_id=66),
    )
    claim = _voice_claim()
    claim.result = result.model_dump(mode="json")
    claim.state_applied = True
    service._claim = MagicMock(return_value=claim)  # type: ignore[method-assign]
    service.input_interpreter.interpret = MagicMock()
    request = httpx.Request("POST", "https://telegram.invalid/editMessageText")
    service.telegram.send_reply.side_effect = [
        httpx.ConnectTimeout("TLS timeout", request=request),
        91,
    ]

    with pytest.raises(httpx.ConnectTimeout, match="TLS timeout"):
        service.process(2)

    service.input_interpreter.interpret.assert_not_called()
    service.engine.handle.assert_not_called()
    service._checkpoint_state.assert_not_called()
    assert service.telegram.send_reply.call_count == 1
    service._finish.assert_called_once_with(2, "failed", "TLS timeout")

    service.process(2)

    service.input_interpreter.interpret.assert_not_called()
    service.engine.handle.assert_not_called()
    service._checkpoint_state.assert_not_called()
    service._checkpoint_reply.assert_called_once_with(2, "7", 91)
    service._checkpoint_tasks.assert_called_once_with(2)
    assert service._finish.call_args_list[-1].args == (2, "done")
