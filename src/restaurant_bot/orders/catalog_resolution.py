"""Применяет результат каталога к позиции заказа без зависимости от канала."""

from __future__ import annotations

import re

import structlog

from restaurant_bot.catalog.evidence import (
    has_complete_query_evidence,
    numeric_evidence,
    query_evidence_tokens,
    remove_phrase_overlap,
    tokens,
)
from restaurant_bot.catalog.resolver import CatalogDecision, CatalogResolver
from restaurant_bot.catalog.safety import has_compatible_numeric_characteristics
from restaurant_bot.catalog.search import CatalogSearch, ListCatalogSearch
from restaurant_bot.conversation.comments import (
    merge_comments,
    remove_catalog_fact_comments,
    remove_exact_comment_fragments,
)
from restaurant_bot.conversation.quantity_resolution import suggested_quantity_for_multiple
from restaurant_bot.domain.models import (
    Candidate,
    CartItem,
    CatalogProduct,
    CommentSource,
    ConversationState,
    ItemStatus,
    SearchScope,
)
from restaurant_bot.domain.text import clean_text, normalize_text
from restaurant_bot.domain.units import UNIT_ALIASES, normalize_unit
from restaurant_bot.input.photo_ingestion import canonical_photo_identity
from restaurant_bot.orders.quantity_provenance import (
    QuantityProvenance,
    reconcile_order_quantity_evidence,
)
from restaurant_bot.parsing.numeric_ranges import numeric_range_spans
from restaurant_bot.parsing.products import parse_product_lines
from restaurant_bot.parsing.quantities import has_explicit_order_marker

logger = structlog.get_logger(__name__)


def _semantic_source(item: CartItem) -> str:
    """Возвращает локальный span для semantic-проверок товара."""
    return item.source_span.strip() or item.source_line.strip()


def _catalog_search_query(item: CartItem) -> str:
    """Возвращает каноническое название товара для получения кандидатов."""
    source_query = item.source_query.strip()
    return remove_phrase_overlap(source_query or _semantic_source(item), item.comment)


def _format_numeric_evidence(value: object) -> str:
    """Представляет числовой признак в компактной форме для сверки каталога."""
    evidence = value
    upper_value = getattr(evidence, "upper_value", None)
    number = getattr(evidence, "value", None)
    unit = getattr(evidence, "unit", "")
    if number is None:
        return ""
    if upper_value is None:
        text = f"{number:g}".replace(".", ",")
    else:
        text = f"{number:g}-{upper_value:g}".replace(".", ",")
    return clean_text(f"{text}{unit}")


def _catalog_evidence_query(item: CartItem) -> str:
    """Добавляет к каноническому запросу только безопасные признаки источника."""
    query = _catalog_search_query(item)
    source = _semantic_source(item)
    if not source:
        return query

    deterministic = parse_product_lines(source)
    parsed = deterministic[0] if len(deterministic) == 1 else None
    if item.quantity_source or item.order_entry_type:
        return query
    if parsed is not None and parsed.packaging_role == "catalog_attribute":
        if not parsed.packaging_text:
            return query
        source = parsed.packaging_text
    elif parsed is not None and parsed.quantity is not None:
        return query

    source_entries = numeric_evidence(source)
    if not source_entries:
        return query

    removable_index: int | None = None
    if item.quantity is not None and item.unit:
        matching_indexes = [
            index
            for index, entry in enumerate(source_entries)
            if entry.upper_value is None
            and abs(entry.value - item.quantity) <= 1e-9
            and normalize_unit(entry.unit) == normalize_unit(item.unit)
        ]
        if matching_indexes:
            # В обычной фразе отдельное количество заказа стоит после названия.  # noqa: RUF003
            # Для повторяющегося числа это оставляет каталожное в источнике.
            removable_index = matching_indexes[-1]

    evidence_parts = [
        _format_numeric_evidence(entry)
        for index, entry in enumerate(source_entries)
        if index != removable_index
    ]
    evidence_parts = [part for part in evidence_parts if part]
    if not evidence_parts:
        return query
    return clean_text(f"{query} {' '.join(evidence_parts)}")


def _prioritize_catalog_evidence(
    candidates: list[Candidate],
    evidence_query: str,
) -> list[Candidate]:
    """Ставит выше кандидатов, подтверждённых числовыми признаками источника."""
    if not numeric_evidence(evidence_query):
        return candidates
    compatible = [
        candidate
        for candidate in candidates
        if has_compatible_numeric_characteristics(evidence_query, candidate.name)
    ]
    if not compatible or len(compatible) == len(candidates):
        return candidates
    return compatible


