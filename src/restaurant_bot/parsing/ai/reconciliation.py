"""Содержит координацию чистой сверки структурированного ответа OpenAI."""

from __future__ import annotations

from typing import Any

import structlog

from restaurant_bot.domain.models import Intent
from restaurant_bot.domain.text import clean_text, normalize_text
from restaurant_bot.parsing.ai.comment_reconciliation import (
    _append_local_item_comment,
    _apply_semantic_comment_bindings,
    _discard_unverified_item_comments,
    _strip_global_comment_scope,
)
from restaurant_bot.parsing.ai.item_reconciliation import (
    _clear_unknown_item_placeholders,
    _restore_dropped_unclassified_terms,
    _restore_omitted_explicit_items,
    remove_unsupported_query_qualifiers,
)
from restaurant_bot.parsing.ai.quantity_reconciliation import (
    _restore_reference_ranges_in_queries,
    _restore_unordered_measurement_pair,
    restore_explicit_order_terms,
)
from restaurant_bot.parsing.ai.shadow_items import _collapse_shadow_item_projections
from restaurant_bot.parsing.comment_scope import has_explicit_global_comment_scope
from restaurant_bot.parsing.products import parse_product_lines
from restaurant_bot.parsing.quantities import parse_quantity_unit
from restaurant_bot.parsing.semantic.boundaries import derive_item_source_spans
from restaurant_bot.parsing.semantic.gate import apply_semantic_gate
from restaurant_bot.parsing.semantic.measurements import (
    extract_semantic_facts,
    has_global_quantity_scope,
    strip_order_quantity_from_query,
)
from restaurant_bot.parsing.semantic.models import SemanticFactKind

logger = structlog.get_logger(__name__)


def _numeric_fact_log(source_text: str) -> list[dict[str, Any]]:
    """Готовит короткий список числовых source-фактов для structured log."""
    facts: list[dict[str, Any]] = []
    for fact in extract_semantic_facts(source_text):
        if fact.kind not in {
            SemanticFactKind.CATALOG_ATTRIBUTE,
            SemanticFactKind.MEASUREMENT,
            SemanticFactKind.ORDER_QUANTITY,
        }:
            continue
        quantity, unit = parse_quantity_unit(fact.original_text)
        if quantity is None:
            continue
        facts.append(
            {
                "value": quantity,
                "unit": unit,
                "role": fact.kind.value,
                "provenance": fact.provenance,
                "occurrence_span": [fact.start, fact.end],
            }
        )
    return facts


def _log_quantity_reconciliation(
    before: dict[str, Any], after: dict[str, Any], source_text: str
) -> None:
    """Пишет решение provenance-сверки без пользовательского payload."""
    numeric_facts = _numeric_fact_log(source_text)
    after_quantity = after.get("quantity")
    after_unit = clean_text(after.get("unit"))
    selected = next(
        (
            fact
            for fact in reversed(numeric_facts)
            if fact["role"] == SemanticFactKind.ORDER_QUANTITY.value
            and fact["value"] == after_quantity
            and (not after_unit or fact["unit"] == after_unit)
        ),
        None,
    )
    if selected is None and after_quantity is not None:
        selected = next(
            (
                fact
                for fact in reversed(numeric_facts)
                if fact["value"] == after_quantity
                and (not after_unit or fact["unit"] == after_unit)
            ),
            None,
        )
    before_quantity = before.get("quantity")
    if selected and selected["role"] == SemanticFactKind.ORDER_QUANTITY.value:
        reason = (
            "preserved_explicit_order_quantity"
            if before_quantity == after_quantity
            else "recovered_explicit_order_quantity"
        )
    elif after_quantity is None:
        reason = "rejected_unproven_quantity"
    else:
        reason = "preserved_non_order_quantity"
    logger.info(
        "quantity_reconciliation_decision",
        before_quantity=before_quantity,
        before_unit=clean_text(before.get("unit")),
        after_quantity=after_quantity,
        after_unit=after_unit,
        numeric_facts=numeric_facts,
        selected_fact_role=selected["role"] if selected else "none",
        selected_fact_provenance=selected["provenance"] if selected else "none",
        rejected_competing_facts=[fact for fact in numeric_facts if fact is not selected],
        semantic_item_id=clean_text(after.get("product_query")),
        source_span=source_text,
        candidate_quantity=before_quantity,
        authorized_quantity=after_quantity,
        candidate_occurrence_span=(selected.get("occurrence_span") if selected else None),
        fact_role=selected["role"] if selected else "none",
        provenance=selected["provenance"] if selected else "none",
        catalog_identity_match=bool(selected and selected["role"] == SemanticFactKind.CATALOG_ATTRIBUTE.value),
        decision=reason,
        reason=reason,
    )


def _comment_has_order_quantity(value: str) -> bool:
    """Проверяет, является ли текст комментария заказным количеством."""
    return any(
        fact.kind is SemanticFactKind.ORDER_QUANTITY
        for fact in extract_semantic_facts(value)
    )


