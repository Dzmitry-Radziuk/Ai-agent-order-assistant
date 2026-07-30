from __future__ import annotations

from datetime import UTC, datetime
from time import perf_counter
from typing import Any

import structlog
from redis import Redis

from restaurant_bot.config import Settings
from restaurant_bot.db import SessionLocal
from restaurant_bot.db_models import SubmissionRecord
from restaurant_bot.domain.models import (
    BotReply,
    Button,
    ConversationState,
    PendingSubmission,
    SessionStage,
)
from restaurant_bot.integrations.cache import CatalogCache, chat_lock, google_submission_lock_key
from restaurant_bot.integrations.google_sheets import (
    GoogleSheetsError,
    GoogleSheetsGateway,
    OrderSubmissionResult,
)
from restaurant_bot.integrations.telegram import TelegramClient
from restaurant_bot.repositories.order_events import OrderEventRepository
from restaurant_bot.repositories.sessions import SessionRepository
from restaurant_bot.repositories.submissions import SubmissionRepository
from restaurant_bot.services.replies import cart_reply
from restaurant_bot.services.submission_presenter import (
    _callback_with_revision,
    build_order_status_detail_reply,
    build_order_status_list_reply,
    build_order_status_text,
    group_order_status_rows,
    submission_dispatch_uncertain_reply,
    submission_failure_reply,
    submission_local_saved_reply,
    submission_success_reply,
)
from restaurant_bot.services.text import escape
from restaurant_bot.services.venue_registration import VenueRegistrationService

logger = structlog.get_logger(__name__)


