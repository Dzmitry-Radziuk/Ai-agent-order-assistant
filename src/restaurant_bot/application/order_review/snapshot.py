"""Координирует прикладные контракты «snapshot»."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from restaurant_bot.application.order_review.contracts import ReviewItem, ReviewSnapshot
from restaurant_bot.domain.models import CatalogProduct


def build_review_snapshot(
    products: Iterable[CatalogProduct],
    *,
    venue_code: str,
    venue_name: str,
    spreadsheet_id: str,
) -> ReviewSnapshot:
    """Строит неизменяемый снимок заявки из строк каталога с количеством."""
    items: list[ReviewItem] = []
    for product in products:
        quantities = (
            product.department_quantities.hall or 0,
            product.department_quantities.bar or 0,
            product.department_quantities.kitchen or 0,
        )
        quantity = sum(value for value in quantities if value > 0)
        if quantity <= 0:
            continue
        items.append(
            ReviewItem(
                product_id=product.product_id,
                name=product.name,
                supplier=product.supplier,
                unit=product.unit or "шт",
                quantity=quantity,
                comment=product.comment,
                department_quantities=product.department_quantities.model_copy(deep=True),
            )
        )
    payload = [
        {
            "product_id": item.product_id,
            "name": item.name,
            "supplier": item.supplier,
            "unit": item.unit,
            "quantity": item.quantity,
            "department_quantities": item.department_quantities.model_dump(),
            "comment": item.comment,
        }
        for item in items
    ]
    fingerprint = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ReviewSnapshot(
        venue_code=venue_code,
        venue_name=venue_name,
        spreadsheet_id=spreadsheet_id,
        items=tuple(items),
        fingerprint=fingerprint,
    )
