"""Координирует сервис «submission»."""

from __future__ import annotations

from datetime import UTC, datetime
from time import perf_counter
from typing import Any

import structlog
from redis import Redis

from restaurant_bot.config import Settings
from restaurant_bot.conversation.state.transitions import normalize_cart_page
from restaurant_bot.domain.models import (
    BotReply,
    Button,
    ConversationState,
    PendingSubmission,
    SessionStage,
)
from restaurant_bot.integrations.cache import (
    CatalogCache,
    ChatLease,
    ChatLeaseLostError,
    chat_lock,
    google_submission_lock_key,
)
from restaurant_bot.integrations.google_sheets import (
    CatalogMutationVerification,
    GoogleSheetsError,
    GoogleSheetsGateway,
    OrderSubmissionResult,
)
from restaurant_bot.integrations.telegram import TelegramAPIError, TelegramClient
from restaurant_bot.persistence.database import SessionLocal
from restaurant_bot.persistence.models import SubmissionRecord
from restaurant_bot.presentation.telegram.formatting import escape, heading
from restaurant_bot.presentation.telegram.pagination import CART_PAGE_SIZE
from restaurant_bot.presentation.telegram.replies import cart_reply
from restaurant_bot.presentation.telegram.submission import (
    _callback_with_revision,
    build_order_status_detail_reply,
    build_order_status_list_reply,
    build_order_status_text,
    group_order_status_rows,
    order_status_detail_page_count,
    submission_catalog_conflict_reply,
    submission_catalog_uncertain_reply,
    submission_dispatch_still_uncertain_reply,
    submission_dispatch_uncertain_reply,
    submission_failure_reply,
    submission_local_saved_reply,
    submission_recalculation_uncertain_reply,
    submission_success_reply,
)
from restaurant_bot.presentation.telegram.venue_registration import access_disabled_reply
from restaurant_bot.repositories.order_events import OrderEventRepository
from restaurant_bot.repositories.sessions import SessionRepository
from restaurant_bot.repositories.submissions import SubmissionRepository
from restaurant_bot.services.venue_registration import VenueRegistrationService

logger = structlog.get_logger(__name__)


def _ensure_lease(lease: ChatLease | None) -> None:
    """Проверяет владение чатом перед авторитетным действием."""
    if lease is not None:
        lease.ensure_owned()


