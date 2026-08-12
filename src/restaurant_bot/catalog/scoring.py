"""Рассчитывает оценку совпадения товара с поисковым запросом."""

from __future__ import annotations

from collections.abc import Mapping
from difflib import SequenceMatcher

from restaurant_bot.catalog.evidence import (
    _canonical_token,
    _catalog_abbreviation_match,
    _compact_voice_name,
    _product_identity_tokens,
    supplier_matches_hint,
    tokens,
)
from restaurant_bot.domain.models import CatalogProduct
from restaurant_bot.text_normalization import normalize_text


def _strong_query_evidence_tokens(query: str, product_name: str) -> set[str]:
    """Возвращает точные и канонические признаки без fuzzy-совпадений."""
    product_tokens = _product_identity_tokens(product_name)
    return {
        query_token
        for query_token in _product_identity_tokens(query)
        if any(
            _canonical_token(query_token) == _canonical_token(product_token)
            or _catalog_abbreviation_match(query_token, product_token)
            for product_token in product_tokens
        )
    }


def match_score(
    query: str,
    product: CatalogProduct,
    supplier_hint: str = "",
    *,
    token_frequency: Mapping[str, int] | None = None,
) -> float:
    """Рассчитывает оценку совпадения товара."""
    q = normalize_text(query)
    name = normalize_text(product.name)
    if not q or not name:
        return 0
    if q == name:
        base = 100.0
    else:
        q_tokens = tokens(q)
        n_tokens = tokens(name)
        intersection = len(q_tokens & n_tokens)
        union = len(q_tokens | n_tokens) or 1
        jaccard = intersection / union
        containment = intersection / max(1, len(q_tokens))
        sequence = SequenceMatcher(None, q, name).ratio()
        fuzzy_token = max(
            (
                SequenceMatcher(None, query_token, name_token).ratio()
                for query_token in q_tokens
                for name_token in n_tokens
            ),
            default=0.0,
        )
        substring = 1.0 if q in name or name in q else 0.0
        compact_query = _compact_voice_name(q)
        compact_name = _compact_voice_name(name)
        compact_similarity = (
            SequenceMatcher(None, compact_query, compact_name).ratio()
            if len(compact_query) >= 6 and len(compact_name) >= 6
            else 0.0
        )
        strong_evidence = _strong_query_evidence_tokens(q, name)
        evidence_coverage = (
            len(strong_evidence) / max(1, len(q_tokens))
            if len(q_tokens) == 1 or len(strong_evidence) >= 2
            else 0.0
        )
        rarity_bonus = 0.0
        if token_frequency and len(q_tokens) >= 2 and len(strong_evidence) >= 2:
            rarity_bonus = (
                48
                * sum(
                    1 / max(1, token_frequency.get(_canonical_token(token), 1))
                    for token in strong_evidence
                )
                / max(1, len(q_tokens))
            )
        base = (
            45 * containment
            + 25 * jaccard
            + 25 * sequence
            + 15 * fuzzy_token
            + 5 * substring
            + 18 * evidence_coverage
            + rarity_bonus
        )
        # Этого достаточно для передачи слияния слов в AI-резолвер, но ниже
        # порога автоматического выбора.
        if compact_similarity >= 0.77:
            base = max(base, 55 * compact_similarity)
    if supplier_matches_hint(product.supplier, supplier_hint):
        base += 8
    return min(100.0, base)
