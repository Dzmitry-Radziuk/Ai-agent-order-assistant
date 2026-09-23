"""Применяет результат каталога к позиции заказа без зависимости от канала."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

import structlog

from restaurant_bot.catalog.evidence import (
    has_complete_query_evidence,
    has_sufficient_photo_identity,
    numeric_evidence,
    query_evidence_tokens,
    remove_phrase_overlap,
    tokens,
    unverified_product_terms,
)
from restaurant_bot.catalog.resolver import CatalogDecision, CatalogResolver
from restaurant_bot.catalog.safety import (
    has_compatible_numeric_characteristics,
    has_conflicting_catalog_qualifiers,
)
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
    ExtractedItem,
    ItemStatus,
    SearchScope,
)
from restaurant_bot.domain.text import clean_text, normalize_text
from restaurant_bot.domain.units import UNIT_ALIASES, normalize_unit
from restaurant_bot.input.photo_ingestion import canonical_photo_identity
from restaurant_bot.orders.quantity_provenance import (
    QuantityProvenance,
    has_independent_order_provenance,
    reconcile_order_quantity_evidence,
)
from restaurant_bot.parsing.numeric_ranges import numeric_range_spans
from restaurant_bot.parsing.products import parse_product_lines
from restaurant_bot.parsing.quantities import has_explicit_order_marker

logger = structlog.get_logger(__name__)

# Обычный путь оставляет прежние границы токенов: это важно для устойчивого
# сопоставления результатов vision с каталогом.  # noqa: RUF003
# Отдельный шаблон включается
# только для действительно склеенных OCR-фрагментов вроде «400грГорчица».  # noqa: RUF003
_SOURCE_TOKEN_RE = re.compile(r"[a-zа-яё0-9%]+", flags=re.IGNORECASE)
_COMPACT_SOURCE_TOKEN_RE = re.compile(
    r"[A-ZА-ЯЁ]?[a-zа-яё]+|[A-ZА-ЯЁ]+(?![a-zа-яё])|\d+|%",
)
_COMPACT_SOURCE_MARKER_RE = re.compile(
    r"(?<=[a-zа-яё])(?=[A-ZА-ЯЁ])|(?<![A-Za-zА-Яа-яЁё])\d+[A-Za-zА-Яа-яЁё]+",
)


@dataclass(frozen=True, slots=True)
class _CatalogSourceMatch:
    """Хранит точное вхождение полного названия каталога в исходную строку."""

    product: CatalogProduct
    start_token: int
    end_token: int


def _semantic_source(item: CartItem) -> str:
    """Возвращает локальный span для semantic-проверок товара."""
    return item.source_span.strip() or item.source_line.strip()


def _has_multiple_source_items(item: CartItem) -> bool:
    """Проверяет, содержит ли исходная строка несколько товарных позиций."""
    full_source = item.source_line.strip()
    local_source = _semantic_source(item)
    if (
        not full_source
        or not local_source
        or normalize_text(full_source) == normalize_text(local_source)
    ):
        return False
    return len(parse_product_lines(full_source)) > 1


def _quantity_authorization_source(item: CartItem) -> str:
    """Выбирает полный источник количества только для одной товарной позиции."""
    full_source = item.source_line.strip()
    local_source = _semantic_source(item)
    if full_source and not _has_multiple_source_items(item):
        return full_source
    return local_source or full_source


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
    if has_independent_order_provenance(
        item.quantity_source,
        "",
        item.order_entry_type,
    ):
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


def _ordered_text_tokens(value: str) -> list[str]:
    """Возвращает все нормализованные токены в исходном порядке."""
    pattern = _source_token_pattern(value)
    return [normalize_text(match.group()) for match in pattern.finditer(value)]


def _source_token_matches(value: str) -> list[re.Match[str]]:
    """Возвращает токены исходной строки вместе с исходными границами."""
    return list(_source_token_pattern(value).finditer(value))


def _source_token_pattern(value: str) -> re.Pattern[str]:
    """Выбирает специальную токенизацию только для склеенного OCR-текста."""
    if _COMPACT_SOURCE_MARKER_RE.search(value):
        return _COMPACT_SOURCE_TOKEN_RE
    return _SOURCE_TOKEN_RE


def _specific_catalog_name_tokens(product: CatalogProduct) -> tuple[str, ...]:
    """Возвращает полный токенный контур достаточно конкретного названия товара."""
    ordered = tuple(_ordered_text_tokens(product.name))
    return ordered if len(tokens(product.name)) >= 2 and len(ordered) >= 2 else ()


def _trim_recovered_source_segment(value: str) -> str:
    """Убирает только разделитель списка с хвоста восстановленного сегмента."""
    trimmed = value.strip(" \t,;—–-")
    return re.sub(r"\s+(?:и|а)$", "", trimmed, flags=re.IGNORECASE).strip()


class CatalogResolutionService:
    """Применяет найденный каталог к позициям черновика заказа."""

    def __init__(self, catalog_resolver: CatalogResolver) -> None:
        """Сохраняет чистый resolver поиска и решений каталога."""
        self.catalog_resolver = catalog_resolver
        self._compound_item_catalog: list[CatalogProduct] | None = None
        self._compound_item_index: dict[
            tuple[str, str], tuple[tuple[CatalogProduct, tuple[str, ...]], ...]
        ] = {}

    def recover_catalog_compound_items(
        self,
        items: list[ExtractedItem],
        catalog: list[CatalogProduct],
    ) -> list[ExtractedItem]:
        """Разделяет один склеенный AI-элемент только по полным совпадениям каталога."""
        if len(items) != 1 or not catalog:
            return items
        item = items[0]
        if item.catalog_identity_provenance or has_independent_order_provenance(
            item.quantity_source,
            item.order_entry_text,
            item.order_entry_type,
        ):
            return items

        source = item.source_span.strip() or item.source_line.strip()
        source_matches = _source_token_matches(source)
        source_tokens = [normalize_text(match.group()) for match in source_matches]
        if len(source_tokens) < 4:
            return items

        matches = self._compound_catalog_matches(source_tokens, catalog)
        selected = self._select_non_overlapping_catalog_matches(matches)
        if len(selected) < 2:
            return items

        recovered = self._build_recovered_compound_items(item, source, source_matches, selected)
        if len(recovered) < 2:
            return items
        logger.info(
            "catalog_identity_compound_item_split",
            recovered_item_count=len(recovered),
            selected_product_ids=[match.product.product_id for match in selected],
        )
        return recovered

    def _compound_catalog_matches(
        self,
        source_tokens: list[str],
        catalog: list[CatalogProduct],
    ) -> list[_CatalogSourceMatch]:
        """Находит полные названия каталога как последовательности токенов источника."""
        index = self._compound_item_index_for(catalog)
        matches: list[_CatalogSourceMatch] = []
        for start in range(len(source_tokens) - 1):
            candidates = index.get((source_tokens[start], source_tokens[start + 1]), ())
            for product, product_tokens in candidates:
                end = start + len(product_tokens)
                if source_tokens[start:end] != list(product_tokens):
                    continue
                matches.append(_CatalogSourceMatch(product, start_token=start, end_token=end))
        return matches

    def _compound_item_index_for(
        self,
        catalog: list[CatalogProduct],
    ) -> dict[tuple[str, str], tuple[tuple[CatalogProduct, tuple[str, ...]], ...]]:
        """Кэширует индекс первых двух токенов для редкой проверки склеенного списка."""
        if self._compound_item_catalog is catalog:
            return self._compound_item_index

        mutable_index: dict[tuple[str, str], list[tuple[CatalogProduct, tuple[str, ...]]]] = {}
        for product in catalog:
            product_tokens = _specific_catalog_name_tokens(product)
            if not product_tokens:
                continue
            first_pair = (product_tokens[0], product_tokens[1])
            mutable_index.setdefault(first_pair, []).append((product, product_tokens))
        self._compound_item_catalog = catalog
        self._compound_item_index = {key: tuple(value) for key, value in mutable_index.items()}
        return self._compound_item_index

    @staticmethod
    def _select_non_overlapping_catalog_matches(
        matches: list[_CatalogSourceMatch],
    ) -> list[_CatalogSourceMatch]:
        """Выбирает самый длинный доказанный товар в каждой позиции исходной строки."""
        by_start: dict[int, list[_CatalogSourceMatch]] = {}
        for match in matches:
            by_start.setdefault(match.start_token, []).append(match)

        selected: list[_CatalogSourceMatch] = []
        token_index = min(by_start, default=0)
        while token_index in by_start:
            candidate = min(
                by_start[token_index],
                key=lambda match: (
                    -(match.end_token - match.start_token),
                    match.product.name,
                    match.product.product_id,
                ),
            )
            selected.append(candidate)
            next_starts = [start for start in by_start if start >= candidate.end_token]
            if not next_starts:
                break
            token_index = min(next_starts)
        return selected

    @staticmethod
    def _build_recovered_compound_items(
        item: ExtractedItem,
        source: str,
        source_matches: list[re.Match[str]],
        matches: list[_CatalogSourceMatch],
    ) -> list[ExtractedItem]:
        """Создаёт локальные доказательства позиций, не копируя фасовку между товарами."""
        recovered: list[ExtractedItem] = []
        for index, match in enumerate(matches):
            start = source_matches[match.start_token].start()
            end = (
                source_matches[matches[index + 1].start_token].start()
                if index + 1 < len(matches)
                else len(source)
            )
            segment = _trim_recovered_source_segment(source[start:end])
            parsed = parse_product_lines(segment)
            if len(parsed) != 1:
                return []
            parsed_item = parsed[0]
            update: dict[str, object] = {
                "department": item.department,
                "supplier_hint": item.supplier_hint,
                "source_line": segment,
                "source_span": segment,
            }
            if index == len(matches) - 1:
                parsed_comment = parsed_item.comment or parsed_item.user_comment_to_supplier
                item_comment = item.comment or item.user_comment_to_supplier
                update.update(
                    {
                        "comment": merge_comments(parsed_comment, item_comment),
                        "user_comment_to_supplier": "",
                        "comment_source": (
                            item.comment_source if item_comment else parsed_item.comment_source
                        ),
                    }
                )
            recovered.append(parsed_item.model_copy(update=update))
        return recovered

    def merge_catalog_qualified_items(
        self,
        items: list[ExtractedItem],
        catalog: list[CatalogProduct],
        search_scope: SearchScope | None = None,
        *,
        supplier_hint: str = "",
    ) -> list[ExtractedItem]:
        """Объединяет хвост позиции с предыдущим товаром только по доказательству каталога."""
        if len(items) < 2 or not catalog:
            return items

        merged = list(items)
        index = 1
        while index < len(merged):
            previous = merged[index - 1]
            fragment = merged[index]
            candidate = self._qualified_fragment_candidate(
                previous,
                fragment,
                catalog,
                search_scope,
                supplier_hint=supplier_hint,
            )
            if candidate is None:
                index += 1
                continue
            merged[index - 1] = self._merge_qualified_fragment(previous, fragment, candidate)
            logger.info(
                "catalog_identity_fragment_merged",
                base_query=previous.product_query,
                fragment_query=fragment.product_query,
                selected_product_id=candidate.product_id,
            )
            del merged[index]
        return merged

    def _qualified_fragment_candidate(
        self,
        previous: ExtractedItem,
        fragment: ExtractedItem,
        catalog: list[CatalogProduct],
        search_scope: SearchScope | None,
        *,
        supplier_hint: str,
    ) -> CatalogProduct | None:
        """Находит товар, для которого два соседних запроса являются одной идентичностью."""
        previous_source = normalize_text(previous.source_line or previous.source_span)
        fragment_source = normalize_text(fragment.source_line or fragment.source_span)
        fragment_query = clean_text(fragment.product_query)
        previous_query = clean_text(previous.product_query)
        if (
            not previous_source
            or not fragment_source
            or not previous_query
            or not fragment_query
            or fragment.comment
            or fragment.user_comment_to_supplier
        ):
            return None

        hint = fragment.supplier_hint or previous.supplier_hint or supplier_hint
        base_result = self.catalog_resolver.search(
            previous_query,
            catalog,
            hint,
            search_scope,
        )
        fragment_result = self.catalog_resolver.search(
            fragment_query,
            catalog,
            hint,
            search_scope,
        )
        if (
            not base_result.found_in_scope
            or not fragment_result.found_in_scope
            or not base_result.candidates
            or not fragment_result.candidates
        ):
            return None
        if self._have_distinct_complete_catalog_identities(
            previous_query,
            base_result.candidates,
            fragment_query,
            fragment_result.candidates,
        ):
            return None
        combined_query = clean_text(f"{previous_query} {fragment_query}")
        combined_result = self.catalog_resolver.search(
            combined_query,
            catalog,
            hint,
            search_scope,
        )
        base_ids = {candidate.product_id for candidate in base_result.candidates}
        shared_ids = base_ids & {candidate.product_id for candidate in fragment_result.candidates}
        for candidate in combined_result.candidates:
            if candidate.product_id not in shared_ids:
                continue
            base_evidence = query_evidence_tokens(previous_query, candidate.name)
            fragment_evidence = query_evidence_tokens(fragment_query, candidate.name)
            combined_evidence = query_evidence_tokens(combined_query, candidate.name)
            if (
                fragment_evidence
                and fragment_evidence.isdisjoint(base_evidence)
                and len(combined_evidence) > len(base_evidence)
            ):
                if self._is_independent_quantity_item(fragment, fragment_query, candidate):
                    continue
                return next(
                    product for product in catalog if product.product_id == candidate.product_id
                )
        return None

    @staticmethod
    def _have_distinct_complete_catalog_identities(
        previous_query: str,
        previous_candidates: Sequence[Candidate],
        fragment_query: str,
        fragment_candidates: Sequence[Candidate],
    ) -> bool:
        """Не склеивает запросы, каждый из которых уже описывает отдельный товар."""
        previous_ids = {
            candidate.product_id
            for candidate in previous_candidates
            if has_complete_query_evidence(previous_query, candidate.name)
        }
        fragment_ids = {
            candidate.product_id
            for candidate in fragment_candidates
            if has_complete_query_evidence(fragment_query, candidate.name)
        }
        return bool(previous_ids and fragment_ids and previous_ids.isdisjoint(fragment_ids))

    @staticmethod
    def _is_independent_quantity_item(
        fragment: ExtractedItem,
        fragment_query: str,
        candidate: Candidate,
    ) -> bool:
        """Не объединяет самостоятельную многословную позицию с предыдущим товаром."""
        if fragment.quantity is None or not fragment.unit:
            return False
        if not has_complete_query_evidence(fragment_query, candidate.name):
            return False
        fragment_tokens = tokens(fragment_query)
        if len(fragment_tokens) <= 1:
            return False
        supplier_tokens = tokens(candidate.supplier)
        supplier_overlap = len(fragment_tokens & supplier_tokens)
        return not supplier_tokens or supplier_overlap * 2 < len(fragment_tokens)

    @staticmethod
    def _merge_qualified_fragment(
        previous: ExtractedItem,
        fragment: ExtractedItem,
        product: CatalogProduct,
    ) -> ExtractedItem:
        """Сохраняет одну позицию и переносит в неё подтверждённое количество хвоста."""
        source_values = [
            value.strip()
            for value in (previous.source_line, fragment.source_line)
            if value and value.strip()
        ]
        source = clean_text(" ".join(dict.fromkeys(source_values)))
        span_values = [
            value.strip()
            for value in (previous.source_span, fragment.source_span)
            if value and value.strip()
        ]
        source_span = clean_text(" ".join(dict.fromkeys(span_values)))
        source_for_quantity = source or source_span
        fragment_authorization = reconcile_order_quantity_evidence(
            source_for_quantity,
            fragment.quantity,
            fragment.unit,
            quantity_source=fragment.quantity_source,
            order_entry_text=fragment.order_entry_text,
            order_entry_type=fragment.order_entry_type,
            catalog_name=product.name,
            packaging_role=fragment.packaging_role,
            product_query=fragment.product_query,
        )
        previous_authorization = reconcile_order_quantity_evidence(
            source_for_quantity,
            previous.quantity,
            previous.unit,
            quantity_source=previous.quantity_source,
            order_entry_text=previous.order_entry_text,
            order_entry_type=previous.order_entry_type,
            catalog_name=product.name,
            packaging_role=previous.packaging_role,
            product_query=previous.product_query,
        )
        quantity = previous.quantity
        unit = previous.unit
        quantity_source = previous.quantity_source
        order_entry_text = previous.order_entry_text
        order_entry_type = previous.order_entry_type
        if previous_authorization.provenance is QuantityProvenance.ORDER:
            quantity = previous_authorization.quantity
            unit = previous_authorization.unit
        elif fragment_authorization.provenance is QuantityProvenance.ORDER:
            quantity = fragment_authorization.quantity
            unit = fragment_authorization.unit
            quantity_source = fragment.quantity_source
            order_entry_text = fragment.order_entry_text
            order_entry_type = fragment.order_entry_type
        return previous.model_copy(
            update={
                "product_query": clean_text(f"{previous.product_query} {fragment.product_query}"),
                "quantity": quantity,
                "unit": unit,
                "quantity_source": quantity_source,
                "order_entry_text": order_entry_text,
                "order_entry_type": order_entry_type,
                "source_line": source,
                "source_span": source_span,
            }
        )

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
        if search.similar_only:
            item.status = ItemStatus.AMBIGUOUS
            return
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
        """Разрешает proven venue-table identity до обычного fuzzy-поиска."""
        if item.catalog_identity_provenance != "venue_table_exact_candidate" or catalog is None:
            return False
        products_by_row: dict[int, list[CatalogProduct]] = {}
        for product in catalog:
            if product.row_number is not None:
                products_by_row.setdefault(product.row_number, []).append(product)

        row_number = item.photo_sheet_row_number
        if row_number is not None and item.photo_sheet_row_number_confidence >= 0.9:
            row_matches = products_by_row.get(row_number, [])
            if item.photo_sheet_row_number_authoritative and len(row_matches) == 1:
                if _has_verified_photo_identity(item.source_query, row_matches[0].name):
                    return self._select_photo_identity_product(
                        item,
                        row_matches[0],
                        method="authoritative_row_number",
                        reason="verified_sheet_row_and_product_identity",
                    )
                logger.warning(
                    "photo_venue_catalog_identity",
                    resolution_method="unsafe",
                    sheet_row_number=row_number,
                    candidate_count=1,
                    reason="authoritative_row_product_conflict",
                )
                item.photo_sheet_row_number_authoritative = False
            if item.photo_sheet_row_number_authoritative:
                # Номер строки может относиться к другому представлению листа,
                # поэтому отсутствие однозначной строки каталога не является
                # вариантом товара для выбора. Продолжаем безопасный поиск по
                # прочитанному названию, а не создаём пустую карточку выбора.  # noqa: RUF003
                item.photo_sheet_row_number_authoritative = False
                logger.info(
                    "photo_venue_catalog_identity",
                    resolution_method="fallback",
                    sheet_row_number=row_number,
                    candidate_count=len(row_matches),
                    reason="authoritative_row_missing_or_duplicate_fallback_to_name",
                )
            if len(row_matches) == 1 and _has_verified_photo_identity(
                item.source_query, row_matches[0].name
            ):
                return self._select_photo_identity_product(
                    item,
                    row_matches[0],
                    method="row_number",
                    reason="row_number_and_lexical_sanity",
                )
            logger.info(
                "photo_venue_catalog_identity",
                resolution_method="unsafe",
                sheet_row_number=row_number,
                candidate_count=len(row_matches),
                reason="row_number_lexical_mismatch_or_duplicate",
            )

        identity = canonical_photo_identity(item.source_query)
        if identity:
            exact_matches = [
                product for product in catalog if canonical_photo_identity(product.name) == identity
            ]
            if len(exact_matches) == 1:
                return self._select_photo_identity_product(
                    item,
                    exact_matches[0],
                    method="canonical_exact",
                    reason="unique_normalized_full_identity",
                )
            if len(exact_matches) > 1:
                logger.info(
                    "photo_venue_catalog_identity",
                    resolution_method="duplicate",
                    candidate_count=len(exact_matches),
                    reason="duplicate_exact_identity",
                )

        ocr_matches = [
            product
            for product in catalog
            if _has_verified_photo_identity(item.source_query, product.name)
        ]
        if len(ocr_matches) == 1:
            return self._select_photo_identity_product(
                item,
                ocr_matches[0],
                method="ocr_tolerant_unique",
                reason="unique_non_measurement_identity",
            )
        logger.info(
            "photo_venue_catalog_identity",
            resolution_method="fallback",
            sheet_row_number=row_number,
            candidate_count=len(ocr_matches),
            reason="identity_not_proven",
        )
        return False

    def _select_photo_identity_product(
        self,
        item: CartItem,
        product: CatalogProduct,
        *,
        method: str,
        reason: str,
    ) -> bool:
        """Применяет proven product без generic candidate или AI resolution."""
        candidate = Candidate(
            product_id=product.product_id,
            name=product.name,
            supplier=product.supplier,
            unit=product.unit,
            reason=f"venue_table_{method}",
        )
        item.candidates = [candidate]
        logger.info(
            "photo_venue_catalog_identity",
            resolution_method=method,
            sheet_row_number=item.photo_sheet_row_number,
            candidate_count=1,
            reason=reason,
            ignored_ocr_measurement_conflict=method == "ocr_tolerant_unique",
        )
        self.apply_catalog(item, candidate, [product])
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
        if _has_multiple_source_items(item) and item.packaging_role == "none":
            return None
        source = normalize_text(_quantity_authorization_source(item))
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
        authorization = reconcile_order_quantity_evidence(
            source,
            item.quantity,
            item.unit,
            quantity_source=item.quantity_source,
            order_entry_type=item.order_entry_type,
            catalog_name=packaging_candidates[0].name,
            packaging_role=item.packaging_role,
            product_query=item.source_query,
        )
        if authorization.provenance is QuantityProvenance.ORDER:
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
        preserve_multi_item_quantity = (
            _has_multiple_source_items(item) and item.packaging_role == "none"
        )
        if (
            item.quantity is not None
            and item.unit
            and candidate is not None
            and not preserve_source_free_quantity
            and not preserve_multi_item_quantity
        ):
            authorization = reconcile_order_quantity_evidence(
                _quantity_authorization_source(item) or item.source_query,
                item.quantity,
                item.unit,
                quantity_source=item.quantity_source,
                order_entry_type=item.order_entry_type,
                catalog_name=product_name,
                packaging_role=item.packaging_role,
                product_query=item.source_query,
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
            source_line=_quantity_authorization_source(item),
            include_source_query=False,
        )
        if not item.comment:
            item.comment_source = CommentSource.NONE

    @staticmethod
    def _reconcile_quantity_with_catalog_name(item: CartItem, product_name: str) -> None:
        """Отделяет количество заказа от фасовки в имени каталога."""
        if _has_multiple_source_items(item) and item.packaging_role == "none":
            return
        if (
            not _quantity_authorization_source(item)
            and not item.quantity_source
            and item.quantity is not None
            and has_complete_query_evidence(item.source_query, product_name)
        ):
            return
        authorization = reconcile_order_quantity_evidence(
            _quantity_authorization_source(item) or item.source_query,
            item.quantity,
            item.unit,
            quantity_source=item.quantity_source,
            order_entry_type=item.order_entry_type,
            catalog_name=product_name,
            packaging_role=item.packaging_role,
            product_query=item.source_query,
        )
        if authorization.provenance is QuantityProvenance.ORDER:
            item.quantity = authorization.quantity
            item.unit = authorization.unit
        elif item.quantity is not None:
            item.quantity = None
            item.unit = ""


def _has_verified_photo_identity(query: str, product_name: str) -> bool:
    """Подтверждает фото-товар без потери существенного признака названия."""
    return (
        has_sufficient_photo_identity(query, product_name)
        and not unverified_product_terms(query, product_name)
        and not has_conflicting_catalog_qualifiers(query, product_name)
    )
