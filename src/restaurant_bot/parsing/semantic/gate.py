"""Проверяет итоговые позиции перед передачей их в черновик заказа."""

from __future__ import annotations

import re
from typing import Any

from restaurant_bot.domain.models import CommentSource, ExtractedItem
from restaurant_bot.domain.text import clean_text, normalize_text
from restaurant_bot.orders.quantity_provenance import reconcile_order_quantity_evidence
from restaurant_bot.parsing.comment_policy import explicit_supplier_comment
from restaurant_bot.parsing.semantic.boundaries import (
    build_item_references,
    is_catalog_tail_fragment,
    query_anchor_overlap,
    semantic_anchor_tokens,
)
from restaurant_bot.parsing.semantic.measurements import (
    extract_semantic_facts,
    is_catalog_tail_text,
)
from restaurant_bot.parsing.semantic.models import SemanticFactKind


def _source_supports_comment(comment: str, source_text: str) -> bool:
    """Проверяет наличие комментария в исходной фразе и его явную семантику."""
    value = normalize_text(comment).strip(" .,;:-—–")
    source = normalize_text(source_text)
    if not value:
        return False
    if explicit_supplier_comment(comment):
        marker = normalize_text(comment).split(maxsplit=1)[0]
        return marker in source
    if value not in source:
        return False
    if re.search(r"\b(?:примерно|приблизительно|около)\s+\d", value, flags=re.I):
        return False
    if is_catalog_tail_text(comment, source_text) and not re.search(
        r"\b(?:разлож\w*|привез\w*|достав\w*|постав\w*|упаков\w*|выбер\w*|"
        r"желательн\w*|обязательн\w*|позвон\w*|смешив\w*)\b",
        value,
        flags=re.I,
    ):
        return False
    facts = extract_semantic_facts(source_text)
    return not any(
        fact.kind is SemanticFactKind.CATALOG_ATTRIBUTE
        and value in normalize_text(fact.original_text)
        for fact in facts
    ) and not any(
        fact.kind
        in {
            SemanticFactKind.MEASUREMENT,
            SemanticFactKind.ORDER_QUANTITY,
        }
        and value == normalize_text(fact.original_text)
        for fact in facts
    )


def _clear_catalog_comments(items: list[dict[str, Any]], source_text: str) -> None:
    """Удаляет комментарии, совпадающие только с фактами каталога."""
    for item in items:
        comment = clean_text(item.get("comment") or item.get("user_comment_to_supplier"))
        context = clean_text(item.get("source_span")) or clean_text(item.get("source_line")) or source_text
        if comment and not _source_supports_comment(comment, context):
            item["comment"] = ""
            item["user_comment_to_supplier"] = ""
            item["comment_source"] = CommentSource.NONE.value


def _clear_unanchored_supplier_hints(items: list[dict[str, Any]], source_text: str) -> None:
    """Удаляет подсказку поставщика, не подтверждённую явной связью в источнике."""
    explicit_relation = re.search(
        r"\b(?:у|от)\s+поставщик\w*\b|\b(?:закаж\w*|купить)\s+у\b",
        normalize_text(source_text),
        flags=re.I,
    )
    if explicit_relation:
        return
    source = normalize_text(source_text)
    for item in items:
        hint = normalize_text(item.get("supplier_hint") or "")
        query = normalize_text(item.get("product_query") or "")
        if hint and source and hint in source and hint in query:
            item["supplier_hint"] = ""


