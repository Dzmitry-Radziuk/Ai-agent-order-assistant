"""Создаёт начальную схему.

Идентификатор ревизии: 0001
Предыдущая ревизия:
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Применяет текущую миграцию базы данных."""
    op.create_table(
        "bot_sessions",
        sa.Column("telegram_id", sa.String(length=64), primary_key=True),
        sa.Column("state_name", sa.String(length=64), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "telegram_updates",
        sa.Column("update_id", sa.BigInteger(), primary_key=True),
        sa.Column("chat_id", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("state_applied", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reply_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("tasks_enqueued", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_telegram_updates_chat_id", "telegram_updates", ["chat_id"])
    op.create_index("ix_telegram_updates_status", "telegram_updates", ["status"])
    op.create_table(
        "submission_records",
        sa.Column("order_no", sa.String(length=64), primary_key=True),
        sa.Column("telegram_id", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("history_written", sa.Boolean(), nullable=False),
        sa.Column("catalog_updated", sa.Boolean(), nullable=False),
        sa.Column("recalc_done", sa.Boolean(), nullable=False),
        sa.Column("finalized", sa.Boolean(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_submission_records_telegram_id", "submission_records", ["telegram_id"])


def downgrade() -> None:
    """Откатывает текущую миграцию базы данных."""
    op.drop_index("ix_submission_records_telegram_id", table_name="submission_records")
    op.drop_table("submission_records")
    op.drop_index("ix_telegram_updates_status", table_name="telegram_updates")
    op.drop_index("ix_telegram_updates_chat_id", table_name="telegram_updates")
    op.drop_table("telegram_updates")
    op.drop_table("bot_sessions")
