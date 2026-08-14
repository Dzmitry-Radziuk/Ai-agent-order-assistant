"""Разрешает относительные даты history-запросов детерминированно."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from restaurant_bot.domain.history import HistoryDateReference
from restaurant_bot.domain.text import normalize_text

_EXPLICIT_DATE_RE = re.compile(
    r"\b(?P<day>\d{1,2})[./-](?P<month>\d{1,2})(?:[./-](?P<year>\d{2,4}))?\b"
)


def business_today(timezone_name: str = "Europe/Minsk") -> date:
    """Возвращает текущую дату по часовому поясу приложения."""
    return datetime.now(ZoneInfo(timezone_name)).date()


def date_reference_for(
    text: str,
    *,
    today: date | None = None,
    timezone_name: str = "Europe/Minsk",
) -> tuple[HistoryDateReference, date | None]:
    """Извлекает ссылку на сегодня, завтра или явную дату."""
    normalized = normalize_text(text)
    if re.search(r"\bсегодня\b", normalized):
        return HistoryDateReference.TODAY, None
    if re.search(r"\bзавтра\b", normalized):
        return HistoryDateReference.TOMORROW, None
    match = _EXPLICIT_DATE_RE.search(normalized)
    if not match:
        return HistoryDateReference.NONE, None
    day = int(match.group("day"))
    month = int(match.group("month"))
    raw_year = match.group("year")
    current_date = today or business_today(timezone_name)
    year = int(raw_year) if raw_year else current_date.year
    if raw_year and len(raw_year) == 2:
        year += 2000
    try:
        return HistoryDateReference.EXPLICIT, date(year, month, day)
    except ValueError:
        return HistoryDateReference.NONE, None


def resolve_date_reference(
    reference: HistoryDateReference,
    *,
    today: date,
    explicit_date: date | None = None,
) -> date | None:
    """Разрешает относительную ссылку относительно переданных часов приложения."""
    if reference is HistoryDateReference.TODAY:
        return today
    if reference is HistoryDateReference.TOMORROW:
        return today + timedelta(days=1)
    if reference is HistoryDateReference.EXPLICIT:
        return explicit_date
    return None


def parse_history_date(value: object) -> date | None:
    """Разбирает поддержанные текстовые форматы даты строки истории."""
    text = str(value or "").strip()
    for pattern in ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:10], pattern).date()
        except ValueError:
            continue
    return None


def parse_history_datetime(value: object) -> datetime | None:
    """Разбирает поддержанные форматы времени создания заявки."""
    text = str(value or "").strip()
    for pattern in (
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(text[:19], pattern)
        except ValueError:
            continue
    return None
