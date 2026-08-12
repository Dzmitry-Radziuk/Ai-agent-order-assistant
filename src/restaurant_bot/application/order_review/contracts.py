from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReviewItem:
    """Хранит одну активную позицию из текущего листа заявки."""

    product_id: str
    name: str
    supplier: str
    unit: str
    quantity: float
    comment: str = ""


@dataclass(frozen=True, slots=True)
class ReviewSnapshot:
    """Хранит проверяемый снимок текущей заявки заведения."""

    venue_code: str
    venue_name: str
    spreadsheet_id: str
    items: tuple[ReviewItem, ...]
    fingerprint: str

    @property
    def supplier_count(self) -> int:
        """Возвращает число поставщиков в текущей заявке."""
        return len({item.supplier for item in self.items if item.supplier})
