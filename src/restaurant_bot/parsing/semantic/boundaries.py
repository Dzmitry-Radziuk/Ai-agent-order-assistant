"""Строит устойчивые ссылки на товары и проверяет их источниковые якоря."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from restaurant_bot.domain.models import ExtractedItem
from restaurant_bot.domain.text import clean_text, normalize_text
from restaurant_bot.domain.units import UNIT_ALIASES
from restaurant_bot.parsing.number_words import NUMBER_WORDS
from restaurant_bot.parsing.semantic.measurements import catalog_packaging_span
from restaurant_bot.parsing.semantic.models import SemanticItemReference


@dataclass(frozen=True, slots=True)
class ItemSourceSpan:
    """Хранит детерминированный участок исходной фразы для одной позиции."""

    start: int
    end: int
    text: str


_SOURCE_TOKEN_RE = re.compile(r"[^\W_]+", flags=re.UNICODE)


def _source_tokens(value: str) -> list[tuple[str, int, int]]:
    """Возвращает нормализованные слова с их исходными границами."""
    return [
        (normalize_text(match.group(0)), match.start(), match.end())
        for match in _SOURCE_TOKEN_RE.finditer(value)
    ]


def _token_matches(left: str, right: str) -> bool:
    """Проверяет точное или безопасное лексическое совпадение якоря."""
    if left == right:
        return True
    return (
        len(left) >= 4
        and len(right) >= 4
        and left[0] == right[0]
        and SequenceMatcher(None, left, right).ratio() >= 0.72
    )


def _anchor_start(
    query: str,
    source_tokens: list[tuple[str, int, int]],
    minimum_index: int,
) -> int | None:
    """Находит начало товарного якоря после предыдущей позиции."""
    query_tokens = [token for token, _, _ in _source_tokens(query)]
    anchor_tokens = semantic_anchor_tokens(query)
    if not query_tokens or not anchor_tokens:
        return None

    candidates: list[tuple[int, int, int]] = []
    for source_index in range(minimum_index, len(source_tokens)):
        source_token = source_tokens[source_index][0]
        if not any(_token_matches(source_token, token) for token in anchor_tokens):
            continue
        matched = 0
        query_index = 0
        for candidate_index in range(source_index, min(len(source_tokens), source_index + 32)):
            candidate_token = source_tokens[candidate_index][0]
            while (
                query_index < len(query_tokens) and query_tokens[query_index] not in anchor_tokens
            ):
                query_index += 1
            if query_index >= len(query_tokens):
                break
            if _token_matches(candidate_token, query_tokens[query_index]):
                matched += 1
                query_index += 1
        candidates.append((matched, -source_index, source_index))
    if not candidates:
        return None
    _, _, source_index = max(candidates)
    return source_index


def derive_item_source_spans(
    source_text: str,
    items: Sequence[Mapping[str, Any] | Any],
) -> tuple[ItemSourceSpan | None, ...]:
    """Разделяет исходную фразу по независимым товарным якорям."""
    source = str(source_text or "").strip()
    if not source or not items:
        return tuple(None for _ in items)
    if len(items) == 1:
        return (ItemSourceSpan(0, len(source), source),)

    tokens = _source_tokens(source)
    starts: list[int] = []
    minimum_index = 0
    for item in items:
        query = (
            item.product_query if hasattr(item, "product_query") else item.get("product_query", "")
        )
        start_index = _anchor_start(clean_text(query), tokens, minimum_index)
        if start_index is None:
            return tuple(None for _ in items)
        starts.append(start_index)
        minimum_index = start_index + 1

    adjusted_starts = [
        _extend_start_over_order_quantity(
            source,
            tokens,
            start_index,
            0 if index == 0 else starts[index - 1] + 1,
        )
        for index, start_index in enumerate(starts)
    ]
    spans: list[ItemSourceSpan] = []
    for index, start_index in enumerate(adjusted_starts):
        start = tokens[start_index][1]
        end = (
            tokens[adjusted_starts[index + 1]][1]
            if index + 1 < len(adjusted_starts)
            else len(source)
        )
        end = _trim_separator_connector(source, start, end)
        if index == len(starts) - 1:
            end = _trim_conflicting_detached_quantity(source, start, end)
        spans.append(ItemSourceSpan(start, end, source[start:end].strip(" ,;")))
    return tuple(spans)


def _extend_start_over_order_quantity(
    source: str,
    source_tokens: list[tuple[str, int, int]],
    anchor_index: int,
    minimum_index: int,
) -> int:
    """Включает непосредственно стоящее перед товаром явное количество в его source span."""
    if anchor_index <= minimum_index:
        return anchor_index

    previous_index = anchor_index - 1
    previous_token = source_tokens[previous_index][0]
    separator = source[source_tokens[previous_index][2] : source_tokens[anchor_index][1]]
    if separator and not separator.isspace():
        return anchor_index
    unit_tokens = {normalize_text(value) for value in UNIT_ALIASES}
    if previous_token in unit_tokens and previous_index > minimum_index:
        number_index = previous_index - 1
        number_token = source_tokens[number_index][0]
        if _is_explicit_number_token(number_token) and number_index >= minimum_index:
            return number_index

    if _is_explicit_number_token(previous_token) and previous_index >= minimum_index:
        return previous_index
    return anchor_index


def _is_explicit_number_token(value: str) -> bool:
    """Проверяет цифровое или словесное обозначение количества."""
    return bool(re.fullmatch(r"\d+(?:[,.]\d+)?", value)) or value in NUMBER_WORDS


def _trim_separator_connector(source: str, start: int, end: int) -> int:
    """Убирает союз после позиции, чтобы её последнее число осталось локальным."""
    fragment = source[start:end]
    match = re.search(
        r"\s*,?\s*(?:\u0438|\u0438\u043b\u0438|\u0430|\u0442\u0430\u043a\u0436\u0435)\s*$",
        fragment,
        flags=re.I,
    )
    return start + match.start() if match is not None else end


def _trim_conflicting_detached_quantity(source: str, start: int, end: int) -> int:
    """Оставляет detached quantity вне товара, если товар уже имеет свой заказ."""
    marker_re = re.compile(
        r"(?:^|[.!?])\s*(?:\u043d\u0443\u0436\u043d\u043e|\u043d\u0430\u0434\u043e|\u0437\u0430\u043a\u0430\u0436\w*|\u0434\u043e\u0431\u0430\u0432\w*)\b",
        flags=re.I,
    )
    from restaurant_bot.parsing.semantic.measurements import extract_semantic_facts

    for marker in marker_re.finditer(source, start, end):
        prefix = source[start : marker.start()].rstrip(" .!?;,")
        if any(fact.kind.value == "order_quantity" for fact in extract_semantic_facts(prefix)):
            return marker.start()
    return end


_NON_ANCHOR_WORDS = (
    set(UNIT_ALIASES)
    | set(NUMBER_WORDS)
    | {
        "и",
        "или",
        "либо",
        "на",
        "в",
        "во",
        "по",
        "с",
        "без",
        "упаковка",
        "упаковке",
        "упаковки",
        "коробка",
        "коробке",
        "коробки",
        "пачка",
        "пачке",
        "пачки",
        "пакет",
    }
)


def semantic_anchor_tokens(value: str) -> frozenset[str]:
    """Возвращает содержательные слова, способные подтвердить товарный якорь."""
    normalized = normalize_text(value)
    words = re.findall(r"[a-zа-яё]+", normalized, flags=re.I)
    return frozenset(
        word
        for word in words
        if len(word) > 1 and word not in _NON_ANCHOR_WORDS and not word.isdigit()
    )


def build_item_references(items: list[ExtractedItem]) -> tuple[SemanticItemReference, ...]:
    """Строит устойчивые ссылки по товарному запросу, а не по позиции AI-списка."""
    return tuple(
        SemanticItemReference(
            item_index=index,
            source_text=item.source_line,
            anchor_text=clean_text(item.product_query),
            anchor_tokens=semantic_anchor_tokens(item.product_query),
        )
        for index, item in enumerate(items)
    )


def query_anchor_overlap(query: str, reference: SemanticItemReference) -> int:
    """Считает совпавшие якоря с учётом безопасных окончаний слов."""
    query_tokens = semantic_anchor_tokens(query)
    return sum(
        1
        for query_token in query_tokens
        if any(
            _token_matches(query_token, reference_token)
            for reference_token in reference.anchor_tokens
        )
    )


def has_independent_product_anchor(
    query: str, references: tuple[SemanticItemReference, ...]
) -> bool:
    """Проверяет, подтверждает ли фрагмент хотя бы одну самостоятельную позицию."""
    return any(query_anchor_overlap(query, reference) > 0 for reference in references)


def is_catalog_tail_fragment(query: str, source_text: str) -> bool:
    """Отмечает хвост после фасовки, если в нём нет нового товарного якоря."""
    value = normalize_text(query).strip(" .,;:-—–")
    source = normalize_text(source_text)
    if not value or value not in source:
        return False
    span = catalog_packaging_span(source_text)
    if span is None:
        return value.startswith(("в упаковке", "в коробке", "упаковка", "коробка"))
    query_start = source.find(value)
    return query_start >= span[1] or value.startswith(
        ("в упаковке", "в коробке", "упаковка", "коробка")
    )
