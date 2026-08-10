from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from uuid import uuid4

import structlog
from openai import APIConnectionError, APITimeoutError, RateLimitError
from redis import Redis
from structlog.contextvars import bound_contextvars

from restaurant_bot.config import Settings
from restaurant_bot.db import SessionLocal
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
from restaurant_bot.integrations.cache import (
    CatalogCache,
    ChatLease,
    ChatLeaseLostError,
    chat_lock,
)
from restaurant_bot.integrations.google_sheets import GoogleSheetsGateway
from restaurant_bot.integrations.openai_client import OpenAIService
from restaurant_bot.integrations.telegram import TELEGRAM_TRANSIENT_ERRORS, TelegramClient
from restaurant_bot.logging import sanitize_log_value
from restaurant_bot.repositories.order_events import OrderEventRepository
from restaurant_bot.repositories.sessions import SessionRepository
from restaurant_bot.repositories.updates import (
    STALE_PROCESSING_AFTER,
    UpdateRepository,
    UpdateSequenceDeferred,
)
from restaurant_bot.services.conversation_handlers.comment_scope import comment_scope_items
from restaurant_bot.services.conversation_handlers.state_compatibility import (
    CompatibilityAction,
    CompatibilityContext,
    StateCompatibilityPolicy,
)
from restaurant_bot.services.engine import ConversationEngine
from restaurant_bot.services.input_normalizer import normalize_telegram_update
from restaurant_bot.services.input_recognition import InputRecognitionService
from restaurant_bot.services.matching import (
    has_compatible_numeric_characteristics,
    has_conflicting_catalog_qualifiers,
    has_unscoped_product_variant_qualifier,
    is_broad_category_query,
    is_safe_catalog_name_equivalent,
    unverified_product_terms,
)
from restaurant_bot.services.order_review import OrderReviewService
from restaurant_bot.services.parser import infer_intent
from restaurant_bot.services.text import clean_text, normalize_text
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


