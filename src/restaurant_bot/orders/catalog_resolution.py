"""Применяет результат каталога к позиции заказа без зависимости от канала."""

from __future__ import annotations

import re

from restaurant_bot.catalog.evidence import (
    has_complete_query_evidence,
    query_evidence_tokens,
    remove_phrase_overlap,
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
from restaurant_bot.domain.text import normalize_text
from restaurant_bot.domain.units import UNIT_ALIASES, normalize_unit
from restaurant_bot.parsing.numeric_ranges import numeric_range_spans
from restaurant_bot.parsing.products import parse_product_lines
from restaurant_bot.parsing.quantities import (
    has_explicit_order_marker,
    has_explicit_order_quantity,
    parse_quantity_unit,
)


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
        search_query = remove_phrase_overlap(item.source_query, item.comment)
        search = search_backend.search(
            search_query,
            supplier_hint=item.supplier_hint,
            search_scope=search_scope,
        )
        candidates = list(search.candidates)
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
                search_query = remove_phrase_overlap(item.source_query, item.comment)
                search = search_backend.search(
                    search_query,
                    supplier_hint=item.supplier_hint,
                    search_scope=search_scope,
                )
                candidates = list(search.candidates)
                item.candidates = candidates
                item.supplier_search_locked = search.supplier_search_locked
                if not search.found_in_scope:
                    item.status = ItemStatus.NOT_FOUND
                    return
        item.supplier_search_locked = False
        reconciliation_candidate: Candidate | None = None
        if candidates:
            primary = candidates[0]
            primary_evidence = query_evidence_tokens(item.source_query, primary.name)
            primary_decision = self.catalog_resolver.decide(
                item.source_query,
                candidates,
                comment=item.comment,
                packaging_text=item.packaging_text,
                packaging_role=item.packaging_role,
            )
            if (
                has_complete_query_evidence(item.source_query, primary.name)
                or primary_decision is CatalogDecision.AUTO_SELECT
                or (len(candidates) == 1 and len(primary_evidence) >= 2)
            ):
                reconciliation_candidate = primary
        self._sanitize_catalog_facts_before_resolution(item, reconciliation_candidate)
        decision = self.catalog_resolver.decide(
            item.source_query,
            candidates,
            comment=item.comment,
            packaging_text=item.packaging_text,
            packaging_role=item.packaging_role,
        )
        if decision is CatalogDecision.CLARIFY:
            item.status = ItemStatus.AMBIGUOUS
            return
        self.apply_catalog(item, candidates[0], catalog, catalog_search=search_backend)

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
        source = normalize_text(item.source_line)
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
        source = normalize_text(item.source_line)
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
        if item.quantity is not None and item.unit and not item.quantity_source:
            quantity_text = f"{item.quantity:g} {item.unit}"
            explicit_order = has_explicit_order_quantity(item.source_line, item.quantity)
            if (
                has_compatible_numeric_characteristics(quantity_text, product_name)
                and (item.packaging_role == "catalog_attribute" or not explicit_order)
            ) or (
                not explicit_order
                and item.source_line
                and normalize_text(item.source_line) == normalize_text(product_name)
            ):
                item.quantity = None
                item.unit = ""
        item.comment = remove_catalog_fact_comments(
            item.comment,
            item.source_query,
            product_name,
            source_line=item.source_line,
            include_source_query=False,
        )
        if not item.comment:
            item.comment_source = CommentSource.NONE

    @staticmethod
    def _reconcile_quantity_with_catalog_name(item: CartItem, product_name: str) -> None:
        """Отделяет количество заказа от фасовки в имени каталога."""
        if item.quantity_source:
            return
        if (
            item.quantity is not None
            and item.source_line
            and normalize_text(item.source_line) == normalize_text(product_name)
            and not has_explicit_order_quantity(item.source_line, item.quantity)
        ):
            item.quantity = None
            item.unit = ""
            return
        if numeric_range_spans(item.source_line):
            parsed_source = parse_product_lines(item.source_line)
            if len(parsed_source) == 1:
                parsed_item = parsed_source[0]
                if not has_explicit_order_quantity(item.source_line, item.quantity):
                    item.quantity = parsed_item.quantity
                    item.unit = parsed_item.unit if parsed_item.quantity is not None else ""
                return
            # Общая строка нескольких товаров уже обработана parser-ом.
            return
        source_tokens = re.findall(r"[a-zа-яё0-9%]+", normalize_text(item.source_line), flags=re.I)
        product_tokens = re.findall(r"[a-zа-яё0-9%]+", normalize_text(product_name), flags=re.I)
        if not source_tokens or not product_tokens or len(source_tokens) < len(product_tokens):
            return

        product_start = next(
            (
                index
                for index in range(len(source_tokens) - len(product_tokens) + 1)
                if source_tokens[index : index + len(product_tokens)] == product_tokens
            ),
            None,
        )
        if product_start is None:
            return

        before = " ".join(source_tokens[:product_start])
        after = " ".join(source_tokens[product_start + len(product_tokens) :])
        explicit_quantity: tuple[float | None, str] = (None, "")
        for outside_name in (after, before):
            quantity, unit = parse_quantity_unit(outside_name)
            if quantity is not None:
                explicit_quantity = (quantity, unit)
                break

        if item.quantity is None and explicit_quantity[0] is not None:
            item.quantity, item.unit = explicit_quantity
