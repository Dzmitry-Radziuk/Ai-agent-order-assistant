"""Содержит схемы и чистую постобработку структурированных ответов OpenAI."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Literal

from pydantic import BaseModel, Field

from restaurant_bot.domain.models import CommentSource, ExtractedItem, Intent, ParsedCommand
from restaurant_bot.services.comment_policy import (
    explicit_supplier_comment,
    supplier_comment_start,
)
from restaurant_bot.services.parser import (
    has_explicit_global_comment_scope,
    parse_product_lines,
    parse_quantity_unit,
)
from restaurant_bot.services.text import (
    NUMBER_WORDS,
    UNIT_ALIASES,
    clean_text,
    normalize_text,
    normalize_unit,
    numeric_range_spans,
    parse_number_words,
    remove_global_comment_overlap,
    to_float,
)

_LARGE_ORDER_LIST_MIN_LINES = 10
_LARGE_ORDER_LIST_CHUNK_SIZE = 8

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

_PACKAGING_ROLE_CONFIDENCE_THRESHOLD = 0.85
_PACKAGING_PREFERENCE_RE = re.compile(
    r"(?:нужн\w*\s+(?:фасов\w*|упаков\w*)|желательно\b|"
    r"только\s+(?:в|по|упаков\w*|фасов\w*)|"
    r"(?:упаков\w*|фасов\w*)\s+(?:должн\w*|по)\b|"
    r"упаков\w*\s+по\b|привез\w*\s+(?:кусоч\w*|по)\b)",
    flags=re.I,
)


class CommentBindingSchema(BaseModel):
    """Описывает комментарий и товары, к которым он относится."""

    text: str = ""
    scope: Literal["item", "group", "order", "ambiguous"] = "ambiguous"
    target_item_indexes: list[int] = Field(default_factory=list)
    confidence: float = Field(default=0, ge=0, le=1)


class ParsedInputSchema(BaseModel):
    """Проверяет структурированный результат извлечения товаров."""

    intent: Intent = Intent.UNKNOWN
    text: str = ""
    items: list[ExtractedItem] = Field(default_factory=list)
    target_query: str = ""
    target_queries: list[str] = Field(default_factory=list)
    selected_index: int | None = None
    selection_query: str = ""
    edit_quantity: float | None = None
    edit_unit: str = ""
    global_comment: str = ""
    comment_target_query: str = ""
    comment_text: str = ""
    comment_action: Literal["add", "remove"] = "add"
    comment_scope: Literal["item", "order"] = "item"
    comment_bindings: list[CommentBindingSchema] = Field(default_factory=list)
    document_type: str = ""


class ProductMatchDecision(BaseModel):
    """Проверяет решение ИИ о сопоставлении товара."""

    action: str = Field(pattern="^(select|ambiguous|not_found)$")
    selected_product_id: str = ""
    candidate_product_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0, ge=0, le=1)
    contradictions: list[str] = Field(default_factory=list)
    reason: str = ""


class VisibleActionDecision(BaseModel):
    """Проверяет выбор действия среди кнопок текущего экрана."""

    action_id: str = ""
    confidence: float = Field(default=0, ge=0, le=1)
    reason: str = ""


class CommentScopeDecision(BaseModel):
    """Описывает ответ пользователя на уточнение области комментария."""

    action: Literal["items", "order", "cancel", "ambiguous"] = "ambiguous"
    target_item_indexes: list[int] = Field(default_factory=list)
    confidence: float = Field(default=0, ge=0, le=1)
    reason: str = ""


def _quantity_outside_packaged_query(
    product_query: str,
    source_line: str,
) -> tuple[float | None, str]:
    """Находит количество после названия товара с указанной фасовкой."""
    normalized_query = normalize_text(product_query)
    normalized_source = normalize_text(source_line)
    unit_pattern = "|".join(
        sorted((re.escape(unit) for unit in UNIT_ALIASES), key=len, reverse=True)
    )
    if not re.search(rf"\d+(?:[,.]\d+)?\s*(?:{unit_pattern})\b", normalized_query):
        return None, ""
    query_start = normalized_source.find(normalized_query)
    if query_start < 0:
        return None, ""

    before = normalized_source[:query_start].strip(" ,;:-—–")
    after = normalized_source[query_start + len(normalized_query) :].strip(" ,;:-—–")
    for outside_query in (after, before):
        quantity, unit = parse_quantity_unit(outside_query)
        if quantity is not None and unit:
            return quantity, unit
    return None, ""


def _quantities_with_units(source_line: str) -> list[tuple[float, str]]:
    """Извлекает все явно указанные количества вместе с единицами."""
    normalized = normalize_text(source_line).replace(",", ".")
    normalized = re.sub(r"(?<=\d)(?=[a-zа-я])", " ", normalized, flags=re.I)
    range_spans = numeric_range_spans(normalized)
    if range_spans:
        characters = list(normalized)
        for start, end in range_spans:
            characters[start:end] = [" "] * (end - start)
        normalized = "".join(characters)
    tokens = [token.strip(" .,:;—–-") for token in normalized.split()]
    found: list[tuple[float, str]] = []
    index = 0
    while index < len(tokens):
        parsed = parse_number_words(tokens, index)
        if parsed is None:
            index += 1
            continue
        quantity, end = parsed
        raw_unit = tokens[end] if end < len(tokens) else ""
        if raw_unit in UNIT_ALIASES:
            found.append((quantity, normalize_unit(raw_unit)))
            index = end + 1
            continue
        index += 1
    return found


def _terminal_order_quantity(source_line: str) -> tuple[float | None, str]:
    """Read a quantity only when the user marked it at the end of a line."""
    unit_pattern = "|".join(
        sorted((re.escape(unit) for unit in UNIT_ALIASES), key=len, reverse=True)
    )
    match = re.search(
        rf"(?:^|\s)[-—–:]\s*(?P<quantity>\d+(?:[,.]\d+)?)\s*(?P<unit>{unit_pattern})?\s*$",
        clean_text(source_line).rstrip(" .!?"),
        flags=re.IGNORECASE,
    )
    if match is None:
        return None, ""
    return float(match.group("quantity").replace(",", ".")), normalize_unit(
        match.group("unit") or ""
    )


def _trailing_quantity_with_unit(source_line: str) -> tuple[float | None, str]:
    """Находит количество с единицей в самом конце строки, вне диапазона."""
    unit_pattern = "|".join(
        sorted((re.escape(unit) for unit in UNIT_ALIASES), key=len, reverse=True)
    )
    source = clean_text(source_line).rstrip(" .!?,;:)")
    match = re.search(
        rf"(?<![\d-])(?P<quantity>\d+(?:[,.]\d+)?)\s*(?P<unit>{unit_pattern})\s*$",
        source,
        flags=re.IGNORECASE,
    )
    if match is None or any(
        start < match.end() and match.start() < end for start, end in numeric_range_spans(source)
    ):
        return None, ""
    return float(match.group("quantity").replace(",", ".")), normalize_unit(match.group("unit"))


def _clear_unknown_item_placeholders(items: list[dict[str, Any]]) -> None:
    """Очищает служебные заглушки ИИ в полях маршрутизации товара."""
    for item in items:
        for field in ("department", "supplier_hint", "source_department"):
            if normalize_text(item.get(field)) in _EMPTY_AI_VALUES:
                item[field] = ""


def _query_is_already_represented(known_query: str, recovered_query: str) -> bool:
    """Сравнивает многословные названия с учётом разговорных окончаний."""
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
    words = re.findall(r"[a-zа-яё0-9]+", normalize_text(value), flags=re.I)
    return any(
        len(word) > 1
        and word not in _NON_PRODUCT_FRAGMENT_WORDS
        and not word.isdigit()
        and not any(char.isdigit() for char in word)
        for word in words
    )


def _collapse_redundant_ai_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Схлопывает дубли одного источника и отбрасывает служебные обломки.

    Модель иногда возвращает одну голосовую позицию дважды: отдельно с
    комментарием и отдельно с количеством. Также диапазон фасовки может
    превращаться в псевдотовар «или». Такие элементы не являются двумя
    товарами и не должны попадать в черновик.
    """
    collapsed: list[dict[str, Any]] = []
    for item in items:
        query = clean_text(item.get("product_query"))
        source = normalize_text(item.get("source_line")).strip(" .,;:-—–")
        if (
            not _contains_product_query_word(query)
            and collapsed
            and source
            and source == normalize_text(collapsed[-1].get("source_line")).strip(" .,;:-—–")
        ):
            continue
        duplicate = None
        for existing in collapsed:
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


