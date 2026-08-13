from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from restaurant_bot.domain.models import PendingSubmission
from restaurant_bot.persistence.models import SubmissionRecord


class SubmissionRepository:
    """Сохраняет контрольные точки отправки заявок."""

    def __init__(self, db: Session):
        """Инициализирует компонент."""
        self.db = db

    def get_or_create(self, telegram_id: str, pending: PendingSubmission) -> SubmissionRecord:
        """Возвращает или создаёт запись отправки."""
        row = self.db.scalar(
            select(SubmissionRecord)
            .where(SubmissionRecord.order_no == pending.order_no)
            .with_for_update()
        )
        if row is None:
            row = SubmissionRecord(
                order_no=pending.order_no,
                trace_id=pending.trace_id,
                telegram_id=telegram_id,
                payload=pending.model_dump(mode="json"),
            )
            self.db.add(row)
            self.db.flush()
        return row
