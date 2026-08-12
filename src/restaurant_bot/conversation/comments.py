"""Содержит channel-neutral операции над комментариями черновика."""

from __future__ import annotations

import re

from restaurant_bot.domain.models import (
    CartItem,
    CommentSource,
    ConversationState,
    ExtractedItem,
    ItemStatus,
)
from restaurant_bot.parsing.comment_policy import comment_semantic_key
from restaurant_bot.domain.units import UNIT_ALIASES, normalize_unit
from restaurant_bot.parsing.number_words import NUMBER_WORDS
from restaurant_bot.text_normalization import clean_text, normalize_text


def merge_comments(*values: str) -> str:
    """Объединяет комментарии engine с нормализацией пробелов и без повторов."""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        for part in str(value or "").split(";"):
            comment = " ".join(part.split()).strip(" .,;")
            key = comment_semantic_key(comment)
            if comment and key not in seen:
                result.append(comment)
            seen.add(key)
    return "; ".join(result)


def merge_scope_comments(*values: str) -> str:
    """Объединяет комментарии CommentScopeHandler без схлопывания пробелов."""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        for part in str(value or "").split(";"):
            cleaned = part.strip(" .,;")
            key = comment_semantic_key(cleaned)
            if cleaned and key not in seen:
                result.append(cleaned)
                seen.add(key)
    return "; ".join(result)


def remove_exact_comment_fragments(comment: str, excluded: str) -> str:
    """Отделяет старое примечание каталога от комментария заявки."""
    excluded_parts = {
        normalize_text(part).strip(" .,;")
        for part in str(excluded or "").split(";")
        if normalize_text(part).strip(" .,;")
    }
    kept = [
        part.strip(" .,;")
        for part in str(comment or "").split(";")
        if part.strip(" .,;") and normalize_text(part).strip(" .,;") not in excluded_parts
    ]
    return "; ".join(kept)


def remove_global_comment_overlap(item_comment: str, global_comment: str) -> str:
    """Удаляет общую часть и разговорные слова охвата из локального комментария."""
    item_text = clean_text(item_comment).strip(" .,;")
    global_text = clean_text(global_comment).strip(" .,;")
    if not item_text or not global_text:
        return item_text
    match = re.search(re.escape(global_text), item_text, flags=re.I)
    if match is None:
        return item_text
    remaining = f"{item_text[: match.start()]} {item_text[match.end() :]}"
    scope_residue = (
        r"(?:(?:все|всё|всем)"
        r"(?:\s+(?:это(?:\s+дело)?|эти\w*"
        r"(?:\s+(?:товар\w*|позици\w*))?|товар\w*|позици\w*))?"
        r"|для\s+всех(?:\s+(?:товар\w*|позици\w*))?)"
    )
    remaining = re.sub(
        rf"(?:\b(?:и|а)\s+)?{scope_residue}\s*(?=$|[,;:—–-])",
        " ",
        remaining,
        flags=re.I,
    )
    remaining = re.sub(r"\s+", " ", remaining)
    return remaining.strip(" .,;:-—–")


def apply_global_comment(state: ConversationState, global_comment: str) -> None:
    """Добавляет общий комментарий один раз ко всем активным товарам заявки."""
    for item in state.cart:
        if item.status != ItemStatus.SKIPPED:
            item.comment = merge_comments(item.comment, global_comment)
            item.comment_source = CommentSource.SEMANTIC


def remove_cart_comment_shadows(state: ConversationState) -> None:
    """Удаляет ложные позиции, созданные из комментариев."""
    shadow_ids: set[str] = set()
    for owner in state.cart:
        comments = {
            normalize_text(part).strip(" .,;")
            for part in owner.comment.split(";")
            if normalize_text(part).strip(" .,;")
        }
        if not comments:
            continue
        for shadow in state.cart:
            if shadow.id == owner.id or shadow.catalog_product_id:
                continue
            if normalize_text(shadow.source_query).strip(" .,;") not in comments:
                continue
            if owner.quantity is None and shadow.quantity is not None:
                owner.quantity = shadow.quantity
                owner.unit = shadow.unit or owner.unit
            shadow_ids.add(shadow.id)
    if not shadow_ids:
        return
    state.cart = [item for item in state.cart if item.id not in shadow_ids]
    if state.current_issue_item_id in shadow_ids:
        state.current_issue_item_id = ""


