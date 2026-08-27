"""Разрешает товарный запрос относительно каталога без изменения диалога."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from restaurant_bot.catalog.evidence import (
    has_complete_query_evidence,
    query_evidence_tokens,
    supplier_matches_hint,
    tokens,
    unverified_product_terms,
)
from restaurant_bot.catalog.retrieval import rank_candidates, rank_similar_candidates
from restaurant_bot.catalog.safety import (
    can_auto_select,
    has_compatible_numeric_characteristics,
    has_conflicting_catalog_qualifiers,
    has_unscoped_product_variant_qualifier,
    is_broad_category_query,
)
from restaurant_bot.domain.models import Candidate, CatalogProduct, SearchScope
from restaurant_bot.domain.text import normalize_text
from restaurant_bot.parsing.comment_policy import supplier_comment_start


class CatalogDecision(StrEnum):
    """Описывает безопасный результат проверки найденных кандидатов."""

    AUTO_SELECT = "auto_select"
    CLARIFY = "clarify"


@dataclass(frozen=True, slots=True)
class CatalogSearchResult:
    """Хранит кандидатов и границу разрешённой области поиска."""

    candidates: tuple[Candidate, ...]
    found_in_scope: bool
    supplier_search_locked: bool = False
    similar_only: bool = False


@dataclass(frozen=True, slots=True)
class CatalogQuerySplit:
    """Разделяет поисковое название и явно обозначенное пожелание."""

    product_query: str
    supplier_comment: str


class CatalogResolver:
    """Инкапсулирует поиск и hard veto автоматического выбора товара."""

    def search(
        self,
        query: str,
        catalog: list[CatalogProduct],
        supplier_hint: str = "",
        search_scope: SearchScope | None = None,
    ) -> CatalogSearchResult:
        """Ищет кандидатов только в разрешённой области каталога."""
        strict_supplier = bool(supplier_hint and search_scope != SearchScope.ANY_SUPPLIER)
        search_catalog = catalog
        if strict_supplier:
            search_catalog = [
                product
                for product in catalog
                if supplier_matches_hint(product.supplier, supplier_hint)
            ]
        effective_hint = supplier_hint if search_scope != SearchScope.ANY_SUPPLIER else ""
        candidates = rank_candidates(query, search_catalog, effective_hint)
        if strict_supplier:
            candidates = self._complete_candidates(query, candidates)
        if candidates:
            return CatalogSearchResult(tuple(candidates), found_in_scope=True)
        similar_candidates = rank_similar_candidates(query, search_catalog, effective_hint)
        if similar_candidates:
            return CatalogSearchResult(
                tuple(similar_candidates),
                found_in_scope=True,
                similar_only=True,
            )
        if not strict_supplier:
            return CatalogSearchResult((), found_in_scope=False)

        outside_candidates = self._complete_candidates(query, rank_candidates(query, catalog))
        return CatalogSearchResult(
            tuple(outside_candidates),
            found_in_scope=False,
            supplier_search_locked=True,
        )

    def canonical_supplier_hint(
        self,
        supplier_hint: str,
        catalog: list[CatalogProduct],
    ) -> str:
        """Подтверждает подсказку поставщика актуальными строками каталога."""
        hint = supplier_hint.strip()
        if not hint:
            return ""
        matches = {
            product.supplier.strip()
            for product in catalog
            if product.supplier.strip() and supplier_matches_hint(product.supplier, hint)
        }
        if not matches:
            return ""
        if len(matches) == 1:
            return matches.pop()
        return hint

    def split_explicit_supplier_comment(
        self,
        query: str,
        candidates: list[Candidate] | tuple[Candidate, ...],
    ) -> CatalogQuerySplit | None:
        """Отделяет пожелание только при подтверждённом языковом маркере."""
        if len(candidates) < 2:
            return None
        query_words = re.findall(r"[a-zа-яё0-9]+", normalize_text(query), flags=re.I)
        query_tokens = tokens(query)
        first_evidence = query_evidence_tokens(query, candidates[0].name)
        second_evidence = query_evidence_tokens(query, candidates[1].name)
        shared_category_evidence = first_evidence == second_evidence
        uniquely_supported_variant = len(first_evidence) >= 2 and len(first_evidence) > len(
            second_evidence
        )
        if not (
            first_evidence
            and (shared_category_evidence or uniquely_supported_variant)
            and len(first_evidence) < len(query_tokens)
            and not any(
                has_complete_query_evidence(query, candidate.name) for candidate in candidates
            )
        ):
            return None
        remaining_words = [word for word in query_words if word not in first_evidence]
        comment_start = supplier_comment_start(remaining_words)
        if comment_start is None:
            return None
        supplier_comment_words = remaining_words[comment_start:]
        product_extra_words = remaining_words[:comment_start]
        product_words = [
            word for word in query_words if word in first_evidence or word in product_extra_words
        ]
        return CatalogQuerySplit(
            product_query=" ".join(product_words),
            supplier_comment=" ".join(supplier_comment_words),
        )

    def decide(
        self,
        query: str,
        candidates: list[Candidate] | tuple[Candidate, ...],
        *,
        comment: str = "",
        packaging_text: str = "",
        packaging_role: str = "none",
    ) -> CatalogDecision:
        """Разрешает автоподстановку только после всех hard veto."""
        if not candidates:
            return CatalogDecision.CLARIFY
        if any(candidate.reason == "similar" for candidate in candidates):
            return CatalogDecision.CLARIFY
        primary = candidates[0]
        unverified_terms = unverified_product_terms(query, primary.name)
        must_clarify = (
            is_broad_category_query(query, list(candidates))
            or not can_auto_select(list(candidates))
            or not has_compatible_numeric_characteristics(query, primary.name)
            or (
                packaging_role == "catalog_attribute"
                and not has_compatible_numeric_characteristics(packaging_text, primary.name)
            )
            or has_conflicting_catalog_qualifiers(query, primary.name)
            or has_unscoped_product_variant_qualifier(comment)
            or packaging_role == "ambiguous"
            or (unverified_terms and supplier_comment_start(unverified_terms) is None)
        )
        if must_clarify:
            return CatalogDecision.CLARIFY
        return CatalogDecision.AUTO_SELECT

    @staticmethod
    def product_for(
        candidate: Candidate,
        catalog: list[CatalogProduct],
    ) -> CatalogProduct | None:
        """Возвращает строку каталога выбранного кандидата."""
        return next(
            (product for product in catalog if product.product_id == candidate.product_id),
            None,
        )

    @staticmethod
    def _complete_candidates(
        query: str,
        candidates: list[Candidate],
    ) -> list[Candidate]:
        """Оставляет кандидатов, подтверждающих все слова товарного запроса."""
        return [
            candidate
            for candidate in candidates
            if has_complete_query_evidence(query, candidate.name)
        ]
