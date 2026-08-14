"""Разбирает список товаров из одной строки листа «История»."""

from __future__ import annotations

import re

from restaurant_bot.domain.history import HistoryProductEntry, HistoryRow
from restaurant_bot.domain.text import clean_text

_ITEM_RE = re.compile(r"^\s*(?:(?:•|[-*])\s*|\d+[.)]\s*)?(?P<value>.+?)\s*$")
_SEPARATOR_RE = re.compile(r"\s+[—–]\s+")
_DEPARTMENT_RE = re.compile(r"^(?:зал|бар|кухня|склад|товары)\s*:\s*$", re.IGNORECASE)


def _product_name(line: str) -> str:
    """Удаляет только подтверждённый display-tail количества."""
    match = _ITEM_RE.fullmatch(line)
    value = match.group("value") if match else line
    value = _SEPARATOR_RE.split(value, maxsplit=1)[0]
    return clean_text(value).strip("-—–: ")


def parse_history_product_list(row: HistoryRow) -> list[HistoryProductEntry]:
    """Разворачивает список товаров строки в отдельные записи с общими доказательствами."""
    text = clean_text(row.product_list)
    if not text:
        return []
    entries: list[HistoryProductEntry] = []
    for line in row.product_list.splitlines():
        clean_line = clean_text(line)
        if not clean_line or _DEPARTMENT_RE.fullmatch(clean_line):
            continue
        name = _product_name(clean_line)
        if not name or _DEPARTMENT_RE.fullmatch(name):
            continue
        entries.append(
            HistoryProductEntry(
                product_name=name,
                order_number=row.order_number,
                venue_name=row.venue_name,
                supplier=row.supplier,
                stage=row.stage,
                delivery_date=row.delivery_date,
                delivery_date_text=row.delivery_date_text,
                created_at=row.created_at,
                created_at_text=row.created_at_text,
                row=row,
            )
        )
    if entries:
        return entries
    name = _product_name(row.product_list)
    return (
        [
            HistoryProductEntry(
                product_name=name,
                order_number=row.order_number,
                venue_name=row.venue_name,
                supplier=row.supplier,
                stage=row.stage,
                delivery_date=row.delivery_date,
                delivery_date_text=row.delivery_date_text,
                created_at=row.created_at,
                created_at_text=row.created_at_text,
                row=row,
            )
        ]
        if name
        else []
    )
