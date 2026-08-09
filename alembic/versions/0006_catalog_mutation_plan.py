"""Добавляет сохранённый план и lifecycle каталожной мутации.

Идентификатор ревизии: 0006
Предыдущая ревизия: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Добавляет H1-поля с безопасным backfill старых записей."""
    op.add_column(
        "submission_records",
        sa.Column(
            "catalog_update_status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
    )
    op.add_column(
        "submission_records",
        sa.Column(
            "catalog_update_plan",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "submission_records",
        sa.Column(
            "catalog_update_operation_id",
            sa.String(length=128),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )
    op.add_column(
        "submission_records",
        sa.Column("catalog_update_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "submission_records",
        sa.Column("catalog_update_completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE submission_records
            SET catalog_update_status = CASE
                WHEN catalog_updated THEN 'completed'
                ELSE 'pending'
            END
            """
        )
    )


def downgrade() -> None:
    """Удаляет только H1-поля, не изменяя существующие данные заявки."""
    op.drop_column("submission_records", "catalog_update_completed_at")
    op.drop_column("submission_records", "catalog_update_started_at")
    op.drop_column("submission_records", "catalog_update_operation_id")
    op.drop_column("submission_records", "catalog_update_plan")
    op.drop_column("submission_records", "catalog_update_status")
