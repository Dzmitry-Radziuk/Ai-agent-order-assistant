"""Сохраняет совместимость импортов каталога и временно владеет помощником кратности заказа."""

from __future__ import annotations

import math

from restaurant_bot.catalog.evidence import (
    canonical_search_query,
    has_catalog_search_evidence,
    has_complete_query_evidence,
    query_evidence_tokens,
    supplier_matches_hint,
    tokens,
    unverified_product_terms,
)
from restaurant_bot.catalog.retrieval import rank_candidates
from restaurant_bot.catalog.safety import (
    can_auto_select,
    has_compatible_numeric_characteristics,
    has_conflicting_catalog_qualifiers,
    has_product_variant_qualifier,
    has_unscoped_product_variant_qualifier,
    is_broad_category_query,
    is_safe_catalog_name_equivalent,
)
from restaurant_bot.catalog.scoring import match_score


def nearest_valid_multiple(quantity: float, multiple: float | None) -> float | None:
    """Возвращает ближайшее допустимое кратное."""
    if not multiple or multiple <= 0:
        return None
    ratio = quantity / multiple
    nearest = math.ceil(ratio - 1e-9) * multiple
    if abs(nearest - quantity) < 1e-9:
        return None
    return round(nearest, 6)


__all__ = [
    "can_auto_select",
    "canonical_search_query",
    "has_catalog_search_evidence",
    "has_compatible_numeric_characteristics",
    "has_complete_query_evidence",
    "has_conflicting_catalog_qualifiers",
    "has_product_variant_qualifier",
    "has_unscoped_product_variant_qualifier",
    "is_broad_category_query",
    "is_safe_catalog_name_equivalent",
    "match_score",
    "nearest_valid_multiple",
    "query_evidence_tokens",
    "rank_candidates",
    "supplier_matches_hint",
    "tokens",
    "unverified_product_terms",
]
