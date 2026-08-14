"""Читает только venue-scoped строки Google Sheets листа «История»."""

from __future__ import annotations

from typing import Any

from restaurant_bot.application.history.contracts import HistoryReader
from restaurant_bot.domain.history import HistoryRow
from restaurant_bot.domain.text import normalize_text
from restaurant_bot.integrations.google_sheets import GoogleSheetsGateway
from restaurant_bot.parsing.history.dates import parse_history_date, parse_history_datetime


class GoogleHistoryRepository(HistoryReader):
    """Адаптирует GoogleSheetsGateway к нейтральному history reader."""

    SHEET_NAME = "История"

    def __init__(self, sheets: GoogleSheetsGateway) -> None:
        """Сохраняет существующий Google Sheets adapter без нового источника данных."""
        self.sheets = sheets

    def read_history(self, spreadsheet_id: str, venue_name: str = "") -> list[HistoryRow]:
        """Читает лист «История» и отбрасывает строки других заведений."""
        raw_rows = self.sheets.read_rows(self.SHEET_NAME, spreadsheet_id)
        rows = [self._row(row) for row in raw_rows]
        expected = normalize_text(venue_name)
        if not expected:
            return rows
        with_venue = [row for row in rows if normalize_text(row.venue_name)]
        if not with_venue:
            return rows
        return [row for row in with_venue if normalize_text(row.venue_name) == expected]

    @staticmethod
    def _first(row: dict[str, Any], *keys: str) -> str:
        """Возвращает первое непустое значение из поддержанных alias-столбцов."""
        for key in keys:
            value = str(row.get(key) or "").strip()
            if value:
                return value
        return ""

    @classmethod
    def _row(cls, row: dict[str, Any]) -> HistoryRow:
        """Преобразует сырую строку таблицы в доказательный контракт."""
        delivery_text = cls._first(
            row,
            "Дата поставки",
            "Дата доставки",
            "Ожидаемая дата доставки",
            "delivery_date",
        )
        created_text = cls._first(row, "Время создания заявки", "Дата создания", "created_at")
        return HistoryRow(
            order_number=cls._first(row, "Номер заявки", "№ Заявки", "ID заявки", "order_no"),
            venue_name=cls._first(
                row,
                "Условное наз-ие заведения",
                "Условное название заведения",
                "Заведение",
                "venue_name",
            ),
            supplier=cls._first(
                row,
                "Условное название поставщика",
                "Основной поставщик (Условное наз-ие)",
                "Поставщик",
                "supplier",
            ),
            stage=cls._first(row, "Стадия", "Статус", "status"),
            delivery_date=parse_history_date(delivery_text),
            delivery_date_text=delivery_text,
            created_at=parse_history_datetime(created_text),
            created_at_text=created_text,
            product_list=cls._first(row, "Список товаров", "Товары", "product_list"),
            raw=dict(row),
        )
