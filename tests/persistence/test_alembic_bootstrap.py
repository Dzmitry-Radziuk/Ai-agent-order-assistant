"""Проверяет регистрацию ORM-моделей в метаданных Alembic."""

from restaurant_bot.persistence.alembic import target_metadata
from restaurant_bot.persistence.database import Base


def test_alembic_metadata_registers_current_orm_tables() -> None:
    """Проверяет, что Alembic видит все текущие таблицы ORM."""
    assert target_metadata is Base.metadata
    assert set(target_metadata.tables) == {
        "bot_sessions",
        "order_events",
        "submission_records",
        "telegram_updates",
        "venue_bindings",
    }
