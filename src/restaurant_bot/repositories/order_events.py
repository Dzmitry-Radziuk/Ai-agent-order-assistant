"""Читает и сохраняет данные через репозиторий «order events»."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from restaurant_bot.observability import sanitize_log_value
from restaurant_bot.persistence.models import OrderEvent, TelegramUpdate

_MAX_DETAILS_DEPTH = 3
_MAX_DETAILS_ITEMS = 50
_MAX_DETAILS_STRING = 1000
_PRIVATE_AUDIT_KEYS = {"error", "error_message"}


def sanitize_audit_details(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Ограничивает размер метаданных аудита и удаляет секреты и пользовательский текст."""
    if depth >= _MAX_DETAILS_DEPTH:
        return "<max-depth>"
    if isinstance(value, dict):
        return {
            str(child_key)[:100]: sanitize_audit_details(
                child,
                key=str(child_key),
                depth=depth + 1,
            )
            for child_key, child in list(value.items())[:_MAX_DETAILS_ITEMS]
        }
    if isinstance(value, (list, tuple, set)):
        return [
            sanitize_audit_details(child, key=key, depth=depth + 1)
            for child in list(value)[:_MAX_DETAILS_ITEMS]
        ]
    privacy_key = "text" if key.lower() in _PRIVATE_AUDIT_KEYS else key
    sanitized = sanitize_log_value(value, key=privacy_key, include_user_content=False)
    if isinstance(sanitized, str) and len(sanitized) > _MAX_DETAILS_STRING:
        return f"{sanitized[:_MAX_DETAILS_STRING]}…<truncated>"
    return sanitized


class OrderEventRepository:
    """Записывает идемпотентные события заказа и очищает старые журналы."""

    def __init__(self, db: Session):
        """Инициализирует репозиторий."""
        self.db = db

    def append_once(
        self,
        *,
        idempotency_key: str,
        trace_id: str,
        event_type: str,
        telegram_user_id: str,
        telegram_chat_id: str,
        venue_code: str = "",
        order_no: str | None = None,
        status: str = "ok",
        details: dict[str, Any] | None = None,
    ) -> None:
        """Добавляет событие один раз, даже если Celery повторно доставит задачу."""
        if not trace_id:
            return
        statement = (
            insert(OrderEvent)
            .values(
                idempotency_key=idempotency_key[:128],
                trace_id=trace_id[:36],
                order_no=(order_no or "")[:64] or None,
                telegram_user_id=telegram_user_id[:64],
                telegram_chat_id=telegram_chat_id[:64],
                venue_code=venue_code[:32],
                event_type=event_type[:64],
                status=status[:32],
                details=sanitize_audit_details(details or {}),
            )
            .on_conflict_do_nothing(index_elements=[OrderEvent.idempotency_key])
        )
        self.db.execute(statement)

    def cleanup(self, *, event_retention_days: int, update_retention_days: int) -> tuple[int, int]:
        """Удаляет только устаревшие события и завершённые Telegram updates."""
        now = datetime.now(UTC)
        event_result = self.db.execute(
            delete(OrderEvent).where(
                OrderEvent.created_at < now - timedelta(days=event_retention_days)
            )
        )
        update_result = self.db.execute(
            delete(TelegramUpdate).where(
                TelegramUpdate.created_at < now - timedelta(days=update_retention_days),
                TelegramUpdate.status.in_(("done", "ignored", "failed")),
            )
        )
        return (
            int(getattr(event_result, "rowcount", 0) or 0),
            int(getattr(update_result, "rowcount", 0) or 0),
        )
