"""Формирует channel-neutral исход history-запроса."""

from __future__ import annotations

from datetime import date

from restaurant_bot.domain.history import (
    HistoryAnswer,
    HistoryAnswerKind,
    HistoryMatch,
    HistoryQuery,
)
from restaurant_bot.history.ranking import is_ambiguous, rank_matches


def build_history_answer(
    query: HistoryQuery,
    matches: list[HistoryMatch],
    *,
    history_row_count: int,
    expanded_product_count: int,
    had_matching_products: bool,
    target_date: date | None = None,
) -> HistoryAnswer:
    """Строит результат с отдельными исходами ambiguity, no-active и not-found."""
    ranked = rank_matches(matches)
    if history_row_count == 0 or expanded_product_count == 0:
        return HistoryAnswer(
            kind=HistoryAnswerKind.EMPTY,
            query=query,
            history_row_count=history_row_count,
            expanded_product_count=expanded_product_count,
            target_date=target_date,
        )
    if not ranked:
        kind = HistoryAnswerKind.NO_ACTIVE if had_matching_products else HistoryAnswerKind.NOT_FOUND
        return HistoryAnswer(
            kind=kind,
            query=query,
            history_row_count=history_row_count,
            expanded_product_count=expanded_product_count,
            target_date=target_date,
        )
    if is_ambiguous(ranked):
        return HistoryAnswer(
            kind=HistoryAnswerKind.AMBIGUOUS,
            query=query,
            matches=ranked[:3],
            alternatives=ranked[1:3],
            history_row_count=history_row_count,
            expanded_product_count=expanded_product_count,
            target_date=target_date,
        )
    return HistoryAnswer(
        kind=HistoryAnswerKind.RESULTS,
        query=query,
        matches=ranked[:8],
        history_row_count=history_row_count,
        expanded_product_count=expanded_product_count,
        target_date=target_date,
    )
