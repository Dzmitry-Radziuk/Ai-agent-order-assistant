from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from restaurant_bot.db_models import TelegramUpdate

UNFINISHED_UPDATE_STATUSES = ("queued", "processing")
STALE_PROCESSING_AFTER = timedelta(minutes=5)


class UpdateSequenceDeferred(RuntimeError):
    """Сообщает, что более раннее обновление этого чата ещё не завершено."""


class UpdateRepository:
    """Идемпотентно сохраняет обновления Telegram."""

    def __init__(self, db: Session):
        """Инициализирует компонент."""
        self.db = db

    def enqueue_once(self, update_id: int, chat_id: str, payload: dict[str, Any]) -> bool:
        """Ставит обновление Telegram в очередь только один раз."""
        try:
            with self.db.begin_nested():
                self.db.add(TelegramUpdate(update_id=update_id, chat_id=chat_id, payload=payload))
                self.db.flush()
        except IntegrityError:
            return False
        return True

    def get_status(self, update_id: int) -> str | None:
        """Возвращает статус сохранённого обновления без изменения записи."""
        update = self.db.get(TelegramUpdate, update_id)
        return update.status if update is not None else None

    def has_lower_unfinished(self, chat_id: str, update_id: int) -> bool:
        """Проверяет наличие более раннего незавершённого обновления чата."""
        lower_id = self.db.scalar(
            select(TelegramUpdate.update_id)
            .where(
                TelegramUpdate.chat_id == chat_id,
                TelegramUpdate.update_id < update_id,
                TelegramUpdate.status.in_(UNFINISHED_UPDATE_STATUSES),
            )
            .order_by(TelegramUpdate.update_id)
            .limit(1)
        )
        return lower_id is not None

    def recoverable_for_redrive(self, stale_before: datetime) -> list[TelegramUpdate]:
        """Выбирает самое раннее доступное для восстановления обновление каждого чата."""
        rows = list(
            self.db.scalars(
                select(TelegramUpdate)
                .where(
                    or_(
                        TelegramUpdate.status == "queued",
                        and_(
                            TelegramUpdate.status == "processing",
                            TelegramUpdate.updated_at < stale_before,
                        ),
                    )
                )
                .order_by(TelegramUpdate.chat_id, TelegramUpdate.update_id)
            ).all()
        )
        unfinished = list(
            self.db.scalars(
                select(TelegramUpdate)
                .where(TelegramUpdate.status.in_(UNFINISHED_UPDATE_STATUSES))
                .order_by(TelegramUpdate.chat_id, TelegramUpdate.update_id)
            ).all()
        )
        first_by_chat: dict[str, TelegramUpdate] = {}
        for row in unfinished:
            first_by_chat.setdefault(row.chat_id, row)
        recoverable_ids = {row.update_id for row in rows}
        result: list[TelegramUpdate] = []
        for row in first_by_chat.values():
            if row.update_id in recoverable_ids:
                result.append(row)
        return result

    def get_for_update(self, update_id: int) -> TelegramUpdate | None:
        """Загружает запись с блокировкой для изменения."""
        return self.db.scalar(
            select(TelegramUpdate).where(TelegramUpdate.update_id == update_id).with_for_update()
        )

    def defer_if_current_attempt(self, update_id: int, attempt: int) -> bool:
        """Возвращает в очередь только указанную текущую попытку обработки."""
        result = self.db.execute(
            update(TelegramUpdate)
            .where(
                TelegramUpdate.update_id == update_id,
                TelegramUpdate.status == "processing",
                TelegramUpdate.attempts == attempt,
            )
            .values(status="queued", error=None)
        )
        rowcount = getattr(result, "rowcount", 0)
        return int(rowcount or 0) == 1
