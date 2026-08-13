"""Проверяет поведение, связанное с модулем «test completion notification state»."""

from pathlib import Path

from restaurant_bot.persistence.models import SubmissionRecord
from restaurant_bot.services.submission import SubmissionService


def test_completion_notification_migration_is_chained_from_0007() -> None:
    """Проверяет цепочку миграций контрольной точки уведомления."""
    migration = Path("alembic/versions/0008_completion_notification_state.py").read_text(
        encoding="utf-8"
    )

    assert 'revision = "0008"' in migration
    assert 'down_revision = "0007"' in migration


def test_completion_notification_migration_backfills_legacy_boolean() -> None:
    """Проверяет перенос старого boolean в безопасные статусы."""
    migration = Path("alembic/versions/0008_completion_notification_state.py").read_text(
        encoding="utf-8"
    )

    assert "WHEN completion_notified THEN 'completed'" in migration
    assert "ELSE 'pending'" in migration
    assert (
        'op.drop_column("submission_records", "completion_notification_completed_at")' in migration
    )
    assert 'op.drop_column("submission_records", "completion_notification_started_at")' in migration
    assert 'op.drop_column("submission_records", "completion_notification_status")' in migration
    assert 'op.drop_column("submission_records", "completion_notified")' not in migration


def test_submission_record_has_completion_notification_lifecycle_fields() -> None:
    """Проверяет поля жизненного цикла уведомления в ORM."""
    columns = SubmissionRecord.__table__.c

    assert columns.completion_notification_status.nullable is False
    assert columns.completion_notification_started_at.nullable is True
    assert columns.completion_notification_completed_at.nullable is True


def test_completion_notification_status_preserves_legacy_and_rejects_contradictions() -> None:
    """Проверяет безопасную интерпретацию старых и противоречивых записей."""
    legacy_completed = type(
        "Record", (), {"completion_notification_status": "", "completion_notified": True}
    )()
    legacy_pending = type(
        "Record", (), {"completion_notification_status": "", "completion_notified": False}
    )()
    uncertain = type(
        "Record", (), {"completion_notification_status": "completed", "completion_notified": False}
    )()

    assert SubmissionService._completion_notification_status(legacy_completed) == "completed"
    assert SubmissionService._completion_notification_status(legacy_pending) == "pending"
    assert SubmissionService._completion_notification_status(uncertain) == "uncertain"
