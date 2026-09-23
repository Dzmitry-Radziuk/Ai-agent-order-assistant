"""Координирует сервис «submission»."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from math import isclose
from time import perf_counter
from typing import Any

import structlog
from redis import Redis

from restaurant_bot.config import Settings
from restaurant_bot.domain.departments import normalize_department
from restaurant_bot.domain.models import (
    BotReply,
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
    GoogleSheetsGateway,
    OrderSubmissionResult,
)
from restaurant_bot.integrations.telegram import TelegramClient
from restaurant_bot.parsing.numeric import to_float
from restaurant_bot.persistence.database import SessionLocal
from restaurant_bot.persistence.models import SubmissionRecord
from restaurant_bot.presentation.telegram.submission import (
    build_order_status_text,
    submission_catalog_conflict_reply,
    submission_catalog_uncertain_reply,
    submission_dispatch_uncertain_reply,
    submission_failure_reply,
    submission_local_saved_reply,
    submission_recalculation_recovered_reply,
    submission_recalculation_uncertain_reply,
    submission_success_reply,
)
from restaurant_bot.repositories.order_events import OrderEventRepository
from restaurant_bot.repositories.sessions import SessionRepository
from restaurant_bot.repositories.submissions import SubmissionRepository
from restaurant_bot.services.submission_access import SubmissionAccessService
from restaurant_bot.services.submission_catalog import SubmissionCatalogService
from restaurant_bot.services.submission_completion import SubmissionCompletionService
from restaurant_bot.services.submission_product_add import SubmissionProductAddService
from restaurant_bot.services.submission_recalculation import SubmissionRecalculationService
from restaurant_bot.services.submission_status import SubmissionStatusService
from restaurant_bot.services.venue_registration import VenueRegistrationService

logger = structlog.get_logger(__name__)

__all__ = [
    "SubmissionService",
    "build_order_status_text",
    "submission_local_saved_reply",
    "submission_recalculation_uncertain_reply",
    "submission_success_reply",
]


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
        """Передаёт чтение статуса уведомления специализированному модулю."""
        return SubmissionCompletionService._completion_notification_status(record)

    def _completion_service(self) -> SubmissionCompletionService:
        """Создаёт сервис уведомления с текущими тестовыми и runtime-зависимостями."""
        return SubmissionCompletionService(
            self,
            session_local=SessionLocal,
            session_repository=SessionRepository,
        )

    def _mark_completion_notification_started(self, order_no: str) -> bool:
        """Передаёт фиксацию начала уведомления специализированному модулю."""
        return self._completion_service()._mark_completion_notification_started(order_no)

    def _mark_completion_notification_completed(self, order_no: str) -> None:
        """Передаёт фиксацию доставки уведомления специализированному модулю."""
        self._completion_service()._mark_completion_notification_completed(order_no)

    def _mark_completion_notification_uncertain(self, order_no: str, error: str) -> None:
        """Передаёт фиксацию неопределённого уведомления специализированному модулю."""
        self._completion_service()._mark_completion_notification_uncertain(order_no, error)

    def _send_completion(
        self,
        chat_id: str,
        state: ConversationState,
        external_order_no: str,
        checkpoint_order_no: str,
        *,
        lease: ChatLease | None = None,
    ) -> None:
        """Передаёт подтверждение завершения специализированному модулю."""
        self._completion_service()._send_completion(
            chat_id,
            state,
            external_order_no,
            checkpoint_order_no,
            lease=lease,
        )

    def _send_local_completion(
        self,
        chat_id: str,
        state: ConversationState,
        order_no: str,
        *,
        lease: ChatLease | None = None,
    ) -> None:
        """Передаёт локальное подтверждение специализированному модулю."""
        self._completion_service()._send_local_completion(
            chat_id,
            state,
            order_no,
            lease=lease,
        )

    def _load_unnotified_completion(
        self,
        chat_id: str,
    ) -> tuple[str, str, ConversationState, bool] | None:
        """Передаёт поиск неотправленного подтверждения специализированному модулю."""
        return self._completion_service()._load_unnotified_completion(chat_id)

    def check_pending_submissions(self, *, limit: int = 100) -> int:
        """Проверяет незавершённые заявки и безопасно продолжает retryable-сбои."""
        from sqlalchemy import select

        with SessionLocal() as db:
            records = db.scalars(
                select(SubmissionRecord)
                .where(SubmissionRecord.finalized.is_(False))
                .order_by(SubmissionRecord.created_at)
                .limit(limit)
            ).all()
            candidates = [
                (
                    record.order_no,
                    record.telegram_id,
                    PendingSubmission.model_validate(record.payload),
                    bool(record.dispatch_started),
                    bool(record.dispatch_completed),
                    self._recalc_status(record),
                )
                for record in records
            ]

        recovered = 0
        for (
            order_no,
            chat_id,
            pending,
            dispatch_started,
            dispatch_completed,
            recalc_status,
        ) in candidates:
            if dispatch_started and not dispatch_completed:
                try:
                    if self._recover_dispatch_from_history(chat_id, pending):
                        recovered += 1
                except Exception:
                    logger.exception(
                        "submission_status_check_failed",
                        chat_id=chat_id,
                        order_no=order_no,
                    )
                continue
            if recalc_status in {"started", "uncertain"}:
                try:
                    if self._recover_recalculation(chat_id, pending):
                        recovered += 1
                        if self._has_active_pending_state(chat_id, order_no):
                            self.submit(chat_id, report_failure=False)
                        else:
                            self._finalize_detached_local_recalculation(pending)
                except Exception:
                    logger.exception(
                        "submission_recalculation_recovery_failed",
                        chat_id=chat_id,
                        order_no=order_no,
                    )
                continue
            if not self._has_retryable_pending_state(chat_id, order_no):
                continue
            try:
                self.submit(chat_id, report_failure=False)
            except Exception:
                logger.exception(
                    "submission_recovery_failed",
                    chat_id=chat_id,
                    order_no=order_no,
                )
        logger.info(
            "submission_status_check_completed",
            checked_count=len(candidates),
            recovered_count=recovered,
        )
        return recovered

    def _recover_recalculation(
        self,
        chat_id: str,
        pending: PendingSubmission,
    ) -> bool:
        """Периодически повторяет конвергентный перерасчёт до успеха."""
        with chat_lock(self.redis, chat_id, timeout=300) as lease:
            if lease is not None:
                lease.ensure_owned()
            with SessionLocal() as db:
                _, state = SessionRepository(db).get_for_update(chat_id)
            spreadsheet_id = pending.spreadsheet_id or state.spreadsheet_id
            if not spreadsheet_id or not self._has_current_access(
                chat_id,
                user_id=pending.telegram_user_id or chat_id,
                bound_chat_id=pending.telegram_chat_id or chat_id,
                venue_code=pending.venue_code or state.venue_code,
                spreadsheet_id=spreadsheet_id,
            ):
                return False
            if not self._claim_recalculation_recovery(pending.order_no):
                return False
            try:
                prepared = self.sheets.prepare_recalculation(spreadsheet_id)
                if lease is not None:
                    lease.ensure_owned()
                self.sheets.send_recalculation(prepared)
            except Exception as exc:
                if lease is not None:
                    lease.ensure_owned()
                self._mark_recalc_uncertain(
                    chat_id,
                    pending.order_no,
                    str(exc) or type(exc).__name__,
                )
                return False
            if lease is not None:
                lease.ensure_owned()
            self._mark_recalc_completed(pending.order_no)
            logger.info(
                "submission_recalculation_recovered",
                chat_id=chat_id,
                order_no=pending.order_no,
            )
            return True

    def _claim_recalculation_recovery(self, order_no: str) -> bool:
        """Атомарно резервирует следующую попытку перерасчёта в локальной БД."""
        from sqlalchemy import select

        with SessionLocal.begin() as db:
            record = db.scalar(
                select(SubmissionRecord)
                .where(SubmissionRecord.order_no == order_no)
                .with_for_update()
            )
            if record is None or self._recalc_status(record) == "completed":
                return False
            attempt = self._recalc_attempt(record.recalc_operation_id)
            next_attempt = attempt + 1
            record.recalc_status = "started"
            record.recalc_operation_id = f"recalc:{order_no}:attempt:{next_attempt}"
            record.recalc_started_at = datetime.now(UTC)
            record.recalc_completed_at = None
            record.recalc_done = False
            record.last_error = None
            pending = PendingSubmission.model_validate(record.payload)
            self._append_submission_event(
                OrderEventRepository(db),
                pending,
                event_type="submission_recalculation_retry_started",
                idempotency_key=f"order:{order_no}:recalc-retry-{next_attempt}",
                details={
                    "operation_id": record.recalc_operation_id,
                    "attempt": next_attempt,
                },
            )
            return True

    @staticmethod
    def _recalc_attempt(operation_id: str) -> int:
        """Извлекает номер попытки из локального идентификатора перерасчёта."""
        marker = ":attempt:"
        if marker not in operation_id:
            return 1 if operation_id else 0
        value = operation_id.rsplit(marker, 1)[-1]
        try:
            return max(0, int(value))
        except ValueError:
            return 1

    @staticmethod
    def _has_active_pending_state(chat_id: str, order_no: str) -> bool:
        """Проверяет, что восстановленная заявка ещё является текущим черновиком."""
        with SessionLocal() as db:
            _, state = SessionRepository(db).get_for_update(chat_id)
        pending = state.pending_submission
        return bool(pending is not None and pending.order_no == order_no)

    def _finalize_detached_local_recalculation(self, pending: PendingSubmission) -> None:
        """Завершает старую локальную заявку после reset без изменения нового черновика."""
        if getattr(self.settings, "google_order_submission_enabled", True):
            return
        from sqlalchemy import select

        with SessionLocal.begin() as db:
            record = db.scalar(
                select(SubmissionRecord)
                .where(SubmissionRecord.order_no == pending.order_no)
                .with_for_update()
            )
            if (
                record is None
                or record.finalized
                or not record.recalc_done
                or record.dispatch_started
            ):
                return
            record.finalized = True
            record.completion_notified = False
            record.completion_notification_status = "started"
            record.completion_notification_started_at = datetime.now(UTC)
            record.completion_notification_completed_at = None
            record.last_error = None
            self._append_submission_event(
                OrderEventRepository(db),
                pending,
                event_type="submission_recalculation_recovered_after_reset",
                idempotency_key=f"order:{pending.order_no}:recalc-recovered-after-reset",
            )
            self._append_submission_event(
                OrderEventRepository(db),
                pending,
                event_type="submission_notification_started",
                idempotency_key=f"order:{pending.order_no}:notification-started",
            )

        try:
            chat_id = pending.telegram_chat_id or pending.telegram_user_id
            self.telegram.send_reply(
                chat_id,
                submission_recalculation_recovered_reply(pending.order_no),
            )
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            self._mark_completion_notification_uncertain(pending.order_no, error)
            logger.warning(
                "submission_recalculation_recovery_notification_uncertain",
                order_no=pending.order_no,
                error_type=type(exc).__name__,
            )
            return
        self._mark_completion_notification_completed(pending.order_no)

    @staticmethod
    def _has_retryable_pending_state(chat_id: str, order_no: str) -> bool:
        """Проверяет, что чат всё ещё хранит безопасный снимок для retry."""
        with SessionLocal() as db:
            _, state = SessionRepository(db).get_for_update(chat_id)
        pending = state.pending_submission
        return bool(
            pending is not None and pending.order_no == order_no and not pending.failed_stage
        )

    def _recover_dispatch_from_history(
        self,
        chat_id: str,
        pending: PendingSubmission,
    ) -> bool:
        """Находит неопределённую отправку в истории под блокировкой чата."""
        with chat_lock(self.redis, chat_id, timeout=300) as lease:
            if lease is not None:
                lease.ensure_owned()
            return self._recover_dispatch_from_history_locked(chat_id, pending, lease=lease)

    def _recover_dispatch_from_history_locked(
        self,
        chat_id: str,
        pending: PendingSubmission,
        *,
        lease: ChatLease | None = None,
    ) -> bool:
        """Находит неопределённую отправку в истории без повторного POST."""
        from sqlalchemy import select

        with SessionLocal() as db:
            _, state = SessionRepository(db).get_for_update(chat_id)
        _ensure_lease(lease)
        spreadsheet_id = pending.spreadsheet_id or state.spreadsheet_id
        if not spreadsheet_id or not self._has_current_access(
            chat_id,
            user_id=pending.telegram_user_id or chat_id,
            bound_chat_id=pending.telegram_chat_id or chat_id,
            venue_code=pending.venue_code or state.venue_code,
            spreadsheet_id=spreadsheet_id,
        ):
            return False
        rows = self.sheets.read_order_statuses(
            [pending.order_no],
            spreadsheet_id,
            state.venue_name or state.restaurant,
        )
        _ensure_lease(lease)
        if not rows:
            return False
        recovered_order_no = self._history_order_number(rows[0]) or pending.order_no
        notify_state: ConversationState | None = None
        with SessionLocal.begin() as db:
            sessions = SessionRepository(db)
            session_row, current_state = sessions.get_for_update(chat_id)
            record = db.scalar(
                select(SubmissionRecord)
                .where(SubmissionRecord.order_no == pending.order_no)
                .with_for_update()
            )
            if record is None or record.finalized:
                return False
            current_pending = current_state.pending_submission
            active_in_chat = bool(
                current_pending is not None and current_pending.order_no == pending.order_no
            )
            record.dispatch_completed = True
            record.dispatch_completed_at = datetime.now(UTC)
            record.external_order_no = recovered_order_no
            record.finalized = True
            record.last_error = None
            if active_in_chat:
                self._apply_successful_submission_state(
                    current_state,
                    recovered_order_no,
                    status="submitted",
                )
                sessions.save(chat_id, current_state, session_row)
                notify_state = current_state
                event_type = "submission_recovered_from_history"
            else:
                record.completion_notification_status = "suppressed"
                record.completion_notified = True
                event_type = "submission_recovered_after_reset"
            self._append_submission_event(
                OrderEventRepository(db),
                pending,
                event_type=event_type,
                idempotency_key=f"order:{pending.order_no}:recovered-from-history",
                details={"history_order_no": recovered_order_no},
            )
        _ensure_lease(lease)
        if notify_state is not None:
            self._send_completion(
                chat_id,
                notify_state,
                recovered_order_no,
                pending.order_no,
            )
        logger.info(
            "submission_recovered_from_history",
            chat_id=chat_id,
            order_no=pending.order_no,
            history_order_no=recovered_order_no,
            active_in_chat=notify_state is not None,
        )
        return True

    @staticmethod
    def _history_order_number(row: dict[str, Any]) -> str:
        """Возвращает номер заявки из строки истории через read-only owner."""
        return SubmissionStatusService._history_order_number(row)

    def send_status(
        self,
        chat_id: str,
        *,
        page: int = 0,
        selected_index: int | None = None,
        order_number: str = "",
        detail_page: int = 0,
    ) -> None:
        """Передаёт просмотр статусов read-only координатору."""
        SubmissionStatusService(
            self,
            session_local=SessionLocal,
            session_repository=SessionRepository,
            chat_lock_factory=chat_lock,
        ).send_status(
            chat_id,
            page=page,
            selected_index=selected_index,
            order_number=order_number,
            detail_page=detail_page,
        )

    def _send_order_status_page(
        self,
        chat_id: str,
        state: ConversationState,
        *,
        page: int,
        lease: ChatLease | None = None,
    ) -> None:
        """Передаёт страницу статусов read-only координатору."""
        SubmissionStatusService(
            self,
            session_local=SessionLocal,
            session_repository=SessionRepository,
            chat_lock_factory=chat_lock,
        )._send_order_status_page(
            chat_id,
            state,
            page=page,
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
        """Передаёт детали статуса read-only координатору."""
        SubmissionStatusService(
            self,
            session_local=SessionLocal,
            session_repository=SessionRepository,
            chat_lock_factory=chat_lock,
        )._send_order_status_detail(
            chat_id,
            state,
            selected_index=selected_index,
            order_number=order_number,
            detail_page=detail_page,
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
        """Передаёт доставку карточки статуса read-only координатору."""
        SubmissionStatusService(
            self,
            session_local=SessionLocal,
            session_repository=SessionRepository,
            chat_lock_factory=chat_lock,
        )._send_status_reply(
            chat_id,
            state,
            reply,
            lease=lease,
        )

    def _persist_order_status_view(
        self,
        chat_id: str,
        *,
        page: int,
        order_numbers: list[str],
        lease: ChatLease | None = None,
    ) -> None:
        """Передаёт сохранение read-only представления статусов координатору."""
        SubmissionStatusService(
            self,
            session_local=SessionLocal,
            session_repository=SessionRepository,
            chat_lock_factory=chat_lock,
        )._persist_order_status_view(
            chat_id,
            page=page,
            order_numbers=order_numbers,
            lease=lease,
        )

    def submit_product_add(self, chat_id: str) -> None:
        """Передаёт запрос снабженцу специализированному координатору."""
        SubmissionProductAddService(
            self,
            session_local=SessionLocal,
            session_repository=SessionRepository,
            chat_lock_factory=chat_lock,
        ).submit_product_add(chat_id)

    def _send_product_add_success(
        self,
        chat_id: str,
        state: ConversationState,
        request: dict[str, Any],
        *,
        lease: ChatLease | None = None,
    ) -> None:
        """Передаёт успешную запись специализированному координатору."""
        SubmissionProductAddService(
            self,
            session_local=SessionLocal,
            session_repository=SessionRepository,
            chat_lock_factory=chat_lock,
        )._send_product_add_success_impl(
            chat_id,
            state,
            request,
            lease=lease,
        )

    def _persist_worker_ui(
        self,
        chat_id: str,
        revision: int,
        message_id: int | None,
        message_text: str,
        *,
        lease: ChatLease | None = None,
    ) -> None:
        """Сохраняет состояние интерфейса через product-add owner."""
        SubmissionProductAddService(
            self,
            session_local=SessionLocal,
            session_repository=SessionRepository,
            chat_lock_factory=chat_lock,
        ).persist_worker_ui(
            chat_id,
            revision,
            message_id,
            message_text,
            lease=lease,
        )

    @staticmethod
    def _uncertain_product_add_error(exc: Exception) -> bool:
        """Передаёт классификацию ошибки product-add специализированному owner."""
        return SubmissionProductAddService._uncertain_product_add_error(exc)

    @staticmethod
    def _apply_product_add_write_outcome(
        state: ConversationState,
        request_id: str,
        status: str,
        error: str = "",
    ) -> dict[str, Any] | None:
        """Передаёт сохранение результата product-add специализированному owner."""
        return SubmissionProductAddService._apply_product_add_write_outcome(
            state,
            request_id,
            status,
            error,
        )

    def _has_current_access(
        self,
        chat_id: str,
        *,
        user_id: str,
        bound_chat_id: str,
        venue_code: str,
        spreadsheet_id: str,
    ) -> bool:
        """Передаёт проверку доступа специализированному owner."""
        return SubmissionAccessService(
            self,
            session_local=SessionLocal,
            session_repository=SessionRepository,
        )._has_current_access(
            chat_id,
            user_id=user_id,
            bound_chat_id=bound_chat_id,
            venue_code=venue_code,
            spreadsheet_id=spreadsheet_id,
        )

    def _send_access_disabled(self, chat_id: str) -> None:
        """Передаёт отказ доступа специализированному owner."""
        SubmissionAccessService(
            self,
            session_local=SessionLocal,
            session_repository=SessionRepository,
        )._send_access_disabled(chat_id)

    def _load_pending(self, chat_id: str) -> PendingSubmission | None:
        """Передаёт загрузку pending-снимка специализированному owner."""
        return SubmissionAccessService(
            self,
            session_local=SessionLocal,
            session_repository=SessionRepository,
        )._load_pending(chat_id)

    @staticmethod
    def _require_venue_spreadsheet_id(spreadsheet_id: str) -> str:
        """Передаёт проверку таблицы специализированному owner."""
        return SubmissionAccessService._require_venue_spreadsheet_id(spreadsheet_id)

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
        """Передаёт чтение статуса каталога специализированному owner."""
        return SubmissionCatalogService._catalog_status(record)

    @staticmethod
    def _recalc_status(record: SubmissionRecord) -> str:
        """Передаёт проверку статуса пересчёта специализированному owner."""
        return SubmissionRecalculationService._recalc_status(record)

    def _mark_recalc_started(self, order_no: str) -> None:
        """Передаёт начало пересчёта специализированному owner."""
        SubmissionRecalculationService(
            self,
            session_local=SessionLocal,
            session_repository=SessionRepository,
        )._mark_recalc_started(order_no)

    def _mark_recalc_completed(self, order_no: str) -> None:
        """Передаёт завершение пересчёта специализированному owner."""
        SubmissionRecalculationService(
            self,
            session_local=SessionLocal,
            session_repository=SessionRepository,
        )._mark_recalc_completed(order_no)

    def _mark_recalc_uncertain(self, chat_id: str, order_no: str, error: str) -> None:
        """Передаёт неопределённый результат пересчёта специализированному owner."""
        SubmissionRecalculationService(
            self,
            session_local=SessionLocal,
            session_repository=SessionRepository,
        )._mark_recalc_uncertain(chat_id, order_no, error)

    def _send_recalc_uncertain_reply(self, chat_id: str, order_no: str) -> None:
        """Передаёт безопасное уведомление специализированному owner."""
        SubmissionRecalculationService(
            self,
            session_local=SessionLocal,
            session_repository=SessionRepository,
        )._send_recalc_uncertain_reply(chat_id, order_no)

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

    def _catalog_plan_validation_error(
        self,
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
        expected: dict[tuple[str, str], float] = defaultdict(float)
        allowed_departments = {"Зал", "Бар", "Кухня"}
        for row in pending.rows:
            quantity = to_float(row.get("Кол-во", row.get("Количество"))) or 0
            if quantity <= 0:
                continue
            product_id = str(row.get("ID товара") or "").strip()
            if not product_id:
                return "В строке заявки с количеством отсутствует ID товара."
            department = normalize_department(row.get("_department")) or normalize_department(
                getattr(self.settings, "default_department", "Кухня")
            )
            if department not in allowed_departments:
                return "В строке заявки указано неизвестное подразделение."
            expected[(product_id, department)] += quantity
        if not expected:
            return ""

        actual: dict[tuple[str, str], float] = defaultdict(float)
        for mutation in plan["mutations"]:
            if mutation["kind"] != "quantity":
                continue
            department = normalize_department(mutation.get("department"))
            if department not in allowed_departments:
                return "План изменения каталога содержит неизвестное подразделение."
            actual[(str(mutation["product_id"]), department)] += float(mutation["increment"])
        if set(actual) != set(expected):
            return "План изменения каталога покрывает не все позиции и подразделения заявки."
        if any(
            not isclose(actual[key], quantity, rel_tol=1e-9, abs_tol=1e-9)
            for key, quantity in expected.items()
        ):
            return "Количество в плане изменения каталога не совпадает с заявкой."
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