def _lease_kwargs(lease: ChatLease | None) -> dict[str, Any]:
    """Возвращает именованный lease только для реального контекста блокировки."""
    return {"lease": lease} if lease is not None else {}


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
        with chat_lock(self.redis, chat_id, timeout=300) as lease:
            if lease is not None:
                lease.ensure_owned()
            pending = self._load_pending(chat_id)
            if pending is None:
                logger.info("submission_without_pending_order", chat_id=chat_id)
                completion = self._load_unnotified_completion(chat_id)
                if completion is not None:
                    checkpoint_order_no, external_order_no, state, dispatched = completion
                    if dispatched:
                        if lease is not None:
                            lease.ensure_owned()
                        self._send_completion(
                            chat_id,
                            state,
                            external_order_no,
                            checkpoint_order_no,
                            **({"lease": lease} if lease is not None else {}),
                        )
                    else:
                        if lease is not None:
                            lease.ensure_owned()
                        self._send_local_completion(
                            chat_id,
                            state,
                            checkpoint_order_no,
                            **({"lease": lease} if lease is not None else {}),
                        )
                return
            if not self._has_current_access(
                chat_id,
                user_id=pending.telegram_user_id or chat_id,
                bound_chat_id=pending.telegram_chat_id or chat_id,
                venue_code=pending.venue_code,
                spreadsheet_id=pending.spreadsheet_id,
            ):
                if lease is not None:
                    lease.ensure_owned()
                self._send_access_disabled(chat_id)
                return
            dispatch_enabled = getattr(
                getattr(self, "settings", None),
                "google_order_submission_enabled",
                True,
            )
            finalized = False
            recalc_send_started = False
            try:
                if lease is not None:
                    lease.ensure_owned()
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
                        if lease is not None:
                            lease.ensure_owned()
                        stage_started = perf_counter()
                        if not self._run_catalog_update(
                            chat_id,
                            pending,
                            record,
                            **_lease_kwargs(lease),
                        ):
                            return
                        logger.info(
                            "submission_catalog_updated",
                            chat_id=chat_id,
                            order_no=pending.order_no,
                            duration_ms=round((perf_counter() - stage_started) * 1000),
                        )
                        record = self._get_record(pending.order_no)
                        if lease is not None:
                            lease.ensure_owned()
                        recalc_status = self._recalc_status(record)
                        if recalc_status == "pending":
                            if lease is not None:
                                lease.ensure_owned()
                            prepared_recalculation = self.sheets.prepare_recalculation(
                                spreadsheet_id
                            )
                            if lease is not None:
                                lease.ensure_owned()
                            self.catalog_cache.invalidate(spreadsheet_id)
                            if lease is not None:
                                lease.ensure_owned()
                            self._mark_recalc_started(pending.order_no)
                            stage_started = perf_counter()
                            recalc_send_started = True
                            try:
                                if lease is not None:
                                    lease.ensure_owned()
                                self.sheets.send_recalculation(prepared_recalculation)
                            except Exception as exc:
                                if lease is not None:
                                    lease.ensure_owned()
                                self._mark_recalc_uncertain(
                                    chat_id,
                                    pending.order_no,
                                    str(exc) or type(exc).__name__,
                                )
                                if lease is not None:
                                    lease.ensure_owned()
                                self._send_recalc_uncertain_reply(
                                    chat_id,
                                    pending.order_no,
                                )
                                return
                            if lease is not None:
                                lease.ensure_owned()
                            self._mark_recalc_completed(pending.order_no)
                            if lease is not None:
                                lease.ensure_owned()
                            recalc_send_started = False
                            logger.info(
                                "submission_recalculation_completed",
                                chat_id=chat_id,
                                order_no=pending.order_no,
                                duration_ms=round((perf_counter() - stage_started) * 1000),
                            )
                        elif recalc_status == "started":
                            self._mark_recalc_uncertain(
                                chat_id,
                                pending.order_no,
                                "Предыдущая попытка пересчёта не завершила контрольную точку.",
                            )
                            self._send_recalc_uncertain_reply(chat_id, pending.order_no)
                            return
                        elif recalc_status == "uncertain":
                            self._send_recalc_uncertain_reply(chat_id, pending.order_no)
                            return
                        elif recalc_status != "completed":
                            self._mark_recalc_uncertain(
                                chat_id,
                                pending.order_no,
                                "Состояние пересчёта заявки повреждено.",
                            )
                            self._send_recalc_uncertain_reply(chat_id, pending.order_no)
                            return
                        record = self._get_record(pending.order_no)
                        if lease is not None:
                            lease.ensure_owned()
                        if record.dispatch_completed:
                            external_order_no = record.external_order_no or pending.order_no
                        elif dispatch_enabled:
                            if lease is not None:
                                lease.ensure_owned()
                            prepared = self.sheets.prepare_order_submission(
                                spreadsheet_id,
                                pending.order_no,
                            )
                            self._mark_dispatch_started(pending.order_no)
                            stage_started = perf_counter()
                            try:
                                if lease is not None:
                                    lease.ensure_owned()
                                result = self.sheets.send_order_submission(prepared)
                            except Exception as exc:
                                if lease is not None:
                                    lease.ensure_owned()
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
                                if lease is not None:
                                    lease.ensure_owned()
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
                    if lease is not None:
                        lease.ensure_owned()
                    self._send_dispatch_uncertain_once(
                        chat_id,
                        pending.order_no,
                        **_lease_kwargs(lease),
                    )
                    return
                was_dispatched = dispatch_enabled or record.dispatch_completed
                if lease is not None:
                    lease.ensure_owned()
                if was_dispatched:
                    final_state = self._finalize(
                        chat_id,
                        pending.order_no,
                        external_order_no,
                        **_lease_kwargs(lease),
                    )
                else:
                    final_state = self._finalize(
                        chat_id,
                        pending.order_no,
                        external_order_no,
                        dispatched=False,
                        **_lease_kwargs(lease),
                    )
                finalized = True
                if was_dispatched:
                    if lease is not None:
                        lease.ensure_owned()
                    self._send_completion(
                        chat_id,
                        final_state,
                        external_order_no,
                        pending.order_no,
                        **({"lease": lease} if lease is not None else {}),
                    )
                else:
                    if lease is not None:
                        lease.ensure_owned()
                    self._send_local_completion(
                        chat_id,
                        final_state,
                        pending.order_no,
                        **({"lease": lease} if lease is not None else {}),
                    )
                logger.info(
                    "submission_completed" if was_dispatched else "submission_saved_locally",
                    chat_id=chat_id,
                    order_no=pending.order_no,
                    external_order_no=external_order_no,
                    total_ms=round((perf_counter() - started_at) * 1000),
                )
            except ChatLeaseLostError:
                raise
            except Exception as exc:
                logger.exception("submission_failed", chat_id=chat_id, order_no=pending.order_no)
                if recalc_send_started and not finalized:
                    try:
                        self._mark_recalc_uncertain(
                            chat_id,
                            pending.order_no,
                            str(exc) or type(exc).__name__,
                        )
                        self._send_recalc_uncertain_reply(chat_id, pending.order_no)
                    except Exception:
                        logger.exception(
                            "submission_recalculation_uncertain_recovery_failed",
                            chat_id=chat_id,
                            order_no=pending.order_no,
                        )
                    return
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

    @staticmethod
    def _completion_notification_status(record: SubmissionRecord) -> str:
        """Возвращает безопасный статус уведомления о завершении."""
        if bool(getattr(record, "completion_notified", False)):
            return "completed"
        status = getattr(record, "completion_notification_status", "")
        if not status:
            return "pending"
        if status not in {"pending", "started", "uncertain", "completed"}:
            return "uncertain"
        if status == "completed":
            return "uncertain"
        return status

    def _mark_completion_notification_started(self, order_no: str) -> bool:
        """Фиксирует начало уведомления до вызова Telegram."""
        from sqlalchemy import select

        with SessionLocal.begin() as db:
            record = db.scalar(
                select(SubmissionRecord)
                .where(SubmissionRecord.order_no == order_no)
                .with_for_update()
            )
            if record is None:
                raise RuntimeError(f"Submission record {order_no} not found")
            if self._completion_notification_status(record) != "pending":
                return False
            record.completion_notification_status = "started"
            record.completion_notification_started_at = datetime.now(UTC)
            record.completion_notification_completed_at = None
            record.completion_notified = False
            record.last_error = None
            pending = PendingSubmission.model_validate(record.payload)
            self._append_submission_event(
                OrderEventRepository(db),
                pending,
                event_type="submission_notification_started",
                idempotency_key=f"order:{order_no}:notification-started",
            )
            return True

    def _mark_completion_notification_completed(self, order_no: str) -> None:
        """Фиксирует успешную доставку уведомления после Telegram."""
        from sqlalchemy import select

        with SessionLocal.begin() as db:
            record = db.scalar(
                select(SubmissionRecord)
                .where(SubmissionRecord.order_no == order_no)
                .with_for_update()
            )
            if record is None:
                raise RuntimeError(f"Submission record {order_no} not found")
            record.completion_notification_status = "completed"
            record.completion_notification_completed_at = datetime.now(UTC)
            record.completion_notified = True
            record.last_error = None
            pending = PendingSubmission.model_validate(record.payload)
            self._append_submission_event(
                OrderEventRepository(db),
                pending,
                event_type="submission_notification_sent",
                idempotency_key=f"order:{order_no}:notification-sent",
            )

    def _mark_completion_notification_uncertain(self, order_no: str, error: str) -> None:
        """Блокирует повтор уведомления после неизвестного результата Telegram."""
        from sqlalchemy import select

        with SessionLocal.begin() as db:
            record = db.scalar(
                select(SubmissionRecord)
                .where(SubmissionRecord.order_no == order_no)
                .with_for_update()
            )
            if record is None:
                raise RuntimeError(f"Submission record {order_no} not found")
            record.completion_notification_status = "uncertain"
            record.completion_notified = False
            record.last_error = error[:4000]
            pending = PendingSubmission.model_validate(record.payload)
            self._append_submission_event(
                OrderEventRepository(db),
                pending,
                event_type="submission_notification_uncertain",
                idempotency_key=f"order:{order_no}:notification-uncertain",
                status="uncertain",
                details={"error": error},
            )

    def _send_completion(
        self,
        chat_id: str,
        state: ConversationState,
        external_order_no: str,
        checkpoint_order_no: str,
        *,
        lease: ChatLease | None = None,
    ) -> None:
        """Отправляет подтверждение с защитой от автоматического дубля."""
        if lease is not None:
            lease.ensure_owned()
        if not self._mark_completion_notification_started(checkpoint_order_no):
            return
        try:
            if lease is not None:
                lease.ensure_owned()
            self.telegram.send_reply(
                chat_id,
                submission_success_reply(state, external_order_no),
            )
            if lease is not None:
                lease.ensure_owned()
        except ChatLeaseLostError:
            raise
        except Exception as exc:
            try:
                self._mark_completion_notification_uncertain(
                    checkpoint_order_no,
                    str(exc) or type(exc).__name__,
                )
            finally:
                raise
        if lease is not None:
            lease.ensure_owned()
        self._mark_completion_notification_completed(checkpoint_order_no)

    def _send_local_completion(
        self,
        chat_id: str,
        state: ConversationState,
        order_no: str,
        *,
        lease: ChatLease | None = None,
    ) -> None:
        """Подтверждает локальную запись с защитой от автоматического дубля."""
        if lease is not None:
            lease.ensure_owned()
        if not self._mark_completion_notification_started(order_no):
            return
        try:
            if lease is not None:
                lease.ensure_owned()
            self.telegram.send_reply(
                chat_id,
                submission_local_saved_reply(state, order_no),
            )
            if lease is not None:
                lease.ensure_owned()
        except ChatLeaseLostError:
            raise
        except Exception as exc:
            try:
                self._mark_completion_notification_uncertain(
                    order_no,
                    str(exc) or type(exc).__name__,
                )
            finally:
                raise
        if lease is not None:
            lease.ensure_owned()
        self._mark_completion_notification_completed(order_no)

    def _load_unnotified_completion(
        self,
        chat_id: str,
    ) -> tuple[str, str, ConversationState, bool] | None:
        """Загружает только безопасную первую доставку уведомления."""
        from sqlalchemy import select

        from restaurant_bot.persistence.models import SubmissionRecord

        with SessionLocal.begin() as db:
            records = db.scalars(
                select(SubmissionRecord)
                .where(
                    SubmissionRecord.telegram_id == chat_id,
                    SubmissionRecord.finalized.is_(True),
                    SubmissionRecord.completion_notified.is_(False),
                )
                .order_by(SubmissionRecord.created_at.desc())
            ).all()
            _, state = SessionRepository(db).get_for_update(chat_id)
            for record in records:
                status = self._completion_notification_status(record)
                if status == "pending":
                    return (
                        record.order_no,
                        record.external_order_no or record.order_no,
                        state,
                        record.dispatch_completed,
                    )
                if status == "started":
                    record.completion_notification_status = "uncertain"
                    record.last_error = (
                        "Предыдущая попытка уведомления не завершила контрольную точку."
                    )
                    pending = PendingSubmission.model_validate(record.payload)
                    self._append_submission_event(
                        OrderEventRepository(db),
                        pending,
                        event_type="submission_notification_uncertain",
                        idempotency_key=f"order:{record.order_no}:notification-uncertain",
                        status="uncertain",
                        details={"reason": "recovery_after_started"},
                    )
            return None

    def send_status(
        self,
        chat_id: str,
        *,
        page: int = 0,
        selected_index: int | None = None,
        order_number: str = "",
        detail_page: int = 0,
    ) -> None:
        """Показывает список заявок или подробности выбранной заявки."""
        with chat_lock(self.redis, chat_id) as lease:
            if lease is not None:
                lease.ensure_owned()
            with SessionLocal.begin() as db:
                _, state = SessionRepository(db).get_for_update(chat_id)
            if lease is not None:
                lease.ensure_owned()
            if not self._has_current_access(
                chat_id,
                user_id=state.telegram_user_id or chat_id,
                bound_chat_id=state.telegram_chat_id or chat_id,
                venue_code=state.venue_code,
                spreadsheet_id=state.spreadsheet_id,
            ):
                if lease is not None:
                    lease.ensure_owned()
                self._send_access_disabled(chat_id)
                return
            if selected_index is not None or order_number:
                self._send_order_status_detail(
                    chat_id,
                    state,
                    selected_index=selected_index,
                    order_number=order_number,
                    detail_page=detail_page,
                    lease=lease,
                )
                return
            self._send_order_status_page(chat_id, state, page=max(0, page), lease=lease)

    def _send_order_status_page(
        self,
        chat_id: str,
        state: ConversationState,
        *,
        page: int,
        lease: ChatLease | None = None,
    ) -> None:
        """Показывает одну страницу реальных заявок текущего заведения."""
        spreadsheet_id = self._require_venue_spreadsheet_id(state.spreadsheet_id)
        if lease is not None:
            lease.ensure_owned()
        venue_name = state.venue_name or state.restaurant
        rows = self.sheets.read_recent_order_statuses(
            spreadsheet_id,
            venue_name,
            offset=page * self.ORDER_STATUS_PAGE_SIZE,
            limit=self.ORDER_STATUS_PAGE_SIZE + 1,
        )
        groups = group_order_status_rows(rows)
        if page > 0 and not groups:
            self._send_order_status_page(chat_id, state, page=0, lease=lease)
            return
        shown_groups = groups[: self.ORDER_STATUS_PAGE_SIZE]
        shown_rows = [row for _, order_rows in shown_groups for row in order_rows]
        order_numbers = [number for number, _ in shown_groups]
        self._persist_order_status_view(
            chat_id,
            page=page,
            order_numbers=order_numbers,
            lease=lease,
        )
        self._send_status_reply(
            chat_id,
            state,
            build_order_status_list_reply(
                shown_rows,
                page=page,
                has_more=len(groups) > self.ORDER_STATUS_PAGE_SIZE,
            ),
            lease=lease,
        )

    def _send_order_status_detail(
        self,
        chat_id: str,
        state: ConversationState,
        *,
        selected_index: int | None,
        order_number: str,
        detail_page: int = 0,
        lease: ChatLease | None = None,
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
                lease=lease,
            )
            return

        rows = self.sheets.read_order_statuses(
            [target],
            self._require_venue_spreadsheet_id(state.spreadsheet_id),
            state.venue_name or state.restaurant,
        )
        if not rows:
            if (
                state.status == "dispatch_uncertain"
                and state.pending_submission is not None
                and state.pending_submission.order_no == target
            ):
                self._send_status_reply(
                    chat_id,
                    state,
                    submission_dispatch_still_uncertain_reply(state, target),
                    lease=lease,
                )
                return
            self._send_status_reply(
                chat_id,
                state,
                BotReply(
                    text=(
                        f"🔸 {heading('Заявка не найдена')}\n\n"
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
                lease=lease,
            )
            return

        preview_state = state.model_copy(deep=True)
        preview_state.last_order_no = target
        preview_state.submitted_order_numbers = []
        status_text = build_order_status_text(
            rows,
            preview_state,
            detail_page=max(0, detail_page),
            display_index=selected,
        )
        self._send_status_reply(
            chat_id,
            state,
            build_order_status_detail_reply(
                status_text,
                page=max(0, state.order_status_page),
                selected_index=selected,
                detail_page=max(0, detail_page),
                detail_page_count=order_status_detail_page_count(
                    rows,
                    preview_state,
                    display_index=selected,
                ),
            ),
            lease=lease,
        )

    def _send_status_reply(
        self,
        chat_id: str,
        state: ConversationState,
        reply: BotReply,
        *,
        lease: ChatLease | None = None,
    ) -> None:
        """Обновляет одну карточку статусов и не накапливает сообщения."""
        previous_message_id = state.ui_message_id
        if lease is not None:
            lease.ensure_owned()
        reply.edit_message_id = previous_message_id or None
        try:
            message_id = self.telegram.send_reply(chat_id, reply)
        except TelegramAPIError as exc:
            description = str(exc).lower()
            can_fallback = previous_message_id and any(
                marker in description
                for marker in (
                    "message to edit not found",
                    "message can't be edited",
                    "message is too old",
                )
            )
            if not can_fallback:
                raise
            reply.edit_message_id = None
            if lease is not None:
                lease.ensure_owned()
            message_id = self.telegram.send_reply(chat_id, reply)
        if lease is not None:
            lease.ensure_owned()
        if isinstance(message_id, int):
            self._persist_worker_ui(
                chat_id,
                state.ui_revision,
                message_id,
                reply.text,
                lease=lease,
            )

    @staticmethod
    def _persist_order_status_view(
        chat_id: str,
        *,
        page: int,
        order_numbers: list[str],
        lease: ChatLease | None = None,
    ) -> None:
        """Запоминает показанный список для голосового и кнопочного выбора."""
        with SessionLocal.begin() as db:
            sessions = SessionRepository(db)
            row, state = sessions.get_for_update(chat_id)
            state.order_status_view_active = True
            state.order_status_page = page
            state.order_status_detail_page = 0
            state.order_status_detail_active = False
            state.order_status_selected_index = None
            state.order_status_selected_order_number = ""
            state.order_status_order_numbers = order_numbers
            if lease is not None:
                lease.ensure_owned()
            sessions.save(chat_id, state, row)

    def submit_product_add(self, chat_id: str) -> None:
        """Отправляет запрос на добавление нового товара."""
        with chat_lock(self.redis, chat_id, timeout=300) as lease:
            if lease is not None:
                lease.ensure_owned()
            with SessionLocal.begin() as db:
                _, access_state = SessionRepository(db).get_for_update(chat_id)
            if lease is not None:
                lease.ensure_owned()
            if not self._has_current_access(
                chat_id,
                user_id=access_state.telegram_user_id or chat_id,
                bound_chat_id=access_state.telegram_chat_id or chat_id,
                venue_code=access_state.venue_code,
                spreadsheet_id=access_state.spreadsheet_id,
            ):
                if lease is not None:
                    lease.ensure_owned()
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
                request["status"] = "write_uncertain"
                request["updated_at"] = datetime.now(UTC).isoformat()
                if lease is not None:
                    lease.ensure_owned()
                sessions.save(chat_id, state, row)

            try:
                if lease is not None:
                    lease.ensure_owned()
                self.sheets.append_product_request(
                    {"description": request["description"]},
                    self._require_venue_spreadsheet_id(state.spreadsheet_id),
                )
                if lease is not None:
                    lease.ensure_owned()
                status = "submitted"
                error = ""
            except Exception as exc:
                if lease is not None:
                    lease.ensure_owned()
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
                if lease is not None:
                    lease.ensure_owned()
                sessions.save(chat_id, state, row)

            if status == "submitted":
                if lease is not None:
                    lease.ensure_owned()
                if lease is None:
                    self._send_product_add_success(chat_id, state, request)
                else:
                    self._send_product_add_success(chat_id, state, request, lease=lease)
                return
            elif status == "write_failed":
                reply = BotReply(
                    text=f"{heading('Запрос не отправлен')}\n\nОн сохранён в черновике. Можно безопасно повторить отправку с тем же ID запроса.",
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
                    text=f"{heading('Не удалось подтвердить отправку')}\n\nЗапрос сохранён в черновике, но бот не уверен, что он попал в таблицу. Сообщите менеджеру по снабжению. Повторно отправлять запрос не нужно."
                )
            if lease is not None:
                lease.ensure_owned()
            self.telegram.send_reply(chat_id, reply)

    def _send_product_add_success(
        self,
        chat_id: str,
        state: ConversationState,
        request: dict[str, Any],
        *,
        lease: ChatLease | None = None,
    ) -> None:
        """Показывает успешную запись запроса на новый товар."""
        confirmation = BotReply(
            text=f"<i>Запрос менеджеру отправлен</i>\n\n{escape(request['description'])}",
            edit_message_id=state.ui_message_id or None,
        )
        if lease is not None:
            lease.ensure_owned()
        self.telegram.send_reply(chat_id, confirmation)
        if lease is not None:
            lease.ensure_owned()

        state.ui_revision += 1
        normalize_cart_page(state, page_size=CART_PAGE_SIZE)
        draft = cart_reply(state)
        suffix = f":r{state.ui_revision}"
        for row in draft.rows:
            for button in row:
                if button.callback_data:
                    button.callback_data += suffix
        if lease is not None:
            lease.ensure_owned()
        draft_message_id = self.telegram.send_reply(chat_id, draft)
        if lease is not None:
            lease.ensure_owned()
        if lease is None:
            self._persist_worker_ui(chat_id, state.ui_revision, draft_message_id, draft.text)
        else:
            self._persist_worker_ui(
                chat_id,
                state.ui_revision,
                draft_message_id,
                draft.text,
                lease=lease,
            )

    @staticmethod
    def _persist_worker_ui(
        chat_id: str,
        revision: int,
        message_id: int | None,
        message_text: str,
        *,
        lease: ChatLease | None = None,
    ) -> None:
        """Сохраняет состояние интерфейса фоновой задачи."""
        with SessionLocal.begin() as db:
            sessions = SessionRepository(db)
            row, state = sessions.get_for_update(chat_id)
            state.ui_revision = revision
            state.ui_message_id = message_id
            state.ui_message_text = message_text
            if lease is not None:
                lease.ensure_owned()
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
            access_disabled_reply(),
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

    @staticmethod
    def _catalog_status(record: SubmissionRecord) -> str:
        """Возвращает канонический жизненный цикл изменения каталога."""
        status = getattr(record, "catalog_update_status", "")
        if status in {"pending", "started", "uncertain", "completed", "conflict"}:
            if status == "pending" and getattr(record, "catalog_updated", False):
                return "completed"
            return status
        return "completed" if getattr(record, "catalog_updated", False) else "pending"

    @staticmethod
    def _recalc_status(record: SubmissionRecord) -> str:
        """Возвращает безопасный статус жизненного цикла пересчёта."""
        status = getattr(record, "recalc_status", "")
        recalc_done = bool(getattr(record, "recalc_done", False))
        if not status:
            return "completed" if recalc_done else "pending"
        if status not in {"pending", "started", "uncertain", "completed"}:
            return "uncertain"
        if status == "completed":
            return "completed" if recalc_done else "uncertain"
        if status == "pending" and recalc_done:
            return "completed"
        if status in {"started", "uncertain"} and recalc_done:
            return "uncertain"
        return status

    def _mark_recalc_started(self, order_no: str) -> None:
        """Фиксирует начало пересчёта до первого внешнего вызова."""
        from sqlalchemy import select

        with SessionLocal.begin() as db:
            record = db.scalar(
                select(SubmissionRecord)
                .where(SubmissionRecord.order_no == order_no)
                .with_for_update()
            )
            if record is None:
                raise RuntimeError(f"Submission record {order_no} not found")
            status = self._recalc_status(record)
            if status != "pending":
                if status == "completed":
                    return
                raise RuntimeError("Recalculation is already started or uncertain")
            record.recalc_status = "started"
            record.recalc_operation_id = f"recalc:{order_no}"
            record.recalc_started_at = datetime.now(UTC)
            record.recalc_completed_at = None
            record.recalc_done = False
            record.last_error = None
            pending = PendingSubmission.model_validate(record.payload)
            self._append_submission_event(
                OrderEventRepository(db),
                pending,
                event_type="submission_recalculation_started",
                idempotency_key=f"order:{order_no}:recalc-started",
                details={"operation_id": record.recalc_operation_id},
            )

    def _mark_recalc_completed(self, order_no: str) -> None:
        """Фиксирует подтверждённое завершение пересчёта."""
        from sqlalchemy import select

        with SessionLocal.begin() as db:
            record = db.scalar(
                select(SubmissionRecord)
                .where(SubmissionRecord.order_no == order_no)
                .with_for_update()
            )
            if record is None:
                raise RuntimeError(f"Submission record {order_no} not found")
            record.recalc_status = "completed"
            record.recalc_done = True
            record.recalc_completed_at = datetime.now(UTC)
            record.last_error = None
            pending = PendingSubmission.model_validate(record.payload)
            self._append_submission_event(
                OrderEventRepository(db),
                pending,
                event_type="submission_recalculation_completed",
                idempotency_key=f"order:{order_no}:recalc-completed",
            )

    def _mark_recalc_uncertain(self, chat_id: str, order_no: str, error: str) -> None:
        """Блокирует повтор пересчёта после неизвестного внешнего результата."""
        from sqlalchemy import select

        with SessionLocal.begin() as db:
            sessions = SessionRepository(db)
            row, state = sessions.get_for_update(chat_id)
            state.stage = SessionStage.SUBMISSION_FAILED
            state.status = "recalculation_uncertain"
            if state.pending_submission:
                state.pending_submission.failed_stage = "recalculation_uncertain"
                state.pending_submission.last_error = error[:1000]
            sessions.save(chat_id, state, row)
            record = db.scalar(
                select(SubmissionRecord)
                .where(SubmissionRecord.order_no == order_no)
                .with_for_update()
            )
            if record is None:
                raise RuntimeError(f"Submission record {order_no} not found")
            record.recalc_status = "uncertain"
            record.recalc_done = False
            record.last_error = error[:4000]
            pending = PendingSubmission.model_validate(record.payload)
            self._append_submission_event(
                OrderEventRepository(db),
                pending,
                event_type="submission_recalculation_uncertain",
                idempotency_key=f"order:{order_no}:recalc-uncertain",
                status="uncertain",
                details={"error": error},
            )

    def _send_recalc_uncertain_reply(self, chat_id: str, order_no: str) -> None:
        """Показывает безопасное уведомление без повторной отправки."""
        with SessionLocal() as db:
            _, state = SessionRepository(db).get_for_update(chat_id)
        self.telegram.send_reply(
            chat_id,
            submission_recalculation_uncertain_reply(state, order_no),
        )

    def _run_catalog_update(
        self,
        chat_id: str,
        pending: PendingSubmission,
        record: SubmissionRecord,
        *,
        lease: ChatLease | None = None,
    ) -> bool:
        """Выполняет план каталога с проверкой ячеек перед безопасным восстановлением."""
        _ensure_lease(lease)
        status = self._catalog_status(record)
        if status == "completed":
            return True
        if status == "conflict":
            self._send_catalog_conflict_reply(
                chat_id,
                pending.order_no,
                **_lease_kwargs(lease),
            )
            return False
        if status == "pending":
            operation_id = f"catalog:{pending.order_no}"
            _ensure_lease(lease)
            plan = self.sheets.prepare_catalog_mutation(
                pending.rows,
                pending.spreadsheet_id,
                operation_id=operation_id,
                order_no=pending.order_no,
            )
            _ensure_lease(lease)
            validation_error = self._catalog_plan_validation_error(plan, pending, record)
            if validation_error:
                _ensure_lease(lease)
                self._mark_catalog_conflict(
                    chat_id,
                    pending.order_no,
                    validation_error,
                    **_lease_kwargs(lease),
                )
                self._send_catalog_conflict_reply(
                    chat_id,
                    pending.order_no,
                    **_lease_kwargs(lease),
                )
                return False
            if not plan["mutations"]:
                self._persist_catalog_completed(
                    pending.order_no,
                    plan,
                    **_lease_kwargs(lease),
                )
                return True
            self._persist_catalog_started(
                pending.order_no,
                plan,
                **_lease_kwargs(lease),
            )
            return self._apply_initial_catalog_plan(
                chat_id,
                pending.order_no,
                plan,
                **_lease_kwargs(lease),
            )

        plan = record.catalog_update_plan
        _ensure_lease(lease)
        validation_error = self._catalog_plan_validation_error(plan, pending, record)
        if validation_error:
            _ensure_lease(lease)
            self._mark_catalog_conflict(
                chat_id,
                pending.order_no,
                validation_error,
                **_lease_kwargs(lease),
            )
            self._send_catalog_conflict_reply(
                chat_id,
                pending.order_no,
                **_lease_kwargs(lease),
            )
            return False
        _ensure_lease(lease)
        verification = self.sheets.verify_catalog_mutation(plan)
        _ensure_lease(lease)
        if verification is CatalogMutationVerification.APPLIED:
            self._checkpoint(
                pending.order_no,
                "catalog_updated",
                **_lease_kwargs(lease),
            )
            return True
        if verification is CatalogMutationVerification.NOT_APPLIED:
            return self._controlled_catalog_apply(
                chat_id,
                pending.order_no,
                plan,
                **_lease_kwargs(lease),
            )
        if verification is CatalogMutationVerification.CONFLICT:
            self._mark_catalog_conflict(
                chat_id,
                pending.order_no,
                "Значения каталога изменились.",
                **_lease_kwargs(lease),
            )
            self._send_catalog_conflict_reply(
                chat_id,
                pending.order_no,
                **_lease_kwargs(lease),
            )
            return False
        self._mark_catalog_uncertain(
            chat_id,
            pending.order_no,
            record.last_error or "Не удалось прочитать каталог.",
            **_lease_kwargs(lease),
        )
        self._send_catalog_uncertain_reply(
            chat_id,
            pending.order_no,
            **_lease_kwargs(lease),
        )
        return False

    def _apply_initial_catalog_plan(
        self,
        chat_id: str,
        order_no: str,
        plan: dict[str, Any],
        *,
        lease: ChatLease | None = None,
    ) -> bool:
        """Применяет первый план и запускает ограниченное восстановление по read-back."""
        _ensure_lease(lease)
        try:
            self.sheets.apply_catalog_mutation(plan)
        except ChatLeaseLostError:
            raise
        except Exception as exc:
            _ensure_lease(lease)
            verification = self.sheets.verify_catalog_mutation(plan)
            _ensure_lease(lease)
            if verification is CatalogMutationVerification.APPLIED:
                self._checkpoint(order_no, "catalog_updated", **_lease_kwargs(lease))
                return True
            if verification is CatalogMutationVerification.NOT_APPLIED:
                return self._controlled_catalog_apply(
                    chat_id,
                    order_no,
                    plan,
                    **_lease_kwargs(lease),
                )
            if verification is CatalogMutationVerification.CONFLICT:
                self._mark_catalog_conflict(
                    chat_id,
                    order_no,
                    str(exc) or type(exc).__name__,
                    **_lease_kwargs(lease),
                )
                self._send_catalog_conflict_reply(
                    chat_id,
                    order_no,
                    **_lease_kwargs(lease),
                )
                return False
            self._mark_catalog_uncertain(
                chat_id,
                order_no,
                str(exc) or type(exc).__name__,
                **_lease_kwargs(lease),
            )
            self._send_catalog_uncertain_reply(
                chat_id,
                order_no,
                **_lease_kwargs(lease),
            )
            return False

        _ensure_lease(lease)
        verification = self.sheets.verify_catalog_mutation(plan)
        _ensure_lease(lease)
        if verification is CatalogMutationVerification.APPLIED:
            self._checkpoint(order_no, "catalog_updated", **_lease_kwargs(lease))
            return True
        if verification is CatalogMutationVerification.NOT_APPLIED:
            return self._controlled_catalog_apply(
                chat_id,
                order_no,
                plan,
                **_lease_kwargs(lease),
            )
        if verification is CatalogMutationVerification.CONFLICT:
            self._mark_catalog_conflict(
                chat_id,
                order_no,
                "Значения каталога изменились.",
                **_lease_kwargs(lease),
            )
            self._send_catalog_conflict_reply(
                chat_id,
                order_no,
                **_lease_kwargs(lease),
            )
            return False
        self._mark_catalog_uncertain(
            chat_id,
            order_no,
            "Не удалось проверить результат записи каталога.",
            **_lease_kwargs(lease),
        )
        self._send_catalog_uncertain_reply(
            chat_id,
            order_no,
            **_lease_kwargs(lease),
        )
        return False

    def _controlled_catalog_apply(
        self,
        chat_id: str,
        order_no: str,
        plan: dict[str, Any],
        *,
        lease: ChatLease | None = None,
    ) -> bool:
        """Разрешает одну запись только после подтверждения исходного состояния."""
        _ensure_lease(lease)
        try:
            self.sheets.apply_catalog_mutation(plan)
        except ChatLeaseLostError:
            raise
        except Exception as exc:
            _ensure_lease(lease)
            verification = self.sheets.verify_catalog_mutation(plan)
            _ensure_lease(lease)
            if verification is CatalogMutationVerification.APPLIED:
                self._checkpoint(order_no, "catalog_updated", **_lease_kwargs(lease))
                return True
            if verification is CatalogMutationVerification.CONFLICT:
                self._mark_catalog_conflict(
                    chat_id,
                    order_no,
                    str(exc) or type(exc).__name__,
                    **_lease_kwargs(lease),
                )
                self._send_catalog_conflict_reply(
                    chat_id,
                    order_no,
                    **_lease_kwargs(lease),
                )
                return False
            self._mark_catalog_uncertain(
                chat_id,
                order_no,
                str(exc) or type(exc).__name__,
                **_lease_kwargs(lease),
            )
            self._send_catalog_uncertain_reply(
                chat_id,
                order_no,
                **_lease_kwargs(lease),
            )
            return False

        _ensure_lease(lease)
        verification = self.sheets.verify_catalog_mutation(plan)
        _ensure_lease(lease)
        if verification is CatalogMutationVerification.APPLIED:
            self._checkpoint(order_no, "catalog_updated", **_lease_kwargs(lease))
            return True
        if verification is CatalogMutationVerification.CONFLICT:
            self._mark_catalog_conflict(
                chat_id,
                order_no,
                "Значения каталога изменились.",
                **_lease_kwargs(lease),
            )
            self._send_catalog_conflict_reply(
                chat_id,
                order_no,
                **_lease_kwargs(lease),
            )
            return False
        self._mark_catalog_uncertain(
            chat_id,
            order_no,
            "Не удалось подтвердить повторную запись каталога.",
            **_lease_kwargs(lease),
        )
        self._send_catalog_uncertain_reply(
            chat_id,
            order_no,
            **_lease_kwargs(lease),
        )
        return False

    @staticmethod
    def _catalog_plan_validation_error(
        plan: Any,
        pending: PendingSubmission,
        record: SubmissionRecord,
    ) -> str:
        """Проверяет связи плана с заявкой до чтения или изменения таблицы."""
        if not GoogleSheetsGateway._valid_catalog_mutation_plan(plan):
            return "Сохранённый план изменения каталога повреждён."
        expected_operation_id = f"catalog:{pending.order_no}"
        if plan["operation_id"] != expected_operation_id:
            return "Идентификатор операции каталога не совпадает с заявкой."
        if (
            record.catalog_update_operation_id
            and plan["operation_id"] != record.catalog_update_operation_id
        ):
            return "Идентификатор сохранённого плана не совпадает с контрольной точкой."
        if plan["order_no"] != pending.order_no or plan["order_no"] != record.order_no:
            return "Номер заявки в плане каталога не совпадает с заявкой."
        if plan["spreadsheet_id"] != pending.spreadsheet_id:
            return "Таблица в плане каталога не совпадает с таблицей заведения."
        return ""

    def _persist_catalog_started(
        self,
        order_no: str,
        plan: dict[str, Any],
        *,
        lease: ChatLease | None = None,
    ) -> None:
        """Сохраняет неизменяемый план и статус started до вызова Google Sheets."""
        from sqlalchemy import select

        _ensure_lease(lease)
        with SessionLocal.begin() as db:
            record = db.scalar(
                select(SubmissionRecord)
                .where(SubmissionRecord.order_no == order_no)
                .with_for_update()
            )
            if record is None:
                raise RuntimeError(f"Submission record {order_no} not found")
            _ensure_lease(lease)
            record.catalog_update_plan = plan
            record.catalog_update_operation_id = plan["operation_id"]
            record.catalog_update_status = "started"
            record.catalog_update_started_at = datetime.now(UTC)
            record.catalog_update_completed_at = None
            record.catalog_updated = False
            record.last_error = None
            pending = PendingSubmission.model_validate(record.payload)
            self._append_submission_event(
                OrderEventRepository(db),
                pending,
                event_type="submission_catalog_update_started",
                idempotency_key=f"order:{order_no}:catalog-update-started",
            )

    def _persist_catalog_completed(
        self,
        order_no: str,
        plan: dict[str, Any],
        *,
        lease: ChatLease | None = None,
    ) -> None:
        """Фиксирует пустой план как completed без внешней записи."""
        from sqlalchemy import select

        _ensure_lease(lease)
        with SessionLocal.begin() as db:
            record = db.scalar(
                select(SubmissionRecord)
                .where(SubmissionRecord.order_no == order_no)
                .with_for_update()
            )
            if record is None:
                raise RuntimeError(f"Submission record {order_no} not found")
            _ensure_lease(lease)
            record.catalog_update_plan = plan
            record.catalog_update_operation_id = plan["operation_id"]
            record.catalog_update_status = "completed"
            record.catalog_updated = True
            record.catalog_update_completed_at = datetime.now(UTC)
            record.last_error = None
            pending = PendingSubmission.model_validate(record.payload)
            self._append_submission_event(
                OrderEventRepository(db),
                pending,
                event_type="submission_catalog_updated",
                idempotency_key=f"order:{order_no}:catalog_updated",
            )

    def _mark_catalog_uncertain(
        self,
        chat_id: str,
        order_no: str,
        error: str,
        *,
        lease: ChatLease | None = None,
    ) -> None:
        """Фиксирует неизвестный результат и запрещает повторную запись в таблицу."""
        from sqlalchemy import select

        _ensure_lease(lease)
        with SessionLocal.begin() as db:
            sessions = SessionRepository(db)
            row, state = sessions.get_for_update(chat_id)
            _ensure_lease(lease)
            state.stage = SessionStage.SUBMISSION_FAILED
            state.status = "catalog_update_uncertain"
            if state.pending_submission:
                state.pending_submission.failed_stage = "catalog_update_uncertain"
                state.pending_submission.last_error = error[:1000]
            sessions.save(chat_id, state, row)
            _ensure_lease(lease)
            record = db.scalar(
                select(SubmissionRecord)
                .where(SubmissionRecord.order_no == order_no)
                .with_for_update()
            )
            if record is None:
                raise RuntimeError(f"Submission record {order_no} not found")
            _ensure_lease(lease)
            record.catalog_update_status = "uncertain"
            record.catalog_updated = False
            record.last_error = error[:4000]
            pending = PendingSubmission.model_validate(record.payload)
            self._append_submission_event(
                OrderEventRepository(db),
                pending,
                event_type="submission_catalog_update_uncertain",
                idempotency_key=f"order:{order_no}:catalog-update-uncertain",
                status="uncertain",
                details={"error": error},
            )

    def _send_catalog_uncertain_reply(
        self,
        chat_id: str,
        order_no: str,
        *,
        lease: ChatLease | None = None,
    ) -> None:
        """Показывает спокойное сообщение без технических терминов и повторной записи."""
        _ensure_lease(lease)
        with SessionLocal() as db:
            _, state = SessionRepository(db).get_for_update(chat_id)
        _ensure_lease(lease)
        self.telegram.send_reply(
            chat_id,
            submission_catalog_uncertain_reply(state, order_no),
        )

    def _mark_catalog_conflict(
        self,
        chat_id: str,
        order_no: str,
        error: str,
        *,
        lease: ChatLease | None = None,
    ) -> None:
        """Фиксирует конфликт каталога и запрещает автоматическую перезапись."""
        from sqlalchemy import select

        _ensure_lease(lease)
        with SessionLocal.begin() as db:
            sessions = SessionRepository(db)
            row, state = sessions.get_for_update(chat_id)
            _ensure_lease(lease)
            state.stage = SessionStage.SUBMISSION_FAILED
            state.status = "catalog_update_conflict"
            if state.pending_submission:
                state.pending_submission.failed_stage = "catalog_update_conflict"
                state.pending_submission.last_error = error[:1000]
            sessions.save(chat_id, state, row)
            _ensure_lease(lease)
            record = db.scalar(
                select(SubmissionRecord)
                .where(SubmissionRecord.order_no == order_no)
                .with_for_update()
            )
            if record is None:
                raise RuntimeError(f"Submission record {order_no} not found")
            _ensure_lease(lease)
            record.catalog_update_status = "conflict"
            record.catalog_updated = False
            record.last_error = error[:4000]
            pending = PendingSubmission.model_validate(record.payload)
            self._append_submission_event(
                OrderEventRepository(db),
                pending,
                event_type="submission_catalog_verification_conflict",
                idempotency_key=f"order:{order_no}:catalog-verification-conflict",
                status="conflict",
                details={"error": error},
            )

    def _send_catalog_conflict_reply(
        self,
        chat_id: str,
        order_no: str,
        *,
        lease: ChatLease | None = None,
    ) -> None:
        """Показывает понятное сообщение без кнопки опасного повтора."""
        _ensure_lease(lease)
        with SessionLocal() as db:
            _, state = SessionRepository(db).get_for_update(chat_id)
        _ensure_lease(lease)
        self.telegram.send_reply(
            chat_id,
            submission_catalog_conflict_reply(state, order_no),
        )

    def _checkpoint(
        self,
        order_no: str,
        field: str,
        *,
        lease: ChatLease | None = None,
    ) -> None:
        """Отмечает завершение этапа отправки заявки."""
        from sqlalchemy import select

        _ensure_lease(lease)
        with SessionLocal.begin() as db:
            record = db.scalar(
                select(SubmissionRecord)
                .where(SubmissionRecord.order_no == order_no)
                .with_for_update()
            )
            if record is None:
                raise RuntimeError(f"Submission record {order_no} not found")
            _ensure_lease(lease)
            setattr(record, field, True)
            if field == "catalog_updated":
                record.catalog_update_status = "completed"
                record.catalog_update_completed_at = datetime.now(UTC)
            if field == "recalc_done":
                record.recalc_status = "completed"
                record.recalc_completed_at = datetime.now(UTC)
            if field == "completion_notified":
                record.completion_notification_status = "completed"
                record.completion_notification_completed_at = datetime.now(UTC)
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

    def _send_dispatch_uncertain_once(
        self,
        chat_id: str,
        order_no: str,
        *,
        lease: ChatLease | None = None,
    ) -> None:
        """Один раз предупреждает пользователя и не предлагает повторную отправку."""
        _ensure_lease(lease)
        record = self._get_record(order_no)
        if record.dispatch_uncertain_notified:
            return
        with SessionLocal() as db:
            _, state = SessionRepository(db).get_for_update(chat_id)
        _ensure_lease(lease)
        self.telegram.send_reply(
            chat_id,
            submission_dispatch_uncertain_reply(state, order_no),
        )
        _ensure_lease(lease)
        self._checkpoint(
            order_no,
            "dispatch_uncertain_notified",
            **_lease_kwargs(lease),
        )

    def _finalize(
        self,
        chat_id: str,
        order_no: str,
        external_order_no: str,
        *,
        dispatched: bool = True,
        lease: ChatLease | None = None,
    ) -> ConversationState:
        """Финализирует записанную заявку с учётом внешней отправки."""
        from sqlalchemy import select

        _ensure_lease(lease)
        with SessionLocal.begin() as db:
            sessions = SessionRepository(db)
            row, state = sessions.get_for_update(chat_id)
            _ensure_lease(lease)
            self._apply_successful_submission_state(
                state,
                external_order_no,
                status="submitted" if dispatched else "saved_locally",
            )
            _ensure_lease(lease)
            sessions.save(chat_id, state, row)
            record = db.scalar(
                select(SubmissionRecord)
                .where(SubmissionRecord.order_no == order_no)
                .with_for_update()
            )
            if record:
                _ensure_lease(lease)
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
            state.status = "submission_failed"
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
