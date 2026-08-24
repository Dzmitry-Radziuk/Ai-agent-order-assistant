"""Содержит координацию чистой сверки структурированного ответа OpenAI."""

from __future__ import annotations

import re
from typing import Any

import structlog

from restaurant_bot.catalog.safety import is_standalone_product_form_query
from restaurant_bot.domain.models import Intent
from restaurant_bot.domain.text import clean_text, normalize_text
from restaurant_bot.parsing.ai.comment_reconciliation import (
    _append_local_item_comment,
    _apply_semantic_comment_bindings,
    _discard_unverified_item_comments,
    _strip_conversational_product_leadin,
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
from restaurant_bot.parsing.commands.item_commands import (
    has_explicit_add_items,
    has_unrepresented_order_quantity_evidence,
)
from restaurant_bot.parsing.comment_policy import is_comment_control_text
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
from restaurant_bot.parsing.semantic_routing import classify_bot_conversation

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
        catalog_identity_match=bool(
            selected and selected["role"] == SemanticFactKind.CATALOG_ATTRIBUTE.value
        ),
        decision=reason,
        reason=reason,
    )


def _comment_has_order_quantity(value: str) -> bool:
    """Проверяет, является ли текст комментария заказным количеством."""
    return any(
        fact.kind is SemanticFactKind.ORDER_QUANTITY for fact in extract_semantic_facts(value)
    )


def _extend_partial_source_with_order_tail(
    provided: str,
    derived: str,
    global_comment: str = "",
) -> str:
    """Возвращает подтверждённый локальный span вместе с комментарием."""
    provided = clean_text(provided)
    derived = clean_text(derived)
    if not provided or not derived or normalize_text(provided) == normalize_text(derived):
        return derived or provided
    prefix = derived[: len(provided)]
    if normalize_text(prefix) != normalize_text(provided):
        return provided
    # ``derived`` is bounded by the next independent product anchor, so it also
    # owns the supplier instruction between this item and that next anchor.
    # The only exception is a separately parsed final order comment: it is not
    # evidence of the last item's identity or local comment.
    raw_global_comment = clean_text(global_comment)
    comment_variants = {
        raw_global_comment,
        _strip_global_comment_scope(raw_global_comment),
    }
    for comment_variant in sorted(comment_variants, key=len, reverse=True):
        if not comment_variant:
            continue
        comment_pattern = re.escape(comment_variant).replace("ё", "[её]")
        trailing_global = re.compile(
            rf"(?:[\s,.;:—–-]+){comment_pattern}\s*$",
            flags=re.IGNORECASE,
        )
        if trailing_global.search(derived):
            derived = trailing_global.sub("", derived).strip(" .,;:-—–")
            derived = re.sub(r"\s+(?:и|а)\s*$", "", derived, flags=re.IGNORECASE)
            break
    return derived


def _global_comment_start(source_text: str, global_comment: str) -> int | None:
    """Возвращает начало явного финального комментария в исходной фразе."""
    raw_global_comment = clean_text(global_comment)
    comment_variants = {
        raw_global_comment,
        _strip_global_comment_scope(raw_global_comment),
    }
    for comment_variant in sorted(comment_variants, key=len, reverse=True):
        if not comment_variant:
            continue
        comment_pattern = re.escape(comment_variant).replace("ё", "[её]")
        match = re.search(rf"{comment_pattern}\s*$", source_text, flags=re.IGNORECASE)
        if match is not None:
            return match.start()
    return None


