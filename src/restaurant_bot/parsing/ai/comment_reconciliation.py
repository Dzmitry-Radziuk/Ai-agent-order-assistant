"""Содержит проверку, связывание и восстановление комментариев ИИ."""

from __future__ import annotations

import re
from typing import Any

from restaurant_bot.conversation.comments import remove_global_comment_overlap
from restaurant_bot.domain.models import CommentSource
from restaurant_bot.domain.text import clean_text, normalize_text
from restaurant_bot.parsing.ai.quantity_reconciliation import _remove_matching_quantity
from restaurant_bot.parsing.comment_policy import explicit_supplier_comment, supplier_comment_start
from restaurant_bot.parsing.comment_scope import (
    has_explicit_global_comment_scope,
    has_explicit_group_comment_scope,
    has_explicit_order_comment_scope,
)
from restaurant_bot.parsing.numeric import to_float
from restaurant_bot.parsing.semantic.boundaries import (
    build_item_references,
    query_anchor_overlap,
)
from restaurant_bot.parsing.semantic.measurements import (
    extract_semantic_facts,
    is_catalog_tail_text,
)
from restaurant_bot.parsing.semantic.models import SemanticFactKind

_COMMENT_BINDING_CONFIDENCE = 0.9
_COMMENT_ACTION_RE = re.compile(
    r"\b(?:разлож\w*|привез\w*|достав\w*|постав\w*|упаков\w*|выбер\w*|"
    r"желательн\w*|обязательн\w*|позвон\w*|смешив\w*)\b",
    flags=re.I,
)


def _comment_source_is_authorized(comment: str, source_text: str) -> bool:
    """Проверяет, что текст комментария не является числовым фактом каталога."""
    value = normalize_text(comment).strip(" .,;:-—–")
    source = normalize_text(source_text)
    if not value or value not in source:
        return False
    if re.search(r"\b(?:примерно|приблизительно|около)\s+\d", value, flags=re.I):
        return False
    if is_catalog_tail_text(comment, source_text) and not _COMMENT_ACTION_RE.search(value):
        return False
    facts = extract_semantic_facts(source_text)
    if any(
        fact.kind is SemanticFactKind.ORDER_QUANTITY and normalize_text(fact.original_text) in value
        for fact in facts
    ):
        return False
    return not any(
        fact.kind
        in {
            SemanticFactKind.CATALOG_ATTRIBUTE,
            SemanticFactKind.MEASUREMENT,
            SemanticFactKind.ORDER_QUANTITY,
        }
        and value == normalize_text(fact.original_text)
        for fact in extract_semantic_facts(source_text)
    )


_RELATIONAL_COMMENT_RE = re.compile(
    r"\b(?:отдельно|раздельно|вместе|по\s+отдельности|не\s+смешивать)\b",
    flags=re.I,
)

_CONVERSATIONAL_PRODUCT_LEADIN_RE = re.compile(
    r"""
    ^\s*
    (?:(?:ну|так|значит|слушай|слушайте)\s*[,;:]?\s*)*
    (?:(?:пожалуйста|прошу|будь\s+добр\w*|будьте\s+(?:добр\w*|любезн\w*))
       \s*[,;:]?\s*)?
    (?:
        (?:мне|нам)\s+
        (?:надо|нужно|нуж\w*|понадоб\w*|требу\w*)
        (?:\s+(?:добавить|заказать|внести|записать|включить|положить|оформить))?
      | (?:я|мы)\s+
        (?:хо(?:ч|т)\w*|планиру\w*|собира\w*|буду|будем)
        (?:\s+(?:добавить|заказать|внести|записать|включить|положить|оформить))?
      | (?:можно|можешь|можете)
        (?:\s+(?:мне|нам))?
        (?:\s+(?:добавить|заказать|внести|записать|включить|положить|оформить))?
      | давай(?:те)?
        (?:\s+(?:добавим|закажем|внесём|внесем|запишем|включим|положим|оформим))?
      | (?:добавь|добавьте|закажи|закажите|внеси|внесите|запиши|запишите|
          включи|включите|положи|положите|возьми|возьмите|оформи|оформите|
          подбери|подберите|найди|найдите)
        (?:\s+(?:мне|нам))?
      | (?:надо|нужно|требуется|хотелось\s+бы)
        (?:\s+(?:добавить|заказать|внести|записать|включить|положить|оформить))?
    )\b
    (?:\s+(?:в|к)\s+(?:заказ\w*|заявк\w*|черновик\w*))?
    (?:\s*[,;:]?\s*(?:пожалуйста|прошу)\b)?
    \s*[,;:]?\s*
    """,
    flags=re.I | re.X,
)

