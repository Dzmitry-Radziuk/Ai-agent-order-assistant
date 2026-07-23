"""Добавляет контроль доставки финального уведомления.

Идентификатор ревизии: 0003
Предыдущая ревизия: 0002
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Применяет текущую миграцию базы данных."""
    op.add_column(
        "submission_records",
        sa.Column(
            "completion_notified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    """Откатывает текущую миграцию базы данных."""
    op.drop_column("submission_records", "completion_notified")
