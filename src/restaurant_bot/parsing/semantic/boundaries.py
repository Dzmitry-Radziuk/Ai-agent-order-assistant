"""Строит устойчивые ссылки на товары и проверяет их источниковые якоря."""

from __future__ import annotations

import re

from restaurant_bot.domain.models import ExtractedItem
from restaurant_bot.domain.text import clean_text, normalize_text
from restaurant_bot.domain.units import UNIT_ALIASES
from restaurant_bot.parsing.number_words import NUMBER_WORDS
from restaurant_bot.parsing.semantic.measurements import catalog_packaging_span
from restaurant_bot.parsing.semantic.models import SemanticItemReference

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
    """Считает число общих содержательных слов кандидата и ссылки товара."""
    return len(semantic_anchor_tokens(query) & reference.anchor_tokens)


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