def _projections_share_anchor(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Проверяет, являются ли два AI-фрагмента одной проекцией товара."""
    left_tokens = semantic_anchor_tokens(left.get("product_query") or "")
    right_tokens = semantic_anchor_tokens(right.get("product_query") or "")
    if left_tokens == right_tokens:
        return True
    return len(left_tokens & right_tokens) >= 2


def _merge_order_quantity(
    target: dict[str, Any], candidate: dict[str, Any], source_text: str
) -> None:
    """Переносит количество из служебного фрагмента только при источниковом подтверждении."""
    if target.get("quantity") is not None or candidate.get("quantity") is None:
        return
    quantity = reconcile_order_quantity_evidence(
        clean_text(candidate.get("source_span")) or source_text,
        candidate.get("quantity"),
        candidate.get("unit") or "",
        quantity_source=candidate.get("quantity_source") or "",
        order_entry_text=candidate.get("order_entry_text") or "",
        order_entry_type=candidate.get("order_entry_type") or "",
        packaging_role=candidate.get("packaging_role") or "none",
    )
    if quantity.provenance.value == "order":
        target["quantity"] = quantity.quantity
        target["unit"] = quantity.unit
        target["quantity_source"] = candidate.get("quantity_source") or "source"


def apply_semantic_gate(
    items: list[dict[str, Any]],
    deterministic: list[ExtractedItem],
    source_text: str,
) -> list[dict[str, Any]]:
    """Схлопывает неподтверждённые AI-фрагменты и сохраняет источниковые факты."""
    if not items or not deterministic:
        _clear_catalog_comments(items, source_text)
        _clear_unanchored_supplier_hints(items, source_text)
        return items
    references = build_item_references(deterministic)
    same_source = [
        item
        for item in items
        if not item.get("source_line")
        or (
            not item.get("source_span")
            and normalize_text(item.get("source_line")) == normalize_text(source_text)
        )
    ]
    if len(deterministic) == 1 and same_source:
        reference = references[0]
        independent = [
            item
            for item in same_source
            if query_anchor_overlap(item.get("product_query") or "", reference) > 0
            and not is_catalog_tail_fragment(item.get("product_query") or "", source_text)
        ]
        if len(independent) > 1 and all(
            _projections_share_anchor(independent[0], candidate) for candidate in independent[1:]
        ):
            owner = max(
                independent,
                key=lambda item: query_anchor_overlap(item.get("product_query") or "", reference),
            )
            merged = dict(owner)
            for candidate in independent:
                if candidate is owner:
                    continue
                _merge_order_quantity(merged, candidate, source_text)
                candidate_comment = clean_text(
                    candidate.get("comment") or candidate.get("user_comment_to_supplier")
                )
                if (
                    candidate_comment
                    and _source_supports_comment(
                        candidate_comment,
                        clean_text(candidate.get("source_span")) or source_text,
                    )
                    and not merged.get("comment")
                ):
                    merged["comment"] = candidate_comment
                    merged["user_comment_to_supplier"] = candidate_comment
            _clear_catalog_comments([merged], source_text)
            _clear_unanchored_supplier_hints([merged], source_text)
            return [merged]
        if len(independent) > 1:
            _clear_catalog_comments(independent, source_text)
            _clear_unanchored_supplier_hints(independent, source_text)
            return independent
        owner = max(
            same_source,
            key=lambda item: query_anchor_overlap(item.get("product_query") or "", reference),
        )
        if query_anchor_overlap(owner.get("product_query") or "", reference) == 0:
            return [item.model_dump() for item in deterministic]
        merged = dict(owner)
        for candidate in same_source:
            if candidate is owner:
                continue
            _merge_order_quantity(merged, candidate, source_text)
            candidate_comment = clean_text(
                candidate.get("comment") or candidate.get("user_comment_to_supplier")
            )
            if candidate_comment and not merged.get("comment"):
                merged["comment"] = candidate_comment
                merged["user_comment_to_supplier"] = candidate_comment
        _clear_catalog_comments([merged], source_text)
        _clear_unanchored_supplier_hints([merged], source_text)
        return [merged]

    accepted: list[dict[str, Any]] = []
    for item in items:
        query = item.get("product_query") or ""
        if (
            item.get("source_span")
            or not item.get("source_line")
            or normalize_text(item.get("source_line")) != normalize_text(source_text)
        ):
            accepted.append(item)
            continue
        scores = [query_anchor_overlap(query, reference) for reference in references]
        if max(scores, default=0) > 0:
            accepted.append(item)
    if not accepted:
        return [item.model_dump() for item in deterministic]
    _clear_catalog_comments(accepted, source_text)
    _clear_unanchored_supplier_hints(accepted, source_text)
    return accepted
