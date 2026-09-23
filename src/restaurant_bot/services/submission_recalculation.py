"""Координирует контрольные точки пересчёта заявки."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from restaurant_bot.domain.models import PendingSubmission, SessionStage
from restaurant_bot.persistence.models import SubmissionRecord
from restaurant_bot.presentation.telegram.submission import submission_recalculation_uncertain_reply
from restaurant_bot.repositories.order_events import OrderEventRepository

if TYPE_CHECKING:
    from restaurant_bot.services.submission import SubmissionService


class SubmissionRecalculationService:
    """Владеет контрольными точками и recovery пересчёта."""

    def __init__(
        self,
        owner: SubmissionService,
        *,
        session_local: Any,
        session_repository: Any,
    ) -> None:
        """Подключает общий сервис для событий и Telegram."""
        self._owner = owner
        self._session_local = session_local
        self._session_repository = session_repository

    @property
    def telegram(self) -> Any:
        """Возвращает Telegram-клиент общего сервиса отправки."""
        return self._owner.telegram

    def _append_submission_event(self, *args: Any, **kwargs: Any) -> None:
        """Передаёт аудит события общему владельцу отправки."""
        self._owner._append_submission_event(*args, **kwargs)

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

        with self._session_local.begin() as db:
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
            record.recalc_operation_id = f"recalc:{order_no}:attempt:1"
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

        with self._session_local.begin() as db:
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
        """Фиксирует неопределённый результат и оставляет recovery планировщику."""
        from sqlalchemy import select

        with self._session_local.begin() as db:
            sessions = self._session_repository(db)
            row, state = sessions.get_for_update(chat_id)
            active_pending = state.pending_submission
            if active_pending is not None and active_pending.order_no == order_no:
                state.stage = SessionStage.SUBMISSION_FAILED
                state.status = "recalculation_uncertain"
                active_pending.failed_stage = "recalculation_uncertain"
                active_pending.last_error = error[:1000]
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
        with self._session_local() as db:
            _, state = self._session_repository(db).get_for_update(chat_id)
        self.telegram.send_reply(
            chat_id,
            submission_recalculation_uncertain_reply(state, order_no),
        )
