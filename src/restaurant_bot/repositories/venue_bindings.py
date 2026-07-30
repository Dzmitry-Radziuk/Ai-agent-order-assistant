from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from restaurant_bot.db_models import VenueBinding


class VenueBindingRepository:
    """Сохраняет привязки пользователей к заведениям."""

    def __init__(self, db: Session):
        """Инициализирует компонент."""
        self.db = db

    def get_active(self, user_id: str, chat_id: str) -> VenueBinding | None:
        """Возвращает активную привязку пользователя и чата."""
        return self.db.scalar(
            select(VenueBinding).where(
                VenueBinding.channel == "telegram",
                VenueBinding.telegram_user_id == user_id,
                VenueBinding.telegram_chat_id == chat_id,
                VenueBinding.is_active.is_(True),
                VenueBinding.sync_status == "synced",
            )
        )

    def get_active_by_chat(self, chat_id: str) -> VenueBinding | None:
        """Возвращает активную привязку по чату."""
        return self.db.scalar(
            select(VenueBinding).where(
                VenueBinding.channel == "telegram",
                VenueBinding.telegram_chat_id == chat_id,
                VenueBinding.is_active.is_(True),
                VenueBinding.sync_status == "synced",
            )
        )

    def get_revoked(self, user_id: str, chat_id: str) -> VenueBinding | None:
        """Возвращает последнюю привязку, отключённую через центральный реестр."""
        return self.db.scalar(
            select(VenueBinding)
            .where(
                VenueBinding.channel == "telegram",
                VenueBinding.telegram_user_id == user_id,
                VenueBinding.telegram_chat_id == chat_id,
                VenueBinding.is_active.is_(False),
                VenueBinding.sync_status == "revoked",
            )
            .order_by(VenueBinding.updated_at.desc(), VenueBinding.id.desc())
            .limit(1)
        )

    def set_access(self, binding_id: int, *, active: bool) -> VenueBinding | None:
        """Включает или отзывает доступ для сохранённой привязки."""
        row = self.db.get(VenueBinding, binding_id)
        if row is None:
            return None
        row.is_active = active
        row.sync_status = "synced" if active else "revoked"
        row.sync_error = None
        self.db.flush()
        return row

    def bind(
        self,
        *,
        user_id: str,
        chat_id: str,
        username: str,
        venue_code: str,
        venue_name: str,
        legal_name: str,
        spreadsheet_id: str,
        spreadsheet_url: str,
    ) -> tuple[VenueBinding, list[VenueBinding], bool]:
        """Создаёт активную привязку пользователя к заведению."""
        rows = list(
            self.db.scalars(
                select(VenueBinding)
                .where(
                    VenueBinding.channel == "telegram",
                    VenueBinding.telegram_user_id == user_id,
                    VenueBinding.telegram_chat_id == chat_id,
                )
                .with_for_update()
            )
        )
        current = next((row for row in rows if row.venue_code == venue_code), None)
        created = current is None
        if current is None:
            current = VenueBinding(
                channel="telegram",
                telegram_user_id=user_id,
                telegram_chat_id=chat_id,
                username=username,
                venue_code=venue_code,
                venue_name=venue_name,
                legal_name=legal_name or None,
                spreadsheet_id=spreadsheet_id,
                spreadsheet_url=spreadsheet_url or None,
                is_active=True,
                sync_status="pending",
            )
            self.db.add(current)
        else:
            current.username = username
            current.venue_name = venue_name
            current.legal_name = legal_name or None
            current.spreadsheet_id = spreadsheet_id
            current.spreadsheet_url = spreadsheet_url or None
            current.is_active = True
            current.sync_status = "pending"
            current.sync_error = None

        deactivated = []
        for row in rows:
            if row is not current and row.is_active:
                row.is_active = False
                deactivated.append(row)
        self.db.flush()
        return current, deactivated, created

    def mark_sync(self, binding_id: int, status: str, error: str = "") -> None:
        """Сохраняет результат синхронизации привязки."""
        row = self.db.get(VenueBinding, binding_id)
        if row is None:
            return
        row.sync_status = status
        row.sync_error = error or None
        self.db.flush()

    def restore_after_sync_failure(
        self,
        *,
        binding_id: int,
        user_id: str,
        chat_id: str,
        previous_code: str,
        error: str,
    ) -> None:
        """Восстанавливает прежнюю привязку после ошибки синхронизации."""
        rows = list(
            self.db.scalars(
                select(VenueBinding)
                .where(
                    VenueBinding.channel == "telegram",
                    VenueBinding.telegram_user_id == user_id,
                    VenueBinding.telegram_chat_id == chat_id,
                )
                .with_for_update()
            )
        )
        failed = next((row for row in rows if row.id == binding_id), None)
        if failed is not None:
            failed.is_active = False
            failed.sync_status = "failed"
            failed.sync_error = error or None
        previous = next((row for row in rows if row.venue_code == previous_code), None)
        if previous is not None:
            previous.is_active = True
            previous.sync_status = "synced"
            previous.sync_error = None
        self.db.flush()