_COMMENT_SCOPE_ALL_RE = re.compile(
    r"\b(?:все|всё|всем|оба|обе|обоих|обеих|обоим|обеим|кажд\w*|перечисленн\w*)\b"
    r"|\b(?:для|к|ко)\s+(?:них|ним|этих|этим)(?:\s+товар\w*)?\b",
    flags=re.I,
)

_COMMENT_SCOPE_POSITION_RE = re.compile(
    r"\b(?:перв\w*|втор\w*|трет\w*|четверт\w*|пят\w*|последн\w*|"
    r"предпоследн\w*|позици\w*\s*№?\s*\d+|товар\w*\s*№?\s*\d+|"
    r"\d+\s*[-–—]?\s*(?:й|я|е|ю))\b",
    flags=re.I,
)

_TRAILING_ROOT_PROCESSING_RE = re.compile(
    r"(?P<instruction>"
    r"(?:"
    r"(?:срез|длина)\s+корн(?:я|ей)"
    r"|(?:срезать|обрезать|подрезать|укоротить|оставить)\s+корн\w*"
    r"|корн\w*\s+(?:срезать|обрезать|подрезать|укоротить|оставить)"
    r")"
    r"[^.!?]*?\d+(?:[,.]\d+)?\s*"
    r"(?:см|сантиметр\w*|мм|миллиметр\w*)"
    r"(?:\s*,?\s*(?:не\s+больше|не\s+более))?"
    r")\s*[.!?]*$",
    flags=re.I,
)

_ROOT_PROCESSING_QUERY_RE = re.compile(
    r"^(?:"
    r"(?:срез|длина)\s+корн(?:я|ей)"
    r"|(?:срезать|обрезать|подрезать|укоротить|оставить)\s+корн\w*"
    r"|корн\w*\s+(?:срезать|обрезать|подрезать|укоротить|оставить)"
    r")$",
    flags=re.I,
)

_ROOT_PROCESSING_PREFIX_RE = re.compile(
    r"^(?:"
    r"(?:срез|длина)\s+корн(?:я|ей)"
    r"|(?:срезать|обрезать|подрезать|укоротить|оставить)\s+корн\w*"
    r"|корн\w*\s+(?:срезать|обрезать|подрезать|укоротить|оставить)"
    r")",
    flags=re.I,
)


def _strip_global_comment_scope(value: str) -> str:
    """Удаляет слова охвата из начала уже распознанного общего комментария."""
    comment = clean_text(value).strip(" .,;:-—–")
    if not comment:
        return ""
    if re.match(
        r"^\s*(?:\u0432\u0441\u0435\u0433\u043e|\u0438\u0442\u043e\u0433\u043e)\s+"
        r"(?:\u043d\u0443\u0436\u043d\u043e|\u043d\u0430\u0434\u043e|\u0442\u0440\u0435\u0431\u0443\u0435\u0442\u0441\u044f)\b",
        comment,
        flags=re.I,
    ):
        return comment
    comment = re.sub(
        r"^(?:(?:и|а)\s+)?(?:все|всё|всем|для всех)"
        r"(?:\s+(?:товар\w*|позици\w*|это(?:\s+дело)?))?"
        r"(?:\s+(?:нужно|надо|требуется|просьба))?"
        r"(?:\s*[,;:—–-]\s*|\s+)",
        "",
        comment,
        flags=re.I,
    )
    comment = re.sub(r"^(?:нужно|надо|требуется|просьба)\s+", "", comment, flags=re.I)
    return clean_text(comment).strip(" .,;:-—–")


def _append_local_item_comment(
    item: dict[str, Any],
    comment: str,
    source: CommentSource = CommentSource.SEMANTIC,
) -> None:
    """Добавляет локальный комментарий к позиции без повторения текста."""
    addition = clean_text(comment).strip(" .,;:-—–")
    if not addition:
        return
    existing = clean_text(item.get("comment") or item.get("user_comment_to_supplier")).strip(
        " .,;:-—–"
    )
    if normalize_text(addition) == normalize_text(existing):
        merged = existing
    elif existing:
        merged = f"{existing}; {addition}"
    else:
        merged = addition
    item["comment"] = merged
    item["user_comment_to_supplier"] = merged
    item["comment_source"] = source.value


