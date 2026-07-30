from __future__ import annotations

import os
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from restaurant_bot.integrations.cache import CatalogCache, chat_lock
from restaurant_bot.integrations.google_sheets import GoogleSheetsGateway
from restaurant_bot.integrations.openai_client import OpenAIService
from restaurant_bot.integrations.telegram import TELEGRAM_TRANSIENT_ERRORS, TelegramClient
from restaurant_bot.repositories.order_events import OrderEventRepository
from restaurant_bot.repositories.sessions import SessionRepository
from restaurant_bot.repositories.updates import UpdateRepository
from restaurant_bot.services.engine import ConversationEngine
from restaurant_bot.services.input_normalizer import normalize_telegram_update
from restaurant_bot.services.matching import is_broad_category_query
from restaurant_bot.services.parser import infer_intent, parse_quantity_unit
from restaurant_bot.services.text import normalize_text, normalize_unit
from restaurant_bot.services.venue_registration import (
    RegistrationResult,
    VenueContext,
    VenueRegistrationService,
)

logger = structlog.get_logger(__name__)
_OPENAI_TRANSIENT_ERRORS = (APIConnectionError, APITimeoutError, RateLimitError)


@dataclass(slots=True)
class ClaimedUpdate:
    """Хранит взятое в обработку обновление Telegram."""

    payload: dict[str, Any]
    result: dict[str, Any] | None
    state_applied: bool
    reply_sent: bool
    tasks_enqueued: bool


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
        self.sheets = sheets
        self.catalog = CatalogCache(settings, redis, sheets)
        self.engine = ConversationEngine(settings)
        self.tracer = openai_service.tracer
        self.registration = VenueRegistrationService(settings, redis, sheets)

    def process(self, update_id: int) -> None:
        """Обрабатывает одно обновление Telegram целиком."""
        started_at = perf_counter()
        timings: dict[str, int] = {}
        claim = self._claim(update_id)
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
            chat_lock(self.redis, event.chat_id),
        ):
            processing_message_id: int | None = None
            try:
                result = (
                    EngineResult.model_validate(claim.result)
                    if claim.state_applied and claim.result
                    else None
                )

                registration = self.registration.handle(event)
                if registration.handled:
                    self._complete_registration(update_id, event, claim, registration)
                    self._finish(update_id, "done")
                    trace.update(output={"status": "done", "route": "registration"})
                    return

                venue_context = self.registration.context_for(event)
                if venue_context is None:
                    logger.info(
                        "unauthorized_business_request",
                        telegram_user_id=event.telegram_user_id,
                        chat_id=event.chat_id,
                        input_type=event.input_type.value,
                    )
                    self._complete_unauthorized(update_id, event, claim)
                    self._finish(update_id, "done")
                    trace.update(output={"status": "done", "route": "unauthorized"})
                    return
                self._ensure_venue_session(event, venue_context)
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
                    catalog_needed = self._needs_catalog(command)
                    fresh_catalog_required = self._requires_fresh_catalog(command)
                    if catalog_needed:
                        catalog = (
                            self.catalog.get(state.spreadsheet_id, force_refresh=True)
                            if fresh_catalog_required
                            else self.catalog.get(state.spreadsheet_id)
                        )
                    else:
                        catalog = []
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
                    previous_trace_id = state.order_trace_id
                    previous_new_order_confirmation = state.pending_new_order_confirmation
                    result = self.engine.handle(event, command, state, catalog)

                    # AI работает за пределами DB-транзакции. Он может выбрать только ID из shortlist.
                    result = self._resolve_ai_pending(event, result, catalog)
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

                    if processing_message_id:
                        result.reply.edit_message_id = processing_message_id
                    elif event.input_type == InputKind.CALLBACK and event.callback_message_id:
                        result.reply.edit_message_id = event.callback_message_id
                    result.state.ui_revision += 1
                    self._attach_ui_revision(result.reply, result.state.ui_revision)
                    result.state.ui_message_text = result.reply.text
                    self._store_visible_actions(result.state, result.reply)
                    stage_started = perf_counter()
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
                    stage_started = perf_counter()
                    message_id = self.telegram.send_reply(event.chat_id, result.reply)
                    self._checkpoint_reply(update_id, event.chat_id, message_id)
                    timings["reply_ms"] = round((perf_counter() - stage_started) * 1000)
                    log.info(
                        "telegram_reply_sent",
                        message_id=message_id,
                        reply_ms=timings["reply_ms"],
                        **self._reply_log(result.reply),
                    )
                    claim.reply_sent = True

                if not claim.tasks_enqueued:
                    self._enqueue_side_effects(event.chat_id, result)
                    self._checkpoint_tasks(update_id)
                    claim.tasks_enqueued = True
                    task_names = [
                        name
                        for enabled, name in (
                            (result.enqueue_submission, "submit_order"),
                            (result.enqueue_order_status, "send_order_status"),
                            (result.enqueue_product_add, "submit_product_add"),
                        )
                        if enabled
                    ]
                    log.info(
                        "background_tasks_checkpointed",
                        task_count=len(task_names),
                        task_names=task_names,
                    )

                if result.invalidate_catalog:
                    self.catalog.invalidate(result.state.spreadsheet_id)
                self._finish(update_id, "done")
                trace.update(output={"status": "done"})
                log.info(
                    "telegram_update_processed",
                    total_ms=round((perf_counter() - started_at) * 1000),
                    **timings,
                )
            except Exception as exc:
                log.exception("telegram_update_failed", error=str(exc))
                trace.update(level="ERROR", status_message=str(exc)[:500])
                self._finish(update_id, "failed", str(exc))
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
                        error_reply = self._error_reply(event)
                        error_reply.edit_message_id = processing_message_id
                        self.telegram.send_reply(event.chat_id, error_reply)
                    except Exception:
                        log.exception("telegram_error_reply_failed")
                raise

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
            self._checkpoint_state(update_id, event.chat_id, result)
        if not claim.reply_sent:
            message_id = self.telegram.send_reply(event.chat_id, reply)
            self._checkpoint_reply(update_id, event.chat_id, message_id)
        if not claim.tasks_enqueued:
            self._checkpoint_tasks(update_id)

    def _complete_unauthorized(self, update_id: int, event: Any, claim: ClaimedUpdate) -> None:
        """Сохраняет отказ в доступе и отвечает пользователю."""
        self._complete_registration(
            update_id,
            event,
            claim,
            RegistrationResult(
                handled=True,
                reply=self.registration.denied_reply(event),
            ),
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

    def _ensure_venue_session(self, event: TelegramEvent, context: VenueContext) -> None:
        """Загружает сессию активного заведения."""
        with SessionLocal.begin() as db:
            sessions = SessionRepository(db)
            row, state = sessions.get_for_update(event.chat_id)
            if state.venue_code and state.venue_code != context.venue_code:
                state = ConversationState()
            self._apply_venue_context(state, context)
            sessions.save(event.chat_id, state, row)

    def _parse(
        self,
        event: TelegramEvent,
        state: ConversationState,
        processing_message_id: int | None = None,
    ) -> ParsedCommand:
        """Разбирает нормализованное событие пользователя."""
        if event.input_type == InputKind.CALLBACK:
            return infer_intent("", event.callback_data)
        if event.input_type == InputKind.TEXT:
            return self._parse_text_in_context(event.text, state)
        if event.input_type in {InputKind.VOICE, InputKind.PHOTO}:
            stage_started = perf_counter()
            downloaded = self.telegram.download_file(event.file_id, event.mime_type)
            logger.info(
                "telegram_media_download_finished",
                input_type=event.input_type.value,
                duration_ms=round((perf_counter() - stage_started) * 1000),
            )
            try:
                if event.input_type == InputKind.VOICE:
                    stage_started = perf_counter()
                    try:
                        transcript = self.openai.transcribe(
                            downloaded.path,
                            prompt=self._voice_transcription_prompt(state),
                        )
                    except _OPENAI_TRANSIENT_ERRORS as error:
                        logger.warning(
                            "voice_transcription_transport_failed",
                            phase="primary",
                            error_type=type(error).__name__,
                        )
                        return ParsedCommand(intent=Intent.UNKNOWN, text="")
                    high_accuracy_retry = False
                    if self._requires_high_accuracy_transcription(transcript, state):
                        high_accuracy_retry = True
                        primary_transcript = transcript
                        try:
                            retry_transcript = self.openai.transcribe(
                                downloaded.path,
                                prompt=self._voice_transcription_prompt(state),
                                high_accuracy=True,
                            )
                            transcript = self._select_transcription_result(
                                primary_transcript,
                                retry_transcript,
                            )
                        except _OPENAI_TRANSIENT_ERRORS as error:
                            logger.warning(
                                "voice_transcription_transport_failed",
                                phase="high_accuracy",
                                error_type=type(error).__name__,
                            )
                            transcript = primary_transcript
                    logger.info(
                        "voice_transcription_finished",
                        duration_ms=round((perf_counter() - stage_started) * 1000),
                        high_accuracy_retry=high_accuracy_retry,
                    )
                    # A Russian voice can occasionally be returned in an
                    # unrelated script by the transcription API.  Never show
                    # that hallucinated text as a product name: it is not a
                    # meaningful draft item and the n8n recovery path is the
                    # useful next step for a cook.
                    if not self._has_supported_voice_letters(transcript):
                        return ParsedCommand(intent=Intent.UNKNOWN, text="")
                    stage_started = perf_counter()
                    parsed = self._parse_text_in_context(transcript, state)
                    logger.info(
                        "voice_text_parse_finished",
                        duration_ms=round((perf_counter() - stage_started) * 1000),
                        intent=parsed.intent.value,
                        item_count=len(parsed.items),
                    )
                    return parsed.model_copy(update={"text": transcript})
                self._update_processing(
                    event.chat_id,
                    processing_message_id,
                    "🔎 <b>Распознаю товары на фото…</b>\n\n"
                    "Для большого списка это может занять до нескольких минут.",
                )
                stage_started = perf_counter()
                parsed = self.openai.parse_photo(downloaded.path, downloaded.mime_type, event.text)
                logger.info(
                    "photo_parse_finished",
                    duration_ms=round((perf_counter() - stage_started) * 1000),
                    item_count=len(parsed.items),
                )
                count = len(parsed.items)
                self._update_processing(
                    event.chat_id,
                    processing_message_id,
                    "📋 <b>Фото распознано</b>\n\n"
                    f"Найдено позиций: {count}. Сверяю товары с каталогом…",
                )
                return parsed
            finally:
                with suppress(OSError):
                    os.unlink(downloaded.path)
        return ParsedCommand(intent=Intent.UNKNOWN, text=event.text)

    def _parse_text_in_context(
        self,
        text: str,
        state: ConversationState,
    ) -> ParsedCommand:
        """Разбирает текст с учётом кнопок, видимых пользователю."""
        callback_data = self._match_visible_action(text, state)
        if callback_data:
            return infer_intent("", callback_data).model_copy(update={"text": text})

        parsed = self.openai.parse_text(text)
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
            return parsed
        return infer_intent("", selected).model_copy(update={"text": text})

    @staticmethod
    def _match_visible_action(text: str, state: ConversationState) -> str:
        """Находит явно названную кнопку текущего экрана."""
        phrase = normalize_text(text)
        if not phrase:
            return ""
        filler_stems = (
            "давай",
            "давайте",
            "пожалуй",
            "можно",
            "хочу",
            "хотел",
            "нужно",
            "надо",
            "пойд",
            "перейд",
        )
        phrase_words = [
            word
            for word in re.findall(r"[a-zа-яё0-9]+", phrase, flags=re.I)
            if word not in {"я", "мы", "мне", "нам", "бы", "сейчас"}
            and not any(word.startswith(stem) for stem in filler_stems)
        ]
        compact_phrase = " ".join(phrase_words)
        phrase_tokens = set(phrase_words)
        best: tuple[float, str] = (0.0, "")
        for action in state.visible_actions:
            label = normalize_text(action.get("label"))
            callback_data = str(action.get("action_id") or "")
            if not label or not callback_data:
                continue
            label_words = re.findall(r"[a-zа-яё0-9]+", label, flags=re.I)
            compact_label = " ".join(label_words)
            if compact_label in (compact_phrase, " ".join(phrase_words)):
                return callback_data
            label_tokens = set(label_words)
            if len(phrase_tokens) >= 2 and phrase_tokens <= label_tokens and label_tokens:
                score = len(phrase_tokens) / len(label_tokens)
                if score > best[0]:
                    best = (score, callback_data)
        return best[1] if best[0] >= 0.72 else ""

    @staticmethod
    def _needs_visible_action_ai(
        text: str,
        parsed: ParsedCommand,
        state: ConversationState,
    ) -> bool:
        """Определяет необходимость смыслового выбора видимой кнопки."""
        if not state.visible_actions or parsed.intent not in {Intent.UNKNOWN, Intent.ADD_ITEMS}:
            return False
        if parsed.intent == Intent.ADD_ITEMS and any(
            item.quantity is not None for item in parsed.items
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
        )
        return state.stage not in {SessionStage.COLLECTING, SessionStage.REVIEW} or any(
            word.startswith(action_stems) for word in words
        )

    @staticmethod
    def _has_supported_voice_letters(transcript: str) -> bool:
        """Проверяет допустимый алфавит распознанной речи."""
        return bool(re.search(r"[A-Za-zА-Яа-яЁё]", transcript))

    @staticmethod
    def _voice_transcription_prompt(state: ConversationState) -> str:
        """Формирует контекст для распознавания голоса."""
        current = state.current_item()
        if current and current.status == ItemStatus.AMBIGUOUS:
            names = "; ".join(candidate.name for candidate in current.candidates[:5])
            return (
                "Русская речь. Пользователь выбирает один из вариантов товара: "
                f"{names}. Он может сказать номер, например «первый» или «вариант два», "
                "либо полное или частичное название. Верни только произнесённый русский текст."
            )
        if current and state.stage == SessionStage.AWAIT_UNIT_QUANTITY and current.catalog_unit:
            expected_unit = normalize_unit(current.catalog_unit)
            product_name = current.catalog_name or current.source_query
            return (
                "Русская речь сотрудника кафе. Пользователь отвечает на просьбу указать "
                f"количество товара «{product_name}» в {expected_unit}. Точно сохрани "
                "произнесённое число и полное название единицы измерения. Особенно не "
                "путай «грамм» и «килограмм». Ничего не заменяй и не придумывай. "
                "Верни только произнесённый русский текст."
            )
        visible_actions = getattr(state, "visible_actions", [])
        visible = "; ".join(
            action.get("label", "") for action in visible_actions[:10] if action.get("label")
        )
        screen_hint = f" На текущем экране есть кнопки: {visible}." if visible else ""
        return (
            "Русская речь сотрудника кафе. Верни только произнесённый русский текст, "
            "ничего не заменяй и не придумывай. Возможные команды: «добавить товары», "
            "«добавь товары», «покажи черновик», «отправить заявку», «очистить черновик», "
            "«не добавлять», «не отправлять», «пропускаем», «первый вариант», "
            "«второй вариант». Сохраняй частицы «не» и «нет» дословно: они меняют "
            "действие на противоположное. Команда не является названием товара."
            f"{screen_hint}"
        )

    @staticmethod
    def _select_transcription_result(primary: str, retry: str) -> str:
        """Не позволяет повторному распознаванию потерять значимую часть речи."""
        primary_normalized = normalize_text(primary)
        retry_normalized = normalize_text(retry)
        if primary_normalized in {"тестовый товар", "тест товар", "test product"}:
            return retry
        if not UpdateOrchestrator._has_supported_voice_letters(retry):
            return primary
        if not UpdateOrchestrator._has_supported_voice_letters(primary):
            return retry

        primary_words = re.findall(r"[a-zа-яё0-9]+", primary_normalized, flags=re.I)
        retry_words = re.findall(r"[a-zа-яё0-9]+", retry_normalized, flags=re.I)
        primary_has_list_structure = bool(
            re.search(r"[,;\n]", primary)
            or len(re.findall(r"\d+(?:[,.]\d+)?", primary_normalized)) >= 2
        )
        if (
            len(primary_words) >= 6
            and len(retry_words) * 2 < len(primary_words)
            and primary_has_list_structure
        ):
            return primary
        return retry

    @staticmethod
    def _requires_high_accuracy_transcription(transcript: str, state: ConversationState) -> bool:
        """Проверяет необходимость повторного распознавания речи."""
        normalized = normalize_text(transcript)
        if normalized in {"тестовый товар", "тест товар", "test product"}:
            return True
        if UpdateOrchestrator._match_visible_action(transcript, state):
            return False
        transcript_words = set(re.findall(r"[a-zа-яё0-9]+", normalized, flags=re.I))
        for action in getattr(state, "visible_actions", []):
            label_words = set(
                re.findall(
                    r"[a-zа-яё0-9]+",
                    normalize_text(action.get("label")),
                    flags=re.I,
                )
            )
            if transcript_words & label_words and transcript_words != label_words:
                return True
        current = state.current_item()
        if current and state.stage == SessionStage.AWAIT_UNIT_QUANTITY and current.catalog_unit:
            quantity, spoken_unit = parse_quantity_unit(re.sub(r"[.!?]+$", "", transcript).strip())
            if (
                quantity is not None
                and spoken_unit
                and normalize_unit(spoken_unit) != normalize_unit(current.catalog_unit)
            ):
                return True
        if current is None or current.status != ItemStatus.AMBIGUOUS:
            return False
        command = infer_intent(transcript)
        if command.intent not in {Intent.UNKNOWN, Intent.ADD_ITEMS}:
            return False
        scores = [
            ConversationEngine._contains_score(normalized, normalize_text(candidate.name))
            for candidate in current.candidates
        ]
        return max(scores, default=0) < 2

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
        if not message_id:
            return
        try:
            self.telegram.send_reply(
                chat_id,
                BotReply(text=text, edit_message_id=message_id),
            )
        except Exception:
            logger.warning("telegram_processing_update_failed", exc_info=True)

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
        pending = [
            item
            for item in result.state.cart
            if item.status == ItemStatus.AMBIGUOUS
            and item.candidates
            and not is_broad_category_query(item.source_query, item.candidates)
        ]
        if not pending:
            return result

        for item in pending:
            item.status = ItemStatus.AI_PENDING

        for item in pending:
            decision = self.openai.choose_catalog_candidate(
                item.source_query,
                [candidate.model_dump() for candidate in item.candidates],
            )
            selected = next(
                (
                    candidate
                    for candidate in item.candidates
                    if candidate.product_id == decision.selected_product_id
                ),
                None,
            )
            # n8n permits an AI reranker to select a shortlisted candidate at
            # high confidence. Requiring literal equality breaks voice
            # corrections such as "Сыропроза" -> "Сироп Роза".
            if decision.action == "select" and selected is not None:
                self.engine._apply_catalog(item, selected, catalog)
            else:
                # `not_found` from AI is never allowed to erase a valid shortlist.
                # The user must still receive the candidate-choice UI.
                item.status = ItemStatus.AMBIGUOUS

        result.state.current_issue_item_id = ""
        return self.engine.handle(
            event,
            ParsedCommand(intent=Intent.CONTINUE_CURRENT),
            result.state,
            catalog,
        )

    def _claim(self, update_id: int) -> ClaimedUpdate | None:
        """Берёт одно обновление из очереди в обработку."""
        stale_before = datetime.now(UTC) - timedelta(minutes=5)
        with SessionLocal.begin() as db:
            update = UpdateRepository(db).get_for_update(update_id)
            if update is None or update.status in {"done", "ignored"}:
                return None
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
            )

    def _checkpoint_state(
        self,
        update_id: int,
        chat_id: str,
        result: EngineResult,
        *,
        audit_context: dict[str, Any] | None = None,
    ) -> None:
        """Сохраняет контрольную точку изменения состояния."""
        with SessionLocal.begin() as db:
            sessions = SessionRepository(db)
            session_row, _ = sessions.get_for_update(chat_id)
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

    def _checkpoint_reply(self, update_id: int, chat_id: str, message_id: int | None) -> None:
        """Сохраняет контрольную точку ответа Telegram."""
        with SessionLocal.begin() as db:
            update = UpdateRepository(db).get_for_update(update_id)
            if update is None:
                raise RuntimeError(f"Update {update_id} disappeared")
            update.reply_sent = True
            if message_id:
                sessions = SessionRepository(db)
                session_row, state = sessions.get_for_update(chat_id)
                state.ui_message_id = message_id
                sessions.save(chat_id, state, session_row)

    def _checkpoint_tasks(self, update_id: int) -> None:
        """Сохраняет контрольную точку фоновых задач."""
        with SessionLocal.begin() as db:
            update = UpdateRepository(db).get_for_update(update_id)
            if update:
                update.tasks_enqueued = True

    @staticmethod
    def _enqueue_side_effects(chat_id: str, result: EngineResult) -> None:
        """Ставит отложенные действия в очередь."""
        if result.enqueue_submission:
            from restaurant_bot.workers.tasks import submit_order

            submit_order.delay(chat_id)
        if result.enqueue_order_status:
            from restaurant_bot.workers.tasks import send_order_status

            send_order_status.delay(
                chat_id,
                page=result.order_status_page,
                selected_index=result.order_status_selected_index,
                order_number=result.order_status_order_number,
            )
        if result.enqueue_product_add:
            from restaurant_bot.workers.tasks import submit_product_add

            submit_product_add.delay(chat_id)

    def _finish(self, update_id: int, status: str, error: str | None = None) -> None:
        """Помечает обновление успешно обработанным."""
        with SessionLocal.begin() as db:
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