_OPENAI_TRANSIENT_ERRORS = (APIConnectionError, APITimeoutError, RateLimitError)
_AI_MATCH_SELECT_MIN_CONFIDENCE = 0.90
_AI_MATCH_NOT_FOUND_MIN_CONFIDENCE = 0.80
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
    """Объединяет название и локальный комментарий для проверки широты запроса."""
    return " ".join(part for part in (item.source_query, item.comment) if part).strip()


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
    ):
        """Инициализирует компонент."""
        self.settings = settings
        self.redis = redis
        self.telegram = telegram
        self.openai = openai_service
        self.input_recognition = InputRecognitionService(telegram, openai_service)
        self.sheets = sheets
        self.catalog = CatalogCache(settings, redis, sheets)
        self.engine = ConversationEngine(settings)
        self.tracer = openai_service.tracer
        self.registration = VenueRegistrationService(settings, redis, sheets)
        self.order_review = OrderReviewService(settings, redis, telegram, sheets)

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

                registration = self.registration.handle(event)
                if registration.handled:
                    self._complete_registration(
                        update_id,
                        event,
                        claim,
                        registration,
                        **_lease_kwargs(lease),
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

                    # n8n immediately acknowledges a new voice update with a
                    # temporary card.  The transcription and structured
                    # parsing can take several seconds; keeping the previous
                    # keyboard active during that time invites stale clicks.
                    # The final reply edits this same card below.
                    if event.input_type == InputKind.VOICE:
                        stage_started = perf_counter()
                        processing_message_id = self._send_processing_best_effort(
                            log,
                            event.chat_id,
                            self._voice_processing_reply(),
                        )
                        # A slow Telegram edit must never postpone the visible
                        # acknowledgement that the voice message was accepted.
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
                            self._text_processing_reply(),
                        )
                        self._disable_keyboard_best_effort(log, event.chat_id, state.ui_message_id)
                        timings["processing_card_ms"] = round(
                            (perf_counter() - stage_started) * 1000
                        )

                    stage_started = perf_counter()
                    command = self._parse(event, state, processing_message_id)
                    timings["parse_ms"] = round((perf_counter() - stage_started) * 1000)
                    log.info("command_parsed", **self._command_log(command))
                    if event.input_type is InputKind.PHOTO and command.intent in _REVIEW_INTENTS:
                        command = ParsedCommand(intent=Intent.UNKNOWN, text=event.text)
                    sheet_review_decision = (
                        self.engine.state_compatibility_policy.evaluate(
                            command,
                            state,
                            CompatibilityContext.SHEET_REVIEW,
                        )
                        if event.input_type is not InputKind.CALLBACK
                        else None
                    )
                    sheet_review_ambiguous = (
                        sheet_review_decision is not None
                        and sheet_review_decision.action is CompatibilityAction.AMBIGUOUS
                    )
                    if command.intent == Intent.SEARCH_ALL_SUPPLIERS:
                        if event.callback_message_id:
                            self.telegram.delete_message(
                                event.chat_id,
                                event.callback_message_id,
                            )
                        processing_message_id = self.telegram.send_reply(
                            event.chat_id,
                            self._all_suppliers_processing_reply(),
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
                        else (
                            self._handle_review_command(event, state, command)
                            if review_command
                            else self.engine.handle(event, command, state, catalog)
                        )
                    )
                    _ensure_lease(lease)

                    # AI работает за пределами DB-транзакции. Он может выбрать только ID из shortlist.
                    if not review_command:
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
                    # State/result are already checkpointed. The Celery retry
                    # will reload them and repeat only Telegram delivery, never
                    # parsing or applying the user's items a second time.
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
                            "⛔ <b>Ссылка относится к другому заведению</b>\n\n"
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
            token = self.order_review.new_token()
            state.review_token = token
            state.review_snapshot_hash = snapshot.fingerprint
            state.review_venue_code = state.venue_code
            state.review_submission_in_progress = False
            state.review_mode = _SHEET_REVIEW_MODE
            state.stage = SessionStage.REVIEW
            state.status = "review"
            return EngineResult(
                state=state,
                reply=self.order_review.preview_reply(
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
                    rows=[[Button(text="📦 Показать черновик", callback_data="v2:back")]],
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
                refreshed_token = self.order_review.new_token()
                state.review_token = refreshed_token
                state.review_snapshot_hash = current.fingerprint
                state.review_submission_in_progress = False
                state.stage = SessionStage.REVIEW
                state.status = "review"
                return EngineResult(
                    state=state,
                    reply=self.order_review.preview_reply(
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
                            "ℹ️ <b>Отправка пока отключена</b>\n\n"
                            "Заявка проверена, но поставщикам ничего не отправлено. "
                            "Данные таблицы не изменены."
                        ),
                        rows=[
                            [
                                Button(
                                    text="🔄 Обновить заявку",
                                    callback_data=f"v2:review:{state.review_venue_code}",
                                )
                            ],
                            [Button(text="↩️ Закрыть", callback_data=f"v2:review_cancel:{token}")],
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
        rows = (
            [[Button(text="🔄 Обновить заявку", callback_data=f"v2:review:{code}")]] if code else []
        )
        return BotReply(
            text="⚠️ Эта карточка заявки устарела. Обновите заявку и проверьте её ещё раз.",
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
        lease: ChatLease | None = None,
    ) -> None:
        """Сохраняет результат обработанной регистрации и отвечает."""
        with SessionLocal() as db:
            _, current = SessionRepository(db).get_for_update(event.chat_id)
        state = ConversationState() if registration.reset_session else current
        if registration.context:
            self._apply_venue_context(state, registration.context)
        reply = registration.reply or VenueRegistrationService.not_bound_reply()
        if event.input_type == InputKind.CALLBACK and event.callback_message_id:
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

    def _parse(
        self,
        event: TelegramEvent,
        state: ConversationState,
        processing_message_id: int | None = None,
    ) -> ParsedCommand:
        """Разбирает нормализованное событие пользователя."""
        if event.input_type == InputKind.CALLBACK and event.callback_data.startswith("v2:review"):
            return infer_intent("", event.callback_data)
        if event.input_type == InputKind.CALLBACK and state.pending_comment_items:
            return self._parse_pending_comment_scope(event.callback_data, state)
        if event.input_type == InputKind.CALLBACK:
            return infer_intent("", event.callback_data)
        if event.input_type == InputKind.TEXT:
            return self._parse_text_in_context(event.text, state)
        if event.input_type in {InputKind.VOICE, InputKind.PHOTO}:
            return self._recognizer().recognize_media(
                event,
                state,
                self._parse_text_in_context,
                processing_message_id,
            )
        return ParsedCommand(intent=Intent.UNKNOWN, text=event.text)

    def _recognizer(self) -> InputRecognitionService:
        """Возвращает сервис распознавания, включая облегчённые тестовые экземпляры."""
        service = getattr(self, "input_recognition", None)
        if service is None:
            service = InputRecognitionService(self.telegram, self.openai)
            self.input_recognition = service
        return service

    def _parse_text_in_context(
        self,
        text: str,
        state: ConversationState,
    ) -> ParsedCommand:
        """Разбирает текст с учётом кнопок, видимых пользователю."""
        review_match = re.fullmatch(
            r"/start(?:@[A-Za-z0-9_]+)?\s+review_([A-Za-zА-ЯЁ0-9]{4,32})",
            clean_text(text),
            flags=re.IGNORECASE,
        )
        if review_match:
            return ParsedCommand(
                intent=Intent.REVIEW_ORDER,
                text=text,
                callback_target=review_match.group(1),
            )
        # A clear reference to a button belongs to the current screen. Keep
        # it out of the generic product parser, except while a comment-scope
        # answer is being resolved by its dedicated contextual flow.
        if not state.pending_comment_items:
            callback_data = self._match_visible_action(text, state)
            if callback_data:
                return infer_intent("", callback_data).model_copy(update={"text": text})
        parsed = self.openai.parse_text(text)
        review_command = self._parse_sheet_review_command(text, parsed, state)
        if review_command is not None:
            return review_command
        if (
            state.pending_comment_items
            and self.engine.state_compatibility_policy.should_try_contextual_fallback(
                state,
                parsed,
                CompatibilityContext.COMMENT_SCOPE,
            )
        ):
            return self._parse_pending_comment_scope(text, state)
        # Visible buttons are a contextual fallback after global parsing. A
        # concrete product command must remain an ADD_ITEMS intent.
        if parsed.intent is Intent.UNKNOWN or (
            parsed.intent is Intent.ADD_ITEMS and not parsed.items
        ):
            callback_data = self._match_visible_action(text, state)
            if callback_data:
                return infer_intent("", callback_data).model_copy(update={"text": text})
        if not self._needs_visible_action_ai(text, parsed, state):
            return parsed
        try:
            selected = self.openai.choose_visible_action(
                text,
                state.ui_message_text,
                state.visible_actions,
            )
        except _OPENAI_TRANSIENT_ERRORS as error:
            logger.warning(
                "visible_action_transport_failed",
                error_type=type(error).__name__,
            )
            return ParsedCommand(intent=Intent.UNKNOWN, text=text)
        if not selected:
            if parsed.intent is Intent.ADD_ITEMS and parsed.items:
                return ParsedCommand(intent=Intent.UNKNOWN, text=text)
            return parsed
        return infer_intent("", selected).model_copy(update={"text": text})

    def _parse_sheet_review_command(
        self,
        text: str,
        parsed: ParsedCommand,
        state: ConversationState,
    ) -> ParsedCommand | None:
        """Нормализует только подтверждённую структурированную команду sheet-review."""
        if state.stage != SessionStage.REVIEW or state.review_mode != _SHEET_REVIEW_MODE:
            return None

        policy = getattr(
            getattr(self, "engine", None),
            "state_compatibility_policy",
            StateCompatibilityPolicy(),
        )
        decision = policy.evaluate(
            parsed,
            state,
            CompatibilityContext.SHEET_REVIEW,
        )
        if decision.action is CompatibilityAction.INTERRUPT:
            return parsed
        if decision.action is CompatibilityAction.AMBIGUOUS:
            return ParsedCommand(intent=Intent.UNKNOWN, text=text)
        if decision.action is not CompatibilityAction.CONTINUE:
            return parsed.model_copy(update={"text": text})

        if parsed.intent in {Intent.REVIEW_SUBMIT, Intent.REVIEW_CANCEL}:
            return parsed.model_copy(update={"callback_target": state.review_token, "text": text})
        if parsed.intent in {Intent.SUBMIT_REQUEST, Intent.SUBMIT_AS_IS, Intent.CONFIRM} or (
            parsed.dialogue_response.value == "affirm"
        ):
            return parsed.model_copy(
                update={
                    "intent": Intent.REVIEW_SUBMIT,
                    "callback_target": state.review_token,
                    "text": text,
                }
            )
        if parsed.intent in {Intent.CANCEL, Intent.BACK} or (
            parsed.dialogue_response.value == "decline"
        ):
            return parsed.model_copy(
                update={
                    "intent": Intent.REVIEW_CANCEL,
                    "callback_target": state.review_token,
                    "text": text,
                }
            )
        return parsed.model_copy(update={"text": text})

    def _parse_pending_comment_scope(
        self,
        text: str,
        state: ConversationState,
    ) -> ParsedCommand:
        """Разбирает ответ на вопрос об области ожидающего комментария."""
        callback = clean_text(text).casefold()
        scope_items = comment_scope_items(state)
        item_count = len(scope_items)
        action = ""
        target_indexes: list[int] = []
        confidence = 0.0

        if callback.startswith("v2:comment:"):
            target = callback.removeprefix("v2:comment:").split(":r", maxsplit=1)[0]
            confidence = 1.0
            if target == "all":
                action = "items"
                target_indexes = list(range(item_count))
            elif target == "last" and item_count:
                action = "items"
                target_indexes = [item_count - 1]
            elif target == "order":
                action = "order"
            elif target == "cancel":
                action = "cancel"
        else:
            try:
                decision = self.openai.resolve_comment_scope(
                    text,
                    [item.product_query for item in scope_items],
                )
            except _OPENAI_TRANSIENT_ERRORS as error:
                logger.warning(
                    "comment_scope_transport_failed",
                    error_type=type(error).__name__,
                )
                action = "ambiguous"
            else:
                action = decision.action
                target_indexes = decision.target_item_indexes
                confidence = decision.confidence

        carries_items = action in {"items", "order"}
        intent = (
            Intent.ADD_ITEMS
            if carries_items
            else Intent.CANCEL
            if action == "cancel"
            else Intent.CLARIFY_CURRENT
        )
        return ParsedCommand(
            intent=intent,
            text=text,
            items=(
                [item.model_copy(deep=True) for item in state.pending_comment_items]
                if carries_items
                else []
            ),
            comment_scope_action=action or "ambiguous",
            comment_target_indexes=target_indexes,
            confidence=confidence,
        )

    @staticmethod
    def _match_visible_action(text: str, state: ConversationState) -> str:
        """Находит явно названную кнопку текущего экрана."""
        return InputRecognitionService.match_visible_action(text, state)

    @staticmethod
    def _needs_visible_action_ai(
        text: str,
        parsed: ParsedCommand,
        state: ConversationState,
    ) -> bool:
        """Определяет необходимость смыслового выбора видимой кнопки."""
        if state.stage == SessionStage.REVIEW and state.review_mode == _SHEET_REVIEW_MODE:
            return False
        if not state.visible_actions or parsed.intent not in {Intent.UNKNOWN, Intent.ADD_ITEMS}:
            return False
        if (
            parsed.intent == Intent.ADD_ITEMS
            and parsed.items
            and (
                parsed.explicit_add_items
                or any(item.quantity is not None or item.unit for item in parsed.items)
            )
        ):
            return False
        words = normalize_text(text).split()
        action_stems = (
            "выбр",
            "остав",
            "введ",
            "укаж",
            "исправ",
            "измен",
            "поменя",
            "покаж",
            "посмотр",
            "откр",
            "перей",
            "верн",
            "отправ",
            "добав",
            "убер",
            "удал",
            "очист",
            "начн",
            "повтор",
            "пропуст",
            "пров",
            "прав",
        )
        return state.stage not in {SessionStage.COLLECTING, SessionStage.REVIEW} or any(
            word.startswith(action_stems) for word in words
        )

    @staticmethod
    def _has_supported_voice_letters(transcript: str) -> bool:
        """Проверяет допустимый алфавит распознанной речи."""
        return InputRecognitionService.has_supported_voice_letters(transcript)

    @staticmethod
    def _voice_transcription_prompt(state: ConversationState) -> str:
        """Формирует контекст для распознавания голоса."""
        return InputRecognitionService.voice_transcription_prompt(state)

    @staticmethod
    def _select_transcription_result(primary: str, retry: str) -> str:
        """Не позволяет повторному распознаванию потерять значимую часть речи."""
        return InputRecognitionService.select_transcription_result(primary, retry)

    @staticmethod
    def _requires_high_accuracy_transcription(transcript: str, state: ConversationState) -> bool:
        """Проверяет необходимость повторного распознавания речи."""
        return InputRecognitionService.requires_high_accuracy_transcription(transcript, state)

    def _has_distinct_transcription_fallback(self) -> bool:
        """Разрешает повтор только при реально отличающейся модели."""
        return InputRecognitionService.has_distinct_models(self.openai)

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
        """Проверяет, является ли текст вводом названий товаров."""
        if event.input_type != InputKind.TEXT or not event.text.strip():
            return False
        if state.stage in {
            SessionStage.AWAIT_UNIT_QUANTITY,
            SessionStage.AWAIT_MULTIPLE_QUANTITY,
            SessionStage.AWAIT_PRODUCT_ADD_DETAILS,
            SessionStage.AWAIT_SUBMIT_CONFIRM,
            SessionStage.SUBMITTING,
        }:
            return False
        return infer_intent(event.text).intent == Intent.ADD_ITEMS

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
            self.engine._apply_catalog(item, selected, catalog)
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
                return self.engine.handle(
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
                item.comment,
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
                and not unverified_product_terms(item.source_query, selected.name)
            )
            if can_select and selected is not None:
                self.engine._apply_catalog(item, selected, catalog)
            elif (
                decision.action == "not_found"
                and decision.confidence >= _AI_MATCH_NOT_FOUND_MIN_CONFIDENCE
            ):
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
        return self.engine.handle(
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

    @staticmethod
    def _enqueue_side_effects(
        chat_id: str,
        result: EngineResult,
        *,
        lease: ChatLease | None = None,
    ) -> None:
        """Ставит отложенные действия в очередь."""
        if result.enqueue_submission:
            if lease is not None:
                _ensure_lease(lease)
            from restaurant_bot.workers.tasks import submit_order

            submit_order.delay(chat_id)
        if result.enqueue_order_status:
            if lease is not None:
                _ensure_lease(lease)
            from restaurant_bot.workers.tasks import send_order_status

            send_order_status.delay(
                chat_id,
                page=result.order_status_page,
                detail_page=result.order_status_detail_page,
                selected_index=result.order_status_selected_index,
                order_number=result.order_status_order_number,
            )
        if result.enqueue_product_add:
            if lease is not None:
                _ensure_lease(lease)
            from restaurant_bot.workers.tasks import submit_product_add

            submit_product_add.delay(chat_id)
        if result.enqueue_review_submission:
            if lease is not None:
                _ensure_lease(lease)
            from restaurant_bot.workers.tasks import submit_review_order

            submit_review_order.delay(chat_id, result.state.review_token)

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
                    "⚠️ <b>Не удалось распознать фото</b>\n\n"
                    "Если список большой, отправьте его двумя или тремя фотографиями покрупнее. "
                    "Уже добавленные товары останутся в черновике."
                ),
                rows=[[Button(text="Черновик", callback_data="v2:cart")]],
            )
        return BotReply(
            text="Не удалось обработать сообщение. Попробуйте ещё раз или откройте черновик.",
            rows=[[Button(text="Черновик", callback_data="v2:cart")]],
        )
