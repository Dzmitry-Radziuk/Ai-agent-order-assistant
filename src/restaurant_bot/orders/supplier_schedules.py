"""Проверяет дни приёма заявки и доставки по расписанию поставщика."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, timedelta

from restaurant_bot.domain.models import CartItem
from restaurant_bot.domain.text import normalize_text

_WEEKDAYS = {
    "пн": 0,
    "понедельник": 0,
    "понедельника": 0,
    "вт": 1,
    "вторник": 1,
    "вторника": 1,
    "ср": 2,
    "среда": 2,
    "среду": 2,
    "среды": 2,
    "чт": 3,
    "четверг": 3,
    "четверга": 3,
    "пт": 4,
    "пятница": 4,
    "пятницу": 4,
    "пятницы": 4,
    "сб": 5,
    "суббота": 5,
    "субботу": 5,
    "субботы": 5,
    "вс": 6,
    "воскресенье": 6,
    "воскресенья": 6,
}


@dataclass(frozen=True, slots=True)
class SupplierScheduleWarning:
    """Описывает одно несовпадение заявки с графиком поставщика."""

    supplier: str
    product_names: tuple[str, ...]
    order_date: date | None = None
    nearest_order_date: date | None = None
    requested_delivery_date: date | None = None
    nearest_delivery_date: date | None = None


def _schedule_weekdays(value: str) -> frozenset[int]:
    """Извлекает только распознанные дни недели из ячейки графика."""
    tokens = re.findall(r"[a-zа-яё]+", normalize_text(value), flags=re.IGNORECASE)
    return frozenset(_WEEKDAYS[token] for token in tokens if token in _WEEKDAYS)


def _next_allowed_day(start: date, weekdays: frozenset[int]) -> date | None:
    """Возвращает ближайший следующий разрешённый день."""
    if not weekdays:
        return None
    for offset in range(1, 8):
        candidate = start + timedelta(days=offset)
        if candidate.weekday() in weekdays:
            return candidate
    return None


def _requested_delivery_date(comment: str, today: date) -> date | None:
    """Извлекает безопасно доказанную дату доставки из комментария."""
    text = normalize_text(comment)
    if not text:
        return None
    if "послезавтра" in text:
        return today + timedelta(days=2)
    if re.search(r"\bзавтра\b", text):
        return today + timedelta(days=1)
    if re.search(r"\bсегодня\b", text):
        return today

    iso = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", text)
    if iso is not None:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None

    dotted = re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](20\d{2}))?\b", text)
    if dotted is not None:
        year = int(dotted.group(3) or today.year)
        try:
            candidate = date(year, int(dotted.group(2)), int(dotted.group(1)))
        except ValueError:
            return None
        if dotted.group(3) is None and candidate < today:
            try:
                candidate = candidate.replace(year=year + 1)
            except ValueError:
                return None
        return candidate

    for token, weekday in _WEEKDAYS.items():
        if len(token) <= 2 or not re.search(rf"\b{re.escape(token)}\b", text):
            continue
        offset = (weekday - today.weekday()) % 7
        return today + timedelta(days=offset)
    return None


def supplier_schedule_warnings(
    items: list[CartItem],
    *,
    today: date,
) -> list[SupplierScheduleWarning]:
    """Возвращает только доказанные нарушения графика для активных товаров."""
    grouped: dict[
        tuple[str, str, str, date | None, date | None],
        list[str],
    ] = {}
    for item in items:
        order_days = _schedule_weekdays(item.supplier_order_schedule)
        delivery_days = _schedule_weekdays(item.supplier_delivery_schedule)
        requested_delivery = _requested_delivery_date(item.comment, today)

        invalid_order = bool(order_days) and today.weekday() not in order_days
        invalid_delivery = (
            requested_delivery is not None
            and bool(delivery_days)
            and requested_delivery.weekday() not in delivery_days
        )
        if not invalid_order and not invalid_delivery:
            continue

        nearest_order = _next_allowed_day(today, order_days) if invalid_order else None
        nearest_delivery = (
            _next_allowed_day(requested_delivery, delivery_days)
            if invalid_delivery and requested_delivery is not None
            else None
        )
        key = (
            item.supplier,
            item.supplier_order_schedule,
            item.supplier_delivery_schedule,
            nearest_order,
            requested_delivery if invalid_delivery else None,
        )
        grouped.setdefault(key, []).append(item.catalog_name or item.source_query)

    warnings: list[SupplierScheduleWarning] = []
    for key, names in grouped.items():
        supplier, order_schedule, delivery_schedule, nearest_order, requested_delivery = key
        delivery_days = _schedule_weekdays(delivery_schedule)
        warnings.append(
            SupplierScheduleWarning(
                supplier=supplier,
                product_names=tuple(dict.fromkeys(names)),
                order_date=today if nearest_order is not None and order_schedule else None,
                nearest_order_date=nearest_order,
                requested_delivery_date=requested_delivery,
                nearest_delivery_date=(
                    _next_allowed_day(requested_delivery, delivery_days)
                    if requested_delivery is not None
                    else None
                ),
            )
        )
    return warnings


def supplier_schedule_fingerprint(items: list[CartItem], *, today: date) -> str:
    """Связывает одноразовое подтверждение графика с неизменённым черновиком и датой."""
    payload = [
        {
            "id": item.id,
            "product_id": item.catalog_product_id,
            "quantity": item.quantity,
            "unit": item.unit,
            "department": item.department,
            "department_quantities": item.department_quantities.model_dump(),
            "comment": item.comment,
            "order_schedule": item.supplier_order_schedule,
            "delivery_schedule": item.supplier_delivery_schedule,
        }
        for item in items
    ]
    raw = json.dumps(
        {"today": today.isoformat(), "items": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