def restore_explicit_order_terms(
    items: list[dict[str, Any]], source_text: str = ""
) -> list[dict[str, Any]]:
    """Восстанавливает явно указанное количество или единицу."""
    recovered_from_message = parse_product_lines(source_text)
    for index, item in enumerate(items):
        original_line = clean_text(item.get("source_line")) or clean_text(source_text)
        terminal_quantity, terminal_unit = _terminal_order_quantity(original_line)
        shared_source = len(items) > 1 and (
            "\n" in str(item.get("source_line") or "")
            or normalize_text(original_line) == normalize_text(clean_text(source_text))
        )
        if terminal_quantity is not None and not shared_source:
            item["source_line"] = original_line
            item["quantity"] = terminal_quantity
            item["unit"] = terminal_unit
            continue
        outside_quantity, outside_unit = _quantity_outside_packaged_query(
            clean_text(item.get("product_query")),
            original_line,
        )
        if outside_quantity is not None:
            item["source_line"] = original_line
            item["quantity"] = outside_quantity
            item["unit"] = outside_unit
            continue

        # A source can contain both packaging in the name and order quantity.
        # Keep the model value only when it is explicitly present in the source.
        explicit_terms = _quantities_with_units(original_line)
        model_quantity = to_float(item.get("quantity"))
        model_unit = normalize_unit(item.get("unit"))
        if numeric_range_spans(original_line):
            trailing_quantity, trailing_unit = _trailing_quantity_with_unit(original_line)
            if trailing_quantity is None:
                # Вариант «... 0,9-1,3 кг - 2» содержит явно заказанное
                # количество без единицы. Концы диапазона к нему не относятся.
                trailing_quantity, trailing_unit = _terminal_order_quantity(original_line)
            if len(explicit_terms) == 1 and trailing_quantity is not None:
                # После справочного диапазона безопасным количеством является
                # только отдельно названное значение заказа.
                item["source_line"] = original_line
                item["quantity"], item["unit"] = trailing_quantity, trailing_unit
                continue
            if len(explicit_terms) == 1 and model_quantity is not None:
                explicit_quantity, explicit_unit = explicit_terms[0]
                if abs(model_quantity - explicit_quantity) <= 1e-9:
                    # Разрешаем комментарий после явно распознанного заказа,
                    # например ``... 10 кг, желательно завтра``.
                    item["source_line"] = original_line
                    item["quantity"], item["unit"] = explicit_quantity, explicit_unit
                    continue
            if not explicit_terms or len(explicit_terms) == 1:
                # ИИ не должен превращать первый endpoint диапазона в заказ.
                # Дальше обычный диалог попросит пользователя уточнить объём.
                item["source_line"] = original_line
                item["quantity"] = None
                item["unit"] = ""
                continue
        if (
            len(items) == 1
            and len(explicit_terms) >= 2
            and model_quantity is not None
            and model_unit
            and any(
                abs(quantity - model_quantity) <= 1e-9 and unit == model_unit
                for quantity, unit in explicit_terms
            )
        ):
            item["source_line"] = original_line
            item["quantity"] = model_quantity
            item["unit"] = model_unit
            continue

        # n8n recovers each explicit spoken order term even when the model
        # returned one shared source_line for a multi-product voice message.
        # Positional recovery from the complete user message is authoritative
        # when both enumerations have the same length. The model often copies
        # the whole message into every source_line; parsing that field first
        # would assign the final product's quantity to all preceding items.
        source = None
        if len(items) == len(recovered_from_message):
            source = recovered_from_message[index]
        if source is None:
            source_line = clean_text(item.get("source_line"))
            recovered = parse_product_lines(source_line)
            source = recovered[0] if len(recovered) == 1 else None
        if source is not None:
            if not clean_text(item.get("source_line")):
                item["source_line"] = source.source_line or clean_text(source_text)
            source_terminal_quantity, source_terminal_unit = _terminal_order_quantity(
                source.source_line
            )
            if source_terminal_quantity is not None:
                item["quantity"] = source_terminal_quantity
                item["unit"] = source_terminal_unit
                continue
            # The original spoken/source line is authoritative. In
            # particular, the model must not invent "one bottle" for a line
            # that contains no quantity at all.
            item["quantity"] = source.quantity
            item["unit"] = source.unit if source.quantity is not None else ""
            if source.quantity is not None:
                continue

        # Spoken "пару яблок" is an explicit quantity for this item, not a
        # continuation of the preceding product's number.
        query = normalize_text(item.get("product_query"))
        pair = re.search(
            r"\b(?:пару|пара)\s+([a-zа-я][a-zа-я0-9-]*)",
            normalize_text(source_text),
        )
        if pair and query and (pair.group(1) in query or query in pair.group(1)):
            item["quantity"] = 2.0
            item["unit"] = "шт"
    return items