def _resolve_item_source_spans(
    items: list[dict[str, Any]], source_text: str
) -> tuple[Any, ...]:
    """Заполняет локальные source span, сохраняя полный исходный source_line."""
    had_source_spans = any(clean_text(item.get("source_span")) for item in items)
    provided_sources = [clean_text(item.get("source_line")) for item in items]
    for item in items:
        item["source_span"] = ""
    if items and all(provided_sources) and not had_source_spans:
        for item, provided_source in zip(items, provided_sources, strict=True):
            if normalize_text(provided_source) != normalize_text(source_text):
                item["source_span"] = provided_source
        return tuple(None for _ in items)
    spans = derive_item_source_spans(source_text, items)
    for item, span in zip(items, spans, strict=True):
        item["source_span"] = span.text if span is not None else ""
        if (
            not clean_text(item.get("source_span"))
            and clean_text(item.get("source_line"))
            and normalize_text(item.get("source_line")) != normalize_text(source_text)
        ):
            item["source_span"] = clean_text(item.get("source_line"))
        if source_text and not clean_text(item.get("source_line")):
            item["source_line"] = source_text
    return spans


def _reconcile_global_order_comment(
    global_comment: str,
    source_text: str,
    spans: tuple[Any, ...],
) -> tuple[str, str]:
    """Не допускает превращения detached quantity в комментарий или общий заказ."""
    if not global_comment:
        return global_comment, ""
    if has_global_quantity_scope(global_comment):
        return "", global_comment
    if not _comment_has_order_quantity(global_comment):
        return global_comment, ""
    source_facts = [
        fact
        for fact in extract_semantic_facts(source_text)
        if fact.kind is SemanticFactKind.ORDER_QUANTITY
    ]
    comment_value = normalize_text(global_comment)
    for fact in source_facts:
        if normalize_text(fact.original_text) not in comment_value:
            continue
        if any(span is not None and span.start <= fact.start < span.end for span in spans):
            return "", ""
        return "", global_comment
    return "", global_comment


def recover_omitted_explicit_items(payload: dict[str, Any], source_text: str) -> dict[str, Any]:
    """Сохраняет результат ИИ и добавляет товары только при пустом ответе."""
    items = list(payload.get("items") or [])
    bindings = list(payload.pop("comment_bindings", []) or [])
    _clear_unknown_item_placeholders(items)
    intent = Intent(payload.get("intent", Intent.UNKNOWN))
    if intent not in {Intent.ADD_ITEMS, Intent.UNKNOWN}:
        payload["items"] = []
        return payload

    deterministic = parse_product_lines(source_text)
    spans = _resolve_item_source_spans(items, source_text)
    remove_unsupported_query_qualifiers(items, source_text)
    global_comment = _strip_global_comment_scope(clean_text(payload.get("global_comment")))
    global_comment, quantity_clarification = _reconcile_global_order_comment(
        global_comment,
        source_text,
        spans,
    )
    if quantity_clarification and not payload.get("comment_clarification"):
        payload["comment_clarification"] = quantity_clarification
    payload["global_comment"] = global_comment
    items = _apply_semantic_comment_bindings(payload, items, bindings, source_text, deterministic)
    global_comment = _strip_global_comment_scope(clean_text(payload.get("global_comment")))
    global_comment, quantity_clarification = _reconcile_global_order_comment(
        global_comment,
        source_text,
        spans,
    )
    if quantity_clarification and not payload.get("comment_clarification"):
        payload["comment_clarification"] = quantity_clarification

    # ИИ иногда дублирует локальную привязку в global_comment. Такой текст
    # уже записан в конкретную позицию и не должен распространяться движком на
    # ранее собранную корзину.
    if global_comment and items and not has_explicit_global_comment_scope(source_text):
        normalized_global = normalize_text(global_comment).strip(" .,;:-—–")
        local_binding = any(
            normalize_text(binding.get("text")).strip(" .,;:-—–") == normalized_global
            and clean_text(binding.get("scope")).casefold() in {"item", "group"}
            for binding in bindings
        )
        if local_binding:
            global_comment = ""
        else:
            _append_local_item_comment(items[-1], global_comment)
            global_comment = ""

    # Запасной путь нужен только когда ИИ действительно не вернул ни одной позиции.
    # Нельзя повторно разбирать исходную фразу поверх уже распознанных товаров:
    # это создаёт дубликаты и затирает комментарии.
    if not items and deterministic:
        payload["intent"] = Intent.ADD_ITEMS
        payload["items"] = [item.model_dump() for item in deterministic]
        payload["global_comment"] = global_comment
        return payload

    before_quantity_reconciliation = [dict(item) for item in items]
    restore_explicit_order_terms(items, source_text)
    for before, after in zip(before_quantity_reconciliation, items, strict=False):
        _log_quantity_reconciliation(
            before,
            after,
            clean_text(after.get("source_span")) or source_text,
        )
    _discard_unverified_item_comments(items, bindings, source_text, global_comment)
    _restore_dropped_unclassified_terms(items, deterministic, global_comment)
    items = _restore_omitted_explicit_items(items, deterministic, global_comment)
    items = _collapse_shadow_item_projections(
        items, source_text, deterministic, global_comment, bindings
    )
    _restore_unordered_measurement_pair(items, deterministic)
    _restore_reference_ranges_in_queries(items, source_text, deterministic)
    for item in items:
        item["product_query"] = strip_order_quantity_from_query(
            item.get("product_query") or "",
            clean_text(item.get("source_span")) or source_text,
        )
    items = apply_semantic_gate(items, deterministic, source_text)
    for item in items:
        if item.get("comment") and not item.get("user_comment_to_supplier"):
            item["user_comment_to_supplier"] = item["comment"]
        elif item.get("user_comment_to_supplier") and not item.get("comment"):
            item["comment"] = item["user_comment_to_supplier"]

    payload["global_comment"] = global_comment
    payload["items"] = items
    return payload