def normalize_existing_catalog_comments(state: ConversationState) -> None:
    """Очищает подтверждённые ошибки нормализации в сохранённом черновике."""
    for item in state.cart:
        if not item.catalog_name or not item.comment:
            continue
        item.comment = remove_catalog_fact_comments(
            item.comment,
            item.source_query,
            item.catalog_name,
            include_source_query=True,
        )
        if not item.comment:
            item.comment_source = CommentSource.NONE


def comment_left_after_catalog_match(query: str, product_name: str) -> str:
    """Извлекает комментарий, оставшийся после поиска товара."""
    query_words = re.findall(r"[a-zа-я0-9]+", normalize_text(query), flags=re.I)
    product_words = re.findall(r"[a-zа-я0-9]+", normalize_text(product_name), flags=re.I)
    if not query_words or not product_words:
        return ""

    matched_query_indexes: set[int] = set()
    next_query_index = 0
    for product_word in product_words:
        match_index = next(
            (
                index
                for index in range(next_query_index, len(query_words))
                if query_words[index] == product_word
            ),
            None,
        )
        if match_index is None:
            continue
        matched_query_indexes.add(match_index)
        next_query_index = match_index + 1

    required_matches = min(2, len(product_words))
    if len(matched_query_indexes) < required_matches:
        return ""

    remainder = [
        word for index, word in enumerate(query_words) if index not in matched_query_indexes
    ]
    return " ".join(remainder).strip(" .,;")