def _binding_target_indexes(binding: dict[str, Any], item_count: int) -> list[int]:
    """Возвращает уникальные допустимые индексы товаров из привязки комментария."""
    indexes: list[int] = []
    for value in binding.get("target_item_indexes") or []:
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and 0 <= value < item_count
            and value not in indexes
        ):
            indexes.append(value)
    return indexes


def _validated_binding_target_indexes(
    binding: dict[str, Any],
    items: list[dict[str, Any]],
    deterministic: list[Any] | None,
) -> list[int]:
    """Переводит индекс AI в подтверждённую ссылку на товарную позицию."""
    indexes = _binding_target_indexes(binding, len(items))
    if not indexes or not deterministic:
        return indexes
    references = build_item_references(deterministic)
    resolved: list[int] = []
    for index in indexes:
        query = items[index].get("product_query") or ""
        reference_scores = [query_anchor_overlap(query, reference) for reference in references]
        best_reference_score = max(reference_scores, default=0)
        if best_reference_score <= 0:
            continue
        reference = references[reference_scores.index(best_reference_score)]
        item_scores = [
            query_anchor_overlap(item.get("product_query") or "", reference) for item in items
        ]
        best_item_score = max(item_scores, default=0)
        best_items = [
            item_index for item_index, score in enumerate(item_scores) if score == best_item_score
        ]
        if best_item_score > item_scores[index]:
            if len(best_items) != 1:
                continue
            resolved.append(best_items[0])
        else:
            resolved.append(index)
    return list(dict.fromkeys(resolved))


def _merge_global_comment(payload: dict[str, Any], comment: str) -> None:
    """Добавляет общий комментарий без повторения уже сохранённого текста."""
    addition = clean_text(comment).strip(" .,;:-—–")
    existing = clean_text(payload.get("global_comment")).strip(" .,;:-—–")
    if not addition or normalize_text(addition) == normalize_text(existing):
        return
    payload["global_comment"] = f"{existing}; {addition}" if existing else addition


def _remove_bound_comment(item: dict[str, Any], comment: str) -> None:
    """Удаляет только точную ошибочную привязку, не затрагивая другие пожелания."""
    normalized_comment = normalize_text(comment).strip(" .,;:-—–")
    existing = clean_text(item.get("comment") or item.get("user_comment_to_supplier"))
    fragments = [part.strip(" .,;:-—–") for part in existing.split(";")]
    kept = [
        part
        for part in fragments
        if part and normalize_text(part).strip(" .,;:-—–") != normalized_comment
    ]
    value = "; ".join(kept)
    item["comment"] = value
    item["user_comment_to_supplier"] = value
    if not value:
        item["comment_source"] = CommentSource.NONE.value


def _starts_as_detached_sentence(source_text: str, comment: str) -> bool:
    """Проверяет, начинается ли комментарий после границы отдельного предложения."""
    source = normalize_text(source_text)
    value = normalize_text(comment)
    start = source.rfind(value)
    if start < 0:
        return False
    return source[:start].rstrip().endswith((";", ".", "!", "?", "\n"))


def _strip_conversational_product_leadin(value: str) -> str:
    """Удаляет разговорную просьбу, которая не является комментарием к товару."""
    return _CONVERSATIONAL_PRODUCT_LEADIN_RE.sub("", clean_text(value), count=1).strip()


def _strip_product_facts_from_item_binding(
    comment: str,
    item: dict[str, Any],
    items: list[dict[str, Any]],
) -> str:
    """Оставляет в ИИ-привязке только пожелание без названия и количества товара."""
    value = clean_text(comment).strip(" .,;:-—–")
    query = clean_text(item.get("product_query")).strip(" .,;:-—–")
    if not value or not query:
        return value

    query_match = re.search(
        rf"(?<![a-zа-яё0-9]){re.escape(query)}(?![a-zа-яё0-9])",
        value,
        flags=re.I,
    )
    if query_match is None:
        return value

    remainder = f"{value[: query_match.start()]} {value[query_match.end() :]}"
    remainder, quantity_removed = _remove_matching_quantity(
        remainder,
        to_float(item.get("quantity")),
        clean_text(item.get("unit")),
    )
    if not quantity_removed:
        return value

    normalized_remainder = normalize_text(remainder)
    for other in items:
        if other is item:
            continue
        other_query = normalize_text(other.get("product_query")).strip(" .,;:-—–")
        if other_query and re.search(
            rf"(?<![a-zа-яё0-9]){re.escape(other_query)}(?![a-zа-яё0-9])",
            normalized_remainder,
            flags=re.I,
        ):
            return ""

    remainder = re.sub(r"^(?:и|а также|а)\s+", "", remainder, flags=re.I)
    remainder = re.sub(r"\s+(?:и|а также|а)$", "", remainder, flags=re.I)
    return _strip_conversational_product_leadin(remainder).strip(" .,;:-—–")


