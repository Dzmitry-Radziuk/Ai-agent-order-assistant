from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from restaurant_bot.db_models import BotSession
from restaurant_bot.domain.models import ConversationState


class SessionRepository:
    """Транзакционно сохраняет состояния диалогов."""

    def __init__(self, db: Session):
        """Инициализирует компонент."""
        self.db = db

    def get_for_update(self, telegram_id: str) -> tuple[BotSession | None, ConversationState]:
        """Загружает запись с блокировкой для изменения."""
        row = self.db.scalar(
            select(BotSession).where(BotSession.telegram_id == telegram_id).with_for_update()
        )
        if row is None:
            return None, ConversationState()
        return row, ConversationState.model_validate(row.data)

    def save(
        self,
        telegram_id: str,
        state: ConversationState,
        existing: BotSession | None = None,
    ) -> BotSession:
        """Сохраняет состояние диалога."""
        if existing is None:
            existing = BotSession(
                telegram_id=telegram_id,
                state_name=state.stage.value,
                data=state.model_dump(mode="json"),
                version=1,
            )
            self.db.add(existing)
        else:
            existing.state_name = state.stage.value
            existing.data = state.model_dump(mode="json")
            existing.version += 1
        self.db.flush()
        return existing
