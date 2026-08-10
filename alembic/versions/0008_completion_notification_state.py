"""Добавляет безопасный жизненный цикл уведомления о завершении заявки.

Идентификатор ревизии: 0008
Предыдущая ревизия: 0007
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Добавляет контрольные точки уведомления и сохраняет старые заявки."""
    op.add_column(
        "submission_records",
        sa.Column(
            "completion_notification_status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
    )
    op.add_column(
        "submission_records",
        sa.Column(
            "completion_notification_started_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "submission_records",
        sa.Column(
            "completion_notification_completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.execute(
        sa.text(
            """
            UPDATE submission_records
            SET completion_notification_status = CASE
                WHEN completion_notified THEN 'completed'
                ELSE 'pending'
            END
            """
        )
    )


def downgrade() -> None:
    """Удаляет только контрольные точки уведомления о завершении."""
    op.drop_column("submission_records", "completion_notification_completed_at")
    op.drop_column("submission_records", "completion_notification_started_at")
    op.drop_column("submission_records", "completion_notification_status")
