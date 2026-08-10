"""Добавляет безопасный жизненный цикл пересчёта заявки.

Идентификатор ревизии: 0007
Предыдущая ревизия: 0006
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Добавляет контрольные точки пересчёта и сохраняет старые записи."""
    op.add_column(
        "submission_records",
        sa.Column(
            "recalc_status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
    )
    op.add_column(
        "submission_records",
        sa.Column(
            "recalc_operation_id",
            sa.String(length=128),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )
    op.add_column(
        "submission_records",
        sa.Column("recalc_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "submission_records",
        sa.Column("recalc_completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE submission_records
            SET recalc_status = CASE
                WHEN recalc_done THEN 'completed'
                ELSE 'pending'
            END
            """
        )
    )


def downgrade() -> None:
    """Удаляет только контрольные точки пересчёта."""
    op.drop_column("submission_records", "recalc_completed_at")
    op.drop_column("submission_records", "recalc_started_at")
    op.drop_column("submission_records", "recalc_operation_id")
    op.drop_column("submission_records", "recalc_status")
