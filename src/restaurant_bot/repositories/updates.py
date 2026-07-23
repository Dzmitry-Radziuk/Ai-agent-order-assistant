from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from restaurant_bot.db_models import TelegramUpdate


class UpdateRepository:
    """Идемпотентно сохраняет обновления Telegram."""

    def __init__(self, db: Session):
        """Инициализирует компонент."""
        self.db = db

    def enqueue_once(self, update_id: int, chat_id: str, payload: dict[str, Any]) -> bool:
        """Ставит обновление Telegram в очередь только один раз."""
        self.db.add(TelegramUpdate(update_id=update_id, chat_id=chat_id, payload=payload))
        try:
            self.db.flush()
            return True
        except IntegrityError:
            self.db.rollback()
            return False

    def get_for_update(self, update_id: int) -> TelegramUpdate | None:
        """Загружает запись с блокировкой для изменения."""
        return self.db.scalar(
            select(TelegramUpdate).where(TelegramUpdate.update_id == update_id).with_for_update()
        )
