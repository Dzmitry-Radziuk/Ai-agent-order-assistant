"""Содержит схлопывание теневых проекций ИИ и ссылочных дублей."""

from __future__ import annotations

import re
from typing import Any

from restaurant_bot.domain.models import ExtractedItem
from restaurant_bot.parsing.ai.comment_reconciliation import (
    _COMMENT_BINDING_CONFIDENCE,
    _TRAILING_ROOT_PROCESSING_RE,
    _apply_trailing_root_processing_comment,
    _binding_target_indexes,
)
from restaurant_bot.parsing.ai.item_reconciliation import (
    _NON_PRODUCT_FRAGMENT_WORDS,
    _collapse_redundant_ai_items,
)
from restaurant_bot.parsing.comment_scope import has_explicit_global_comment_scope
from restaurant_bot.services.text import (
    NUMBER_WORDS,
    UNIT_ALIASES,
    clean_text,
    normalize_text,
    normalize_unit,
    to_float,
)


def collapse_comment_shadow_items(
    items: list[dict[str, Any]],
    global_comment: str = "",
    source_text: str = "",
    deterministic: list[ExtractedItem] | None = None,
) -> list[dict[str, Any]]:
    """Удаляет ложный товар, совпадающий с комментарием."""
    shadow_indexes: set[int] = set()
    normalized_global_comment = normalize_text(global_comment).strip(" .,;")
    deterministic_queries = {
        normalize_text(item.product_query).strip(" .,;:-—–") for item in deterministic or []
    }
    if normalized_global_comment:
        shadow_indexes.update(
            index
            for index, item in enumerate(items)
            if normalize_text(item.get("product_query")).strip(" .,;") == normalized_global_comment
            and normalize_text(item.get("product_query")).strip(" .,;:-—–")
            not in deterministic_queries
        )
    normalized_source = normalize_text(source_text)
    for owner_index, owner in enumerate(items):
        owner_comments = {
            normalize_text(owner.get(field)).strip(" .,;")
            for field in ("comment", "user_comment_to_supplier")
            if normalize_text(owner.get(field)).strip(" .,;")
        }
        if not owner_comments:
            continue
        for shadow_index, shadow in enumerate(items):
            if shadow_index == owner_index:
                continue
            shadow_query = normalize_text(shadow.get("product_query")).strip(" .,;")
            if not shadow_query:
                continue
            owner_source = normalize_text(owner.get("source_line")).strip(" .,;:-—–")
            shadow_source = normalize_text(shadow.get("source_line")).strip(" .,;:-—–")
            if owner_source and shadow_source and owner_source != shadow_source:
                continue
            if normalized_source and shadow_query not in normalized_source:
                continue
            if shadow_query in owner_comments:
                if owner.get("quantity") is None and shadow.get("quantity") is not None:
                    owner["quantity"] = shadow.get("quantity")
                    owner["unit"] = shadow.get("unit") or owner.get("unit") or ""
                shadow_indexes.add(shadow_index)
                continue

            # Модель может продублировать границу между двумя произнесёнными позициями:
            # «желательно холодным. И говядина». Первая часть уже относится к
            # комментарию сиропа, вторая — к следующему товару, поэтому объединённая
            # третья позиция не содержит нового подтверждённого факта.
            for comment in owner_comments:
                if not shadow_query.startswith(comment):
                    continue
                remainder = shadow_query[len(comment) :].strip(" .,;")
                remainder = re.sub(r"^(?:и|а также|а)\s+", "", remainder)
                if not remainder:
                    continue
                if any(
                    target_index != shadow_index
                    and target_index != owner_index
                    and (
                        normalize_text(target.get("product_query")).startswith(remainder)
                        or remainder.startswith(normalize_text(target.get("product_query")))
                    )
                    for target_index, target in enumerate(items)
                    if normalize_text(target.get("product_query"))
                ):
                    shadow_indexes.add(shadow_index)
                    break
    return [item for index, item in enumerate(items) if index not in shadow_indexes]


