"""Формирует ограниченный и детерминированный список кандидатов."""

from __future__ import annotations

from restaurant_bot.catalog.evidence import (
    _canonical_token,
    has_catalog_search_evidence,
    supplier_matches_hint,
    tokens,
)
from restaurant_bot.catalog.scoring import match_score
from restaurant_bot.domain.models import Candidate, CatalogProduct


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
    scoped_catalog = supplier_matches or catalog
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
