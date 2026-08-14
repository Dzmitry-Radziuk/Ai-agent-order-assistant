"""Разбирает список товаров из одной строки листа «История»."""

from __future__ import annotations

import re

from restaurant_bot.domain.history import HistoryProductEntry, HistoryRow
from restaurant_bot.domain.text import clean_text

_ITEM_RE = re.compile(r"^\s*(?:(?:•|[-*])\s*|\d+[.)]\s*)?(?P<value>.+?)\s*$")
_LEGACY_SEPARATOR_RE = re.compile(r"\s+[—–]\s+")
_DISPLAY_TAIL_RE = re.compile(
    r"\s+-\s+\d+(?:[.,]\d+)?\s+"
    r"(?:шт|штук|кг|г|гр|л|мл|уп|упак(?:овка|овок)?|"
    r"пач(?:ка|ки)?|бут(?:ылка|ылок)?|ящик(?:а|ов)?|бан(?:ка|ок)?)"
    r"\s+-\s+\d+(?:[.,]\d+)?\s+руб(?:\.|лей|ля)?\s*$",
    re.IGNORECASE,
)
_DEPARTMENT_RE = re.compile(r"^(?:зал|бар|кухня|склад|товары)\s*:\s*$", re.IGNORECASE)


def _product_name(line: str) -> str:
    """Удаляет только подтверждённый display-tail количества."""
    match = _ITEM_RE.fullmatch(line)
    value = match.group("value") if match else line
    value = _DISPLAY_TAIL_RE.sub("", value)
    value = _LEGACY_SEPARATOR_RE.split(value, maxsplit=1)[0]
    return clean_text(value).strip("-—–: ")


def extract_history_product_name(line: str) -> str:
    """Извлекает название товара из строки списка истории."""
    clean_line = clean_text(line)
    if not clean_line or _DEPARTMENT_RE.fullmatch(clean_line):
        return ""
    name = _product_name(clean_line)
    return "" if _DEPARTMENT_RE.fullmatch(name) else name


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
        name = extract_history_product_name(clean_line)
        if not name:
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
    name = extract_history_product_name(row.product_list)
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
