"""Добавляет привязки пользователей к заведениям.

Идентификатор ревизии: 0002
Предыдущая ревизия: 0001
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Применяет текущую миграцию базы данных."""
    op.create_table(
        "venue_bindings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("telegram_user_id", sa.String(length=64), nullable=False),
        sa.Column("telegram_chat_id", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("venue_code", sa.String(length=32), nullable=False),
        sa.Column("venue_name", sa.String(length=255), nullable=False),
        sa.Column("legal_name", sa.String(length=255), nullable=True),
        sa.Column("spreadsheet_id", sa.String(length=255), nullable=False),
        sa.Column("spreadsheet_url", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("sync_status", sa.String(length=32), nullable=False),
        sa.Column("sync_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "channel",
            "telegram_user_id",
            "telegram_chat_id",
            "venue_code",
            name="uq_venue_binding_identity",
        ),
    )
    op.create_index("ix_venue_bindings_user", "venue_bindings", ["telegram_user_id"])
    op.create_index("ix_venue_bindings_chat", "venue_bindings", ["telegram_chat_id"])
    op.create_index("ix_venue_bindings_code", "venue_bindings", ["venue_code"])
    op.create_index("ix_venue_bindings_active", "venue_bindings", ["is_active"])


def downgrade() -> None:
    """Откатывает текущую миграцию базы данных."""
    op.drop_index("ix_venue_bindings_active", table_name="venue_bindings")
    op.drop_index("ix_venue_bindings_code", table_name="venue_bindings")
    op.drop_index("ix_venue_bindings_chat", table_name="venue_bindings")
    op.drop_index("ix_venue_bindings_user", table_name="venue_bindings")
    op.drop_table("venue_bindings")
