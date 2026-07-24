"""Добавляет ограниченный журнал жизненного цикла заказа.

Идентификатор ревизии: 0004
Предыдущая ревизия: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Добавляет trace ID заявки и таблицу ключевых событий заказа."""
    op.add_column(
        "submission_records",
        sa.Column("trace_id", sa.String(length=36), nullable=False, server_default=""),
    )
    op.create_index(
        "ix_submission_records_trace_id",
        "submission_records",
        ["trace_id"],
    )
    op.create_table(
        "order_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False, unique=True),
        sa.Column("trace_id", sa.String(length=36), nullable=False),
        sa.Column("order_no", sa.String(length=64), nullable=True),
        sa.Column("telegram_user_id", sa.String(length=64), nullable=False),
        sa.Column("telegram_chat_id", sa.String(length=64), nullable=False),
        sa.Column("venue_code", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="ok"),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_order_events_trace_id", "order_events", ["trace_id"])
    op.create_index("ix_order_events_order_no", "order_events", ["order_no"])
    op.create_index("ix_order_events_telegram_user_id", "order_events", ["telegram_user_id"])
    op.create_index("ix_order_events_telegram_chat_id", "order_events", ["telegram_chat_id"])
    op.create_index("ix_order_events_venue_code", "order_events", ["venue_code"])
    op.create_index("ix_order_events_event_type", "order_events", ["event_type"])
    op.create_index("ix_order_events_created_at", "order_events", ["created_at"])


def downgrade() -> None:
    """Удаляет журнал событий заказа."""
    op.drop_table("order_events")
    op.drop_index("ix_submission_records_trace_id", table_name="submission_records")
    op.drop_column("submission_records", "trace_id")
