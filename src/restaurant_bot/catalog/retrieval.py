"""Формирует ограниченный и детерминированный список кандидатов."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from restaurant_bot.catalog.evidence import (
    _canonical_token,
    _product_identity_tokens,
    has_catalog_search_evidence,
    supplier_matches_hint,
    tokens,
)
from restaurant_bot.catalog.safety import (
    has_compatible_numeric_characteristics,
    has_conflicting_catalog_qualifiers,
)
from restaurant_bot.catalog.scoring import match_score
from restaurant_bot.domain.models import Candidate, CatalogProduct
from restaurant_bot.domain.text import normalize_text

_PERCENT_RE = re.compile(r"(?<!\w)(\d+(?:[.,]\d+)?)\s*%")

_CONVERSATIONAL_PRODUCT_ALIASES = {
    "картошка": "картофель",
    "картошки": "картофель",
    "картошку": "картофель",
    "картошке": "картофель",
    "картошкой": "картофель",
    "картошек": "картофель",
}


def rank_candidates(
    query: str,
    catalog: list[CatalogProduct],
    supplier_hint: str = "",
    limit: int = 5,
) -> list[Candidate]:
    """Ранжирует кандидатов из каталога."""
    supplier_matches = [
        product for product in catalog if supplier_matches_hint(product.supplier, supplier_hint)
    ]
    if supplier_hint and not supplier_matches:
        return []
    scoped_catalog = supplier_matches if supplier_hint else catalog
    token_frequency: dict[str, int] = {}
    for product in scoped_catalog:
        for token in {_canonical_token(token) for token in tokens(product.name)}:
            token_frequency[token] = token_frequency.get(token, 0) + 1
    ranked: list[Candidate] = []
    for product in scoped_catalog:
        score = match_score(
            query,
            product,
            supplier_hint,
            token_frequency=token_frequency,
        )
        # Слабого суммарного сходства недостаточно: перед показом уточнения
        # требуется настоящее совпадение токена.
        if score < 20 or not has_catalog_search_evidence(query, product):
            continue
        ranked.append(
            Candidate(
                product_id=product.product_id,
                name=product.name,
                supplier=product.supplier,
                unit=product.unit,
                score=round(score, 2),
                reason="deterministic",
            )
        )
    ranked.sort(key=lambda item: (-item.score, item.name))
    return ranked[:limit]


def rank_similar_candidates(
    query: str,
    catalog: list[CatalogProduct],
    supplier_hint: str = "",
    limit: int = 5,
) -> list[Candidate]:
    """Возвращает только безопасные похожие варианты для уточнения пользователем."""
    query_tokens = _product_identity_tokens(query)
    if not query_tokens:
        return []
    supplier_matches = [
        product for product in catalog if supplier_matches_hint(product.supplier, supplier_hint)
    ]
    if supplier_hint and not supplier_matches:
        return []
    scoped_catalog = supplier_matches if supplier_hint else catalog
    ranked: list[Candidate] = []
    for product in scoped_catalog:
        product_tokens = _product_identity_tokens(product.name)
        if not product_tokens:
            continue
        if not has_compatible_numeric_characteristics(query, product.name):
            continue
        if not _compatible_percentages(query, product.name):
            continue
        if has_conflicting_catalog_qualifiers(query, product.name):
            continue
        similarities: list[float] = []
        for query_token in query_tokens:
            matches = [
                _similarity_for_product_tokens(query_token, product_token)
                for product_token in product_tokens
                if len(query_token) >= 4
                and len(product_token) >= 4
                and _same_initial_or_conversational_alias(query_token, product_token)
            ]
            best = max(matches, default=0.0)
            if best < 0.72:
                break
            similarities.append(best)
        if len(similarities) != len(query_tokens):
            continue
        score = 20.0 + sum(similarities) / len(similarities) * 20.0
        ranked.append(
            Candidate(
                product_id=product.product_id,
                name=product.name,
                supplier=product.supplier,
                unit=product.unit,
                score=round(score, 2),
                reason="similar",
            )
        )
    ranked.sort(key=lambda item: (-item.score, item.name))
    return ranked[:limit]


def _same_initial_or_conversational_alias(query_token: str, product_token: str) -> bool:
    """Разрешает только подтверждённый разговорный синоним при поиске вариантов."""
    query = normalize_text(query_token)
    product = normalize_text(product_token)
    return (
        query[:1] == product[:1]
        or _CONVERSATIONAL_PRODUCT_ALIASES.get(query) == product
        or _CONVERSATIONAL_PRODUCT_ALIASES.get(product) == query
    )


def _similarity_for_product_tokens(query_token: str, product_token: str) -> float:
    """Считает похожесть слова товара без ослабления точного сопоставления."""
    query = normalize_text(query_token)
    product = normalize_text(product_token)
    if (
        _CONVERSATIONAL_PRODUCT_ALIASES.get(query) == product
        or _CONVERSATIONAL_PRODUCT_ALIASES.get(product) == query
    ):
        return 1.0
    return SequenceMatcher(None, query, product).ratio()


def _compatible_percentages(query: str, product_name: str) -> bool:
    """Не предлагает вариант с другой явно указанной процентной характеристикой."""
    query_values = {float(value.replace(",", ".")) for value in _PERCENT_RE.findall(query)}
    if not query_values:
        return True
    product_values = {float(value.replace(",", ".")) for value in _PERCENT_RE.findall(product_name)}
    return query_values <= product_values