class SubmissionService:
    """Управляет надёжной отправкой заявок."""

    ORDER_STATUS_PAGE_SIZE = 5

    def __init__(
        self,
        settings: Settings,
        redis: Redis[Any],
        telegram: TelegramClient,
        sheets: GoogleSheetsGateway,
    ):
        """Инициализирует компонент."""
        self.settings = settings
        self.redis = redis
        self.telegram = telegram
        self.sheets = sheets
        self.catalog_cache = CatalogCache(settings, redis, sheets)
        self.registration = VenueRegistrationService(settings, redis, sheets)

    def submit(self, chat_id: str, *, report_failure: bool = True) -> None:
        """Выполняет надёжную отправку текущей заявки."""
        started_at = perf_counter()
        logger.info("submission_started", chat_id=chat_id, report_failure=report_failure)
        with chat_lock(self.redis, chat_id, timeout=300):
            pending = self._load_pending(chat_id)
            if pending is None:
                logger.info("submission_without_pending_order", chat_id=chat_id)
                completion = self._load_unnotified_completion(chat_id)
                if completion is not None:
                    checkpoint_order_no, external_order_no, state, dispatched = completion
                    if dispatched:
                        self._send_completion(
                            chat_id,
                            state,
                            external_order_no,
                            checkpoint_order_no,
                        )
                    else:
                        self._send_local_completion(
                            chat_id,
                            state,
                            checkpoint_order_no,
                        )
                return
            if not self._has_current_access(
                chat_id,
                user_id=pending.telegram_user_id or chat_id,
                bound_chat_id=pending.telegram_chat_id or chat_id,
                venue_code=pending.venue_code,
                spreadsheet_id=pending.spreadsheet_id,
            ):
                self._send_access_disabled(chat_id)
                return
            dispatch_enabled = getattr(
                getattr(self, "settings", None),
                "google_order_submission_enabled",
                True,
            )
            finalized = False
            try:
                record = self._record(chat_id, pending)
                spreadsheet_id = self._require_venue_spreadsheet_id(pending.spreadsheet_id)
                dispatch_uncertain = ""
                external_order_no = ""
                with self.redis.lock(
                    google_submission_lock_key(spreadsheet_id),
                    timeout=300,
                    blocking_timeout=300,
                ):
                    record = self._get_record(pending.order_no)
                    if record.dispatch_started and not record.dispatch_completed:
                        dispatch_uncertain = (
                            record.last_error
                            or "Предыдущая попытка отправки не завершила контрольную точку."
                        )
                        self._mark_dispatch_uncertain(
                            chat_id,
                            pending.order_no,
                            dispatch_uncertain,
                        )
                    else:
                        if not record.catalog_updated:
                            stage_started = perf_counter()
                            self.sheets.increment_catalog_quantities(
                                pending.rows,
                                spreadsheet_id,
                            )
                            self._checkpoint(pending.order_no, "catalog_updated")
                            self.catalog_cache.invalidate(spreadsheet_id)
                            logger.info(
                                "submission_catalog_updated",
                                chat_id=chat_id,
                                order_no=pending.order_no,
                                duration_ms=round((perf_counter() - stage_started) * 1000),
                            )
                        record = self._get_record(pending.order_no)
                        if not record.recalc_done:
                            stage_started = perf_counter()
                            self.sheets.trigger_recalculation(
                                pending.order_no,
                                spreadsheet_id,
                            )
                            self._checkpoint(pending.order_no, "recalc_done")
                            logger.info(
                                "submission_recalculation_completed",
                                chat_id=chat_id,
                                order_no=pending.order_no,
                                duration_ms=round((perf_counter() - stage_started) * 1000),
                            )
                        record = self._get_record(pending.order_no)
                        if record.dispatch_completed:
                            external_order_no = record.external_order_no or pending.order_no
                        elif dispatch_enabled:
                            prepared = self.sheets.prepare_order_submission(
                                spreadsheet_id,
                                pending.order_no,
                            )
                            self._mark_dispatch_started(pending.order_no)
                            stage_started = perf_counter()
                            try:
                                result = self.sheets.send_order_submission(prepared)
                            except Exception as exc:
                                dispatch_uncertain = str(exc)
                                self._mark_dispatch_uncertain(
                                    chat_id,
                                    pending.order_no,
                                    dispatch_uncertain,
                                )
                                logger.exception(
                                    "submission_dispatch_uncertain",
                                    chat_id=chat_id,
                                    order_no=pending.order_no,
                                )
                            else:
                                self._mark_dispatch_completed(
                                    pending.order_no,
                                    result,
                                )
                                external_order_no = result.order_number
                                logger.info(
                                    "submission_dispatched",
                                    chat_id=chat_id,
                                    order_no=pending.order_no,
                                    external_order_no=external_order_no,
                                    base_rows=result.base_rows,
                                    request_rows=result.request_rows,
                                    duration_ms=round((perf_counter() - stage_started) * 1000),
                                )
                        else:
                            external_order_no = pending.order_no
                            logger.info(
                                "submission_dispatch_skipped_by_configuration",
                                chat_id=chat_id,
                                order_no=pending.order_no,
                            )
                if dispatch_uncertain:
                    self._send_dispatch_uncertain_once(
                        chat_id,
                        pending.order_no,
                    )
                    return
                was_dispatched = dispatch_enabled or record.dispatch_completed
                if was_dispatched:
                    final_state = self._finalize(
                        chat_id,
                        pending.order_no,
                        external_order_no,
                    )
                else:
                    final_state = self._finalize(
                        chat_id,
                        pending.order_no,
                        external_order_no,
                        dispatched=False,
                    )
                finalized = True
                if was_dispatched:
                    self._send_completion(
                        chat_id,
                        final_state,
                        external_order_no,
                        pending.order_no,
                    )
                else:
                    self._send_local_completion(
                        chat_id,
                        final_state,
                        pending.order_no,
                    )
                logger.info(
                    "submission_completed" if was_dispatched else "submission_saved_locally",
                    chat_id=chat_id,
                    order_no=pending.order_no,
                    external_order_no=external_order_no,
                    total_ms=round((perf_counter() - started_at) * 1000),
                )
            except Exception as exc:
                logger.exception("submission_failed", chat_id=chat_id, order_no=pending.order_no)
                if finalized:
                    self._remember_transient_error(pending.order_no, str(exc))
                elif report_failure:
                    failed_state = self._fail(chat_id, pending.order_no, str(exc))
                    self.telegram.send_reply(
                        chat_id,
                        submission_failure_reply(failed_state, pending.order_no),
                    )
                else:
                    self._remember_transient_error(pending.order_no, str(exc))
                raise

    def _send_completion(
        self,
        chat_id: str,
        state: ConversationState,
        external_order_no: str,
        checkpoint_order_no: str,
    ) -> None:
        """Отправляет подтверждение завершённой заявки."""
        self.telegram.send_reply(
            chat_id,
            submission_success_reply(state, external_order_no),
        )
        self._checkpoint(checkpoint_order_no, "completion_notified")

    def _send_local_completion(
        self,
        chat_id: str,
        state: ConversationState,
        order_no: str,
    ) -> None:
        """Подтверждает локальную запись без утверждения об отправке."""
        self.telegram.send_reply(
            chat_id,
            submission_local_saved_reply(state, order_no),
        )
        self._checkpoint(order_no, "completion_notified")

    @staticmethod
    def _load_unnotified_completion(
        chat_id: str,
    ) -> tuple[str, str, ConversationState, bool] | None:
        """Загружает завершённую заявку без уведомления."""
        from sqlalchemy import select

        from restaurant_bot.db_models import SubmissionRecord

        with SessionLocal() as db:
            record = db.scalar(
                select(SubmissionRecord)
                .where(
                    SubmissionRecord.telegram_id == chat_id,
                    SubmissionRecord.finalized.is_(True),
                    SubmissionRecord.completion_notified.is_(False),
                )
                .order_by(SubmissionRecord.created_at.desc())
                .limit(1)
            )
            if record is None:
                return None
            _, state = SessionRepository(db).get_for_update(chat_id)
            return (
                record.order_no,
                record.external_order_no or record.order_no,
                state,
                record.dispatch_completed,
            )

    def send_status(
        self,
        chat_id: str,
        *,
        page: int = 0,
        selected_index: int | None = None,
        order_number: str = "",
    ) -> None:
        """Показывает список заявок или подробности выбранной заявки."""
        with chat_lock(self.redis, chat_id):
            with SessionLocal.begin() as db:
                _, state = SessionRepository(db).get_for_update(chat_id)
            if not self._has_current_access(
                chat_id,
                user_id=state.telegram_user_id or chat_id,
                bound_chat_id=state.telegram_chat_id or chat_id,
                venue_code=state.venue_code,
                spreadsheet_id=state.spreadsheet_id,
            ):
                self._send_access_disabled(chat_id)
                return
            if selected_index is not None or order_number:
                self._send_order_status_detail(
                    chat_id,
                    state,
                    selected_index=selected_index,
                    order_number=order_number,
                )
                return
            self._send_order_status_page(chat_id, state, page=max(0, page))

    def _send_order_status_page(
        self,
        chat_id: str,
        state: ConversationState,
        *,
        page: int,
    ) -> None:
        """Показывает одну страницу реальных заявок текущего заведения."""
        spreadsheet_id = self._require_venue_spreadsheet_id(state.spreadsheet_id)
        venue_name = state.venue_name or state.restaurant
        rows = self.sheets.read_recent_order_statuses(
            spreadsheet_id,
            venue_name,
            offset=page * self.ORDER_STATUS_PAGE_SIZE,
            limit=self.ORDER_STATUS_PAGE_SIZE + 1,
        )
        groups = group_order_status_rows(rows)
        if page > 0 and not groups:
            self._send_order_status_page(chat_id, state, page=0)
            return
        shown_groups = groups[: self.ORDER_STATUS_PAGE_SIZE]
        shown_rows = [row for _, order_rows in shown_groups for row in order_rows]
        order_numbers = [number for number, _ in shown_groups]
        self._persist_order_status_view(
            chat_id,
            page=page,
            order_numbers=order_numbers,
        )
        self.telegram.send_reply(
            chat_id,
            build_order_status_list_reply(
                shown_rows,
                page=page,
                has_more=len(groups) > self.ORDER_STATUS_PAGE_SIZE,
            ),
        )

    def _send_order_status_detail(
        self,
        chat_id: str,
        state: ConversationState,
        *,
        selected_index: int | None,
        order_number: str,
    ) -> None:
        """Показывает поставщиков и товары выбранной реальной заявки."""
        stored_numbers = state.order_status_order_numbers
        selected = selected_index or 1
        target = order_number.strip()
        if not target and 1 <= selected <= len(stored_numbers):
            target = stored_numbers[selected - 1]
        if not target:
            self._send_order_status_page(
                chat_id,
                state,
                page=max(0, state.order_status_page),
            )
            return

        rows = self.sheets.read_order_statuses(
            [target],
            self._require_venue_spreadsheet_id(state.spreadsheet_id),
            state.venue_name or state.restaurant,
        )
        if not rows:
            self.telegram.send_reply(
                chat_id,
                BotReply(
                    text=(
                        "⚠️ <b>Заявка не найдена</b>\n\n"
                        f"Заявки {escape(target)} нет в «Истории» этого заведения."
                    ),
                    rows=[
                        [
                            Button(
                                text="← К списку заявок",
                                callback_data=f"v2:orderspage:{state.order_status_page}",
                            )
                        ]
                    ],
                ),
            )
            return

        preview_state = state.model_copy(deep=True)
        preview_state.last_order_no = target
        preview_state.submitted_order_numbers = []
        status_text = build_order_status_text(rows, preview_state)
        self.telegram.send_reply(
            chat_id,
            build_order_status_detail_reply(
                status_text,
                page=max(0, state.order_status_page),
                selected_index=selected,
            ),
        )

    @staticmethod
    def _persist_order_status_view(
        chat_id: str,
        *,
        page: int,
        order_numbers: list[str],
    ) -> None:
        """Запоминает показанный список для голосового и кнопочного выбора."""
        with SessionLocal.begin() as db:
            sessions = SessionRepository(db)
            row, state = sessions.get_for_update(chat_id)
            state.order_status_view_active = True
            state.order_status_page = page
            state.order_status_order_numbers = order_numbers
            sessions.save(chat_id, state, row)

    def submit_product_add(self, chat_id: str) -> None:
        """Отправляет запрос на добавление нового товара."""
        with chat_lock(self.redis, chat_id, timeout=300):
            with SessionLocal.begin() as db:
                _, access_state = SessionRepository(db).get_for_update(chat_id)
            if not self._has_current_access(
                chat_id,
                user_id=access_state.telegram_user_id or chat_id,
                bound_chat_id=access_state.telegram_chat_id or chat_id,
                venue_code=access_state.venue_code,
                spreadsheet_id=access_state.spreadsheet_id,
            ):
                self._send_access_disabled(chat_id)
                return
            with SessionLocal.begin() as db:
                sessions = SessionRepository(db)
                row, state = sessions.get_for_update(chat_id)
                request_id = state.pending_product_add_request_id
                request = next(
                    (
                        item
                        for item in state.product_add_requests
                        if item.get("request_id") == request_id
                    ),
                    None,
                )
                if not request or request.get("status") not in {
                    "pending_write",
                    "retry_pending",
                    "write_failed",
                }:
                    return
                request["status"] = "pending_write"
                request["updated_at"] = datetime.now(UTC).isoformat()
                sessions.save(chat_id, state, row)

            try:
                self.sheets.append_product_request(
                    {"description": request["description"]},
                    self._require_venue_spreadsheet_id(state.spreadsheet_id),
                )
                status = "submitted"
                error = ""
            except Exception as exc:
                error = str(exc)[:1000]
                status = (
                    "write_uncertain" if self._uncertain_product_add_error(exc) else "write_failed"
                )

            with SessionLocal.begin() as db:
                sessions = SessionRepository(db)
                row, state = sessions.get_for_update(chat_id)
                request = self._apply_product_add_write_outcome(state, request_id, status, error)
                if request is None:
                    return
                sessions.save(chat_id, state, row)

            if status == "submitted":
                self._send_product_add_success(chat_id, state, request)
                return
            elif status == "write_failed":
                reply = BotReply(
                    text="<b>Запрос не отправлен</b>\n\nОн сохранён в черновике. Можно безопасно повторить отправку с тем же ID запроса.",
                    rows=[
                        [
                            Button(
                                text="Повторить отправку",
                                callback_data=_callback_with_revision(
                                    state, f"v2:addreqretry:{request_id}"
                                ),
                            )
                        ]
                    ],
                )
            else:
                reply = BotReply(
                    text="<b>Не удалось подтвердить отправку</b>\n\nЗапрос сохранён в черновике, но бот не уверен, что он попал в таблицу. Сообщите менеджеру по снабжению. Повторно отправлять запрос не нужно."
                )
            self.telegram.send_reply(chat_id, reply)

    def _send_product_add_success(
        self, chat_id: str, state: ConversationState, request: dict[str, Any]
    ) -> None:
        """Показывает успешную запись запроса на новый товар."""
        confirmation = BotReply(
            text=f"<b>Запрос менеджеру отправлен</b>\n\n{escape(request['description'])}",
            edit_message_id=state.ui_message_id or None,
        )
        self.telegram.send_reply(chat_id, confirmation)

        state.ui_revision += 1
        draft = cart_reply(state)
        suffix = f":r{state.ui_revision}"
        for row in draft.rows:
            for button in row:
                if button.callback_data:
                    button.callback_data += suffix
        draft_message_id = self.telegram.send_reply(chat_id, draft)
        self._persist_worker_ui(chat_id, state.ui_revision, draft_message_id, draft.text)

    @staticmethod
    def _persist_worker_ui(
        chat_id: str, revision: int, message_id: int | None, message_text: str
    ) -> None:
        """Сохраняет состояние интерфейса фоновой задачи."""
        with SessionLocal.begin() as db:
            sessions = SessionRepository(db)
            row, state = sessions.get_for_update(chat_id)
            state.ui_revision = revision
            state.ui_message_id = message_id
            state.ui_message_text = message_text
            sessions.save(chat_id, state, row)

    @staticmethod
    def _uncertain_product_add_error(exc: Exception) -> bool:
        """Формирует ошибку неопределённой записи товара."""
        text = str(exc).lower()
        return not text or any(
            token in text
            for token in (
                "timeout",
                "timed out",
                "etimedout",
                "network",
                "connection",
                "socket",
                "502",
                "503",
                "504",
                "500",
            )
        )

    @staticmethod
    def _apply_product_add_write_outcome(
        state: ConversationState,
        request_id: str,
        status: str,
        error: str = "",
    ) -> dict[str, Any] | None:
        """Сохраняет результат записи запроса на новый товар."""
        request = next(
            (item for item in state.product_add_requests if item.get("request_id") == request_id),
            None,
        )
        if request is None:
            return None
        now = datetime.now(UTC).isoformat()
        request["status"] = status
        request["updated_at"] = now
        request["sheets_error"] = error
        if status == "submitted":
            request["submitted_at"] = now
        state.pending_product_add_request_id = ""
        state.product_add_write_in_progress = False
        state.stage = SessionStage.REVIEW
        state.status = "review"
        return request

    def _has_current_access(
        self,
        chat_id: str,
        *,
        user_id: str,
        bound_chat_id: str,
        venue_code: str,
        spreadsheet_id: str,
    ) -> bool:
        """Повторно проверяет доступ перед чтением или записью Google Sheets."""
        registration = getattr(self, "registration", None)
        if registration is None:
            return True
        context = registration.context_for_identity(
            user_id,
            bound_chat_id,
            force_refresh=True,
        )
        allowed = bool(
            context
            and (not venue_code or context.venue_code == venue_code)
            and (not spreadsheet_id or context.spreadsheet_id == spreadsheet_id)
        )
        if not allowed:
            logger.info(
                "venue_access_blocked_before_sheet_operation",
                chat_id=chat_id,
                telegram_user_id=user_id,
                venue_code=venue_code,
            )
        return allowed

    def _send_access_disabled(self, chat_id: str) -> None:
        """Отправляет единый ответ об отзыве доступа."""
        self.telegram.send_reply(
            chat_id,
            VenueRegistrationService.access_disabled_reply(),
        )

    def _load_pending(self, chat_id: str) -> PendingSubmission | None:
        """Загружает снимок ожидающей отправки заявки."""
        with SessionLocal() as db:
            _, state = SessionRepository(db).get_for_update(chat_id)
            return state.pending_submission

    @staticmethod
    def _require_venue_spreadsheet_id(spreadsheet_id: str) -> str:
        """Останавливает запись без таблицы активного заведения."""
        target_id = spreadsheet_id.strip()
        if not target_id:
            logger.error("venue_spreadsheet_id_missing")
            raise GoogleSheetsError("Venue spreadsheet ID is required")
        return target_id

    def _record(self, chat_id: str, pending: PendingSubmission) -> SubmissionRecord:
        """Создаёт или загружает запись отправки."""
        with SessionLocal.begin() as db:
            record = SubmissionRepository(db).get_or_create(chat_id, pending)
            self._append_submission_event(
                OrderEventRepository(db),
                pending,
                event_type="submission_processing_started",
                idempotency_key=f"order:{pending.order_no}:processing-started",
            )
            return record

    def _get_record(self, order_no: str) -> SubmissionRecord:
        """Возвращает запись отправки по номеру заявки."""
        from sqlalchemy import select

        with SessionLocal() as db:
            record = db.scalar(
                select(SubmissionRecord).where(SubmissionRecord.order_no == order_no)
            )
            if record is None:
                raise RuntimeError(f"Submission record {order_no} not found")
            db.expunge(record)
            return record

    def _checkpoint(self, order_no: str, field: str) -> None:
        """Отмечает завершение этапа отправки заявки."""
        from sqlalchemy import select

        with SessionLocal.begin() as db:
            record = db.scalar(
                select(SubmissionRecord)
                .where(SubmissionRecord.order_no == order_no)
                .with_for_update()
            )
            if record is None:
                raise RuntimeError(f"Submission record {order_no} not found")
            setattr(record, field, True)
            record.last_error = None
            pending = PendingSubmission.model_validate(record.payload)
            event_types = {
                "catalog_updated": "submission_catalog_updated",
                "recalc_done": "submission_recalculation_completed",
                "dispatch_uncertain_notified": "submission_dispatch_uncertain_notified",
                "completion_notified": "submission_notification_sent",
            }
            event_type = event_types.get(field)
            if event_type:
                self._append_submission_event(
                    OrderEventRepository(db),
                    pending,
                    event_type=event_type,
                    idempotency_key=f"order:{order_no}:{field}",
                )

    def _mark_dispatch_started(self, order_no: str) -> None:
        """Фиксирует начало необратимого вызова до выполнения HTTP POST."""
        from sqlalchemy import select

        with SessionLocal.begin() as db:
            record = db.scalar(
                select(SubmissionRecord)
                .where(SubmissionRecord.order_no == order_no)
                .with_for_update()
            )
            if record is None:
                raise RuntimeError(f"Submission record {order_no} not found")
            if record.dispatch_started:
                return
            record.dispatch_started = True
            record.dispatch_started_at = datetime.now(UTC)
            record.last_error = None
            pending = PendingSubmission.model_validate(record.payload)
            self._append_submission_event(
                OrderEventRepository(db),
                pending,
                event_type="submission_dispatch_started",
                idempotency_key=f"order:{order_no}:dispatch-started",
            )

    def _mark_dispatch_completed(
        self,
        order_no: str,
        result: OrderSubmissionResult,
    ) -> None:
        """Сохраняет внешний номер успешно созданной заявки."""
        from sqlalchemy import select

        with SessionLocal.begin() as db:
            record = db.scalar(
                select(SubmissionRecord)
                .where(SubmissionRecord.order_no == order_no)
                .with_for_update()
            )
            if record is None:
                raise RuntimeError(f"Submission record {order_no} not found")
            record.dispatch_completed = True
            record.external_order_no = result.order_number
            record.dispatch_completed_at = datetime.now(UTC)
            record.last_error = None
            pending = PendingSubmission.model_validate(record.payload)
            self._append_submission_event(
                OrderEventRepository(db),
                pending,
                event_type="submission_dispatched",
                idempotency_key=f"order:{order_no}:dispatched",
                details={
                    "external_order_no": result.order_number,
                    "base_rows": result.base_rows,
                    "request_rows": result.request_rows,
                },
            )

    def _mark_dispatch_uncertain(
        self,
        chat_id: str,
        order_no: str,
        error: str,
    ) -> None:
        """Запрещает повторный POST после неопределённого сетевого результата."""
        from sqlalchemy import select

        with SessionLocal.begin() as db:
            sessions = SessionRepository(db)
            row, state = sessions.get_for_update(chat_id)
            state.stage = SessionStage.SUBMISSION_FAILED
            state.status = "dispatch_uncertain"
            if state.pending_submission:
                state.pending_submission.failed_stage = "dispatch_uncertain"
                state.pending_submission.last_error = error[:1000]
            sessions.save(chat_id, state, row)
            record = db.scalar(
                select(SubmissionRecord)
                .where(SubmissionRecord.order_no == order_no)
                .with_for_update()
            )
            if record is None:
                raise RuntimeError(f"Submission record {order_no} not found")
            record.last_error = error[:4000]
            pending = PendingSubmission.model_validate(record.payload)
            self._append_submission_event(
                OrderEventRepository(db),
                pending,
                event_type="submission_dispatch_uncertain",
                idempotency_key=f"order:{order_no}:dispatch-uncertain",
                status="uncertain",
                details={"error": error},
            )

    def _send_dispatch_uncertain_once(self, chat_id: str, order_no: str) -> None:
        """Один раз предупреждает пользователя и не предлагает повторную отправку."""
        record = self._get_record(order_no)
        if record.dispatch_uncertain_notified:
            return
        with SessionLocal() as db:
            _, state = SessionRepository(db).get_for_update(chat_id)
        self.telegram.send_reply(
            chat_id,
            submission_dispatch_uncertain_reply(state, order_no),
        )
        self._checkpoint(order_no, "dispatch_uncertain_notified")

    def _finalize(
        self,
        chat_id: str,
        order_no: str,
        external_order_no: str,
        *,
        dispatched: bool = True,
    ) -> ConversationState:
        """Финализирует записанную заявку с учётом внешней отправки."""
        from sqlalchemy import select

        with SessionLocal.begin() as db:
            sessions = SessionRepository(db)
            row, state = sessions.get_for_update(chat_id)
            self._apply_successful_submission_state(
                state,
                external_order_no,
                status="submitted" if dispatched else "saved_locally",
            )
            sessions.save(chat_id, state, row)
            record = db.scalar(
                select(SubmissionRecord)
                .where(SubmissionRecord.order_no == order_no)
                .with_for_update()
            )
            if record:
                record.finalized = True
                record.last_error = None
                pending = PendingSubmission.model_validate(record.payload)
                self._append_submission_event(
                    OrderEventRepository(db),
                    pending,
                    event_type=(
                        "submission_completed" if dispatched else "submission_saved_locally"
                    ),
                    idempotency_key=f"order:{order_no}:completed",
                    details={
                        "external_order_no": external_order_no if dispatched else "",
                        "dispatched": dispatched,
                    },
                )
            return state

    @staticmethod
    def _apply_successful_submission_state(
        state: ConversationState,
        order_no: str,
        *,
        status: str = "submitted",
    ) -> None:
        """Применяет успешное завершение отправки заявки."""
        if order_no not in state.submitted_order_numbers:
            state.submitted_order_numbers.append(order_no)
        state.last_order_no = order_no
        state.pending_submission = None
        state.order_trace_id = ""
        state.cart = []
        state.current_issue_item_id = ""
        state.stage = SessionStage.SUBMITTED
        state.status = status

    def _fail(self, chat_id: str, order_no: str, error: str) -> ConversationState:
        """Сохраняет неуспешную отправку заявки."""
        from sqlalchemy import select

        with SessionLocal.begin() as db:
            sessions = SessionRepository(db)
            row, state = sessions.get_for_update(chat_id)
            state.stage = SessionStage.SUBMISSION_FAILED
            if state.pending_submission:
                state.pending_submission.last_error = error[:1000]
            sessions.save(chat_id, state, row)
            record = db.scalar(
                select(SubmissionRecord)
                .where(SubmissionRecord.order_no == order_no)
                .with_for_update()
            )
            if record:
                record.last_error = error[:4000]
                pending = PendingSubmission.model_validate(record.payload)
                self._append_submission_event(
                    OrderEventRepository(db),
                    pending,
                    event_type="submission_failed",
                    idempotency_key=f"order:{order_no}:failed",
                    status="error",
                    details={"error": error},
                )
            return state

    @staticmethod
    def _append_submission_event(
        events: OrderEventRepository,
        pending: PendingSubmission,
        *,
        event_type: str,
        idempotency_key: str,
        status: str = "ok",
        details: dict[str, Any] | None = None,
    ) -> None:
        """Связывает этап фоновой отправки с исходным сценарием пользователя."""
        events.append_once(
            idempotency_key=idempotency_key,
            trace_id=pending.trace_id,
            order_no=pending.order_no,
            telegram_user_id=pending.telegram_user_id or pending.telegram_chat_id,
            telegram_chat_id=pending.telegram_chat_id or pending.telegram_user_id,
            venue_code=pending.venue_code,
            event_type=event_type,
            status=status,
            details=details,
        )

    def _remember_transient_error(self, order_no: str, error: str) -> None:
        """Сохраняет временную ошибку для повторной попытки."""
        from sqlalchemy import select

        with SessionLocal.begin() as db:
            record = db.scalar(
                select(SubmissionRecord)
                .where(SubmissionRecord.order_no == order_no)
                .with_for_update()
            )
            if record:
                record.last_error = error[:4000]