def _resolve_item_source_spans(
    items: list[dict[str, Any]],
    source_text: str,
    global_comment: str = "",
) -> tuple[Any, ...]:
    """Заполняет локальные source span, сохраняя полный исходный source_line."""
    had_source_spans = any(clean_text(item.get("source_span")) for item in items)
    provided_sources = [clean_text(item.get("source_line")) for item in items]
    global_comment_start = _global_comment_start(source_text, global_comment)
    for item in items:
        item["source_span"] = ""
    has_partial_provided_source = any(
        normalize_text(source) != normalize_text(source_text) for source in provided_sources
    )
    if (
        len(items) > 1
        and all(provided_sources)
        and has_partial_provided_source
        and not had_source_spans
    ):
        # source_line модели может быть неполным: например, она часто оставляет
        # количество из хвоста списка за пределами строки товара. Сначала строим
        # границы по независимым товарным якорям исходного сообщения. Это
        # сохраняет строгую provenance-проверку и не требует доверять числу ИИ.
        derived = derive_item_source_spans(source_text, items)
        if all(span is not None for span in derived):
            for item, provided_source, span in zip(items, provided_sources, derived, strict=True):
                assert span is not None
                span_text = span.text
                if (
                    global_comment_start is not None
                    and span.start < global_comment_start < span.end
                ):
                    span_text = re.sub(
                        r"\s+(?:и|а)\s*$",
                        "",
                        source_text[span.start:global_comment_start].strip(" .,;:-—–"),
                        flags=re.IGNORECASE,
                    )
                item["source_span"] = _extend_partial_source_with_order_tail(
                    provided_source,
                    span_text,
                    global_comment,
                )
            return derived
        for item, provided_source in zip(items, provided_sources, strict=True):
            if normalize_text(provided_source) != normalize_text(source_text):
                item["source_span"] = provided_source
        return tuple(None for _ in items)
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
    if (
        intent is Intent.UNKNOWN
        and items
        and not _has_safe_item_recovery_evidence(source_text, deterministic)
    ):
        logger.info(
            "unknown_item_payload_rejected",
            text=source_text,
            item_count=len(items),
        )
        items = []
        payload["items"] = []
    spans = _resolve_item_source_spans(items, source_text, clean_text(payload.get("global_comment")))
    remove_unsupported_query_qualifiers(items, source_text)
    global_comment = _strip_global_comment_scope(clean_text(payload.get("global_comment")))
    if is_comment_control_text(global_comment):
        global_comment = ""
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
    if is_comment_control_text(global_comment):
        global_comment = ""
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
    if not items and deterministic and _has_safe_item_recovery_evidence(source_text, deterministic):
        payload["intent"] = Intent.ADD_ITEMS
        payload["items"] = [item.model_dump() for item in deterministic]
        payload["global_comment"] = global_comment
        return payload

    if not items and deterministic:
        logger.info(
            "deterministic_item_fallback_rejected",
            text=source_text,
            ai_intent=intent.value,
            deterministic_item_count=len(deterministic),
        )
        payload["items"] = []
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
        product_query = _strip_conversational_product_leadin(item.get("product_query") or "")
        product_query = strip_order_quantity_from_query(
            product_query,
            clean_text(item.get("source_span")) or source_text,
        )
        item["product_query"] = re.sub(
            r"\s+\b(?:вес|количество)\b\s*$",
            "",
            product_query,
            flags=re.I,
        ).strip(" .,;:-—–")
    items = apply_semantic_gate(items, deterministic, source_text)
    for item in items:
        if item.get("comment") and not item.get("user_comment_to_supplier"):
            item["user_comment_to_supplier"] = item["comment"]
        elif item.get("user_comment_to_supplier") and not item.get("comment"):
            item["comment"] = item["user_comment_to_supplier"]

    payload["global_comment"] = global_comment
    payload["items"] = items
    return payload


def _has_safe_item_recovery_evidence(
    source_text: str,
    deterministic: list[Any],
) -> bool:
    """Проверяет, подтверждает ли исходная фраза восстановление товарных позиций."""
    if has_unrepresented_order_quantity_evidence(source_text, deterministic):
        return False
    if has_explicit_add_items(source_text, deterministic):
        return True
    if any(
        getattr(item, "quantity", None) is not None and bool(getattr(item, "unit", ""))
        for item in deterministic
    ):
        return True
    if len(deterministic) != 1 or classify_bot_conversation(source_text) is not None:
        return False
    query = clean_text(getattr(deterministic[0], "product_query", ""))
    words = [word for word in normalize_text(query).split() if len(word) > 1]
    return len(words) >= 2 or is_standalone_product_form_query(query)
