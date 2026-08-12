"""Чистые операции выбора позиции черновика и кандидата каталога."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from restaurant_bot.domain.models import (
    Candidate,
    CartItem,
    ConversationState,
    ItemStatus,
)
from restaurant_bot.services.text import normalize_text


class SelectionFailure(StrEnum):
    """Описывает безопасный отказ от выбора кандидата."""

    NO_CURRENT_ITEM = "no_current_item"
    INVALID_CANDIDATE = "invalid_candidate"


@dataclass(frozen=True, slots=True)
class CandidateSelectionResult:
    """Возвращает выбранную позицию, кандидата или причину отказа."""

    item: CartItem | None = None
    candidate: Candidate | None = None
    failure: SelectionFailure | None = None


def contains_score(target: str, candidate: str) -> int:
    """Оценивает совпадение названий по прежним детерминированным правилам."""
    if not target or not candidate:
        return 0
    if target == candidate:
        return 100
    if target in candidate or candidate in target:
        return 80
    target_tokens = re.findall(r"[a-zа-яё0-9%]+", target, flags=re.I)
    candidate_tokens = re.findall(r"[a-zа-яё0-9%]+", candidate, flags=re.I)
    if not target_tokens or not candidate_tokens:
        return 0
    exact_matches = 0
    inflected_matches = 0
    for target_token in target_tokens:
        if target_token in candidate_tokens:
            exact_matches += 1
            continue
        if any(
            tokens_share_stem(target_token, candidate_token) for candidate_token in candidate_tokens
        ):
            inflected_matches += 1
    return exact_matches * 12 + inflected_matches * 10


def tokens_share_stem(left: str, right: str) -> bool:
    """Сравнивает формы слова без агрессивного морфологического угадывания."""
    shorter_length = min(len(left), len(right))
    if shorter_length < 3:
        return False
    common_length = 0
    for left_char, right_char in zip(left, right, strict=False):
        if left_char != right_char:
            break
        common_length += 1
    if shorter_length <= 4:
        return common_length >= 3 and abs(len(left) - len(right)) <= 2
    return common_length >= 4


def find_cart_item(state: ConversationState, target_query: str) -> CartItem | None:
    """Находит только одну однозначно названную активную позицию черновика."""
    if not target_query:
        return None
    active_rows = [row for row in state.cart if row.status is not ItemStatus.SKIPPED]
    by_id = next((row for row in active_rows if row.id == target_query), None)
    if by_id is not None:
        return by_id

    target = normalize_text(target_query)
    scored = sorted(
        (
            (
                max(
                    contains_score(target, normalize_text(row.source_query)),
                    contains_score(target, normalize_text(row.catalog_name)),
                ),
                row,
            )
            for row in active_rows
        ),
        key=lambda pair: pair[0],
        reverse=True,
    )
    if not scored or scored[0][0] <= 0:
        return None
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][1]


def resolve_candidate_selection(
    state: ConversationState,
    *,
    target_item_index: int | None = None,
    selected_candidate_number: int | None = None,
    selection_query: str = "",
) -> CandidateSelectionResult:
    """Однозначно выбирает кандидата без формирования пользовательского ответа."""
    item = state.current_item()
    if target_item_index is not None:
        item = state.cart[target_item_index] if 0 <= target_item_index < len(state.cart) else None
    if item is None or item.status is not ItemStatus.AMBIGUOUS:
        return CandidateSelectionResult(failure=SelectionFailure.NO_CURRENT_ITEM)

    candidate_index = (selected_candidate_number or 0) - 1
    if selection_query:
        query = normalize_text(selection_query)
        scores = [
            contains_score(query, normalize_text(candidate.name)) for candidate in item.candidates
        ]
        best_score = max(scores, default=0)
        candidate_index = (
            scores.index(best_score) if best_score > 0 and scores.count(best_score) == 1 else -1
        )
    if candidate_index < 0 or candidate_index >= len(item.candidates):
        return CandidateSelectionResult(
            item=item,
            failure=SelectionFailure.INVALID_CANDIDATE,
        )
    return CandidateSelectionResult(item=item, candidate=item.candidates[candidate_index])
