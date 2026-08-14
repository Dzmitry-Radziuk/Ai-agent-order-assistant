"""Определяет порты прикладного history use case."""

from __future__ import annotations

from typing import Protocol

from restaurant_bot.domain.history import HistoryRow


class HistoryReader(Protocol):
    """Читает строки истории в пределах одного заведения."""

    def read_history(self, spreadsheet_id: str, venue_name: str = "") -> list[HistoryRow]:
        """Возвращает строки только текущего заведения."""