def remove_unsupported_query_qualifiers(
    items: list[dict[str, Any]], source_text: str
) -> list[dict[str, Any]]:
    """Удаляет характеристики товара, которых не называл пользователь."""
    source_words = re.findall(r"[a-zа-я0-9]+", normalize_text(source_text), flags=re.I)
    if not source_words:
        return items

    for item in items:
        original_words = re.findall(
            r"[a-zа-я0-9]+", clean_text(item.get("product_query")), flags=re.I
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


def collapse_comment_shadow_items(
    items: list[dict[str, Any]], global_comment: str = ""
) -> list[dict[str, Any]]:
    """Удаляет ложный товар, совпадающий с комментарием."""
    shadow_indexes: set[int] = set()
    normalized_global_comment = normalize_text(global_comment).strip(" .,;")
    if normalized_global_comment:
        shadow_indexes.update(
            index
            for index, item in enumerate(items)
            if normalize_text(item.get("product_query")).strip(" .,;") == normalized_global_comment
        )
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
            if shadow_query in owner_comments:
                if owner.get("quantity") is None and shadow.get("quantity") is not None:
                    owner["quantity"] = shadow.get("quantity")
                    owner["unit"] = shadow.get("unit") or owner.get("unit") or ""
                shadow_indexes.add(shadow_index)
                continue

            # The model can duplicate a boundary between two spoken items:
            # "желательно холодным. И говядина".  The first half is already
            # owned by the syrup comment and the second half is already the
            # next product, so the combined third item carries no new fact.
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


def _strip_global_comment_scope(value: str) -> str:
    """Удаляет слова охвата из начала уже распознанного общего комментария."""
    comment = clean_text(value).strip(" .,;:-—–")
    if not comment:
        return ""
    comment = re.sub(
        r"^(?:(?:и|а)\s+)?(?:все|всё|всем|для всех)"
        r"(?:\s+(?:товар\w*|позици\w*|это(?:\s+дело)?))?"
        r"(?:\s*[,;:—–-]\s*|\s+)",
        "",
        comment,
        flags=re.I,
    )
    return clean_text(comment).strip(" .,;:-—–")


def _remove_matching_quantity(
    text: str,
    quantity: float | None,
    unit: str,
) -> tuple[str, bool]:
    """Удаляет из остатка строки указанное заказанное количество."""
    if quantity is None:
        return text, True
    words = list(re.finditer(r"\d+(?:[,.]\d+)?|[a-zа-яё]+", text, flags=re.I))
    tokens = [normalize_text(match.group()) for match in words]
    expected_unit = normalize_unit(unit)
    for index in range(len(tokens)):
        parsed = parse_number_words(tokens, index)
        if parsed is None:
            continue
        parsed_quantity, end = parsed
        if abs(parsed_quantity - quantity) > 1e-9:
            continue
        actual_unit = ""
        span_end = words[end - 1].end()
        if end < len(tokens) and tokens[end] in UNIT_ALIASES:
            actual_unit = normalize_unit(tokens[end])
            span_end = words[end].end()
        if expected_unit and actual_unit != expected_unit:
            continue
        return f"{text[: words[index].start()]} {text[span_end:]}", True
    return text, False


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


_COMMENT_BINDING_CONFIDENCE = 0.9

_EXPLICIT_GROUP_COMMENT_SCOPE_RE = re.compile(
    r"\b(?:оба|обе|обоих|обеих|обоим|обеим)\b"
    r"|\b(?:эти|данные|перечисленные|указанные)\s+(?:товар\w*|позиц\w*)\b"
    r"|\b(?:каждому|к\s+каждому)\s+из\s+(?:них|этих)\b",
    flags=re.I,
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
    return bool(
        _EXPLICIT_GROUP_COMMENT_SCOPE_RE.search(_comment_scope_context(source_text, comment))
    )


def _is_verified_root_group_comment(comment: str, source_text: str) -> bool:
    """Подтверждает безопасное групповое требование к обработке корней."""
    match = _TRAILING_ROOT_PROCESSING_RE.search(clean_text(source_text))
    if match is None:
        return False
    instruction = clean_text(match.group("instruction")).strip(" .,;:-—–")
    return normalize_text(instruction) == normalize_text(comment)


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
        context = clean_text(item.get("source_line")) or clean_text(source_text)
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
            if source_supported and normalized_fragment in normalized_query:
                kept.append(fragment)
                semantic_found = True
                continue
            if normalized_fragment in bound_comments and source_supported:
                kept.append(fragment)
                semantic_found = True
                continue
            if verified_semantic and source_supported:
                kept.append(fragment)
                semantic_found = True
                continue
            if verified_explicit and normalized_fragment in normalized_context:
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
) -> list[dict[str, Any]]:
    """Применяет проверенные ИИ-привязки комментариев и удаляет их ложные товары."""
    if not bindings:
        return items

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
        target_indexes = _binding_target_indexes(binding, len(items))
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
            # A trailing wish belongs to the last item. The model cannot widen
            # that scope without explicit words from the user.
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


def _apply_trailing_root_processing_comment(
    items: list[dict[str, Any]], source_text: str
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
        if _ROOT_PROCESSING_QUERY_RE.fullmatch(normalize_text(item.get("product_query")))
        and normalized_instruction.startswith(normalize_text(item.get("product_query")))
    }
    real_items = [item for index, item in enumerate(items) if index not in shadow_indexes]
    if len(real_items) < 2:
        return items

    for item in real_items:
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


def _source_numeric_ranges(value: str) -> list[str]:
    """Возвращает исходные текстовые диапазоны из строки пользователя."""
    source = clean_text(value)
    return [source[start:end] for start, end in numeric_range_spans(source)]


def _packaging_role_from_context(
    item: dict[str, Any], source: str, range_text: str
) -> tuple[str, float]:
    """Определяет роль диапазона с безопасным неоднозначным запасным вариантом."""
    role = clean_text(item.get("packaging_role")) or "none"
    try:
        confidence = float(item.get("packaging_confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0
    if role in {"catalog_attribute", "user_preference"} and confidence >= (
        _PACKAGING_ROLE_CONFIDENCE_THRESHOLD
    ):
        return role, confidence
    if role == "ambiguous":
        return "ambiguous", max(confidence, 0.5)

    normalized_source = normalize_text(source)
    if _PACKAGING_PREFERENCE_RE.search(normalized_source):
        return "user_preference", 0.9
    range_end = normalized_source.find(normalize_text(range_text))
    if re.search(r"\b(?:фасов\w*|упаков\w*)\b", normalized_source):
        return "ambiguous", 0.5
    if range_end >= 0:
        return "catalog_attribute", 0.9
    return "ambiguous", 0.5


def _append_reference_range_to_query(item: dict[str, Any], range_text: str) -> None:
    """Возвращает диапазон размера из комментария обратно в поисковое название."""
    query = clean_text(item.get("product_query"))
    if not query:
        return
    normalized_range = (
        normalize_text(range_text).replace(",", ".").replace("–", "-").replace("—", "-")
    )
    normalized_query = normalize_text(query).replace(",", ".").replace("–", "-").replace("—", "-")
    if normalized_range in normalized_query:
        return
    item["product_query"] = f"{query} {range_text}".strip()


def _remove_reference_range_from_comment(item: dict[str, Any], range_text: str) -> None:
    """Удаляет размер товара из комментария, если модель ошибочно положила его туда."""
    numbers = re.findall(r"\d+(?:[,.]\d+)?", range_text)
    if len(numbers) != 2:
        return
    left = re.escape(numbers[0]).replace(",", "[,.]")
    right = re.escape(numbers[1]).replace(",", "[,.]")
    unit_pattern = "|".join(
        sorted((re.escape(unit) for unit in UNIT_ALIASES), key=len, reverse=True)
    )
    pattern = re.compile(
        rf"(?<!\w){left}\s*(?:--|[-–—])\s*{right}(?:\s*(?:{unit_pattern}))?(?!\w)",
        flags=re.IGNORECASE,
    )
    for field in ("comment", "user_comment_to_supplier"):
        cleaned = pattern.sub(" ", clean_text(item.get(field)))
        item[field] = re.sub(r"\s+", " ", cleaned).strip(" .,;:-—–")


def _restore_reference_ranges_in_queries(
    items: list[dict[str, Any]],
    source_text: str,
    deterministic: list[ExtractedItem],
) -> None:
    """Не даёт ИИ потерять размер или фасовку из поискового названия."""
    for index, item in enumerate(items):
        source_line = clean_text(item.get("source_line"))
        ranges = _source_numeric_ranges(source_line)
        if not ranges and len(items) == 1:
            ranges = _source_numeric_ranges(source_text)
        if not ranges and len(items) == len(deterministic):
            ranges = _source_numeric_ranges(deterministic[index].source_line)
        if len(ranges) != 1:
            continue
        range_text = ranges[0]
        role, confidence = _packaging_role_from_context(
            item, source_line or source_text, range_text
        )
        item["packaging_text"] = range_text
        item["packaging_role"] = role
        item["packaging_confidence"] = confidence
        if role == "catalog_attribute":
            _append_reference_range_to_query(item, range_text)
            _remove_reference_range_from_comment(item, range_text)
        elif role == "user_preference" and not clean_text(item.get("comment")):
            preference_match = _PACKAGING_PREFERENCE_RE.search(source_line or source_text)
            if preference_match:
                item["comment"] = clean_text(
                    (source_line or source_text)[preference_match.start() :]
                )
                item["user_comment_to_supplier"] = item["comment"]
                item["comment_source"] = CommentSource.EXPLICIT_MARKER.value


def _restore_unordered_measurement_pair(
    items: list[dict[str, Any]],
    deterministic: list[ExtractedItem],
) -> None:
    """Отменяет выбранный ИИ вес из пары без количества заказа."""
    if len(items) != 1 or len(deterministic) != 1:
        return
    recovered = deterministic[0]
    if recovered.packaging_role not in {"ambiguous", "catalog_attribute"}:
        return
    item = items[0]
    item["product_query"] = recovered.product_query
    item["quantity"] = None
    item["unit"] = ""
    item["supplier_hint"] = ""
    item["comment"] = ""
    item["user_comment_to_supplier"] = ""
    item["comment_source"] = CommentSource.NONE.value
    item["source_line"] = recovered.source_line
    item["packaging_text"] = recovered.packaging_text
    item["packaging_role"] = recovered.packaging_role
    item["packaging_confidence"] = recovered.packaging_confidence


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
            source_line = clean_text(item.get("source_line"))
            recovered_from_line = parse_product_lines(source_line)
            if len(recovered_from_line) == 1:
                pairs.append((item, recovered_from_line[0]))

    normalized_global = normalize_text(global_comment).strip(" .,;:-—–")
    for item, recovered in pairs:
        if (
            clean_text(item.get("comment") or item.get("user_comment_to_supplier"))
            or clean_text(item.get("supplier_hint"))
            or recovered.comment
            or recovered.comment_source is not CommentSource.NONE
        ):
            continue
        recovered_query = _strip_conversational_product_leadin(recovered.product_query)
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
            if normalize_text(recovered_query) == normalize_text(item.get("product_query")):
                continue
            item["product_query"] = recovered_query
            item["source_line"] = recovered.source_line or item.get("source_line") or ""
            item["comment_source"] = CommentSource.NONE.value


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
    global_comment = _strip_global_comment_scope(clean_text(payload.get("global_comment")))
    items = _apply_semantic_comment_bindings(payload, items, bindings, source_text)
    global_comment = _strip_global_comment_scope(clean_text(payload.get("global_comment")))

    # ИИ иногда дублирует локальную привязку в global_comment. Такой текст
    # уже записан в конкретную позицию и не должен распространяться engine на
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

    # Fallback нужен только когда ИИ действительно не вернул ни одной позиции.
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
    for item in items:
        if item.get("comment") and not item.get("user_comment_to_supplier"):
            item["user_comment_to_supplier"] = item["comment"]
        elif item.get("user_comment_to_supplier") and not item.get("comment"):
            item["comment"] = item["user_comment_to_supplier"]

    payload["global_comment"] = global_comment
    payload["items"] = items
    return payload


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
            if owner_quantity is None and fragment_quantity is not None:
                owner["quantity"] = fragment_quantity
                owner["unit"] = fragment.get("unit") or owner.get("unit") or ""
            removed.add(fragment_index)
    return [item for index, item in enumerate(items) if index not in removed]


def _remove_connector_fragment_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Удаляет отдельные союзы, ошибочно возвращённые как товарные позиции."""
    connectors = {"на", "и", "или", "либо", "а", "также"}
    source_counts: dict[str, int] = {}
    for item in items:
        source = normalize_text(item.get("source_line")).strip(" .,;:-—–")
        if source:
            source_counts[source] = source_counts.get(source, 0) + 1
    result: list[dict[str, Any]] = []
    for item in items:
        query = normalize_text(item.get("product_query")).strip(" .,;:-—–")
        source = normalize_text(item.get("source_line")).strip(" .,;:-—–")
        if query in connectors and source and source_counts.get(source, 0) > 1:
            continue
        result.append(item)
    return result