def _comment_scope_has_explicit_anchor(text: str, item_names: list[str]) -> bool:
    """Проверяет, что ответ явно указывает товары, их номера или весь список."""
    normalized = normalize_text(text)
    if _COMMENT_SCOPE_ALL_RE.search(normalized) or _COMMENT_SCOPE_POSITION_RE.search(normalized):
        return True

    answer_tokens = set(re.findall(r"[a-zа-яё0-9]+", normalized, flags=re.I))
    ignored = {
        "для",
        "товар",
        "товара",
        "товаров",
        "позиция",
        "позиции",
        "позиций",
        "комментарий",
        "это",
        "только",
    }
    answer_tokens -= ignored

    def same_reference(left: str, right: str) -> bool:
        """Сопоставляет простые падежные формы названного пользователем товара."""
        if left == right:
            return True
        common = 0
        for left_char, right_char in zip(left, right, strict=False):
            if left_char != right_char:
                break
            common += 1
        return common >= 3 and abs(len(left) - len(right)) <= 3

    for item_name in item_names:
        item_tokens = {
            token
            for token in re.findall(r"[a-zа-яё0-9]+", normalize_text(item_name), flags=re.I)
            if len(token) >= 3 and token not in ignored
        }
        if any(
            same_reference(answer_token, item_token)
            for answer_token in answer_tokens
            for item_token in item_tokens
        ):
            return True
    return False


def _comment_scope_context(source_text: str, comment: str) -> str:
    """Возвращает предложение комментария для проверки слов области действия."""
    source = normalize_text(source_text)
    value = normalize_text(comment)
    start = source.rfind(value)
    if start < 0:
        return value
    prefix = source[:start]
    boundary = max((prefix.rfind(mark) for mark in ".!?;\n"), default=-1)
    return f"{prefix[boundary + 1 :]} {value}".strip()


def _has_explicit_group_comment_scope(source_text: str, comment: str) -> bool:
    """Находит явное указание на несколько позиций без расширения до всей заявки."""
    return has_explicit_group_comment_scope(_comment_scope_context(source_text, comment))


def _is_verified_root_group_comment(comment: str, source_text: str) -> bool:
    """Подтверждает безопасное групповое требование к обработке корней."""
    match = _TRAILING_ROOT_PROCESSING_RE.search(clean_text(source_text))
    if match is None:
        return False
    instruction = clean_text(match.group("instruction")).strip(" .,;:-—–")
    return normalize_text(instruction) == normalize_text(comment)


