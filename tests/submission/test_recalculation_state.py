from pathlib import Path

from restaurant_bot.db_models import SubmissionRecord
from restaurant_bot.services.submission import SubmissionService


def test_recalculation_migration_is_chained_from_0006() -> None:
    """Проверяет цепочку ревизий надёжного состояния пересчёта."""
    migration = Path("alembic/versions/0007_recalculation_state.py").read_text(encoding="utf-8")

    assert 'revision = "0007"' in migration
    assert 'down_revision = "0006"' in migration


def test_recalculation_migration_backfills_legacy_boolean() -> None:
    """Проверяет безопасный backfill старого recalc_done."""
    migration = Path("alembic/versions/0007_recalculation_state.py").read_text(encoding="utf-8")

    assert "WHEN recalc_done THEN 'completed'" in migration
    assert "ELSE 'pending'" in migration
    assert 'op.drop_column("submission_records", "recalc_completed_at")' in migration
    assert 'op.drop_column("submission_records", "recalc_started_at")' in migration
    assert 'op.drop_column("submission_records", "recalc_operation_id")' in migration
    assert 'op.drop_column("submission_records", "recalc_status")' in migration


def test_submission_record_has_recalculation_lifecycle_fields() -> None:
    """Проверяет обязательные поля новой контрольной точки в ORM."""
    columns = SubmissionRecord.__table__.c

    assert columns.recalc_status.nullable is False
    assert columns.recalc_operation_id.nullable is False
    assert columns.recalc_started_at.nullable is True
    assert columns.recalc_completed_at.nullable is True


def test_recalculation_status_resolves_legacy_and_corrupt_records() -> None:
    """Проверяет безопасное разрешение старых и противоречивых статусов."""
    legacy_completed = type("Record", (), {"recalc_status": "", "recalc_done": True})()
    legacy_pending = type("Record", (), {"recalc_status": "", "recalc_done": False})()
    corrupt = type("Record", (), {"recalc_status": "completed", "recalc_done": False})()

    assert SubmissionService._recalc_status(legacy_completed) == "completed"
    assert SubmissionService._recalc_status(legacy_pending) == "pending"
    assert SubmissionService._recalc_status(corrupt) == "uncertain"
