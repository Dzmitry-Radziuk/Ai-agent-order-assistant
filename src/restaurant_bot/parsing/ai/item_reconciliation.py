"""Содержит очистку позиций ИИ и восстановление пропущенных товаров."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from restaurant_bot.domain.models import CommentSource, ExtractedItem, ParsedCommand
from restaurant_bot.domain.text import clean_text, normalize_text
from restaurant_bot.domain.units import UNIT_ALIASES
from restaurant_bot.parsing.ai.comment_reconciliation import (
    _append_local_item_comment,
    _strip_conversational_product_leadin,
)
from restaurant_bot.parsing.number_words import NUMBER_WORDS
from restaurant_bot.parsing.numeric import to_float
from restaurant_bot.parsing.products import parse_product_lines
from restaurant_bot.parsing.semantic.measurements import strip_order_quantity_from_query

_MIXED_SCRIPT_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё]+")

_SAFE_LATIN_TO_CYRILLIC = str.maketrans(
    {
        "A": "А",
        "a": "а",
        "C": "С",
        "c": "с",
        "E": "Е",
        "e": "е",
        "K": "К",
        "k": "к",
        "M": "М",
        "m": "м",
        "O": "О",
        "o": "о",
        "P": "П",
        "p": "п",
        "T": "Т",
        "t": "т",
    }
)

_SAFE_MIXED_LATIN_LETTERS = frozenset("AaCcEeKkMmOoPpTt")

_EMPTY_AI_VALUES = {
    "unknown",
    "none",
    "null",
    "n/a",
    "неизвестно",
    "неизвестный",
    "не указано",
    "не указан",
}


def _clear_unknown_item_placeholders(items: list[dict[str, Any]]) -> None:
    """Очищает служебные заглушки ИИ в полях маршрутизации товара."""
    for item in items:
        for field in ("department", "supplier_hint", "source_department"):
            if normalize_text(item.get(field)) in _EMPTY_AI_VALUES:
                item[field] = ""


def _query_is_already_represented(known_query: str, recovered_query: str) -> bool:
    """Сравнивает многословные названия с учётом разговорных окончаний."""
    known_query = normalize_text(known_query).casefold()
    recovered_query = normalize_text(recovered_query).casefold()
    if (
        known_query == recovered_query
        or known_query in recovered_query
        or recovered_query in known_query
    ):
        return True
    known_tokens = re.findall(r"[a-zа-я0-9%]+", known_query, flags=re.I)
    recovered_tokens = re.findall(r"[a-zа-я0-9%]+", recovered_query, flags=re.I)
    if len(known_tokens) < 2:
        return False
    return all(
        any(
            known == recovered
            or (len(known) >= 4 and len(recovered) >= 4 and known[:4] == recovered[:4])
            for recovered in recovered_tokens
        )
        for known in known_tokens
    )


def _semantic_product_query(value: str) -> str:
    """Возвращает название товара без незначимой внешней пунктуации."""
    normalized = normalize_text(value).strip(" .,;:-—–")
    return re.sub(r"\s+(?:пожалуйста|прошу)$", "", normalized, flags=re.I).strip(" .,;:-—–")


_NON_PRODUCT_FRAGMENT_WORDS = (
    set(UNIT_ALIASES)
    | set(NUMBER_WORDS)
    | {
        "и",
        "или",
        "либо",
        "на",
        "а",
        "также",
        "комментарий",
        "комментария",
        "общий",
        "товар",
        "товары",
        "позиция",
        "позиции",
        "заявка",
        "заявку",
        "заказ",
        "заказа",
    }
)


def _contains_product_query_word(value: str) -> bool:
    """Проверяет, содержит ли фрагмент самостоятельное название товара."""
    value = _strip_conversational_product_leadin(value)
    words = re.findall(r"[a-zа-яё0-9]+", normalize_text(value), flags=re.I)
    return any(
        len(word) > 1
        and word not in _NON_PRODUCT_FRAGMENT_WORDS
        and not word.isdigit()
        and not any(char.isdigit() for char in word)
        for word in words
    )


def _collapse_redundant_ai_items(
    items: list[dict[str, Any]], deterministic: list[ExtractedItem] | None = None
) -> list[dict[str, Any]]:
    """Схлопывает дубли одного источника и отбрасывает служебные обломки.

    Модель иногда возвращает одну голосовую позицию дважды: отдельно с
    комментарием и отдельно с количеством. Также диапазон фасовки может
    превращаться в псевдотовар «или». Такие элементы не являются двумя
    товарами и не должны попадать в черновик.
    """
    collapsed: list[dict[str, Any]] = []
    allow_item_count_mutation = deterministic is None or len(deterministic) <= 1
    for item in items:
        query = clean_text(item.get("product_query"))
        source = normalize_text(item.get("source_line")).strip(" .,;:-—–")
        if allow_item_count_mutation and (
            not _contains_product_query_word(query)
            and collapsed
            and source
            and source == normalize_text(collapsed[-1].get("source_line")).strip(" .,;:-—–")
        ):
            continue
        duplicate = None
        for existing in collapsed:
            if not allow_item_count_mutation:
                break
            existing_source = normalize_text(existing.get("source_line")).strip(" .,;:-—–")
            if not source or source != existing_source:
                continue
            existing_query = clean_text(existing.get("product_query"))
            if normalize_text(existing_query) != normalize_text(query):
                continue
            existing_quantity = to_float(existing.get("quantity"))
            quantity = to_float(item.get("quantity"))
            if (
                existing_quantity is not None
                and quantity is not None
                and abs(existing_quantity - quantity) > 1e-9
            ):
                continue
            duplicate = existing
            break
        if duplicate is None:
            collapsed.append(item)
            continue

        if (
            to_float(duplicate.get("quantity")) is None
            and to_float(item.get("quantity")) is not None
        ):
            duplicate["quantity"] = item.get("quantity")
            duplicate["unit"] = item.get("unit") or duplicate.get("unit") or ""
        if not clean_text(duplicate.get("supplier_hint")) and clean_text(item.get("supplier_hint")):
            duplicate["supplier_hint"] = item.get("supplier_hint")
        if clean_text(duplicate.get("packaging_role")) in {"", "none"} and clean_text(
            item.get("packaging_role")
        ) not in {"", "none"}:
            duplicate["packaging_text"] = item.get("packaging_text") or ""
            duplicate["packaging_role"] = item.get("packaging_role")
            duplicate["packaging_confidence"] = item.get("packaging_confidence") or 0
        if not clean_text(duplicate.get("comment")) and clean_text(
            item.get("user_comment_to_supplier")
        ):
            duplicate["comment"] = item.get("user_comment_to_supplier")
        elif item_comment := clean_text(item.get("comment")):
            _append_local_item_comment(duplicate, item_comment)
    return collapsed


def remove_unsupported_query_qualifiers(
    items: list[dict[str, Any]], source_text: str
) -> list[dict[str, Any]]:
    """Удаляет характеристики товара, которых не называл пользователь."""
    source_words = re.findall(r"[a-zа-я0-9]+", normalize_text(source_text), flags=re.I)
    if not source_words:
        return items

    for item in items:
        original_words = re.findall(
            r"[a-zа-яё0-9]+", clean_text(item.get("product_query")), flags=re.I
        )
        normalized_words = [normalize_text(word) for word in original_words]
        supported: list[str] = []
        for original, word in zip(original_words, normalized_words, strict=True):
            if any(
                word == source_word
                or (
                    len(word) >= 4
                    and len(source_word) >= 4
                    and word[0] == source_word[0]
                    and SequenceMatcher(None, word, source_word).ratio() >= 0.72
                )
                for source_word in source_words
            ):
                supported.append(original)
        if supported and len(supported) < len(original_words):
            item["product_query"] = " ".join(supported)
    return items


def _repair_safe_mixed_script_query(value: str) -> str:
    """Исправляет только однозначные латинские буквы внутри русского слова."""

    def replace(match: re.Match[str]) -> str:
        """Заменяет безопасный смешанный токен или возвращает его без изменений."""
        token = match.group(0)
        latin = {char for char in token if char.isascii() and char.isalpha()}
        has_cyrillic = bool(re.search(r"[А-Яа-яЁё]", token))
        if not latin or not has_cyrillic or not latin <= _SAFE_MIXED_LATIN_LETTERS:
            return token
        return token.translate(_SAFE_LATIN_TO_CYRILLIC)

    return _MIXED_SCRIPT_TOKEN_RE.sub(replace, clean_text(value))


def _repair_mixed_script_product_queries(items: list[dict[str, Any]]) -> None:
    """Нормализует безопасные смешанные слова только в поисковых названиях."""
    for item in items:
        item["product_query"] = _repair_safe_mixed_script_query(item.get("product_query") or "")


def _repair_command_mixed_script_queries(command: ParsedCommand) -> ParsedCommand:
    """Возвращает команду с безопасно исправленными смешанными словами товаров."""
    items = []
    changed = False
    for item in command.items:
        query = _repair_safe_mixed_script_query(item.product_query)
        changed = changed or query != item.product_query
        items.append(item.model_copy(update={"product_query": query}))
    return command.model_copy(update={"items": items}) if changed else command


def _restore_dropped_unclassified_terms(
    items: list[dict[str, Any]],
    deterministic: list[ExtractedItem],
    global_comment: str,
) -> None:
    """Возвращает неподтверждённый хвост ИИ-комментария в название товара."""
    pairs: list[tuple[dict[str, Any], ExtractedItem]] = []
    if len(items) == 1 and len(deterministic) == 1:
        pairs.append((items[0], deterministic[0]))
    else:
        for item in items:
            source_line = clean_text(item.get("source_span")) or clean_text(item.get("source_line"))
            recovered_from_line = parse_product_lines(source_line)
            if len(recovered_from_line) == 1:
                pairs.append((item, recovered_from_line[0]))

    normalized_global = normalize_text(global_comment).strip(" .,;:-—–")
    for item, recovered in pairs:
        source_line = clean_text(item.get("source_span")) or clean_text(item.get("source_line"))
        if recovered.packaging_role == "user_preference":
            recovered_query = _strip_conversational_product_leadin(recovered.product_query)
            if recovered_query and _query_is_already_represented(
                recovered_query, item.get("product_query") or ""
            ):
                item["product_query"] = recovered_query
            if not clean_text(item.get("comment") or item.get("user_comment_to_supplier")):
                item["comment"] = recovered.comment
                item["user_comment_to_supplier"] = recovered.user_comment_to_supplier
                item["comment_source"] = recovered.comment_source.value
            item["packaging_text"] = recovered.packaging_text
            item["packaging_role"] = recovered.packaging_role
            item["packaging_confidence"] = recovered.packaging_confidence
            if not clean_text(item.get("source_line")):
                item["source_line"] = recovered.source_line or ""
            continue
        if (
            clean_text(item.get("comment") or item.get("user_comment_to_supplier"))
            or clean_text(item.get("supplier_hint"))
            or recovered.comment
            or recovered.comment_source is not CommentSource.NONE
        ):
            continue
        recovered_query = _strip_conversational_product_leadin(recovered.product_query)
        recovered_query = strip_order_quantity_from_query(recovered_query, source_line)
        if normalized_global:
            recovered_query = re.sub(
                re.escape(normalized_global),
                " ",
                recovered_query,
                count=1,
                flags=re.I,
            ).strip(" .,;:-—–")
        current_words = re.findall(
            r"[a-zа-яё0-9]+",
            normalize_text(item.get("product_query")),
            flags=re.I,
        )
        recovered_words = re.findall(
            r"[a-zа-яё0-9]+",
            normalize_text(recovered_query),
            flags=re.I,
        )
        if not current_words or len(recovered_words) < len(current_words):
            continue
        remaining = list(recovered_words)
        for word in current_words:
            if word not in remaining:
                break
            remaining.remove(word)
        else:
            if _semantic_product_query(recovered_query) == _semantic_product_query(
                item.get("product_query") or ""
            ):
                continue
            item["product_query"] = recovered_query
            if not clean_text(item.get("source_line")):
                item["source_line"] = recovered.source_line or ""
            item["comment_source"] = CommentSource.NONE.value


def _restore_omitted_explicit_items(
    items: list[dict[str, Any]],
    deterministic: list[ExtractedItem],
    global_comment: str = "",
) -> list[dict[str, Any]]:
    """Добавляет только конкретные позиции, явно найденные детерминированным разбором."""
    if not deterministic or len(deterministic) <= len(items):
        return items

    restored = list(items)
    known_queries = [clean_text(item.get("product_query")) for item in restored]
    normalized_global = normalize_text(global_comment).strip(" .,;:-—–")
    for recovered in deterministic:
        recovered_query = clean_text(recovered.product_query)
        if not recovered_query or not _contains_product_query_word(recovered_query):
            continue
        normalized_query = normalize_text(recovered_query).strip(" .,;:-—–")
        if normalized_global and (
            normalized_query == normalized_global
            or normalized_query in normalized_global
            or normalized_global in normalized_query
        ):
            continue
        if recovered.comment and not recovered.quantity:
            continue
        if any(
            _query_is_already_represented(known_query, recovered_query)
            for known_query in known_queries
            if known_query
        ):
            continue
        restored.append(recovered.model_dump())
        known_queries.append(recovered_query)
    return restored
