"""Выбирает безопасные результаты уже после matching и status policy."""

from __future__ import annotations

from datetime import date

from restaurant_bot.domain.history import HistoryMatch
from restaurant_bot.history.matching import equivalent_product_name


def rank_matches(matches: list[HistoryMatch]) -> list[HistoryMatch]:
    """Сортирует совпадения по score и самой поздней известной поставке."""
    return sorted(
        matches,
        key=lambda match: (
            -match.score,
            -(match.entry.delivery_date or date.min).toordinal(),
            match.entry.product_name.casefold(),
        ),
    )


def is_ambiguous(matches: list[HistoryMatch], *, margin: float = 8.0) -> bool:
    """Не разрешает тихий выбор двух близких разных названий."""
    grouped: dict[str, list[HistoryMatch]] = {}
    for match in matches:
        grouped.setdefault(match.query, []).append(match)
    for group in grouped.values():
        if len(group) < 2:
            continue
        first, second = group[:2]
        if equivalent_product_name(first.entry.product_name, second.entry.product_name):
            continue
        if first.score - second.score < margin:
            return True
    return False