def _discard_unverified_item_comments(
    items: list[dict[str, Any]],
    bindings: list[dict[str, Any]],
    source_text: str,
    global_comment: str,
) -> None:
    """Удаляет комментарии ИИ без привязки или явного маркера в исходной фразе."""
    bound_comments = {
        normalize_text(binding.get("text")).strip(" .,;:-—–")
        for binding in bindings
        if clean_text(binding.get("text"))
    }
    normalized_global = normalize_text(global_comment).strip(" .,;:-—–")
    connectors = {"на", "и", "или", "либо", "а", "также"}
    for item_index, item in enumerate(items):
        existing = clean_text(item.get("comment") or item.get("user_comment_to_supplier"))
        context = (
            clean_text(item.get("source_span"))
            or clean_text(item.get("source_line"))
            or clean_text(source_text)
        )
        normalized_context = normalize_text(context)
        context_words = re.findall(r"[a-zа-яё0-9]+", normalized_context, flags=re.I)
        verified_semantic = bool(
            bindings and clean_text(item.get("comment_source")) == CommentSource.SEMANTIC.value
        )
        verified_explicit = bool(
            clean_text(item.get("comment_source")) == CommentSource.EXPLICIT_MARKER.value
        )
        kept: list[str] = []
        product_facts: list[str] = []
        semantic_found = False
        explicit_found = False
        query = clean_text(item.get("product_query"))
        normalized_query = normalize_text(query)
        for fragment in (part.strip(" .,;:-—–") for part in existing.split(";")):
            if not fragment:
                continue
            fragment = remove_global_comment_overlap(fragment, global_comment)
            if not fragment:
                continue
            normalized_fragment = normalize_text(fragment).strip(" .,;:-—–")
            if normalized_global and (
                normalized_fragment == normalized_global or normalized_fragment in normalized_global
            ):
                continue
            source_supported = normalized_fragment in normalized_context
            semantic_source_supported = _comment_source_is_authorized(fragment, context)
            if source_supported and normalized_fragment in normalized_query:
                kept.append(fragment)
                semantic_found = True
                continue
            if (
                normalized_fragment in bound_comments
                and source_supported
                and semantic_source_supported
            ):
                kept.append(fragment)
                semantic_found = True
                continue
            if verified_semantic and source_supported and semantic_source_supported:
                kept.append(fragment)
                semantic_found = True
                continue
            if verified_explicit and source_supported and semantic_source_supported:
                kept.append(fragment)
                explicit_found = True
                continue
            explicit = explicit_supplier_comment(fragment)
            if explicit and (
                normalize_text(explicit) in normalized_context
                or supplier_comment_start(context_words) is not None
            ):
                kept.append(explicit)
                explicit_found = True
            elif normalized_fragment in normalized_context:
                product_facts.append(fragment)
        value = "; ".join(kept)
        if normalized_query in connectors and product_facts:
            source = normalize_text(item.get("source_line")).strip(" .,;:-—–")
            owner = next(
                (
                    candidate
                    for candidate in reversed(items[:item_index])
                    if source
                    and normalize_text(candidate.get("source_line")).strip(" .,;:-—–") == source
                ),
                None,
            )
            if owner is not None:
                owner["product_query"] = clean_text(
                    f"{owner.get('product_query') or ''} {' '.join(product_facts)}"
                )
                product_facts = []
        for fact in product_facts:
            if normalize_text(fact) not in normalized_query:
                query = clean_text(f"{query} {fact}")
                normalized_query = normalize_text(query)
        item["product_query"] = query
        item["comment"] = value
        item["user_comment_to_supplier"] = value
        item["comment_source"] = (
            CommentSource.SEMANTIC.value
            if semantic_found
            else (
                CommentSource.EXPLICIT_MARKER.value if explicit_found else CommentSource.NONE.value
            )
        )


