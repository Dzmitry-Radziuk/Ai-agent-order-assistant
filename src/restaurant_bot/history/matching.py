"""Сопоставляет названия товаров пользователя с товарами истории."""

from __future__ import annotations

from restaurant_bot.domain.history import HistoryMatch, HistoryProductEntry
from restaurant_bot.domain.text import normalize_text
from restaurant_bot.parsing.history.normalization import history_stems


def score_product_query(query: str, product_name: str) -> tuple[float, list[str]]:
    """Возвращает детерминированный score и совпавшие лексемы."""
    query_normalized = normalize_text(query)
    product_normalized = normalize_text(product_name)
    if not query_normalized or not product_normalized:
        return 0.0, []
    if query_normalized == product_normalized:
        return 100.0, history_stems(query_normalized)
    query_tokens = history_stems(query_normalized)
    product_tokens = history_stems(product_normalized)
    if query_normalized in product_normalized:
        return 92.0, query_tokens
    if product_normalized in query_normalized:
        return 88.0, product_tokens
    overlap = [token for token in query_tokens if token in product_tokens]
    if not overlap:
        return 0.0, []
    coverage = len(overlap) / max(1, len(query_tokens))
    precision = len(overlap) / max(1, len(product_tokens))
    score = 55.0 + 30.0 * coverage + 10.0 * precision
    return round(score, 3), overlap


def match_product_query(
    query: str,
    entries: list[HistoryProductEntry],
    *,
    minimum_score: float = 60.0,
) -> list[HistoryMatch]:
    """Возвращает только доказанные lexical matches в порядке score."""
    matches: list[HistoryMatch] = []
    for entry in entries:
        score, tokens = score_product_query(query, entry.product_name)
        if score >= minimum_score:
            matches.append(
                HistoryMatch(
                    entry=entry,
                    query=query,
                    score=score,
                    matched_tokens=tokens,
                )
            )
    return sorted(
        matches, key=lambda match: (-match.score, normalize_text(match.entry.product_name))
    )


def equivalent_product_name(left: str, right: str) -> bool:
    """Проверяет, совпадают ли названия по нормализованным основам слов."""
    left_tokens = set(history_stems(left))
    right_tokens = set(history_stems(right))
    return bool(left_tokens and left_tokens == right_tokens)
