"""Координирует сервис «orchestrator»."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, cast
from uuid import uuid4

import structlog
from redis import Redis
from structlog.contextvars import bound_contextvars

from restaurant_bot.application.background_tasks import BackgroundTaskDispatcher
from restaurant_bot.application.conversation import ConversationApplication
from restaurant_bot.application.conversation.contracts import ConversationInput
from restaurant_bot.application.conversation.use_case import ConversationProcessor
from restaurant_bot.application.history.query_service import HistoryQueryService
from restaurant_bot.application.order_review.token import new_review_token
from restaurant_bot.catalog.evidence import (
    canonical_search_query,
    has_strong_catalog_anchor,
    query_evidence_tokens,
    remove_phrase_overlap,
    unverified_product_terms,
)
from restaurant_bot.catalog.safety import (
    catalog_matching_comment_context,
    has_compatible_numeric_characteristics,
    has_conflicting_catalog_qualifiers,
    has_unscoped_product_variant_qualifier,
    is_broad_category_query,
    is_safe_catalog_name_equivalent,
)
from restaurant_bot.config import Settings
from restaurant_bot.conversation.routing.contracts import (
    CompatibilityAction,
    CompatibilityContext,
)
from restaurant_bot.domain.history import (
    HistoryAnswer,
    HistoryAnswerKind,
    HistoryQuery,
    HistoryQuestionType,
)
from restaurant_bot.domain.models import (
    BotReply,
    Button,
    CartItem,
    CatalogProduct,
    ConversationState,
    EngineResult,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    SessionStage,
    TelegramEvent,
)
from restaurant_bot.domain.text import clean_text, normalize_text
from restaurant_bot.input.media_recognition import InputRecognitionService
from restaurant_bot.input.telegram import normalize_telegram_update, to_conversation_input
from restaurant_bot.input.telegram_callbacks import parse_callback
from restaurant_bot.input.telegram_interpretation import TelegramInputInterpreter
from restaurant_bot.integrations.cache import (
    CatalogCache,
    ChatLease,
    ChatLeaseLostError,
    chat_lock,
)
from restaurant_bot.integrations.google_sheets import GoogleSheetsGateway
from restaurant_bot.integrations.openai_client import OpenAIService
from restaurant_bot.integrations.openai_transcription_policy import has_distinct_models
from restaurant_bot.integrations.telegram import TELEGRAM_TRANSIENT_ERRORS, TelegramClient
from restaurant_bot.observability import sanitize_log_value
from restaurant_bot.parsing.venue_query import is_venue_status_query
from restaurant_bot.persistence.database import SessionLocal
from restaurant_bot.presentation.telegram.conversation import (
    render_conversation_view,
    telegram_action_mapper,
)
from restaurant_bot.presentation.telegram.formatting import heading
from restaurant_bot.presentation.telegram.history import history_reply
from restaurant_bot.presentation.telegram.order_review import preview_reply
from restaurant_bot.presentation.telegram.venue_registration import not_bound_reply
from restaurant_bot.repositories.history import GoogleHistoryRepository
from restaurant_bot.repositories.order_events import OrderEventRepository
from restaurant_bot.repositories.sessions import SessionRepository
from restaurant_bot.repositories.updates import (
    STALE_PROCESSING_AFTER,
    UpdateRepository,
    UpdateSequenceDeferred,
)
from restaurant_bot.services.engine import ConversationEngine
from restaurant_bot.services.order_review import OrderReviewService
from restaurant_bot.services.venue_registration import (
    RegistrationResult,
    VenueContext,
    VenueRegistrationService,
)

logger = structlog.get_logger(__name__)


def _ensure_lease(lease: ChatLease | None) -> None:
    """Проверяет владение чатом, если обработчик работает с реальным lease."""
    if lease is not None:
        lease.ensure_owned()


def _lease_kwargs(lease: ChatLease | None) -> dict[str, Any]:
    """Возвращает совместимые именованные аргументы для необязательного lease."""
    return {"lease": lease} if lease is not None else {}


_AI_MATCH_SELECT_MIN_CONFIDENCE = 0.90
_AI_MATCH_NOT_FOUND_MIN_CONFIDENCE = 0.80


def _contradictions_reference_request(
    contradictions: list[str],
    source_query: str,
    comment: str,
) -> bool:
    """Проверяет, ссылаются ли AI-противоречия на признаки самого запроса."""
    normalized_request = normalize_text(f"{source_query} {comment}")
    if not re.search(
        r"\b(?:без|не|только|именно|обязательно|желательн\w*|свеж\w*|"
        r"охлажд\w*|заморож\w*|сол[её]н\w*|копч[её]н\w*|молод\w*|"
        r"крупн\w*|мелк\w*|бренд|артикул)\b",
        normalized_request,
        flags=re.I,
    ):
        # Базовое слово товара в объяснении модели не является требованием.
        return False
    request_text = f"{source_query} {comment}"
    return any(
        query_evidence_tokens(value, request_text) for value in contradictions if value.strip()
    )


_AI_MATCH_MIN_SCORE = 40.0
_SHEET_REVIEW_MODE = "sheet_link"
_REVIEW_INTENTS = frozenset(
    {
        Intent.REVIEW_ORDER,
        Intent.REVIEW_REFRESH,
        Intent.REVIEW_SUBMIT,
        Intent.REVIEW_CANCEL,
    }
)


def _catalog_match_evidence(item: CartItem) -> str:
    """Возвращает название для определения широты каталожного запроса."""
    return item.source_query


@dataclass(slots=True)
class ClaimedUpdate:
    """Хранит взятое в обработку обновление Telegram."""

    payload: dict[str, Any]
    result: dict[str, Any] | None
    state_applied: bool
    reply_sent: bool
    tasks_enqueued: bool
    attempt: int = 0


class UpdateOrchestrator:
    """Координирует разбор сообщения, состояние и ответ."""

    def __init__(
        self,
        settings: Settings,
        redis: Redis[Any],
        telegram: TelegramClient,
        openai_service: OpenAIService,
        sheets: GoogleSheetsGateway,
        background_tasks: BackgroundTaskDispatcher,
    ):
        """Инициализирует компонент."""
        self.settings = settings
        self.redis = redis
        self.telegram = telegram
        self.openai = openai_service
        self.input_recognition = InputRecognitionService(telegram, openai_service)
        self.sheets = sheets
        self.catalog = CatalogCache(settings, redis, sheets)
        self.history_queries = HistoryQueryService(
            GoogleHistoryRepository(sheets),
            timezone_name=settings.app_timezone,
        )
        self.engine = ConversationEngine(settings)
        self.conversation_application = ConversationApplication(
            cast(ConversationProcessor, self.engine),
            action_mapper=telegram_action_mapper,
        )
        self.input_interpreter = TelegramInputInterpreter(
            self.openai,
            self._recognizer,
            self.engine.state_compatibility_policy,
            timezone_name=settings.app_timezone,
        )
        self.tracer = openai_service.tracer
        self.registration = VenueRegistrationService(settings, redis, sheets)
        self.order_review = OrderReviewService(settings, redis, telegram, sheets)
        self.background_tasks = background_tasks

    def process(self, update_id: int) -> None:
        """Обрабатывает одно обновление Telegram целиком."""
        started_at = perf_counter()
        timings: dict[str, int] = {}
        claim = self._claim(update_id, mark_processing=False)
        timings["claim_ms"] = round((perf_counter() - started_at) * 1000)
        if claim is None:
            return
        event = normalize_telegram_update(claim.payload)
        log = logger.bind(
            update_id=update_id, chat_id=event.chat_id, input_type=event.input_type.value
        )
        log.info(
            "telegram_update_started",
            text=event.text,
            callback_data=event.callback_data,
            has_file=bool(event.file_id),
        )

        if not event.chat_id:
            self._finish(update_id, "ignored")
            return

        callback_started = perf_counter()
        self._answer_callback_best_effort(event.callback_query_id, log)
        if event.callback_query_id:
            timings["callback_ack_ms"] = round((perf_counter() - callback_started) * 1000)
        if event.input_type == InputKind.CALLBACK and event.callback_message_id:
            self._disable_keyboard_best_effort(log, event.chat_id, event.callback_message_id)

        with (
            bound_contextvars(
                update_id=update_id,
                chat_id=event.chat_id,
                input_type=event.input_type.value,
            ),
            self.tracer.observation(
                "telegram_update",
                input={"update_id": update_id, "input_type": event.input_type.value},
                metadata={"chat_hash": self.tracer.anonymized_chat_id(event.chat_id)},
            ) as trace,
            chat_lock(self.redis, event.chat_id) as lease,
        ):
            _ensure_lease(lease)
            claim = self._claim(update_id)
            if claim is None:
                return
            processing_message_id: int | None = None
            analytics = {
                "status": "done",
                "scenario": "message_delivery",
                "intent": "replayed_result",
                "outcome": "success",
                "failure_reason": "",
                "input_type": event.input_type.value,
            }
            try:
                result = (
                    EngineResult.model_validate(claim.result)
                    if claim.state_applied and claim.result
                    else None
                )

                if result is None and event.input_type == InputKind.CALLBACK:
                    processing_reply = self._callback_processing_reply(event)
                    processing_reply.edit_message_id = event.callback_message_id
                    processing_message_id = self._send_processing_best_effort(
                        log,
                        event.chat_id,
                        processing_reply,
                    )

                registration = self.registration.handle(event)
                if (
                    not registration.handled
                    and event.input_type is InputKind.TEXT
                    and is_venue_status_query(event.text)
                ):
                    registration = RegistrationResult(
                        handled=True,
                        reply=self.registration.current_venue_reply(event),
                    )
                if registration.handled:
                    registration_kwargs = _lease_kwargs(lease)
                    if processing_message_id is not None:
                        registration_kwargs["processing_message_id"] = processing_message_id
                    self._complete_registration(
                        update_id,
                        event,
                        claim,
                        registration,
                        **registration_kwargs,
                    )
                    _ensure_lease(lease)
                    self._finish(
                        update_id,
                        "done",
                        **_lease_kwargs(lease),
                    )
                    analytics.update(
                        scenario="registration",
                        intent="registration",
                        outcome="success",
                    )
                    self._record_request_outcome(
                        trace,
                        log,
                        analytics,
                        chat_id=event.chat_id,
                    )
                    return

                venue_context = (
                    self.registration.context_for_identity(
                        event.telegram_user_id,
                        event.chat_id,
                        force_refresh=True,
                    )
                    if self._is_review_event(event)
                    else self.registration.context_for(event)
                )
                if venue_context is None:
                    logger.info(
                        "unauthorized_business_request",
                        telegram_user_id=event.telegram_user_id,
                        chat_id=event.chat_id,
                        input_type=event.input_type.value,
                    )
                    self._complete_unauthorized(
                        update_id,
                        event,
                        claim,
                        processing_message_id=processing_message_id,
                        **_lease_kwargs(lease),
                    )
                    _ensure_lease(lease)
                    self._finish(
                        update_id,
                        "done",
                        **_lease_kwargs(lease),
                    )
                    analytics.update(
                        scenario="access",
                        intent="authorization",
                        outcome="access_denied",
                        failure_reason="unauthorized",
                    )
                    self._record_request_outcome(
                        trace,
                        log,
                        analytics,
                        chat_id=event.chat_id,
                    )
                    return
                self._ensure_venue_session(event, venue_context, lease=lease)
                logger.info(
                    "venue_context_loaded",
                    telegram_user_id=event.telegram_user_id,
                    chat_id=event.chat_id,
                    venue_code=venue_context.venue_code,
                )

                if result is None:
                    stage_started = perf_counter()
                    with SessionLocal() as db:
                        _, state = SessionRepository(db).get_for_update(event.chat_id)
                    timings["state_load_ms"] = round((perf_counter() - stage_started) * 1000)
                    log.info("conversation_state_loaded", **self._state_log(state))

                    # Каждое новое сообщение получает собственную временную карточку:
                    # итог заменяет именно её, но не старый результат выше в чате.
                    # Клавиатуру прежней карточки отключаем сразу, чтобы старый callback
                    # не мог изменить уже начавшийся следующий сценарий.
                    if event.input_type == InputKind.VOICE:
                        stage_started = perf_counter()
                        processing_message_id = self._send_processing_best_effort(
                            log,
                            event.chat_id,
                            self._voice_processing_reply(),
                        )
                        self._disable_keyboard_best_effort(log, event.chat_id, state.ui_message_id)
                        timings["processing_card_ms"] = round(
                            (perf_counter() - stage_started) * 1000
                        )
                    elif event.input_type == InputKind.PHOTO:
                        stage_started = perf_counter()
                        processing_message_id = self._send_processing_best_effort(
                            log,
                            event.chat_id,
                            self._photo_received_reply(),
                        )
                        self._disable_keyboard_best_effort(log, event.chat_id, state.ui_message_id)
                        timings["processing_card_ms"] = round(
                            (perf_counter() - stage_started) * 1000
                        )
                    elif self._should_show_text_processing(event, state):
                        stage_started = perf_counter()
                        processing_message_id = self._send_processing_best_effort(
                            log,
                            event.chat_id,
                            self._text_processing_reply_for_event(event),
                        )
                        self._disable_keyboard_best_effort(log, event.chat_id, state.ui_message_id)
                        timings["processing_card_ms"] = round(
                            (perf_counter() - stage_started) * 1000
                        )

                    stage_started = perf_counter()
                    command = self.input_interpreter.interpret(
                        event,
                        state,
                        processing_message_id,
                    )
                    timings["parse_ms"] = round((perf_counter() - stage_started) * 1000)
                    log.info("command_parsed", **self._command_log(command))
                    if event.input_type is InputKind.PHOTO and command.intent in _REVIEW_INTENTS:
                        command = ParsedCommand(intent=Intent.UNKNOWN, text=event.text)
                    history_command = (
                        command.intent is Intent.HISTORY_QUERY and command.history_query
                    )
                    venue_status_command = command.intent is Intent.VENUE_STATUS
                    sheet_review_decision = (
                        self.engine.state_compatibility_policy.evaluate(
                            command,
                            state,
                            CompatibilityContext.SHEET_REVIEW,
                        )
                        if event.input_type is not InputKind.CALLBACK and not history_command
                        else None
                    )
                    sheet_review_ambiguous = (
                        sheet_review_decision is not None
                        and sheet_review_decision.action is CompatibilityAction.AMBIGUOUS
                    )
                    stage_started = perf_counter()
                    review_command = (
                        command.intent in _REVIEW_INTENTS and not sheet_review_ambiguous
                    )
                    catalog_needed = False if review_command else self._needs_catalog(command)
                    fresh_catalog_required = (
                        False if review_command else self._requires_fresh_catalog(command)
                    )
                    catalog = (
                        (
                            self.catalog.get(state.spreadsheet_id, force_refresh=True)
                            if fresh_catalog_required
                            else self.catalog.get(state.spreadsheet_id)
                        )
                        if catalog_needed
                        else []
                    )
                    timings["catalog_ms"] = round((perf_counter() - stage_started) * 1000)
                    log.info(
                        "catalog_ready",
                        needed=catalog_needed,
                        fresh=fresh_catalog_required,
                        product_count=len(catalog),
                        catalog_ms=timings["catalog_ms"],
                    )

                    stage_started = perf_counter()
                    previous_stage = state.stage.value
                    previous_cart_count = len(state.cart)
                    previous_issue_item_id = state.current_issue_item_id
                    previous_trace_id = state.order_trace_id
                    previous_new_order_confirmation = state.pending_new_order_confirmation
                    previous_order_status_view_active = state.order_status_view_active
                    result = (
                        EngineResult(
                            state=state,
                            reply=self._sheet_review_ambiguous_reply(state),
                        )
                        if sheet_review_ambiguous
                        else self._process_venue_status_query(event, state, venue_context)
                        if venue_status_command
                        else self._process_history_query(command, state)
                        if history_command
                        else (
                            self._handle_review_command(event, state, command)
                            if review_command
                            else self._process_conversation(event, command, state, catalog)
                        )
                    )
                    _ensure_lease(lease)

                    # AI работает за пределами DB-транзакции. Он может выбрать только ID из shortlist.
                    if not review_command and not history_command and not venue_status_command:
                        if not sheet_review_ambiguous:
                            result = self._resolve_ai_pending(event, result, catalog)
                        self._leave_sheet_review_on_regular_command(command, result.state)
                    trace_started = bool(result.state.cart and not result.state.order_trace_id)
                    if trace_started:
                        result.state.order_trace_id = str(uuid4())
                    if result.state.pending_submission:
                        result.state.pending_submission.trace_id = (
                            result.state.pending_submission.trace_id
                            or result.state.order_trace_id
                            or previous_trace_id
                            or str(uuid4())
                        )
                        result.state.pending_submission.telegram_user_id = (
                            event.telegram_user_id or event.chat_id
                        )
                        result.state.pending_submission.telegram_chat_id = event.chat_id
                    timings["engine_ms"] = round((perf_counter() - stage_started) * 1000)
                    log.info(
                        "engine_transition_completed",
                        previous_stage=previous_stage,
                        previous_cart_count=previous_cart_count,
                        engine_ms=timings["engine_ms"],
                        **self._state_log(result.state),
                    )
                    analytics = self._request_analytics(
                        event,
                        command,
                        result.state,
                        previous_stage=previous_stage,
                        previous_cart_count=previous_cart_count,
                        previous_issue_item_id=previous_issue_item_id,
                    )

                    if (
                        command.intent == Intent.ORDER_STATUS
                        and previous_order_status_view_active
                        and state.ui_message_id
                    ):
                        if processing_message_id and processing_message_id != state.ui_message_id:
                            self.telegram.delete_message(event.chat_id, processing_message_id)
                        result.reply.edit_message_id = state.ui_message_id
                    elif processing_message_id:
                        result.reply.edit_message_id = processing_message_id
                    elif event.input_type == InputKind.CALLBACK and event.callback_message_id:
                        result.reply.edit_message_id = event.callback_message_id
                    elif state.ui_message_id:
                        result.reply.edit_message_id = state.ui_message_id
                    result.state.ui_revision += 1
                    self._attach_ui_revision(result.reply, result.state.ui_revision)
                    result.state.ui_message_text = result.reply.text
                    self._store_visible_actions(result.state, result.reply)
                    stage_started = perf_counter()
                    _ensure_lease(lease)
                    self._checkpoint_state(
                        update_id,
                        event.chat_id,
                        result,
                        audit_context={
                            "previous_trace_id": previous_trace_id,
                            "trace_started": trace_started,
                            "telegram_user_id": event.telegram_user_id or event.chat_id,
                            "telegram_chat_id": event.chat_id,
                            "venue_code": result.state.venue_code,
                            "intent": command.intent.value,
                            "input_type": event.input_type.value,
                            "previous_stage": previous_stage,
                            "previous_cart_count": previous_cart_count,
                            "previous_new_order_confirmation": (previous_new_order_confirmation),
                        },
                        **_lease_kwargs(lease),
                    )
                    timings["state_checkpoint_ms"] = round((perf_counter() - stage_started) * 1000)
                    log.info(
                        "conversation_state_checkpointed",
                        state_checkpoint_ms=timings["state_checkpoint_ms"],
                        **self._reply_log(result.reply),
                    )
                    claim.state_applied = True
                    claim.result = result.model_dump(mode="json")

                if not claim.reply_sent:
                    _ensure_lease(lease)
                    stage_started = perf_counter()
                    message_id = self.telegram.send_reply(event.chat_id, result.reply)
                    _ensure_lease(lease)
                    self._checkpoint_reply(
                        update_id,
                        event.chat_id,
                        message_id,
                        **_lease_kwargs(lease),
                    )
                    timings["reply_ms"] = round((perf_counter() - stage_started) * 1000)
                    log.info(
                        "telegram_reply_sent",
                        message_id=message_id,
                        reply_ms=timings["reply_ms"],
                        **self._reply_log(result.reply),
                    )
                    claim.reply_sent = True

                if not claim.tasks_enqueued:
                    _ensure_lease(lease)
                    self._enqueue_side_effects(
                        event.chat_id,
                        result,
                        **_lease_kwargs(lease),
                    )
                    _ensure_lease(lease)
                    self._checkpoint_tasks(
                        update_id,
                        **_lease_kwargs(lease),
                    )
                    claim.tasks_enqueued = True
                    task_names = [
                        name
                        for enabled, name in (
                            (result.enqueue_submission, "submit_order"),
                            (result.enqueue_order_status, "send_order_status"),
                            (result.enqueue_product_add, "submit_product_add"),
                            (result.enqueue_review_submission, "submit_review_order"),
                        )
                        if enabled
                    ]
                    log.info(
                        "background_tasks_checkpointed",
                        task_count=len(task_names),
                        task_names=task_names,
                    )

                if result.invalidate_catalog:
                    _ensure_lease(lease)
                    self.catalog.invalidate(result.state.spreadsheet_id)
                    _ensure_lease(lease)
                self._finish(
                    update_id,
                    "done",
                    **_lease_kwargs(lease),
                )
                self._record_request_outcome(
                    trace,
                    log,
                    analytics,
                    chat_id=event.chat_id,
                    venue_code=result.state.venue_code,
                )
                log.info(
                    "telegram_update_processed",
                    total_ms=round((perf_counter() - started_at) * 1000),
                    **timings,
                )
            except ChatLeaseLostError:
                log.warning("telegram_update_deferred_lease_loss")
                self._defer_if_current_attempt(update_id, claim.attempt)
                raise
            except Exception as exc:
                try:
                    _ensure_lease(lease)
                except ChatLeaseLostError:
                    log.warning("telegram_update_deferred_lease_loss")
                    self._defer_if_current_attempt(update_id, claim.attempt)
                    raise
                log.exception("telegram_update_failed", error=str(exc))
                analytics.update(
                    status="failed",
                    outcome="technical_error",
                    failure_reason=type(exc).__name__,
                )
                self._record_request_outcome(
                    trace,
                    log,
                    analytics,
                    chat_id=event.chat_id,
                )
                trace.update(level="ERROR", status_message=str(exc)[:500])
                self._finish(
                    update_id,
                    "failed",
                    str(exc),
                    **_lease_kwargs(lease),
                )
                delivery_deferred = (
                    claim.state_applied
                    and not claim.reply_sent
                    and isinstance(exc, TELEGRAM_TRANSIENT_ERRORS)
                )
                if delivery_deferred:
                    # Состояние и результат уже сохранены в checkpoint. Повтор Celery
                    # перечитает их и повторит только доставку в Telegram, но не разбор
                    # и не применение позиций пользователя.
                    log.warning(
                        "telegram_reply_delivery_deferred",
                        error_type=type(exc).__name__,
                    )
                elif not claim.reply_sent:
                    try:
                        _ensure_lease(lease)
                        error_reply = self._error_reply(event)
                        error_reply.edit_message_id = processing_message_id
                        self.telegram.send_reply(event.chat_id, error_reply)
                        _ensure_lease(lease)
                    except Exception:
                        log.exception("telegram_error_reply_failed")
                raise

    @staticmethod
    def _leave_sheet_review_on_regular_command(
        command: ParsedCommand,
        state: ConversationState,
    ) -> None:
        """Отключает режим карточки из таблицы после обычного действия в черновике.

        Неизвестная или явно нераспознанная фраза не должна уничтожать
        контекст карточки: пользователь может повторить голосовую команду.
        Любое распознанное обычное действие, напротив, возвращает диалог в
        стандартный режим черновика и удаляет одноразовые данные карточки.
        """
        if state.review_mode != _SHEET_REVIEW_MODE or command.intent == Intent.UNKNOWN:
            return
        state.review_mode = "cart"
        state.review_token = ""
        state.review_snapshot_hash = ""
        state.review_venue_code = ""
        state.review_submission_in_progress = False

    def _handle_review_command(
        self,
        event: TelegramEvent,
        state: ConversationState,
        command: ParsedCommand,
    ) -> EngineResult:
        """Обрабатывает просмотр заявки из deep-link без обращения к n8n."""
        if command.intent in {Intent.REVIEW_ORDER, Intent.REVIEW_REFRESH}:
            requested_code = normalize_text(command.callback_target or command.target_query).upper()
            if requested_code and requested_code != normalize_text(state.venue_code).upper():
                return EngineResult(
                    state=state,
                    reply=BotReply(
                        text=(
                            f"⛔ {heading('Ссылка относится к другому заведению')}\n\n"
                            "Откройте ссылку из таблицы своего заведения."
                        )
                    ),
                )
            context = VenueContext(
                venue_code=state.venue_code,
                venue_name=state.venue_name,
                spreadsheet_id=state.spreadsheet_id,
                spreadsheet_url=state.spreadsheet_url,
                telegram_user_id=event.telegram_user_id or event.chat_id,
                telegram_chat_id=event.chat_id,
            )
            snapshot = self.order_review.snapshot(context)
            token = new_review_token()
            state.review_token = token
            state.review_snapshot_hash = snapshot.fingerprint
            state.review_venue_code = state.venue_code
            state.review_submission_in_progress = False
            state.review_mode = _SHEET_REVIEW_MODE
            state.stage = SessionStage.REVIEW
            state.status = "review"
            return EngineResult(
                state=state,
                reply=preview_reply(
                    snapshot,
                    token,
                    edit_message_id=event.callback_message_id,
                ),
            )

        token = command.callback_target
        if (
            not token
            or token != state.review_token
            or (
                command.callback_revision is not None
                and command.callback_revision != state.ui_revision
            )
        ):
            return EngineResult(state=state, reply=self._review_stale_reply(state))
        if command.intent == Intent.REVIEW_CANCEL:
            state.review_token = ""
            state.review_snapshot_hash = ""
            state.review_venue_code = ""
            state.review_submission_in_progress = False
            state.review_mode = "cart"
            state.stage = SessionStage.COLLECTING if state.cart else SessionStage.SUBMITTED
            state.status = "collecting" if state.cart else "submitted"
            return EngineResult(
                state=state,
                reply=BotReply(
                    text="Отправка заявки отменена.",
                    rows=[[Button(text="Показать черновик", callback_data="v2:back")]],
                ),
            )
        if command.intent == Intent.REVIEW_SUBMIT:
            if state.review_submission_in_progress:
                return EngineResult(
                    state=state,
                    reply=BotReply(text="⏳ Заявка уже проверяется. Подождите немного."),
                )
            context = VenueContext(
                venue_code=state.venue_code,
                venue_name=state.venue_name,
                spreadsheet_id=state.spreadsheet_id,
                spreadsheet_url=state.spreadsheet_url,
                telegram_user_id=event.telegram_user_id or event.chat_id,
                telegram_chat_id=event.chat_id,
            )
            current = self.order_review.snapshot(context)
            if current.fingerprint != state.review_snapshot_hash:
                refreshed_token = new_review_token()
                state.review_token = refreshed_token
                state.review_snapshot_hash = current.fingerprint
                state.review_submission_in_progress = False
                state.stage = SessionStage.REVIEW
                state.status = "review"
                return EngineResult(
                    state=state,
                    reply=preview_reply(
                        current,
                        refreshed_token,
                        changed=True,
                    ),
                )
            if not self.settings.google_order_submission_enabled:
                state.review_submission_in_progress = False
                return EngineResult(
                    state=state,
                    reply=BotReply(
                        text=(
                            f"ℹ️ {heading('Отправка пока отключена')}\n\n"
                            "Заявка проверена, но поставщикам ничего не отправлено. "
                            "Данные таблицы не изменены."
                        ),
                        rows=[
                            [
                                Button(
                                    text="Обновить заявку",
                                    callback_data=f"v2:review:{state.review_venue_code}",
                                )
                            ],
                            [Button(text="Закрыть", callback_data=f"v2:review_cancel:{token}")],
                        ],
                    ),
                )
            state.review_submission_in_progress = True
            state.stage = SessionStage.SUBMITTING
            state.status = "submitting"
            return EngineResult(
                state=state,
                reply=BotReply(text="⏳ Проверяю актуальную заявку перед отправкой…"),
                enqueue_review_submission=True,
            )
        return EngineResult(state=state, reply=self._review_stale_reply(state))

    @staticmethod
    def _is_review_event(event: TelegramEvent) -> bool:
        """Определяет, относится ли обновление к deep-link или карточке проверки."""
        return event.callback_data.startswith("v2:review") or bool(
            re.fullmatch(
                r"/start(?:@[A-Za-z0-9_]+)?\s+review_[A-Za-zА-ЯЁ0-9]{4,32}",
                clean_text(event.text),
                flags=re.IGNORECASE,
            )
        )

    @staticmethod
    def _review_stale_reply(state: ConversationState) -> BotReply:
        """Сообщает, что старая кнопка проверки больше не действует."""
        code = state.review_venue_code or state.venue_code
        rows = [[Button(text="Обновить заявку", callback_data=f"v2:review:{code}")]] if code else []
        return BotReply(
            text="🔸 Эта карточка заявки устарела. Обновите заявку и проверьте её ещё раз.",
            rows=rows,
        )

    @staticmethod
    def _sheet_review_ambiguous_reply(state: ConversationState) -> BotReply:
        """Возвращает безопасный ответ с актуальными кнопками sheet-review."""
        rows: list[list[Button]] = []
        buttons: list[Button] = []
        for action in state.visible_actions:
            callback_data = re.sub(r":r\d+$", "", action.get("action_id", ""))
            if not callback_data.startswith("v2:review_"):
                continue
            if state.review_token and state.review_token not in callback_data:
                continue
            label = action.get("label", "")
            if label:
                buttons.append(Button(text=label, callback_data=callback_data))
        if buttons:
            rows.append(buttons)
        elif state.review_token:
            rows.append(
                [
                    Button(
                        text="Отправить заявку",
                        callback_data=f"v2:review_submit:{state.review_token}",
                    ),
                    Button(
                        text="Отменить",
                        callback_data=f"v2:review_cancel:{state.review_token}",
                    ),
                ]
            )
        if state.review_venue_code:
            rows.append(
                [
                    Button(
                        text="Обновить заявку",
                        callback_data=f"v2:review:{state.review_venue_code}",
                    )
                ]
            )
        text = "Не удалось понять команду для этой заявки."
        if state.ui_message_text:
            text = f"{text}\n\n{state.ui_message_text}"
        return BotReply(text=text, rows=rows)

    @staticmethod
    def _scenario_for_intent(intent: Intent) -> str:
        """Возвращает короткое название пользовательского сценария."""
        if intent in {Intent.GREETING, Intent.HELP}:
            return "onboarding"
        if intent == Intent.ORDER_STATUS:
            return "order_status"
        if intent is Intent.HISTORY_QUERY:
            return "history_query"
        if intent is Intent.VENUE_STATUS:
            return "venue_status"
        if intent in {
            Intent.REVIEW_ORDER,
            Intent.REVIEW_REFRESH,
            Intent.REVIEW_SUBMIT,
            Intent.REVIEW_CANCEL,
        }:
            return "order_review"
        if intent in {
            Intent.PRODUCT_ADD,
            Intent.PRODUCT_ADD_RETRY,
            Intent.PRODUCT_ADD_SKIP,
            Intent.PRODUCT_ADD_LIST,
        }:
            return "product_request"
        if intent in {Intent.SUBMIT_REQUEST, Intent.SUBMIT_AS_IS}:
            return "order_submission"
        if intent in {
            Intent.SHOW_CART,
            Intent.SHOW_FINAL_REVIEW,
            Intent.CHECK_MIN_SUM,
            Intent.CHOOSE_SUPPLIER_WARNING,
            Intent.ADD_SUPPLIER_ITEMS,
        }:
            return "order_review"
        if intent in {Intent.THANKS, Intent.SMALL_TALK}:
            return "conversation"
        if intent == Intent.UNKNOWN:
            return "unrecognized_request"
        return "draft_management"

    @staticmethod
    def _effective_analytics_command(
        event: TelegramEvent,
        command: ParsedCommand,
        state: ConversationState,
        previous_issue_item_id: str,
    ) -> ParsedCommand:
        """Восстанавливает контекстное уточнение количества для аналитики.

        Парсер может закономерно вернуть ``unknown`` для короткого ответа
        вроде «5 штук» или «10 килограмм»: смысл такой фразы определяется
        открытой карточкой товара. Движок уже применяет количество к текущей
        позиции, поэтому итоговый лог не должен записывать обработанный ответ
        как нераспознанный запрос.
        """
        if command.intent is not Intent.UNKNOWN or not previous_issue_item_id:
            return command

        quantity, unit = ConversationEngine._spoken_quantity(event.text or command.text)
        if quantity is None:
            return command

        item = next(
            (candidate for candidate in state.cart if candidate.id == previous_issue_item_id),
            None,
        )
        if (
            item is None
            or item.quantity is None
            or item.status
            not in {
                ItemStatus.MATCHED,
                ItemStatus.UNIT_MISMATCH,
            }
        ):
            return command

        return command.model_copy(
            update={
                "intent": Intent.EDIT_QUANTITY,
                "edit_quantity": quantity,
                "edit_unit": unit,
                "target_query": "",
            }
        )

    def _request_analytics(
        self,
        event: TelegramEvent,
        command: ParsedCommand,
        state: ConversationState,
        *,
        previous_stage: str,
        previous_cart_count: int,
        previous_issue_item_id: str,
    ) -> dict[str, Any]:
        """Формирует компактный и обезличенный результат пользовательского запроса."""
        command = self._effective_analytics_command(
            event,
            command,
            state,
            previous_issue_item_id,
        )
        relevant_items: list[CartItem] = []
        if command.intent == Intent.ADD_ITEMS:
            relevant_items = state.cart[previous_cart_count:]
        elif command.intent in {
            Intent.SUBMIT_REQUEST,
            Intent.SUBMIT_AS_IS,
            Intent.SHOW_FINAL_REVIEW,
            Intent.CHECK_MIN_SUM,
        }:
            if current := state.current_item():
                relevant_items = [current]
        elif command.intent in {
            Intent.SELECT_CANDIDATE,
            Intent.MANUAL_CURRENT,
            Intent.SEARCH_ALL_SUPPLIERS,
            Intent.SWITCH_SUPPLIER,
            Intent.EDIT_QUANTITY,
            Intent.ENTER_OTHER_QUANTITY,
            Intent.USE_CATALOG_UNIT,
            Intent.UNIT_EDIT,
            Intent.UNIT_OK,
            Intent.MERGE_DUPLICATE,
        }:
            current = next(
                (item for item in state.cart if item.id == previous_issue_item_id),
                None,
            )
            if current:
                relevant_items = [current]

        issue_statuses = {
            ItemStatus.NOT_FOUND,
            ItemStatus.AMBIGUOUS,
            ItemStatus.MISSING_QTY,
            ItemStatus.UNIT_MISMATCH,
            ItemStatus.DUPLICATE_PENDING,
            ItemStatus.AI_PENDING,
        }
        issue_counts = Counter(
            item.status.value for item in relevant_items if item.status in issue_statuses
        )
        matched_count = sum(item.status == ItemStatus.MATCHED for item in relevant_items)
        outcome = "success"
        failure_reason = ""

        if command.comment_clarification:
            outcome = "needs_clarification"
            failure_reason = "ambiguous_comment_scope"
        elif command.intent == Intent.UNKNOWN:
            outcome = "failed"
            failure_reason = "unrecognized_request"
        elif command.intent == Intent.ADD_ITEMS and not command.items:
            outcome = "failed"
            failure_reason = "no_items_recognized"
        elif state.stage == SessionStage.SUBMISSION_FAILED and command.intent in {
            Intent.SUBMIT_REQUEST,
            Intent.SUBMIT_AS_IS,
        }:
            outcome = "failed"
            failure_reason = "submission_failed"
        elif issue_counts:
            reason_by_status = (
                (ItemStatus.NOT_FOUND, "product_not_found"),
                (ItemStatus.AMBIGUOUS, "ambiguous_product"),
                (ItemStatus.MISSING_QTY, "missing_quantity"),
                (ItemStatus.UNIT_MISMATCH, "unit_mismatch"),
                (ItemStatus.DUPLICATE_PENDING, "duplicate_product"),
                (ItemStatus.AI_PENDING, "ai_match_pending"),
            )
            failure_reason = next(
                reason for status, reason in reason_by_status if issue_counts.get(status.value, 0)
            )
            if matched_count:
                outcome = "partial"
            elif failure_reason == "product_not_found":
                outcome = "failed"
            else:
                outcome = "needs_clarification"

        analytics: dict[str, Any] = {
            "status": "done",
            "scenario": self._scenario_for_intent(command.intent),
            "intent": command.intent.value,
            "outcome": outcome,
            "failure_reason": failure_reason,
            "input_type": event.input_type.value,
            "stage_from": previous_stage,
            "stage_to": state.stage.value,
            "item_count": len(command.items),
            "cart_count": len(state.cart),
            "issue_count": sum(issue_counts.values()),
            "issue_types": sorted(issue_counts),
        }
        if command.confidence is not None:
            analytics["confidence"] = command.confidence
        if outcome != "success":
            request_text = self._analytics_request_text(event, command)
            if request_text:
                request_key = (
                    "user_text" if self.settings.log_user_content else "request_fingerprint"
                )
                analytics[request_key] = sanitize_log_value(
                    request_text,
                    key="user_text",
                    include_user_content=self.settings.log_user_content,
                    max_content_length=self.settings.log_content_max_length,
                )
        return analytics

    @staticmethod
    def _analytics_request_text(event: TelegramEvent, command: ParsedCommand) -> str:
        """Выбирает наиболее полезную часть неуспешного запроса для диагностики."""
        if command.comment_clarification:
            return event.text or command.text or command.comment_clarification
        if command.target_query:
            return command.target_query
        product_queries = [item.product_query for item in command.items if item.product_query]
        if product_queries:
            return " | ".join(product_queries)
        return command.text or event.text

    def _record_request_outcome(
        self,
        trace: Any,
        log: Any,
        analytics: dict[str, Any],
        *,
        chat_id: str = "",
        venue_code: str = "",
    ) -> None:
        """Записывает один итог запроса в обычный лог и текущую трассу Langfuse."""
        metadata = {
            key: analytics[key]
            for key in (
                "scenario",
                "intent",
                "outcome",
                "failure_reason",
                "input_type",
            )
        }
        if chat_id:
            metadata["chat_hash"] = self.tracer.anonymized_chat_id(chat_id)
        if venue_code:
            metadata["venue_hash"] = self.tracer.anonymized_chat_id(venue_code)
        log.info("user_request_outcome", **analytics)
        trace.update(output=analytics, metadata=metadata)

    @staticmethod
    def _command_log(command: ParsedCommand) -> dict[str, Any]:
        """Формирует диагностический снимок разобранной команды."""
        return {
            "intent": command.intent.value,
            "text": command.text,
            "history_question_type": (
                command.history_query.question_type.value if command.history_query else ""
            ),
            "history_product_queries": (
                command.history_query.product_queries if command.history_query else []
            ),
            "target_query": command.target_query,
            "target_queries": command.target_queries,
            "selected_index": command.selected_index,
            "selection_query": command.selection_query,
            "edit_quantity": command.edit_quantity,
            "edit_unit": command.edit_unit,
            "comment_target_query": command.comment_target_query,
            "comment_text": command.comment_text,
            "comment_action": command.comment_action,
            "comment_scope": command.comment_scope,
            "global_comment": command.global_comment,
            "photo_outcome": command.photo_outcome,
            "item_count": len(command.items),
            "items": [
                {
                    "product_query": item.product_query,
                    "quantity": item.quantity,
                    "unit": item.unit,
                    "department": item.department,
                    "comment": item.comment,
                    "quantity_source": item.quantity_source,
                    "printed_reference_text": item.printed_reference_text,
                    "order_entry_text": item.order_entry_text,
                    "order_entry_type": item.order_entry_type,
                }
                for item in command.items
            ],
        }

    @staticmethod
    def _state_log(state: ConversationState) -> dict[str, Any]:
        """Формирует диагностический снимок состояния диалога."""
        return {
            "stage": state.stage.value,
            "ui_revision": state.ui_revision,
            "current_issue_item_id": state.current_issue_item_id,
            "cart_count": len(state.cart),
            "cart": [
                {
                    "item_id": item.id,
                    "source_query": item.source_query,
                    "catalog_name": item.catalog_name,
                    "quantity": item.quantity,
                    "unit": item.unit,
                    "status": item.status.value,
                    "candidate_count": len(item.candidates),
                    "comment": item.comment,
                }
                for item in state.cart
            ],
            "product_add_request_count": len(state.product_add_requests),
            "has_pending_submission": state.pending_submission is not None,
            "review_mode": state.review_mode,
        }

    @staticmethod
    def _reply_log(reply: BotReply) -> dict[str, Any]:
        """Формирует диагностический снимок ответа Telegram."""
        return {
            "reply_text": reply.text,
            "reply_text_length": len(reply.text),
            "button_count": sum(len(row) for row in reply.rows),
            "button_callbacks": [
                button.callback_data for row in reply.rows for button in row if button.callback_data
            ],
            "edit_message_id": reply.edit_message_id,
        }

    def _answer_callback_best_effort(self, callback_query_id: str, log: Any) -> None:
        """Останавливает индикатор кнопки без блокировки действия."""
        if not callback_query_id:
            return
        try:
            self.telegram.answer_callback(callback_query_id)
        except Exception as exc:
            log.warning("telegram_callback_ack_failed", error_type=type(exc).__name__)

    def _send_processing_best_effort(
        self,
        log: Any,
        chat_id: str,
        reply: BotReply,
    ) -> int | None:
        """Отправляет служебную карточку без остановки основной обработки."""
        try:
            return self.telegram.send_reply(chat_id, reply)
        except Exception as exc:
            log.warning(
                "telegram_processing_card_failed",
                error_type=type(exc).__name__,
            )
            return None

    def _disable_keyboard_best_effort(
        self,
        log: Any,
        chat_id: str,
        message_id: int | None,
    ) -> None:
        """Отключает старую клавиатуру без остановки основной обработки."""
        try:
            self.telegram.disable_keyboard(chat_id, message_id)
        except Exception as exc:
            log.warning(
                "telegram_disable_keyboard_failed",
                error_type=type(exc).__name__,
            )

    def _complete_registration(
        self,
        update_id: int,
        event: Any,
        claim: ClaimedUpdate,
        registration: RegistrationResult,
        *,
        processing_message_id: int | None = None,
        lease: ChatLease | None = None,
    ) -> None:
        """Сохраняет результат обработанной регистрации и отвечает."""
        with SessionLocal() as db:
            _, current = SessionRepository(db).get_for_update(event.chat_id)
        state = ConversationState() if registration.reset_session else current
        if registration.context:
            self._apply_venue_context(state, registration.context)
        reply = registration.reply or not_bound_reply()
        if not isinstance(reply, BotReply):
            raise TypeError("Регистрация вернула неподдерживаемый ответ канала")
        if processing_message_id:
            reply.edit_message_id = processing_message_id
        elif event.input_type == InputKind.CALLBACK and event.callback_message_id:
            reply.edit_message_id = event.callback_message_id
        state.ui_revision += 1
        self._attach_ui_revision(reply, state.ui_revision)
        state.ui_message_text = reply.text
        self._store_visible_actions(state, reply)
        result = EngineResult(state=state, reply=reply)
        if not claim.state_applied:
            if lease is not None:
                _ensure_lease(lease)
            self._checkpoint_state(
                update_id,
                event.chat_id,
                result,
                **_lease_kwargs(lease),
            )
        if not claim.reply_sent:
            if lease is not None:
                _ensure_lease(lease)
            message_id = self.telegram.send_reply(event.chat_id, reply)
            if lease is not None:
                _ensure_lease(lease)
            self._checkpoint_reply(
                update_id,
                event.chat_id,
                message_id,
                **_lease_kwargs(lease),
            )
        if not claim.tasks_enqueued:
            if lease is not None:
                _ensure_lease(lease)
            self._checkpoint_tasks(
                update_id,
                **_lease_kwargs(lease),
            )

    def _complete_unauthorized(
        self,
        update_id: int,
        event: Any,
        claim: ClaimedUpdate,
        *,
        processing_message_id: int | None = None,
        lease: ChatLease | None = None,
    ) -> None:
        """Сохраняет отказ в доступе и отвечает пользователю."""
        self._complete_registration(
            update_id,
            event,
            claim,
            RegistrationResult(
                handled=True,
                reply=self.registration.denied_reply(event),
            ),
            processing_message_id=processing_message_id,
            lease=lease,
        )

    @staticmethod
    def _apply_venue_context(state: ConversationState, context: VenueContext) -> None:
        """Применяет контекст активного заведения."""
        state.telegram_user_id = context.telegram_user_id
        state.telegram_chat_id = context.telegram_chat_id
        state.venue_code = context.venue_code
        state.venue_name = context.venue_name
        state.restaurant = context.venue_name
        state.spreadsheet_id = context.spreadsheet_id
        state.spreadsheet_url = context.spreadsheet_url

    def _ensure_venue_session(
        self,
        event: TelegramEvent,
        context: VenueContext,
        *,
        lease: ChatLease | None = None,
    ) -> None:
        """Загружает сессию активного заведения."""
        with SessionLocal.begin() as db:
            sessions = SessionRepository(db)
            row, state = sessions.get_for_update(event.chat_id)
            if state.venue_code and state.venue_code != context.venue_code:
                state = ConversationState()
            self._apply_venue_context(state, context)
            if lease is not None:
                _ensure_lease(lease)
            sessions.save(event.chat_id, state, row)

    def _recognizer(self) -> InputRecognitionService:
        """Возвращает сервис распознавания с сохранением lazy test-контракта."""
        service = getattr(self, "input_recognition", None)
        if service is None:
            service = InputRecognitionService(self.telegram, self.openai)
            self.input_recognition = service
        return service

    def _process_conversation(
        self,
        event: TelegramEvent,
        command: ParsedCommand,
        state: ConversationState,
        catalog: list[CatalogProduct],
    ) -> EngineResult:
        """Передаёт вход Telegram в общий сценарий диалога и формирует ответ."""
        interaction: ConversationInput = to_conversation_input(event)
        application = getattr(self, "conversation_application", None)
        if application is None:
            application = ConversationApplication(
                cast(ConversationProcessor, self.engine),
                action_mapper=telegram_action_mapper,
            )
            self.conversation_application = application
        result = application.process(interaction, command, state, catalog)
        return EngineResult(
            state=result.state,
            reply=render_conversation_view(result.view),
            enqueue_submission=result.effects.enqueue_submission,
            enqueue_order_status=result.effects.enqueue_order_status,
            order_status_page=result.order_status_page,
            order_status_detail_page=result.order_status_detail_page,
            order_status_selected_index=result.order_status_selected_index,
            order_status_order_number=result.order_status_order_number,
            enqueue_product_add=result.effects.enqueue_product_add,
            enqueue_review_submission=result.effects.enqueue_review_submission,
            invalidate_catalog=result.effects.invalidate_catalog,
        )

    def _process_history_query(
        self,
        command: ParsedCommand,
        state: ConversationState,
    ) -> EngineResult:
        """Выполняет read-only history query, сохраняя текущий черновик."""
        if command.history_query is None:
            return EngineResult(
                state=state, reply=history_reply(self._empty_history_answer(command))
            )
        answer = self.history_queries.execute(
            command.history_query,
            spreadsheet_id=state.spreadsheet_id,
            venue_name=state.venue_name,
        )
        return EngineResult(state=state, reply=history_reply(answer))

    def _process_venue_status_query(
        self,
        event: TelegramEvent,
        state: ConversationState,
        venue_context: VenueContext,
    ) -> EngineResult:
        """Возвращает актуальный ответ о заведении без изменения черновика."""
        return EngineResult(
            state=state,
            reply=self.registration.current_venue_reply(event, venue_context),
        )

    @staticmethod
    def _empty_history_answer(command: ParsedCommand) -> HistoryAnswer:
        """Создаёт безопасный пустой ответ для повреждённой команды."""
        query = command.history_query or HistoryQuery(
            product_queries=["товар"],
            question_type=HistoryQuestionType.CURRENT_STATUS,
            original_text=command.text,
        )
        return HistoryAnswer(kind=HistoryAnswerKind.EMPTY, query=query)

    @staticmethod
    def _voice_transcription_prompt(state: ConversationState) -> str:
        """Формирует контекст для распознавания голоса."""
        return InputRecognitionService.voice_transcription_prompt(state)

    @staticmethod
    def _requires_high_accuracy_transcription(transcript: str, state: ConversationState) -> bool:
        """Проверяет необходимость повторного распознавания речи."""
        return InputRecognitionService.requires_high_accuracy_transcription(transcript, state)

    def _has_distinct_transcription_fallback(self) -> bool:
        """Разрешает повтор только при реально отличающейся модели."""
        return has_distinct_models(self.openai)

    @staticmethod
    def _attach_ui_revision(reply: BotReply, revision: int) -> None:
        """Добавляет версию интерфейса к callback кнопок."""
        if revision <= 0:
            return
        suffix = f":r{revision}"
        for row in reply.rows:
            for button in row:
                if button.callback_data and not re.fullmatch(
                    r"r\d+", button.callback_data.rsplit(":", 1)[-1], re.I
                ):
                    button.callback_data += suffix

    @staticmethod
    def _store_visible_actions(state: ConversationState, reply: BotReply) -> None:
        """Сохраняет кнопки текущего экрана для текстового и голосового управления."""
        state.visible_actions = [
            {"label": button.text, "action_id": button.callback_data}
            for row in reply.rows
            for button in row
            if button.text and button.callback_data
        ]

    @staticmethod
    def _voice_processing_reply() -> BotReply:
        """Формирует временную карточку обработки голоса."""
        return BotReply(text="Обрабатываю голосовое сообщение...")

    @staticmethod
    def _photo_received_reply() -> BotReply:
        """Подтверждает получение фотографии до начала распознавания."""
        return BotReply(text="📷 <b>Фото получено</b>\n\nЗагружаю изображение…")

    @staticmethod
    def _all_suppliers_processing_reply() -> BotReply:
        """Сообщает о поиске товара по полному каталогу поставщиков."""
        return BotReply(text="🔎 <b>Ищу товар у всех поставщиков…</b>")

    @staticmethod
    def _generic_processing_reply() -> BotReply:
        """Формирует нейтральный статус обработки сообщения."""
        return BotReply(text="⏳ Обрабатываю сообщение…")

    @classmethod
    def _processing_reply_for_intent(cls, intent: Intent) -> BotReply:
        """Подбирает короткий статус для известного действия пользователя."""
        if intent in {Intent.ADD_ITEMS, Intent.ADD_MORE}:
            return cls._text_processing_reply()
        if intent == Intent.SEARCH_ALL_SUPPLIERS:
            return cls._all_suppliers_processing_reply()
        if intent == Intent.SELECT_CANDIDATE:
            return BotReply(text="⏳ Обрабатываю выбор…")
        if intent == Intent.ORDER_STATUS:
            return BotReply(text="⏳ Обновляю статусы заявок…")
        if intent is Intent.HISTORY_QUERY:
            return BotReply(text="⏳ Проверяю историю заявок…")
        if intent in {
            Intent.SUBMIT_REQUEST,
            Intent.SUBMIT_AS_IS,
            Intent.SHOW_FINAL_REVIEW,
            Intent.REVIEW_ORDER,
            Intent.REVIEW_REFRESH,
            Intent.REVIEW_SUBMIT,
            Intent.REVIEW_CANCEL,
            Intent.CHECK_MIN_SUM,
        }:
            return BotReply(text="⏳ Проверяю заявку…")
        if intent in {
            Intent.SHOW_CART,
            Intent.BACK,
            Intent.CONTINUE_CURRENT,
        }:
            return BotReply(text="⏳ Открываю черновик…")
        if intent in {
            Intent.REMOVE_ITEM,
            Intent.EDIT_QUANTITY,
            Intent.EDIT_COMMENT,
            Intent.CLEAR_CART,
            Intent.START_NEW_ORDER,
            Intent.SKIP_CURRENT,
            Intent.MANUAL_CURRENT,
            Intent.CONFIRM,
            Intent.CANCEL,
            Intent.ADD_SUPPLIER_ITEMS,
            Intent.CHOOSE_SUPPLIER_WARNING,
            Intent.ACCEPT_SUGGESTED_QUANTITY,
            Intent.KEEP_CURRENT_QUANTITY,
            Intent.ENTER_OTHER_QUANTITY,
            Intent.USE_CATALOG_UNIT,
            Intent.KEEP_MULTIPLE,
            Intent.FIX_MULTIPLE,
            Intent.EDIT_MULTIPLE,
            Intent.UNIT_EDIT,
            Intent.UNIT_OK,
            Intent.MERGE_DUPLICATE,
            Intent.PRODUCT_ADD,
            Intent.PRODUCT_ADD_RETRY,
            Intent.PRODUCT_ADD_SKIP,
            Intent.PRODUCT_ADD_LIST,
            Intent.SWITCH_SUPPLIER,
        }:
            return BotReply(text="⏳ Обновляю черновик…")
        return cls._generic_processing_reply()

    @classmethod
    def _callback_processing_reply(cls, event: TelegramEvent) -> BotReply:
        """Формирует статус обработки нажатой Telegram-кнопки."""
        return cls._processing_reply_for_intent(parse_callback(event.callback_data).intent)

    @classmethod
    def _text_processing_reply_for_event(cls, event: TelegramEvent) -> BotReply:
        """Формирует нейтральный статус до полного разбора текста."""
        del event
        return cls._generic_processing_reply()

    def _update_processing(
        self,
        chat_id: str,
        message_id: int | None,
        text: str,
    ) -> None:
        """Обновляет временную карточку этапа без остановки обработки."""
        self._recognizer().update_processing(chat_id, message_id, text)

    @staticmethod
    def _text_processing_reply() -> BotReply:
        """Формирует временную карточку поиска введённых товаров."""
        return BotReply(text="🔎 Ищу товары в каталоге…")

    @staticmethod
    def _should_show_text_processing(event: TelegramEvent, state: ConversationState) -> bool:
        """Проверяет наличие текста, требующего немедленного подтверждения."""
        del state
        return event.input_type == InputKind.TEXT and bool(event.text.strip())

    @staticmethod
    def _needs_catalog(command: ParsedCommand) -> bool:
        """Проверяет необходимость загрузки каталога."""
        return command.intent in {
            Intent.ADD_ITEMS,
            Intent.SELECT_CANDIDATE,
            Intent.SEARCH_ALL_SUPPLIERS,
            Intent.SUBMIT_REQUEST,
            Intent.SHOW_FINAL_REVIEW,
            Intent.CHECK_MIN_SUM,
        }

    @staticmethod
    def _requires_fresh_catalog(command: ParsedCommand) -> bool:
        """Требует актуальные суммы и количества перед финальной проверкой."""
        return command.intent in {
            Intent.SUBMIT_REQUEST,
            Intent.SHOW_FINAL_REVIEW,
            Intent.CHECK_MIN_SUM,
        }

    def _resolve_ai_pending(
        self,
        event: TelegramEvent,
        result: EngineResult,
        catalog: list[CatalogProduct],
    ) -> EngineResult:
        """Разрешает спорные позиции по безопасному списку кандидатов."""
        if result.state.stage in {
            SessionStage.AWAIT_MANUAL_DETAILS,
            SessionStage.AWAIT_PRODUCT_ADD_DETAILS,
        }:
            return result
        safely_resolved = False
        for item in result.state.cart:
            if item.status != ItemStatus.AMBIGUOUS or not item.candidates:
                continue
            if item.packaging_role == "ambiguous" or has_unscoped_product_variant_qualifier(
                item.comment
            ):
                continue
            if (
                item.packaging_role == "catalog_attribute"
                and not has_compatible_numeric_characteristics(
                    item.packaging_text, item.candidates[0].name
                )
            ):
                continue
            equivalent_products = [
                product
                for product in catalog
                if is_safe_catalog_name_equivalent(item.source_query, product.name)
            ]
            if len(equivalent_products) != 1:
                continue
            equivalent_product = equivalent_products[0]
            selected = next(
                (
                    candidate
                    for candidate in item.candidates
                    if candidate.product_id == equivalent_product.product_id
                ),
                None,
            )
            if selected is None:
                continue
            self.engine.catalog_resolution.apply_catalog(item, selected, catalog)
            safely_resolved = True
            logger.info(
                "catalog_candidate_safe_equivalence_selected",
                target_query=item.source_query,
                selected_product_id=selected.product_id,
            )

        pending = [
            item
            for item in result.state.cart
            if item.status == ItemStatus.AMBIGUOUS
            and item.candidates
            and not is_broad_category_query(_catalog_match_evidence(item), item.candidates)
        ]
        if not pending:
            if safely_resolved:
                result.state.current_issue_item_id = ""
                return self._process_conversation(
                    event,
                    ParsedCommand(intent=Intent.CONTINUE_CURRENT),
                    result.state,
                    catalog,
                )
            return result

        for item in pending:
            item.status = ItemStatus.AI_PENDING

        for item in pending:
            decision = self.openai.choose_catalog_candidate(
                item.source_query,
                [candidate.model_dump() for candidate in item.candidates],
                catalog_matching_comment_context(item.comment),
            )
            search_query = remove_phrase_overlap(item.source_query, item.comment)
            logger.info(
                "catalog_matching_shortlist",
                input_type=event.input_type.value,
                ai_query=item.source_query,
                reconciled_query=item.source_query,
                search_query=search_query,
                source_query=item.source_query,
                canonical_search_query=canonical_search_query(search_query),
                quantity=item.quantity,
                unit=item.unit,
                comment_source=item.comment_source.value,
                packaging={
                    "text": item.packaging_text,
                    "role": item.packaging_role,
                    "confidence": item.packaging_confidence,
                },
                candidates=[
                    {
                        "product_id": candidate.product_id,
                        "title": candidate.name,
                        "score": candidate.score,
                        "evidence": sorted(
                            query_evidence_tokens(item.source_query, candidate.name)
                        ),
                    }
                    for candidate in item.candidates[:5]
                ],
            )
            selected = next(
                (
                    candidate
                    for candidate in item.candidates
                    if candidate.product_id == decision.selected_product_id
                ),
                None,
            )
            can_select = (
                decision.action == "select"
                and selected is not None
                and selected.score >= _AI_MATCH_MIN_SCORE
                and decision.confidence >= _AI_MATCH_SELECT_MIN_CONFIDENCE
                and not decision.contradictions
                and has_compatible_numeric_characteristics(item.source_query, selected.name)
                and (
                    item.packaging_role != "catalog_attribute"
                    or has_compatible_numeric_characteristics(item.packaging_text, selected.name)
                )
                and not has_conflicting_catalog_qualifiers(item.source_query, selected.name)
                and not has_unscoped_product_variant_qualifier(item.comment)
                and item.packaging_role != "ambiguous"
                and selected.reason != "similar"
                and not unverified_product_terms(search_query, selected.name)
            )
            winner_score = selected.score if selected is not None else None
            runner_up_score = item.candidates[1].score if len(item.candidates) > 1 else None
            winner_margin = (
                winner_score - runner_up_score
                if winner_score is not None and runner_up_score is not None
                else None
            )
            gate_reasons: list[str] = []
            if decision.action != "select":
                gate_reasons.append("ai_action_not_select")
            if selected is None:
                gate_reasons.append("selected_candidate_missing")
            elif selected.score < _AI_MATCH_MIN_SCORE:
                gate_reasons.append("score_below_minimum")
            if decision.contradictions:
                gate_reasons.append("catalog_contradiction")
            if item.packaging_role == "ambiguous":
                gate_reasons.append("ambiguous_packaging")
            unresolved_terms = (
                unverified_product_terms(search_query, selected.name)
                if selected is not None
                else []
            )
            if unresolved_terms:
                gate_reasons.append("unresolved_product_terms")
            logger.info(
                "catalog_matching_gate",
                source_query=item.source_query,
                selected_product_id=decision.selected_product_id,
                ai_action=decision.action,
                ai_confidence=decision.confidence,
                auto_select_allowed=can_select,
                winner_score=winner_score,
                runner_up_score=runner_up_score,
                winner_margin=winner_margin,
                contradictions=decision.contradictions,
                unresolved_product_terms=unresolved_terms,
                gate_reasons=gate_reasons,
                quantity=item.quantity,
                unit=item.unit,
                comment_source=item.comment_source.value,
                packaging_role=item.packaging_role,
                packaging_text=item.packaging_text,
            )
            if can_select and selected is not None:
                self.engine.catalog_resolution.apply_catalog(item, selected, catalog)
            elif decision.action == "not_found":
                # A zero-confidence ``not_found`` is not evidence for any
                # candidate. Do not turn it into an unrelated suggestions card.
                similar_candidates = [
                    candidate for candidate in item.candidates if candidate.reason == "similar"
                ]
                if similar_candidates:
                    # Похожий вариант уже отобран строгим поиском для ручного уточнения.
                    # Keep this shortlist for user clarification; a typo must not erase it.
                    item.status = ItemStatus.AMBIGUOUS
                    item.candidates = similar_candidates
                    logger.info(
                        "catalog_candidate_similar_preserved",
                        target_query=item.source_query,
                        candidate_product_ids=[
                            candidate.product_id for candidate in similar_candidates
                        ],
                        ai_confidence=decision.confidence,
                    )
                    continue
                if 0 < decision.confidence < _AI_MATCH_NOT_FOUND_MIN_CONFIDENCE:
                    item.status = ItemStatus.AMBIGUOUS
                    logger.info(
                        "catalog_candidate_ai_not_found_uncertain",
                        target_query=item.source_query,
                        confidence=decision.confidence,
                        candidate_count=len(item.candidates),
                    )
                    continue

                # При уверенном отказе сохраняем только кандидатов, в названии
                # которых есть содержательный якорь товара. Совпадения по одному
                # слову «филе»/«товар» не являются доказательством идентичности.
                anchored_candidates = [
                    candidate
                    for candidate in item.candidates
                    if has_strong_catalog_anchor(search_query, candidate.name)
                ]
                reliable_anchored_candidates = [
                    candidate
                    for candidate in anchored_candidates
                    if has_compatible_numeric_characteristics(item.source_query, candidate.name)
                    and not has_conflicting_catalog_qualifiers(item.source_query, candidate.name)
                    and not unverified_product_terms(search_query, candidate.name)
                    and not _contradictions_reference_request(
                        decision.contradictions,
                        item.source_query,
                        item.comment,
                    )
                ]
                numeric_mismatch_candidates = [
                    candidate
                    for candidate in anchored_candidates
                    if not has_compatible_numeric_characteristics(item.source_query, candidate.name)
                    and not has_conflicting_catalog_qualifiers(item.source_query, candidate.name)
                    and not unverified_product_terms(search_query, candidate.name)
                ]
                preserve_anchored_candidates = (
                    decision.confidence >= _AI_MATCH_NOT_FOUND_MIN_CONFIDENCE
                    or (decision.confidence == 0 and not decision.contradictions)
                    or bool(reliable_anchored_candidates)
                )
                if numeric_mismatch_candidates:
                    anchored_candidates = numeric_mismatch_candidates
                    preserve_anchored_candidates = True
                if preserve_anchored_candidates and anchored_candidates:
                    item.status = ItemStatus.AMBIGUOUS
                    item.candidates = anchored_candidates
                    logger.info(
                        "catalog_candidate_ai_not_found_preserved",
                        target_query=item.source_query,
                        candidate_product_ids=[
                            candidate.product_id for candidate in anchored_candidates
                        ],
                        evidence={
                            candidate.product_id: sorted(
                                query_evidence_tokens(search_query, candidate.name)
                            )
                            for candidate in anchored_candidates
                        },
                        contradictions=decision.contradictions,
                    )
                else:
                    item.status = ItemStatus.NOT_FOUND
                    item.candidates = []
            else:
                item.status = ItemStatus.AMBIGUOUS
                if decision.action == "select":
                    logger.info(
                        "catalog_candidate_ai_selection_blocked",
                        target_query=item.source_query,
                        selected_product_id=decision.selected_product_id,
                        candidate_score=selected.score if selected is not None else None,
                        confidence=decision.confidence,
                        contradictions=decision.contradictions,
                    )

        result.state.current_issue_item_id = ""
        return self._process_conversation(
            event,
            ParsedCommand(intent=Intent.CONTINUE_CURRENT),
            result.state,
            catalog,
        )

    def _claim(self, update_id: int, *, mark_processing: bool = True) -> ClaimedUpdate | None:
        """Берёт одно обновление из очереди в обработку."""
        with SessionLocal.begin() as db:
            repository = UpdateRepository(db)
            update = repository.get_for_update(update_id)
            if update is None or update.status in {"done", "ignored"}:
                return None
            if not mark_processing:
                return ClaimedUpdate(
                    payload=dict(update.payload),
                    result=dict(update.result) if update.result else None,
                    state_applied=update.state_applied,
                    reply_sent=update.reply_sent,
                    tasks_enqueued=update.tasks_enqueued,
                    attempt=update.attempts,
                )
            if repository.has_lower_unfinished(update.chat_id, update_id):
                logger.info(
                    "telegram_update_deferred_sequence",
                    update_id=update_id,
                    chat_id=update.chat_id,
                )
                raise UpdateSequenceDeferred(
                    f"Update {update_id} is waiting for an earlier update in chat {update.chat_id}"
                )
            stale_before = datetime.now(UTC) - STALE_PROCESSING_AFTER
            if (
                update.status == "processing"
                and update.updated_at
                and update.updated_at > stale_before
            ):
                return None
            update.status = "processing"
            update.attempts += 1
            update.error = None
            return ClaimedUpdate(
                payload=dict(update.payload),
                result=dict(update.result) if update.result else None,
                state_applied=update.state_applied,
                reply_sent=update.reply_sent,
                tasks_enqueued=update.tasks_enqueued,
                attempt=update.attempts,
            )

    @staticmethod
    def _defer_if_current_attempt(update_id: int, attempt: int) -> None:
        """Возвращает в очередь только принадлежащую обработчику попытку."""
        if not attempt:
            return
        with SessionLocal.begin() as db:
            UpdateRepository(db).defer_if_current_attempt(update_id, attempt)

    def _checkpoint_state(
        self,
        update_id: int,
        chat_id: str,
        result: EngineResult,
        *,
        audit_context: dict[str, Any] | None = None,
        lease: ChatLease | None = None,
    ) -> None:
        """Сохраняет контрольную точку изменения состояния."""
        with SessionLocal.begin() as db:
            sessions = SessionRepository(db)
            session_row, _ = sessions.get_for_update(chat_id)
            if lease is not None:
                _ensure_lease(lease)
            sessions.save(chat_id, result.state, session_row)
            if audit_context:
                self._append_order_transition_events(
                    OrderEventRepository(db),
                    update_id,
                    result,
                    audit_context,
                )
            update = UpdateRepository(db).get_for_update(update_id)
            if update is None:
                raise RuntimeError(f"Update {update_id} disappeared")
            if lease is not None:
                _ensure_lease(lease)
            update.result = result.model_dump(mode="json")
            update.state_applied = True

    @staticmethod
    def _append_order_transition_events(
        events: OrderEventRepository,
        update_id: int,
        result: EngineResult,
        context: dict[str, Any],
    ) -> None:
        """Записывает ключевые события в одной транзакции с состоянием диалога."""
        previous_trace_id = str(context.get("previous_trace_id") or "")
        trace_id = (
            result.state.order_trace_id
            or (result.state.pending_submission.trace_id if result.state.pending_submission else "")
            or previous_trace_id
        )
        if not trace_id:
            return
        identity = {
            "trace_id": trace_id,
            "telegram_user_id": str(context["telegram_user_id"]),
            "telegram_chat_id": str(context["telegram_chat_id"]),
            "venue_code": str(context.get("venue_code") or ""),
        }
        details = {
            "intent": context["intent"],
            "input_type": context["input_type"],
            "previous_stage": context["previous_stage"],
            "stage": result.state.stage.value,
            "previous_cart_count": context["previous_cart_count"],
            "cart_count": len(result.state.cart),
        }
        if context.get("trace_started"):
            events.append_once(
                idempotency_key=f"trace:{trace_id}:started",
                event_type="order_started",
                details={"cart_count": len(result.state.cart)},
                **identity,
            )
        events.append_once(
            idempotency_key=f"update:{update_id}:order-action",
            event_type="user_action",
            details=details,
            **identity,
        )
        new_order_was_started = (
            context["intent"] == Intent.CLEAR_CART.value
            or (
                context["intent"] == Intent.START_NEW_ORDER.value
                and not result.state.pending_new_order_confirmation
            )
            or (
                bool(context.get("previous_new_order_confirmation"))
                and not result.state.pending_new_order_confirmation
                and not result.state.cart
            )
        )
        if new_order_was_started and previous_trace_id:
            events.append_once(
                idempotency_key=f"trace:{previous_trace_id}:cancelled",
                trace_id=previous_trace_id,
                event_type="order_cancelled",
                telegram_user_id=identity["telegram_user_id"],
                telegram_chat_id=identity["telegram_chat_id"],
                venue_code=identity["venue_code"],
                details={"previous_cart_count": context["previous_cart_count"]},
            )
        pending = result.state.pending_submission
        if pending:
            events.append_once(
                idempotency_key=f"order:{pending.order_no}:requested",
                event_type="submission_requested",
                order_no=pending.order_no,
                details={"row_count": len(pending.rows)},
                **identity,
            )

    def _checkpoint_reply(
        self,
        update_id: int,
        chat_id: str,
        message_id: int | None,
        *,
        lease: ChatLease | None = None,
    ) -> None:
        """Сохраняет контрольную точку ответа Telegram."""
        with SessionLocal.begin() as db:
            if lease is not None:
                _ensure_lease(lease)
            update = UpdateRepository(db).get_for_update(update_id)
            if update is None:
                raise RuntimeError(f"Update {update_id} disappeared")
            update.reply_sent = True
            if message_id:
                sessions = SessionRepository(db)
                session_row, state = sessions.get_for_update(chat_id)
                state.ui_message_id = message_id
                sessions.save(chat_id, state, session_row)

    def _checkpoint_tasks(self, update_id: int, *, lease: ChatLease | None = None) -> None:
        """Сохраняет контрольную точку фоновых задач."""
        with SessionLocal.begin() as db:
            if lease is not None:
                _ensure_lease(lease)
            update = UpdateRepository(db).get_for_update(update_id)
            if update:
                update.tasks_enqueued = True

    def _enqueue_side_effects(
        self,
        chat_id: str,
        result: EngineResult,
        *,
        lease: ChatLease | None = None,
    ) -> None:
        """Ставит отложенные действия в очередь."""
        if result.enqueue_submission:
            _ensure_lease(lease)
            self.background_tasks.submit_order(chat_id)
        if result.enqueue_order_status:
            _ensure_lease(lease)
            self.background_tasks.send_order_status(
                chat_id,
                page=result.order_status_page,
                detail_page=result.order_status_detail_page,
                selected_index=result.order_status_selected_index,
                order_number=result.order_status_order_number,
            )
        if result.enqueue_product_add:
            _ensure_lease(lease)
            self.background_tasks.submit_product_add(chat_id)
        if result.enqueue_review_submission:
            _ensure_lease(lease)
            self.background_tasks.submit_review_order(chat_id, result.state.review_token)

    def _finish(
        self,
        update_id: int,
        status: str,
        error: str | None = None,
        *,
        lease: ChatLease | None = None,
    ) -> None:
        """Помечает обновление успешно обработанным."""
        with SessionLocal.begin() as db:
            _ensure_lease(lease)
            update = UpdateRepository(db).get_for_update(update_id)
            if update:
                update.status = status
                update.error = error

    @staticmethod
    def _error_reply(event: TelegramEvent) -> BotReply:
        """Формирует безопасный ответ об ошибке обработки."""
        from restaurant_bot.domain.models import Button

        if event.input_type == InputKind.PHOTO:
            return BotReply(
                text=(
                    f"🔸 {heading('Не удалось распознать фото')}\n\n"
                    "Если список большой, отправьте его двумя или тремя фотографиями покрупнее. "
                    "Уже добавленные товары останутся в черновике."
                ),
                rows=[[Button(text="Черновик", callback_data="v2:cart")]],
            )
        return BotReply(
            text="Не удалось обработать сообщение. Попробуйте ещё раз или откройте черновик.",
            rows=[[Button(text="Черновик", callback_data="v2:cart")]],
        )