def _apply_semantic_comment_bindings(
    payload: dict[str, Any],
    items: list[dict[str, Any]],
    bindings: list[dict[str, Any]],
    source_text: str,
    deterministic: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """Применяет проверенные ИИ-привязки комментариев и удаляет их ложные товары."""
    if not bindings:
        return items

    payload["global_comment"] = _strip_global_comment_scope(
        clean_text(payload.get("global_comment"))
    )

    bound_comments = {clean_text(binding.get("text")).strip(" .,;:-—–") for binding in bindings}
    for comment in bound_comments:
        if comment:
            for item in items:
                _remove_bound_comment(item, comment)

    shadow_indexes: set[int] = set()
    for binding in bindings:
        comment = clean_text(binding.get("text")).strip(" .,;:-—–")
        scope = clean_text(binding.get("scope")).casefold()
        confidence = to_float(binding.get("confidence")) or 0.0
        target_indexes = (
            _validated_binding_target_indexes(binding, items, deterministic)
            if scope == "item"
            else _binding_target_indexes(binding, len(items))
        )
        scope_context = _comment_scope_context(source_text, comment)
        explicit_order_scope = has_explicit_order_comment_scope(scope_context) or bool(
            re.search(
                r"\bвсем\s+товар\w*\s+(?:нужно|надо|требуется)\b",
                normalize_text(scope_context),
                flags=re.I,
            )
        )
        if explicit_order_scope:
            scope = "order"
            target_indexes = []
            comment = _strip_global_comment_scope(comment)
        if scope == "item" and len(target_indexes) == 1:
            comment = _strip_conversational_product_leadin(comment)
            comment = _strip_product_facts_from_item_binding(
                comment,
                items[target_indexes[0]],
                items,
            )
        valid_scope = (
            scope == "order"
            or (scope == "item" and len(target_indexes) == 1)
            or (scope == "group" and len(target_indexes) >= 2)
        )
        if (
            not comment
            or confidence < _COMMENT_BINDING_CONFIDENCE
            or scope == "ambiguous"
            or not valid_scope
        ):
            if comment and not payload.get("comment_clarification"):
                payload["comment_clarification"] = comment
            continue

        if scope == "order":
            if has_explicit_global_comment_scope(_comment_scope_context(source_text, comment)):
                _merge_global_comment(payload, comment)
            else:
                payload["comment_clarification"] = comment
            continue

        explicit_group_scope = bool(
            _has_explicit_group_comment_scope(source_text, comment)
            or has_explicit_global_comment_scope(_comment_scope_context(source_text, comment))
            or _is_verified_root_group_comment(comment, source_text)
        )
        item_names = [clean_text(item.get("product_query")) for item in items]
        detached_scope_is_unclear = bool(
            len(items) > 1
            and scope in {"item", "group"}
            and not explicit_group_scope
            and _starts_as_detached_sentence(source_text, comment)
            and not _comment_scope_has_explicit_anchor(
                _comment_scope_context(source_text, comment),
                item_names,
            )
        )
        group_scope_is_unclear = bool(
            len(items) > 1
            and scope == "group"
            and not explicit_group_scope
            and (
                _RELATIONAL_COMMENT_RE.search(comment)
                or _starts_as_detached_sentence(source_text, comment)
            )
        )
        if detached_scope_is_unclear or group_scope_is_unclear:
            payload["comment_clarification"] = comment
            continue

        if scope == "group" and not explicit_group_scope:
            # Пожелание в конце относится к последней позиции. Модель не может расширить
            # эту область без явных слов пользователя.
            target_indexes = [max(target_indexes)]

        for index in target_indexes:
            _append_local_item_comment(items[index], comment)

        normalized_comment = normalize_text(comment).strip(" .,;:-—–")
        for index, item in enumerate(items):
            if index in target_indexes:
                continue
            query = normalize_text(item.get("product_query")).strip(" .,;:-—–")
            if query and (
                query == normalized_comment or normalized_comment.startswith(f"{query} ")
            ):
                shadow_indexes.add(index)

    return [item for index, item in enumerate(items) if index not in shadow_indexes]


def _apply_trailing_root_processing_comment(
    items: list[dict[str, Any]],
    source_text: str,
    target_indexes: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Переносит общее требование к срезу корней из ложного товара в комментарии."""
    match = _TRAILING_ROOT_PROCESSING_RE.search(clean_text(source_text))
    if match is None:
        return items

    instruction = clean_text(match.group("instruction")).strip(" .,;:-—–")
    normalized_instruction = normalize_text(instruction)
    shadow_indexes = {
        index
        for index, item in enumerate(items)
        if (
            _ROOT_PROCESSING_QUERY_RE.fullmatch(normalize_text(item.get("product_query")))
            or (
                (
                    root_match := _ROOT_PROCESSING_PREFIX_RE.match(
                        normalize_text(item.get("product_query"))
                    )
                )
                and normalize_text(item.get("product_query"))[root_match.end() :].strip()
                in normalized_instruction
            )
        )
    }
    real_items = [item for index, item in enumerate(items) if index not in shadow_indexes]
    if len(real_items) < 2:
        return items

    applicable_indexes = {
        index
        for index in range(len(items))
        if index not in shadow_indexes and (target_indexes is None or index in target_indexes)
    }
    for index, item in enumerate(items):
        if index not in applicable_indexes:
            continue
        existing = clean_text(item.get("comment") or item.get("user_comment_to_supplier")).strip(
            " .,;:-—–"
        )
        if existing and normalize_text(existing) in normalized_instruction:
            item["comment"] = ""
            item["user_comment_to_supplier"] = ""
            item["comment_source"] = CommentSource.NONE.value
        _append_local_item_comment(item, instruction, CommentSource.EXPLICIT_MARKER)
    return real_items


def _remove_item_global_comment_overlaps(
    items: list[dict[str, Any]],
    global_comment: str,
) -> None:
    """Удаляет продублированный общий комментарий и слова его общего охвата."""
    if not clean_text(global_comment):
        return
    for item in items:
        local_comment = clean_text(item.get("comment") or item.get("user_comment_to_supplier"))
        cleaned = remove_global_comment_overlap(local_comment, global_comment)
        item["comment"] = cleaned
        item["user_comment_to_supplier"] = cleaned
        if not cleaned:
            item["comment_source"] = CommentSource.NONE.value
