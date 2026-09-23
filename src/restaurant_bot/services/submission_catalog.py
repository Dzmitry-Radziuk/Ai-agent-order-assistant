"""Хранит каноническую классификацию состояния обновления каталога."""

from __future__ import annotations

from restaurant_bot.persistence.models import SubmissionRecord


class SubmissionCatalogService:
    """Владеет чистым чтением checkpoint статуса каталога."""

    @staticmethod
    def _catalog_status(record: SubmissionRecord) -> str:
        """Возвращает канонический жизненный цикл изменения каталога."""
        status = getattr(record, "catalog_update_status", "")
        if status in {"pending", "started", "uncertain", "completed", "conflict"}:
            if status == "pending" and getattr(record, "catalog_updated", False):
                return "completed"
            return status
        return "completed" if getattr(record, "catalog_updated", False) else "pending"
