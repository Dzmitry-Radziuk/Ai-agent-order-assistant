"""Чистые операции выбора позиции черновика и кандидата каталога."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from restaurant_bot.catalog.evidence import numeric_evidence
from restaurant_bot.catalog.safety import has_compatible_numeric_characteristics
from restaurant_bot.domain.models import (
    Candidate,
    CartItem,
    ConversationState,
    ItemStatus,
)
from restaurant_bot.domain.text import normalize_text
from restaurant_bot.domain.units import UNIT_ALIASES
from restaurant_bot.parsing.number_words import NUMBER_WORDS
from restaurant_bot.parsing.quantities import has_explicit_order_marker


class SelectionFailure(StrEnum):
    """Описывает безопасный отказ от выбора кандидата."""

    NO_CURRENT_ITEM = "no_current_item"
    INVALID_CANDIDATE = "invalid_candidate"


class CandidateReferenceStatus(StrEnum):
    """Описывает результат проверки фразы относительно карточки кандидатов."""

    UNIQUE = "unique"
    AMBIGUOUS = "ambiguous"
    NO_MATCH = "no_match"


@dataclass(frozen=True, slots=True)
class CandidateSelectionResult:
    """Возвращает выбранную позицию, кандидата или причину отказа."""

    item: CartItem | None = None
    candidate: Candidate | None = None
    failure: SelectionFailure | None = None


@dataclass(frozen=True, slots=True)
class CandidateReferenceResult:
    """Возвращает безопасную оценку фразы в пределах текущих кандидатов."""

    status: CandidateReferenceStatus
    candidate: Candidate | None = None


def _lexical_reference_text(value: str) -> str:
    """Удаляет числа и единицы, оставляя слова названия товара."""
    words = re.findall(r"[a-zа-яё0-9%]+", normalize_text(value), flags=re.I)
    return " ".join(
        word
        for word in words
        if word not in UNIT_ALIASES
        and word not in NUMBER_WORDS
        and not any(char.isdigit() for char in word)
    )


def _lexical_match_count(query: str, candidate: str) -> int:
    """Считает подтверждённые словесные признаки кандидата."""
    query_tokens = _lexical_reference_text(query).split()
    candidate_tokens = _lexical_reference_text(candidate).split()
    return sum(
        1
        for query_token in query_tokens
        if any(
            query_token == candidate_token or tokens_share_stem(query_token, candidate_token)
            for candidate_token in candidate_tokens
        )
    )


def resolve_candidate_reference(
    item: CartItem,
    source_text: str,
) -> CandidateReferenceResult:
    """Разрешает естественную фразу только среди показанных кандидатов."""
    source = normalize_text(source_text)
    if not source or not item.candidates:
        return CandidateReferenceResult(CandidateReferenceStatus.NO_MATCH)
    scored: list[tuple[int, Candidate]] = []
    for candidate in item.candidates:
        lexical_matches = _lexical_match_count(source, candidate.name)
        candidate_numbers = numeric_evidence(candidate.name)
        numeric_match = bool(candidate_numbers) and has_compatible_numeric_characteristics(
            candidate.name,
            source,
        )
        if lexical_matches < 1:
            continue
        if (
            lexical_matches < 2
            and not numeric_match
            and (numeric_evidence(source) or has_explicit_order_marker(source))
        ):
            continue
        score = lexical_matches * 20 + (15 if numeric_match else 0)
        scored.append((score, candidate))
    if not scored:
        return CandidateReferenceResult(CandidateReferenceStatus.NO_MATCH)
    scored.sort(key=lambda pair: pair[0], reverse=True)
    best_score = scored[0][0]
    best = [candidate for score, candidate in scored if score == best_score]
    if len(best) != 1:
        return CandidateReferenceResult(CandidateReferenceStatus.AMBIGUOUS)
    return CandidateReferenceResult(CandidateReferenceStatus.UNIQUE, best[0])


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


def _has_same_lexical_identity(query: str, candidate: str) -> bool:
    """Сравнивает полные словесные названия с учётом падежных окончаний."""
    query_tokens = _lexical_reference_text(query).split()
    candidate_tokens = _lexical_reference_text(candidate).split()
    if not query_tokens or len(query_tokens) != len(candidate_tokens):
        return False
    unmatched_tokens = candidate_tokens.copy()
    for query_token in query_tokens:
        match_index = next(
            (
                index
                for index, candidate_token in enumerate(unmatched_tokens)
                if query_token == candidate_token or tokens_share_stem(query_token, candidate_token)
            ),
            None,
        )
        if match_index is None:
            return False
        unmatched_tokens.pop(match_index)
    return True


def find_cart_item(state: ConversationState, target_query: str) -> CartItem | None:
    """Находит только одну однозначно названную активную позицию черновика."""
    if not target_query:
        return None
    active_rows = [row for row in state.cart if row.status is not ItemStatus.SKIPPED]
    by_id = next((row for row in active_rows if row.id == target_query), None)
    if by_id is not None:
        return by_id

    target = normalize_text(target_query)
    complete_matches = [
        row
        for row in active_rows
        if any(
            candidate and _has_same_lexical_identity(target, candidate)
            for candidate in (
                normalize_text(row.source_query),
                normalize_text(row.catalog_name),
            )
        )
    ]
    if len(complete_matches) == 1:
        return complete_matches[0]
    if len(complete_matches) > 1:
        return None

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
        if candidate_index < 0:
            reference = resolve_candidate_reference(item, query)
            if (
                reference.status is CandidateReferenceStatus.UNIQUE
                and reference.candidate is not None
            ):
                candidate_index = item.candidates.index(reference.candidate)
    if candidate_index < 0 or candidate_index >= len(item.candidates):
        return CandidateSelectionResult(
            item=item,
            failure=SelectionFailure.INVALID_CANDIDATE,
        )
    return CandidateSelectionResult(item=item, candidate=item.candidates[candidate_index])
