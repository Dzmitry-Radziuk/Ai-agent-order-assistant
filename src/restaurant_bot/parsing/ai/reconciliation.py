"""Содержит координацию чистой сверки структурированного ответа OpenAI."""

from __future__ import annotations

from typing import Any

from restaurant_bot.domain.models import Intent
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
from restaurant_bot.text_normalization import clean_text, normalize_text


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
    items = _apply_semantic_comment_bindings(payload, items, bindings, source_text)
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

    restore_explicit_order_terms(items, source_text)
    _discard_unverified_item_comments(items, bindings, source_text, global_comment)
    _restore_dropped_unclassified_terms(items, deterministic, global_comment)
    items = _restore_omitted_explicit_items(items, deterministic, global_comment)
    items = _collapse_shadow_item_projections(
        items, source_text, deterministic, global_comment, bindings
    )
    _restore_unordered_measurement_pair(items, deterministic)
    _restore_reference_ranges_in_queries(items, source_text, deterministic)
    for item in items:
        if item.get("comment") and not item.get("user_comment_to_supplier"):
            item["user_comment_to_supplier"] = item["comment"]
        elif item.get("user_comment_to_supplier") and not item.get("comment"):
            item["comment"] = item["user_comment_to_supplier"]

    payload["global_comment"] = global_comment
    payload["items"] = items
    return payload
