"""Содержит операции над комментариями черновика без привязки к каналу."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from restaurant_bot.catalog.evidence import NumericEvidence, numeric_evidence
from restaurant_bot.conversation.selection import contains_score
from restaurant_bot.domain.models import (
    CartItem,
    CommentSource,
    ConversationState,
    ExtractedItem,
    ItemStatus,
    SessionStage,
)
from restaurant_bot.domain.text import clean_text, normalize_text
from restaurant_bot.domain.units import UNIT_ALIASES, normalize_unit
from restaurant_bot.parsing.comment_policy import comment_semantic_key
from restaurant_bot.parsing.comment_scope import (
    has_explicit_global_comment_scope,
    has_explicit_group_comment_scope,
    has_explicit_order_comment_scope,
    parse_comment_scope_text,
    strip_explicit_comment_scope_prefix,
)
from restaurant_bot.parsing.number_words import NUMBER_WORDS
from restaurant_bot.parsing.semantic.measurements import extract_semantic_facts
from restaurant_bot.parsing.semantic.models import SemanticFactKind

_COMMENT_ACTION_RE = re.compile(
    r"\b(?:привез\w*|достав\w*|постав\w*|упаков\w*|полож\w*|выбер\w*|"
    r"желательн\w*|обязательн\w*|позвон\w*|смешив\w*)\b",
    flags=re.I,
)


@dataclass(frozen=True, slots=True)
class CommentTargetResolution:
    """Хранит безопасно разделённую цель и текст комментария."""

    item: CartItem | None
    target_query: str
    comment_text: str
    ambiguous: bool = False


@dataclass(frozen=True, slots=True)
class CommentOwnershipResolution:
    """Хранит детерминированного владельца комментария по стабильным ID."""

    scope: str
    item_ids: tuple[str, ...] = ()
    comment_text: str = ""


def resolve_comment_ownership(
    state: ConversationState,
    source_text: str,
    comment_text: str,
    candidate_item_ids: Sequence[str],
) -> CommentOwnershipResolution:
    """Разрешает владельца комментария после создания стабильных позиций."""
    comment = clean_text(comment_text).strip(" .,;:-—–")
    source = clean_text(source_text)
    if not comment or normalize_text(comment) not in normalize_text(source):
        return CommentOwnershipResolution("rejected", comment_text=comment)
    if any(
        fact.kind
        in {
            SemanticFactKind.ORDER_QUANTITY,
            SemanticFactKind.MEASUREMENT,
            SemanticFactKind.CATALOG_ATTRIBUTE,
        }
        for fact in extract_semantic_facts(comment)
    ):
        return CommentOwnershipResolution("rejected", comment_text=comment)

    active_by_id = {item.id: item for item in state.cart if item.status is not ItemStatus.SKIPPED}
    candidates = [
        active_by_id[item_id]
        for item_id in dict.fromkeys(candidate_item_ids)
        if item_id in active_by_id
    ]
    if not candidates:
        return CommentOwnershipResolution("rejected", comment_text=comment)

    context = _comment_sentence_context(source, comment)
    if has_explicit_global_comment_scope(context) or has_explicit_order_comment_scope(context):
        return CommentOwnershipResolution(
            "order",
            comment_text=strip_explicit_comment_scope_prefix(comment) or comment,
        )

    matched = [
        item
        for item in candidates
        if max(
            contains_score(normalize_text(item.source_query), normalize_text(context)),
            contains_score(normalize_text(item.catalog_name), normalize_text(context)),
        )
        > 0
    ]
    if len(matched) == 1:
        owner = matched[0]
        if _comment_is_catalog_fact(comment, owner):
            return CommentOwnershipResolution("rejected", comment_text=comment)
        return CommentOwnershipResolution("item", (owner.id,), comment)
    if len(matched) >= 2:
        if has_explicit_group_comment_scope(context) or len(matched) < len(candidates):
            return CommentOwnershipResolution(
                "group",
                tuple(item.id for item in matched),
                comment,
            )
        return CommentOwnershipResolution(
            "ambiguous",
            tuple(item.id for item in matched),
            comment,
        )
    if len(candidates) == 1:
        owner = candidates[0]
        if _comment_is_catalog_fact(comment, owner):
            return CommentOwnershipResolution("rejected", comment_text=comment)
        return CommentOwnershipResolution("item", (owner.id,), comment)
    return CommentOwnershipResolution(
        "ambiguous",
        tuple(item.id for item in candidates),
        comment,
    )


def _comment_sentence_context(source_text: str, comment_text: str) -> str:
    """Возвращает предложение комментария вместе с его локальным target."""
    source = normalize_text(source_text)
    comment = normalize_text(comment_text)
    start = source.rfind(comment)
    if start < 0:
        return source
    sentence_start = max(source.rfind(mark, 0, start) for mark in (".", "!", "?", ";", "\n"))
    sentence_end_candidates = [source.find(mark, start + len(comment)) for mark in ".!?;\n"]
    sentence_end = min(
        (value for value in sentence_end_candidates if value >= 0), default=len(source)
    )
    return source[sentence_start + 1 : sentence_end].strip()


def _comment_is_catalog_fact(comment: str, item: CartItem) -> bool:
    """Проверяет, не является ли комментарий фактом каталога позиции."""
    cleaned = remove_catalog_fact_comments(
        comment,
        item.source_query,
        item.catalog_name,
        source_line=item.source_line,
        include_source_query=True,
    )
    return not cleaned and not _COMMENT_ACTION_RE.search(normalize_text(comment))


def reconcile_comment_target(
    state: ConversationState,
    target_query: str,
    comment_text: str,
) -> CommentTargetResolution:
    """Уточняет границу товара по уникальному совпадению с активным черновиком."""
    target = clean_text(target_query).strip(" .,;:-—–")
    comment = clean_text(comment_text).strip(" .,;:-—–")
    active_items = [item for item in state.cart if item.status is not ItemStatus.SKIPPED]
    if not comment:
        item = _unique_comment_item(active_items, target)
        return CommentTargetResolution(item, target, comment, item is None and bool(target))

    explicit_item = _explicit_comment_item(active_items, target)
    if explicit_item is not None:
        return CommentTargetResolution(explicit_item, target, comment)

    token_matches = re.findall(r"[a-zа-яё0-9%]+", f"{target} {comment}", flags=re.I)
    words = [normalize_text(token) for token in token_matches]
    if len(words) < 2:
        item = _unique_comment_item(active_items, target)
        return CommentTargetResolution(item, target, comment, item is None and bool(target))

    matches: list[tuple[int, int, CartItem]] = []
    for split in range(1, len(words)):
        prefix = " ".join(words[:split])
        scored = [
            (
                max(
                    contains_score(prefix, normalize_text(item.source_query)),
                    contains_score(prefix, normalize_text(item.catalog_name)),
                ),
                item,
            )
            for item in active_items
        ]
        scored.sort(key=lambda pair: pair[0])
        if not scored or scored[-1][0] <= 0:
            continue
        best_score = scored[-1][0]
        best_items = [item for score, item in scored if score == best_score]
        if len(best_items) == 1:
            matches.append((best_score, split, best_items[0]))

    if not matches:
        item = _unique_comment_item(active_items, target)
        return CommentTargetResolution(item, target, comment, item is None and bool(target))

    best_score = max(score for score, _, _ in matches)
    best_matches = [(split, item) for score, split, item in matches if score == best_score]
    if len({item.id for _, item in best_matches}) > 1:
        return CommentTargetResolution(None, target, comment, ambiguous=True)
    split, item = min(best_matches, key=lambda pair: pair[0])
    return CommentTargetResolution(
        item,
        " ".join(token_matches[:split]),
        " ".join(token_matches[split:]),
    )


def _unique_comment_item(active_items: list[CartItem], target: str) -> CartItem | None:
    """Возвращает единственную позицию, совпавшую с целью комментария."""
    if not target:
        return None
    scored = [
        (
            max(
                contains_score(normalize_text(target), normalize_text(item.source_query)),
                contains_score(normalize_text(target), normalize_text(item.catalog_name)),
            ),
            item,
        )
        for item in active_items
    ]
    scored.sort(key=lambda pair: pair[0])
    if not scored or scored[-1][0] <= 0:
        return None
    if len(scored) > 1 and scored[-1][0] == scored[-2][0]:
        return None
    return scored[-1][1]


def _explicit_comment_item(active_items: list[CartItem], target: str) -> CartItem | None:
    """Возвращает позицию только при точном совпадении целевой фразы с названием."""
    target_tokens = _comment_tokens(target)
    if not target_tokens:
        return None
    matches = [
        item
        for item in active_items
        if any(
            _contains_exact_token_phrase(target_tokens, _comment_tokens(candidate))
            for candidate in (item.source_query, item.catalog_name)
        )
    ]
    return matches[0] if len(matches) == 1 else None


def _comment_tokens(value: str) -> list[str]:
    """Разбивает название товара на нормализованные слова для точной проверки."""
    return [normalize_text(token) for token in re.findall(r"[a-zа-яё0-9%]+", value, flags=re.I)]


def _contains_exact_token_phrase(phrase: list[str], candidate: list[str]) -> bool:
    """Проверяет наличие последовательности слов без морфологической подмены."""
    if not phrase or len(phrase) > len(candidate):
        return False
    return any(
        candidate[index : index + len(phrase)] == phrase
        for index in range(len(candidate) - len(phrase) + 1)
    )


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
    fragments = normalized_comment_fragments(global_comment)
    if not fragments:
        return
    for item in state.cart:
        if item.status != ItemStatus.SKIPPED:
            append_item_comment(item, global_comment)
            known = {comment_semantic_key(fragment) for fragment in item.order_comment_fragments}
            for fragment in fragments:
                key = comment_semantic_key(fragment)
                if key and key not in known:
                    item.order_comment_fragments.append(fragment)
                    known.add(key)


def remove_order_comments(state: ConversationState) -> bool:
    """Удаляет только подтверждённые общие фрагменты комментариев."""
    removed = False
    for item in state.cart:
        if item.status == ItemStatus.SKIPPED or not item.order_comment_fragments:
            continue
        global_keys = {
            comment_semantic_key(fragment)
            for fragment in item.order_comment_fragments
            if comment_semantic_key(fragment)
        }
        kept = [
            fragment
            for fragment in item.comment.split(";")
            if comment_semantic_key(fragment) not in global_keys
        ]
        cleaned = "; ".join(part.strip(" .,;:-—–") for part in kept if part.strip(" .,;:-—–"))
        if cleaned != item.comment:
            removed = True
            item.comment = cleaned
            if not cleaned:
                item.comment_source = CommentSource.NONE
        item.order_comment_fragments = []
    return removed


def clear_all_active_comments(state: ConversationState) -> None:
    """Удаляет комментарии только у активных позиций черновика."""
    for item in state.cart:
        if item.status != ItemStatus.SKIPPED:
            clear_item_comment(item)


def clear_item_comment(item: CartItem) -> None:
    """Удаляет комментарий позиции и сбрасывает его происхождение."""
    item.comment = ""
    item.order_comment_fragments = []
    item.comment_source = CommentSource.NONE


def append_item_comment(item: CartItem, comment: str) -> None:
    """Добавляет семантический комментарий позиции без повторов."""
    item.comment = merge_comments(_apply_local_correction(item.comment, comment))
    item.comment_source = CommentSource.SEMANTIC


def normalized_comment_fragments(comment: str) -> list[str]:
    """Возвращает непустые нормализованные фрагменты общего комментария."""
    result: list[str] = []
    seen: set[str] = set()
    for value in str(comment or "").split(";"):
        fragment = " ".join(value.split()).strip(" .,;:-—–")
        key = comment_semantic_key(fragment)
        if fragment and key not in seen:
            result.append(fragment)
            seen.add(key)
    return result


def _apply_local_correction(existing: str, addition: str) -> str:
    """Заменяет исправленный фрагмент, сохраняя остальные пожелания."""
    correction = re.fullmatch(
        r"\s*не\s+(?P<old>.+?)\s*,?\s+(?:а|но|только)\s+(?P<new>.+?)\s*",
        clean_text(addition),
        flags=re.IGNORECASE,
    )
    if correction is None or not existing:
        return merge_comments(existing, addition)

    old = correction.group("old").strip(" .,;:-—–")
    new = correction.group("new").strip(" .,;:-—–")
    if not old or not new:
        return merge_comments(existing, addition)

    normalized_old = normalize_text(old)
    replaced = False
    fragments: list[str] = []
    for fragment in existing.split(";"):
        current = fragment.strip(" .,;:-—–")
        normalized_current = normalize_text(current)
        if normalized_old == normalized_current:
            fragments.append(new)
            replaced = True
            continue
        marker = re.search(
            rf"(?<![\wа-яё]){re.escape(old)}(?![\wа-яё])",
            current,
            flags=re.IGNORECASE,
        )
        if marker is None:
            fragments.append(current)
            continue
        fragments.append(f"{current[: marker.start()]}{new}{current[marker.end() :]}")
        replaced = True

    if not replaced:
        return merge_comments(existing, addition)
    return "; ".join(part.strip(" .,;:-—–") for part in fragments if part.strip(" .,;:-—–"))


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

    def remove_catalog_numeric_fragments(value: str) -> str:
        """Удаляет только числовые фрагменты, подтверждённые названием каталога."""
        normalized = normalize_text(value)
        catalog_numbers = numeric_evidence(product_name)
        source_numbers = numeric_evidence(normalized)
        if not catalog_numbers or not source_numbers:
            return value

        def same_number(left: NumericEvidence, right: NumericEvidence) -> bool:
            """Сравнивает число, диапазон и единицу измерения."""
            if abs(left.value - right.value) > 1e-9:
                return False
            if (left.upper_value is None) != (right.upper_value is None):
                return False
            if left.upper_value is not None:
                assert right.upper_value is not None
                if abs(left.upper_value - right.upper_value) > 1e-9:
                    return False
            return (
                not left.unit
                or not right.unit
                or normalize_unit(left.unit) == normalize_unit(right.unit)
            )

        intervals: list[tuple[int, int]] = []
        for source_number in source_numbers:
            if not any(
                same_number(source_number, catalog_number) for catalog_number in catalog_numbers
            ):
                continue
            start, end = source_number.start, source_number.end
            if source_number.unit:
                unit_pattern = "|".join(
                    sorted((re.escape(unit) for unit in UNIT_ALIASES), key=len, reverse=True)
                )
                unit_match = re.match(rf"\s*(?:{unit_pattern})", normalized[end:], flags=re.I)
                if unit_match:
                    end += unit_match.end()
            prefix = normalized[:start]
            connector = re.search(r"(?:\s+(?:на|и|/)\s*)+$", prefix, flags=re.I)
            if connector:
                start = connector.start()
            elif re.fullmatch(r"\s*(?:на|и|/)\s*", prefix, flags=re.I):
                start = 0
            intervals.append((start, end))
        if not intervals:
            return value
        masked = list(normalized)
        for start, end in intervals:
            masked[start:end] = [" "] * (end - start)
        return clean_text("".join(masked)).strip(" ,;:.-")

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
        numeric_residue = remove_catalog_numeric_fragments(cleaned)
        if numeric_residue != cleaned:
            cleaned = numeric_residue
            if not cleaned:
                continue
            part_words = canonical_words(cleaned)
            normalized_part_words = [packaging_aliases.get(word, word) for word in part_words]
            part_compact = compact_words(cleaned)
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
    state.pending_comment_target_item_ids = []
    state.pending_comment_text = ""
    state.pending_comment_global_comment = ""


def has_pending_comment_data(state: ConversationState) -> bool:
    """Проверяет сохранённый комментарий до завершения товарных вопросов."""
    return bool(
        clean_text(state.pending_comment_text).strip()
        and (
            state.pending_comment_target_item_ids
            or state.pending_comment_existing_item_ids
            or state.pending_comment_items
        )
    )


def has_pending_comment_scope(state: ConversationState) -> bool:
    """Проверяет, что в состоянии есть действующее уточнение области комментария."""
    if state.stage is not SessionStage.AWAIT_COMMENT_SCOPE:
        return False
    if not clean_text(state.pending_comment_text).strip():
        return False
    if comment_scope_existing_items(state):
        return True
    return any(
        clean_text(item.product_query or item.source_line).strip()
        for item in state.pending_comment_items
    )


def prune_pending_comment_item_ids(state: ConversationState) -> None:
    """Удаляет из pending scope идентификаторы пропущенных позиций."""
    if not state.pending_comment_existing_item_ids and not state.pending_comment_target_item_ids:
        return
    active_ids = {item.id for item in state.cart if item.status is not ItemStatus.SKIPPED}
    state.pending_comment_existing_item_ids = [
        item_id for item_id in state.pending_comment_existing_item_ids if item_id in active_ids
    ]
    state.pending_comment_target_item_ids = [
        item_id for item_id in state.pending_comment_target_item_ids if item_id in active_ids
    ]


def comment_scope_existing_items(state: ConversationState) -> list[CartItem]:
    """Возвращает сохранённые позиции черновика в порядке показа уточнения."""
    by_id = {item.id: item for item in state.cart if item.status is not ItemStatus.SKIPPED}
    target_ids = state.pending_comment_target_item_ids or state.pending_comment_existing_item_ids
    return [by_id[item_id] for item_id in dict.fromkeys(target_ids) if item_id in by_id]


def pending_comment_target_items(state: ConversationState) -> list[CartItem]:
    """Возвращает стабильные позиции, которым разрешено назначить комментарий."""
    by_id = {item.id: item for item in state.cart if item.status is not ItemStatus.SKIPPED}
    return [
        by_id[item_id]
        for item_id in dict.fromkeys(state.pending_comment_target_item_ids)
        if item_id in by_id
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
            source_span=item.source_span,
        )
        for item in comment_scope_existing_items(state)
    ]
    return existing + [item.model_copy(deep=True) for item in state.pending_comment_items]


def resolve_comment_scope_text(
    text: str,
    scope_items: Sequence[ExtractedItem],
) -> tuple[str, list[int], float] | None:
    """Разрешает текстовую область комментария только среди показанных позиций."""
    lexical = parse_comment_scope_text(text, len(scope_items))
    if lexical is not None:
        return lexical
    normalized = normalize_text(text).strip(" .,;:!?—–-")
    if not normalized:
        return None
    if re.fullmatch(r"(?:для|к|ко)\s+одн\w*\s+товар\w*", normalized):
        return "ambiguous", [], 0.0

    ordinal_patterns = (
        (r"перв\w*", 0),
        (r"втор\w*", 1),
        (r"трет\w*", 2),
        (r"четверт\w*", 3),
        (r"пят\w*", 4),
    )
    ordinal_prefix = r"(?:(?:только\s+)?(?:для|к|ко)\s+|только\s+)?"
    for pattern, index in ordinal_patterns:
        if re.fullmatch(
            rf"{ordinal_prefix}{pattern}(?:\s+товар\w*)?",
            normalized,
        ):
            if index >= len(scope_items):
                return "ambiguous", [], 0.0
            return "items", [index], 1.0

    target_match = re.fullmatch(
        r"(?:только\s+)?(?:для|к|ко)\s+(?P<target>.+)",
        normalized,
    )
    if target_match is None:
        return None
    target = re.sub(r"\s+товар\w*$", "", target_match.group("target")).strip()
    if not target:
        return "ambiguous", [], 0.0
    if re.search(r"\b(?:перв|втор|трет|четверт|пят)\w*\s+и\s+", target):
        return None
    scores = [
        contains_score(normalize_text(target), normalize_text(item.product_query))
        for item in scope_items
    ]
    best_score = max(scores, default=0)
    if best_score <= 0 or scores.count(best_score) != 1:
        return "ambiguous", [], 0.0
    return "items", [scores.index(best_score)], 1.0