def _remove_contained_query_fragments(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Схлопывает дубли, ставшие частью полного названия того же источника."""
    removed: set[int] = set()
    for owner_index, owner in enumerate(items):
        owner_query = normalize_text(owner.get("product_query")).strip(" .,;:-—–")
        owner_source = normalize_text(owner.get("source_line")).strip(" .,;:-—–")
        if not owner_query or not owner_source:
            continue
        for fragment_index, fragment in enumerate(items):
            if fragment_index == owner_index or fragment_index in removed:
                continue
            fragment_query = normalize_text(fragment.get("product_query")).strip(" .,;:-—–")
            fragment_source = normalize_text(fragment.get("source_line")).strip(" .,;:-—–")
            if (
                fragment_index > owner_index
                and fragment_query == owner_query
                and fragment_source == owner_source
                and to_float(fragment.get("quantity")) == to_float(owner.get("quantity"))
                and normalize_text(fragment.get("unit")) == normalize_text(owner.get("unit"))
            ):
                removed.add(fragment_index)
                continue
            if (
                not fragment_query
                or fragment_source != owner_source
                or fragment_query == owner_query
                or not re.search(
                    rf"(?<![a-zа-яё0-9]){re.escape(fragment_query)}(?![a-zа-яё0-9])",
                    owner_query,
                    flags=re.I,
                )
            ):
                continue
            owner_quantity = to_float(owner.get("quantity"))
            fragment_quantity = to_float(fragment.get("quantity"))
            if (
                owner_quantity is not None
                and fragment_quantity is not None
                and abs(owner_quantity - fragment_quantity) > 1e-9
            ):
                continue
            owner_unit = normalize_unit(owner.get("unit"))
            fragment_unit = normalize_unit(fragment.get("unit"))
            if owner_unit and fragment_unit and owner_unit != fragment_unit:
                continue
            if owner_quantity is None and fragment_quantity is not None:
                owner["quantity"] = fragment_quantity
                owner["unit"] = fragment.get("unit") or owner.get("unit") or ""
            removed.add(fragment_index)
    return [item for index, item in enumerate(items) if index not in removed]


def _remove_connector_fragment_items(
    items: list[dict[str, Any]],
    deterministic: list[ExtractedItem] | None = None,
    source_text: str = "",
) -> list[dict[str, Any]]:
    """Удаляет отдельные союзы, ошибочно возвращённые как товарные позиции."""
    connectors = {"на", "и", "или", "либо", "а", "также"}
    deterministic_queries = {
        normalize_text(item.product_query).strip(" .,;:-—–") for item in deterministic or []
    }
    normalized_source = normalize_text(source_text)
    source_counts: dict[str, int] = {}
    for item in items:
        source = normalize_text(item.get("source_line")).strip(" .,;:-—–")
        if source:
            source_counts[source] = source_counts.get(source, 0) + 1
    result: list[dict[str, Any]] = []
    for item in items:
        query = normalize_text(item.get("product_query")).strip(" .,;:-—–")
        source = normalize_text(item.get("source_line")).strip(" .,;:-—–")
        if (
            query in connectors
            and source
            and source_counts.get(source, 0) > 1
            and query not in deterministic_queries
            and (not normalized_source or query in normalized_source)
        ):
            for owner in result:
                owner_source = normalize_text(owner.get("source_line")).strip(" .,;:-—–")
                owner_query = clean_text(owner.get("product_query"))
                if owner_source != source or not owner_query:
                    continue
                connector_match = re.search(rf"\b{re.escape(query)}\b", owner_query, flags=re.I)
                if connector_match is None:
                    continue
                owner["product_query"] = clean_text(
                    f"{owner_query[: connector_match.end()]} "
                    f"{normalize_text(owner_query[connector_match.end() :])}"
                )
                break
            continue
        result.append(item)
    return result


def _has_source_product_anchor(left: str, right: str) -> bool:
    """Проверяет общий содержательный якорь двух товарных названий."""
    left_words = re.findall(r"[a-zа-яё0-9]+", normalize_text(left), flags=re.I)
    right_words = re.findall(r"[a-zа-яё0-9]+", normalize_text(right), flags=re.I)
    right_set = set(right_words)
    for word in set(left_words) & right_set:
        if len(word) >= 6 and word not in _NON_PRODUCT_FRAGMENT_WORDS:
            return True
    for index in range(len(left_words) - 1):
        if left_words[index : index + 2] == right_words[index : index + 2]:
            return True
        pair = left_words[index : index + 2]
        if len(pair) == 2:
            for right_index in range(len(right_words) - 1):
                if pair == right_words[right_index : right_index + 2]:
                    return True
    return False


def _has_shared_query_prefix(left: str, right: str, minimum_tokens: int = 3) -> bool:
    """Проверяет общий длинный префикс вариантов одного появления в источнике."""
    left_words = re.findall(r"[a-zа-яё0-9]+", normalize_text(left), flags=re.I)
    right_words = re.findall(r"[a-zа-яё0-9]+", normalize_text(right), flags=re.I)
    prefix_length = 0
    for left_word, right_word in zip(left_words, right_words, strict=False):
        if left_word != right_word:
            break
        prefix_length += 1
    return prefix_length >= minimum_tokens


def _query_covers_reference(query: str, reference: str) -> bool:
    """Проверяет ссылочный запрос с допустимым хвостом количества заказа."""
    normalized_query = normalize_text(query).strip(" .,;:-—–")
    normalized_reference = normalize_text(reference).strip(" .,;:-—–")
    if normalized_query == normalized_reference:
        return True
    if not normalized_query.startswith(f"{normalized_reference} "):
        return False
    suffix = normalized_query[len(normalized_reference) :].strip()
    suffix_tokens = re.findall(r"[a-zа-яё0-9]+", suffix, flags=re.I)
    return bool(suffix_tokens) and all(
        token in UNIT_ALIASES or token in NUMBER_WORDS or token.isdigit() for token in suffix_tokens
    )


def _collapse_source_reference_variants(
    items: list[dict[str, Any]],
    deterministic: list[ExtractedItem],
    source_text: str = "",
) -> list[dict[str, Any]]:
    """Удаляет варианты одного подтверждённого появления в источнике."""
    if len(items) < 2 or len(deterministic) > 1:
        return items
    if len(deterministic) == 1:
        reference_query = normalize_text(deterministic[0].product_query).strip(" .,;:-—–")
    else:
        reference_query = ""
        normalized_source = normalize_text(source_text)
        source_keys = {
            normalize_text(item.get("source_line")).strip(" .,;:-—–")
            for item in items
            if normalize_text(item.get("source_line")).strip(" .,;:-—–")
        }
        if len(source_keys) != 1 or not normalized_source:
            return items
        # Если детерминированный разбор не разделил строку с фасовкой,  # noqa: RUF003
        # длинный общий префикс — единственная безопасная опора исходной позиции.
        # Короткие и общие префиксы оставляем для последующего уточнения.
        for candidate in items:
            for other in items:
                if candidate is other:
                    continue
                if _has_shared_query_prefix(
                    clean_text(candidate.get("product_query")),
                    clean_text(other.get("product_query")),
                ):
                    reference_query = normalize_text(candidate.get("product_query")).strip(
                        " .,;:-—–"
                    )
                    break
            if reference_query:
                break
    if not reference_query:
        return items
    source_keys = {
        normalize_text(item.get("source_line")).strip(" .,;:-—–")
        for item in items
        if normalize_text(item.get("source_line")).strip(" .,;:-—–")
    }
    if len(source_keys) != 1:
        return items
    owner_indexes = [
        index
        for index, item in enumerate(items)
        if _query_covers_reference(clean_text(item.get("product_query")), reference_query)
    ]
    if len(owner_indexes) != 1:
        return items
    owner_index = owner_indexes[0]
    owner = items[owner_index]
    removed: set[int] = set()
    for index, candidate in enumerate(items):
        if index == owner_index:
            continue
        candidate_query = clean_text(candidate.get("product_query"))
        if not candidate_query or not _has_source_product_anchor(reference_query, candidate_query):
            continue
        if len(deterministic) == 0 and not _has_shared_query_prefix(
            reference_query, candidate_query
        ):
            continue
        if clean_text(candidate.get("comment") or candidate.get("user_comment_to_supplier")):
            continue
        candidate_quantity = to_float(candidate.get("quantity"))
        owner_quantity = to_float(owner.get("quantity"))
        if (
            candidate_quantity is not None
            and owner_quantity is not None
            and abs(candidate_quantity - owner_quantity) <= 1e-9
            and normalize_unit(candidate.get("unit")) != normalize_unit(owner.get("unit"))
        ):
            continue
        removed.add(index)
    return [item for index, item in enumerate(items) if index not in removed]


def _collapse_shadow_item_projections(
    items: list[dict[str, Any]],
    source_text: str,
    deterministic: list[ExtractedItem],
    global_comment: str,
    bindings: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Удаляет подтверждённые теневые проекции после блока A."""
    root_target_indexes: set[int] | None = None
    normalized_source = normalize_text(source_text)
    processing_match = _TRAILING_ROOT_PROCESSING_RE.search(clean_text(source_text))
    normalized_processing = (
        normalize_text(processing_match.group("instruction")) if processing_match else ""
    )
    for binding in bindings or []:
        if clean_text(binding.get("scope")).casefold() != "group":
            continue
        if (to_float(binding.get("confidence")) or 0.0) < _COMMENT_BINDING_CONFIDENCE:
            continue
        normalized_binding = normalize_text(binding.get("text"))
        if (
            normalized_binding
            and normalized_binding in normalized_source
            and normalized_binding in normalized_processing
        ):
            root_target_indexes = set(_binding_target_indexes(binding, len(items)))
            break
    items = _apply_trailing_root_processing_comment(items, source_text, root_target_indexes)
    explicit_global = global_comment if has_explicit_global_comment_scope(source_text) else ""
    items = collapse_comment_shadow_items(items, explicit_global, source_text, deterministic)
    items = _remove_connector_fragment_items(items, deterministic, source_text)
    items = _collapse_redundant_ai_items(items, deterministic)
    items = _collapse_source_reference_variants(items, deterministic, source_text)
    if len(deterministic) <= 1:
        items = _remove_contained_query_fragments(items)
    return items