def _has_catalog_anchor(query: str, catalog: list[CatalogProduct]) -> bool:
    """Проверяет, есть ли в исходном запросе подтверждённое слово товара из каталога."""
    return any(query_evidence_tokens(query, product.name) for product in catalog)


def _looks_like_unanchored_gibberish(query: str) -> bool:
    """Выделяет многословный запрос без признаков осмысленного товара для уточнения."""
    return len(tokens(query)) >= 3


class CatalogResolutionService:
    """Применяет найденный каталог к позициям черновика заказа."""

    def __init__(self, catalog_resolver: CatalogResolver) -> None:
        """Сохраняет чистый resolver поиска и решений каталога."""
        self.catalog_resolver = catalog_resolver

    def match_item(
        self,
        item: CartItem,
        catalog: list[CatalogProduct] | None = None,
        search_scope: SearchScope | None = None,
        *,
        catalog_search: CatalogSearch | None = None,
    ) -> None:
        """Ищет кандидатов и применяет безопасное решение к позиции."""
        search_backend = catalog_search or ListCatalogSearch(self.catalog_resolver, catalog or ())
        self._remove_unanchored_supplier_hint(item)
        if self._apply_unique_photo_identity(item, catalog):
            return
        search_query = _catalog_search_query(item)
        evidence_query = _catalog_evidence_query(item)
        search = search_backend.search(
            search_query,
            supplier_hint=item.supplier_hint,
            search_scope=search_scope,
        )
        candidates = list(search.candidates)
        candidates = _prioritize_catalog_evidence(candidates, evidence_query)
        packaging_measurement = self._catalog_packaging_measurement(item, candidates)
        if packaging_measurement is not None:
            packaging_value, packaging_unit = packaging_measurement
            item.packaging_text = f"{packaging_value:g} {packaging_unit}"
            item.packaging_role = "catalog_attribute"
            item.packaging_confidence = max(item.packaging_confidence, 0.86)
            item.quantity = None
            item.unit = ""
        item.candidates = candidates
        item.supplier_search_locked = search.supplier_search_locked
        if not search.found_in_scope:
            if (
                catalog is not None
                and item.quantity is None
                and not _has_catalog_anchor(search_query, catalog)
                and _looks_like_unanchored_gibberish(search_query)
            ):
                item.status = ItemStatus.AMBIGUOUS
                return
            item.status = ItemStatus.NOT_FOUND
            return
        if not item.comment and len(candidates) >= 2:
            split = self.catalog_resolver.split_explicit_supplier_comment(
                item.source_query,
                candidates,
            )
            if split is not None:
                item.comment = merge_comments(item.comment, split.supplier_comment)
                item.comment_source = CommentSource.EXPLICIT_MARKER
                item.source_query = split.product_query
                search_query = _catalog_search_query(item)
                evidence_query = _catalog_evidence_query(item)
                search = search_backend.search(
                    search_query,
                    supplier_hint=item.supplier_hint,
                    search_scope=search_scope,
                )
                candidates = list(search.candidates)
                candidates = _prioritize_catalog_evidence(candidates, evidence_query)
                item.candidates = candidates
                item.supplier_search_locked = search.supplier_search_locked
                if not search.found_in_scope:
                    item.status = ItemStatus.NOT_FOUND
                    return
        item.supplier_search_locked = False
        reconciliation_candidate: Candidate | None = None
        if candidates:
            primary = candidates[0]
            primary_evidence = query_evidence_tokens(evidence_query, primary.name)
            primary_decision = self.catalog_resolver.decide(
                evidence_query,
                candidates,
                comment=item.comment,
                packaging_text=item.packaging_text,
                packaging_role=item.packaging_role,
            )
            if (
                has_complete_query_evidence(evidence_query, primary.name)
                or primary_decision is CatalogDecision.AUTO_SELECT
                or (len(candidates) == 1 and len(primary_evidence) >= 2)
            ):
                reconciliation_candidate = primary
        self._sanitize_catalog_facts_before_resolution(item, reconciliation_candidate)
        decision = self.catalog_resolver.decide(
            evidence_query,
            candidates,
            comment=item.comment,
            packaging_text=item.packaging_text,
            packaging_role=item.packaging_role,
        )
        if decision is CatalogDecision.CLARIFY:
            item.status = ItemStatus.AMBIGUOUS
            return
        self.apply_catalog(item, candidates[0], catalog, catalog_search=search_backend)

    def _apply_unique_photo_identity(
        self,
        item: CartItem,
        catalog: list[CatalogProduct] | None,
    ) -> bool:
        """Выбирает единственную точную строку venue-каталога до fuzzy-поиска."""
        if item.catalog_identity_provenance != "venue_table_exact_candidate" or catalog is None:
            return False
        identity = canonical_photo_identity(item.source_query)
        if not identity:
            return False
        matches = [
            product for product in catalog if canonical_photo_identity(product.name) == identity
        ]
        if len(matches) != 1:
            logger.info(
                "photo_exact_catalog_identity",
                exact_match_count=len(matches),
                decision="duplicate" if len(matches) > 1 else "fallback",
                reason="duplicate_exact_identity"
                if len(matches) > 1
                else "exact_identity_not_proven",
            )
            return False
        product = matches[0]
        candidate = Candidate(
            product_id=product.product_id,
            name=product.name,
            supplier=product.supplier,
            unit=product.unit,
            reason="venue_table_exact_identity",
        )
        item.candidates = [candidate]
        logger.info(
            "photo_exact_catalog_identity",
            exact_match_count=1,
            decision="exact_selected",
            reason="unique_normalized_full_identity",
        )
        self.apply_catalog(
            item,
            candidate,
            catalog,
        )
        return True

    def apply_catalog(
        self,
        item: CartItem,
        candidate: Candidate,
        catalog: list[CatalogProduct] | None = None,
        *,
        catalog_search: CatalogSearch | None = None,
    ) -> None:
        """Записывает поля каталога и итоговый статус в позицию."""
        product = (
            catalog_search.product_for(candidate)
            if catalog_search is not None
            else self.catalog_resolver.product_for(candidate, catalog or [])
        )
        if product is None:
            item.status = ItemStatus.NOT_FOUND
            return
        self._sanitize_catalog_facts_before_resolution(item, candidate)
        item.catalog_product_id = product.product_id
        item.catalog_name = product.name
        item.supplier = product.supplier
        item.catalog_unit = normalize_unit(product.unit)
        item.price = product.price
        item.minimum_multiple = product.minimum_multiple
        item.useful_volume = product.useful_volume
        item.supplier_minimum_amount = product.supplier_minimum_amount
        item.supplier_current_sum = product.supplier_current_sum
        item.existing_quantity = product.department_quantities.for_department(item.department) or 0
        self._reconcile_quantity_with_catalog_name(item, product.name)
        user_comment = item.comment
        item.catalog_comment = product.comment
        item.catalog_comment_source = (
            CommentSource.CATALOG if product.comment else CommentSource.NONE
        )
        item.comment = user_comment
        if not user_comment:
            item.comment_source = CommentSource.NONE

        if item.quantity is None:
            item.status = ItemStatus.MISSING_QTY
            return
        if (
            item.unit
            and item.catalog_unit
            and normalize_unit(item.unit) != normalize_unit(item.catalog_unit)
        ):
            # Произнесённая единица считается намерением пользователя.
            item.status = ItemStatus.UNIT_MISMATCH
            return
        elif not item.unit:
            item.unit = item.catalog_unit

        suggested = suggested_quantity_for_multiple(item)
        if suggested:
            item.suggested_quantity = suggested
        else:
            item.suggested_quantity = None
        item.status = ItemStatus.MATCHED

    def refresh_cart_order_values(
        self,
        state: ConversationState,
        catalog: list[CatalogProduct],
    ) -> None:
        """Обновляет актуальные суммы и комментарии выбранных товаров."""
        products_by_id = {product.product_id: product for product in catalog}
        for item in state.cart:
            if item.status != ItemStatus.MATCHED or not item.catalog_product_id:
                continue
            product = products_by_id.get(item.catalog_product_id)
            if product is None:
                continue
            item.supplier_current_sum = product.supplier_current_sum
            item.existing_quantity = (
                product.department_quantities.for_department(item.department) or 0
            )
            item.supplier_minimum_amount = product.supplier_minimum_amount
            item.minimum_multiple = product.minimum_multiple
            item.suggested_quantity = suggested_quantity_for_multiple(item)
            item.catalog_comment = product.comment
            item.catalog_comment_source = (
                CommentSource.CATALOG if product.comment else CommentSource.NONE
            )
            if not (
                item.source_line
                and item.comment_source
                in {
                    CommentSource.SEMANTIC,
                    CommentSource.EXPLICIT_MARKER,
                }
            ):
                item.comment = remove_exact_comment_fragments(item.comment, product.comment)
            if not item.comment:
                item.comment_source = CommentSource.NONE

    @staticmethod
    def _catalog_packaging_measurement(
        item: CartItem,
        candidates: list[Candidate],
    ) -> tuple[float, str] | None:
        """Находит фасовку, распознанную голосом как количество заказа."""
        if item.quantity is None or not item.unit or item.quantity_source:
            return None
        source = normalize_text(_semantic_source(item))
        if not source:
            source = normalize_text(item.source_query)
        if not source or has_explicit_order_marker(source) or numeric_range_spans(source):
            return None

        unit_pattern = "|".join(
            sorted((re.escape(unit) for unit in UNIT_ALIASES), key=len, reverse=True)
        )
        measurements = re.findall(
            rf"(?<![\w-])(?P<value>\d+(?:[,.]\d+)?)\s*"
            rf"(?P<unit>{unit_pattern})\b",
            source,
            flags=re.IGNORECASE,
        )
        if len(measurements) != 1:
            return None
        value, raw_unit = measurements[0]
        spoken_unit = normalize_unit(raw_unit)
        try:
            spoken_value = float(value.replace(",", "."))
        except ValueError:
            return None
        if abs(spoken_value - item.quantity) > 1e-9 or spoken_unit != item.unit:
            return None

        packaging_candidates = [
            candidate
            for candidate in candidates
            if has_compatible_numeric_characteristics(
                f"{spoken_value:g} {spoken_unit}", candidate.name
            )
            and normalize_unit(candidate.unit) != spoken_unit
        ]
        if not packaging_candidates:
            return None
        return spoken_value, spoken_unit

    @staticmethod
    def _remove_unanchored_supplier_hint(item: CartItem) -> None:
        """Удаляет неподтверждённую подсказку поставщика из хвоста строки."""
        hint = normalize_text(item.supplier_hint)
        source = normalize_text(_semantic_source(item))
        if not hint or not source:
            return
        explicit_supplier = re.search(
            r"\b(?:поставщик\w*|у\s+поставщик\w*|от\s+поставщик\w*|купить\s+у)\b",
            source,
            flags=re.I,
        )
        if explicit_supplier:
            return
        query = normalize_text(item.source_query)
        if query and query in source and source.find(query) < source.rfind(hint):
            item.supplier_hint = ""

    def _sanitize_catalog_facts_before_resolution(
        self,
        item: CartItem,
        candidate: Candidate | None,
    ) -> None:
        """Удаляет факты каталога из количества и комментария до решения."""
        product_name = candidate.name if candidate is not None else ""
        preserve_source_free_quantity = (
            not _semantic_source(item)
            and not item.quantity_source
            and has_complete_query_evidence(item.source_query, product_name)
        )
        if (
            item.quantity is not None
            and item.unit
            and candidate is not None
            and not preserve_source_free_quantity
        ):
            authorization = reconcile_order_quantity_evidence(
                _semantic_source(item) or item.source_query,
                item.quantity,
                item.unit,
                quantity_source=item.quantity_source,
                order_entry_type=item.order_entry_type,
                catalog_name=product_name,
                packaging_role=item.packaging_role,
            )
            if authorization.provenance is not QuantityProvenance.ORDER:
                item.quantity = None
                item.unit = ""
            else:
                item.quantity = authorization.quantity
                item.unit = authorization.unit
        item.comment = remove_catalog_fact_comments(
            item.comment,
            item.source_query,
            product_name,
            source_line=_semantic_source(item),
            include_source_query=False,
        )
        if not item.comment:
            item.comment_source = CommentSource.NONE

    @staticmethod
    def _reconcile_quantity_with_catalog_name(item: CartItem, product_name: str) -> None:
        """Отделяет количество заказа от фасовки в имени каталога."""
        if (
            not _semantic_source(item)
            and not item.quantity_source
            and item.quantity is not None
            and has_complete_query_evidence(item.source_query, product_name)
        ):
            return
        authorization = reconcile_order_quantity_evidence(
            _semantic_source(item) or item.source_query,
            item.quantity,
            item.unit,
            quantity_source=item.quantity_source,
            order_entry_type=item.order_entry_type,
            catalog_name=product_name,
            packaging_role=item.packaging_role,
        )
        if authorization.provenance is QuantityProvenance.ORDER:
            item.quantity = authorization.quantity
            item.unit = authorization.unit
        elif item.quantity is not None:
            item.quantity = None
            item.unit = ""
