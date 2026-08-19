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
from restaurant_bot.parsing.semantic.gate import apply_semantic_gate
from restaurant_bot.parsing.semantic.measurements import extract_semantic_facts
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
        reason=reason,
    )


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
    remove_unsupported_query_qualifiers(items, source_text)
    global_comment = _strip_global_comment_scope(clean_text(payload.get("global_comment")))
    payload["global_comment"] = global_comment
    items = _apply_semantic_comment_bindings(payload, items, bindings, source_text, deterministic)
    global_comment = _strip_global_comment_scope(clean_text(payload.get("global_comment")))

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
        _log_quantity_reconciliation(before, after, source_text)
    _discard_unverified_item_comments(items, bindings, source_text, global_comment)
    _restore_dropped_unclassified_terms(items, deterministic, global_comment)
    items = _restore_omitted_explicit_items(items, deterministic, global_comment)
    items = _collapse_shadow_item_projections(
        items, source_text, deterministic, global_comment, bindings
    )
    _restore_unordered_measurement_pair(items, deterministic)
    _restore_reference_ranges_in_queries(items, source_text, deterministic)
    items = apply_semantic_gate(items, deterministic, source_text)
    for item in items:
        if item.get("comment") and not item.get("user_comment_to_supplier"):
            item["user_comment_to_supplier"] = item["comment"]
        elif item.get("user_comment_to_supplier") and not item.get("comment"):
            item["comment"] = item["user_comment_to_supplier"]

    payload["global_comment"] = global_comment
    payload["items"] = items
    return payload
