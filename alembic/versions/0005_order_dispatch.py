"""Переводит отправку заявки на центральный Web App.

Идентификатор ревизии: 0005
Предыдущая ревизия: 0004
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Добавляет контрольные точки финальной отправки без опасного contract-шага."""
    op.add_column(
        "submission_records",
        sa.Column(
            "dispatch_started",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "submission_records",
        sa.Column(
            "dispatch_completed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "submission_records",
        sa.Column(
            "dispatch_uncertain_notified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "submission_records",
        sa.Column("external_order_no", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "submission_records",
        sa.Column("dispatch_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "submission_records",
        sa.Column("dispatch_completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_submission_records_external_order_no",
        "submission_records",
        ["external_order_no"],
    )


def downgrade() -> None:
    """Удаляет контрольные точки финальной отправки."""
    op.drop_index(
        "ix_submission_records_external_order_no",
        table_name="submission_records",
    )
    op.drop_column("submission_records", "dispatch_completed_at")
    op.drop_column("submission_records", "dispatch_started_at")
    op.drop_column("submission_records", "external_order_no")
    op.drop_column("submission_records", "dispatch_uncertain_notified")
    op.drop_column("submission_records", "dispatch_completed")
    op.drop_column("submission_records", "dispatch_started")