def remove_catalog_fact_comments(
    comment: str,
    source_query: str,
    product_name: str,
    *,
    source_line: str = "",
    include_source_query: bool = False,
) -> str:
    """Убирает из комментария характеристики, подтверждённые каталогом."""

    def canonical_words(value: str) -> list[str]:
        """Канонизирует числа и подпись «номер» для сравнения с каталогом."""
        words = re.findall(r"[a-zа-яё0-9]+", normalize_text(value), flags=re.I)
        result: list[str] = []
        index = 0
        while index < len(words):
            word = words[index]
            if word == "номер" and index + 1 < len(words):
                number = NUMBER_WORDS.get(words[index + 1])
                if number is not None and float(number).is_integer():
                    result.append(str(int(number)))
                    index += 2
                    continue
            if word in NUMBER_WORDS and float(NUMBER_WORDS[word]).is_integer():
                result.append(str(int(NUMBER_WORDS[word])))
            else:
                result.append(word)
            index += 1
        return result

    def compact_words(value: str) -> set[str]:
        """Возвращает компактные формы для «д/п», «дп» и похожих записей."""
        compact = {
            re.sub(r"[^a-zа-яё0-9]", "", word)
            for word in canonical_words(value)
            if re.sub(r"[^a-zа-яё0-9]", "", word)
        }
        compact.update(
            re.sub(r"[^a-zа-яё0-9]", "", group)
            for group in re.findall(
                r"[a-zа-яё0-9]+(?:[/.-][a-zа-яё0-9]+)+",
                normalize_text(value),
                flags=re.I,
            )
        )
        return compact

    catalog_facts = set(canonical_words(product_name))
    query_facts = set(canonical_words(source_query)) if include_source_query else set()
    compact_catalog_facts = compact_words(product_name)
    compact_query_facts = compact_words(source_query) if include_source_query else set()
    packaging_aliases = {
        "коробка": "кор",
        "коробке": "кор",
        "коробки": "кор",
        "короб": "кор",
    }
    normalized_unit_words = {normalize_text(alias) for alias in UNIT_ALIASES} | {
        normalize_text(unit) for unit in UNIT_ALIASES.values()
    }
    normalized_unit_values = {normalize_text(unit) for unit in UNIT_ALIASES.values()}

    def is_quantity_only_part(words: list[str]) -> bool:
        """Определяет остаток количества, который не является пожеланием."""
        has_number = False
        has_unit = False
        for word in words:
            if word == "и":
                continue
            if re.fullmatch(r"\d+(?:[.,]\d+)?", word) or word in NUMBER_WORDS:
                has_number = True
                continue
            if (
                word in normalized_unit_words
                or normalize_text(normalize_unit(word)) in normalized_unit_values
            ):
                has_unit = True
                continue
            return False
        return bool(words) and has_number and has_unit

    kept: list[str] = []
    for part in re.split(r"[;,]", str(comment or "")):
        cleaned = " ".join(part.split()).strip(" .,;")
        if not cleaned:
            continue
        part_words = canonical_words(cleaned)
        normalized_part_words = [packaging_aliases.get(word, word) for word in part_words]
        part_compact = compact_words(cleaned)
        if is_quantity_only_part(part_words):
            continue
        normalized_part = normalize_text(cleaned)
        if re.search(
            r"(?:^|\s)(?:без|не|только|желательно|обязательно|если|привез\w*|достав\w*|полож\w*)\b",
            normalized_part,
            flags=re.IGNORECASE,
        ):
            kept.append(cleaned)
            continue
        if (
            include_source_query
            and source_line
            and normalized_part
            and f" {normalized_part} " in f" {normalize_text(source_line)} "
            and f" {normalized_part} " in f" {normalize_text(source_query)} "
        ):
            kept.append(cleaned)
            continue
        all_catalog_facts = all(
            word in catalog_facts
            or word in query_facts
            or word in {"в", "на", "из", "по"}
            or packaging_aliases.get(word, word) in catalog_facts
            for word in normalized_part_words
        )
        compact_catalog_match = bool(part_compact) and all(
            word in compact_catalog_facts or word in compact_query_facts for word in part_compact
        )
        if part_words and (all_catalog_facts or compact_catalog_match):
            continue
        kept.append(cleaned)
    return "; ".join(kept)


def clear_pending_comment(state: ConversationState) -> None:
    """Удаляет временные данные выбора области комментария."""
    state.pending_comment_items = []
    state.pending_comment_existing_item_ids = []
    state.pending_comment_text = ""
    state.pending_comment_global_comment = ""


def prune_pending_comment_item_ids(state: ConversationState) -> None:
    """Удаляет из pending scope идентификаторы пропущенных позиций."""
    if not state.pending_comment_existing_item_ids:
        return
    active_ids = {item.id for item in state.cart if item.status is not ItemStatus.SKIPPED}
    state.pending_comment_existing_item_ids = [
        item_id for item_id in state.pending_comment_existing_item_ids if item_id in active_ids
    ]


def comment_scope_existing_items(state: ConversationState) -> list[CartItem]:
    """Возвращает сохранённые позиции черновика в порядке показа уточнения."""
    by_id = {item.id: item for item in state.cart if item.status is not ItemStatus.SKIPPED}
    return [
        by_id[item_id] for item_id in state.pending_comment_existing_item_ids if item_id in by_id
    ]


def comment_scope_items(state: ConversationState) -> list[ExtractedItem]:
    """Объединяет позиции черновика и новые позиции для выбора области комментария."""
    existing = [
        ExtractedItem(
            product_query=item.catalog_name or item.source_query,
            quantity=item.quantity,
            unit=item.unit or item.catalog_unit,
            comment=item.comment,
            user_comment_to_supplier=item.comment,
            comment_source=item.comment_source,
            source_line=item.source_line,
        )
        for item in comment_scope_existing_items(state)
    ]
    return existing + [item.model_copy(deep=True) for item in state.pending_comment_items]
