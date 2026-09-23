"""Владеет контрольными точками уведомления о завершении заявки."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from restaurant_bot.domain.models import ConversationState, PendingSubmission
from restaurant_bot.integrations.cache import ChatLease, ChatLeaseLostError
from restaurant_bot.persistence.models import SubmissionRecord
from restaurant_bot.presentation.telegram.submission import (
    submission_local_saved_reply,
    submission_success_reply,
)
from restaurant_bot.repositories.order_events import OrderEventRepository

if TYPE_CHECKING:
    from restaurant_bot.services.submission import SubmissionService


class SubmissionCompletionService:
    """Отделяет уведомление пользователя от основного протокола отправки."""

    def __init__(
        self,
        owner: SubmissionService,
        *,
        session_local: Any,
        session_repository: Any,
    ) -> None:
        """Подключает Telegram, аудит и текущие фабрики хранения владельца."""
        self._owner = owner
        self._session_local = session_local
        self._session_repository = session_repository

    @property
    def telegram(self) -> Any:
        """Возвращает Telegram-клиент владельца отправки."""
        return self._owner.telegram

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

    def _append_submission_event(self, *args: Any, **kwargs: Any) -> None:
        """Передаёт запись аудита каноническому владельцу отправки."""
        self._owner._append_submission_event(*args, **kwargs)

    def _mark_completion_notification_started(self, order_no: str) -> bool:
        """Фиксирует начало уведомления до вызова Telegram."""
        from sqlalchemy import select

        with self._session_local.begin() as db:
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

        with self._session_local.begin() as db:
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

        with self._session_local.begin() as db:
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
        if not self._owner._mark_completion_notification_started(checkpoint_order_no):
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
                self._owner._mark_completion_notification_uncertain(
                    checkpoint_order_no,
                    str(exc) or type(exc).__name__,
                )
            finally:
                raise
        if lease is not None:
            lease.ensure_owned()
        self._owner._mark_completion_notification_completed(checkpoint_order_no)

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
        if not self._owner._mark_completion_notification_started(order_no):
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
                self._owner._mark_completion_notification_uncertain(
                    order_no,
                    str(exc) or type(exc).__name__,
                )
            finally:
                raise
        if lease is not None:
            lease.ensure_owned()
        self._owner._mark_completion_notification_completed(order_no)

    def _load_unnotified_completion(
        self,
        chat_id: str,
    ) -> tuple[str, str, ConversationState, bool] | None:
        """Загружает только безопасную первую доставку уведомления."""
        from sqlalchemy import select

        with self._session_local.begin() as db:
            records = db.scalars(
                select(SubmissionRecord)
                .where(
                    SubmissionRecord.telegram_id == chat_id,
                    SubmissionRecord.finalized.is_(True),
                    SubmissionRecord.completion_notified.is_(False),
                )
                .order_by(SubmissionRecord.created_at.desc())
            ).all()
            _, state = self._session_repository(db).get_for_update(chat_id)
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
