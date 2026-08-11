"""Содержит восстановление количества и измерений из исходной фразы."""

from __future__ import annotations

import re
from typing import Any

from restaurant_bot.domain.models import CommentSource, ExtractedItem
from restaurant_bot.parsing.products import parse_product_lines
from restaurant_bot.parsing.quantities import parse_quantity_unit
from restaurant_bot.services.text import (
    UNIT_ALIASES,
    clean_text,
    normalize_text,
    normalize_unit,
    numeric_range_spans,
    parse_number_words,
    to_float,
)

_PACKAGING_ROLE_CONFIDENCE_THRESHOLD = 0.85

_PACKAGING_PREFERENCE_RE = re.compile(
    r"(?:нужн\w*\s+(?:фасов\w*|упаков\w*)|желательно\b|"
    r"только\s+(?:в|по|упаков\w*|фасов\w*)|"
    r"(?:упаков\w*|фасов\w*)\s+(?:должн\w*|по)\b|"
    r"упаков\w*\s+по\b|привез\w*\s+(?:кусоч\w*|по)\b)",
    flags=re.I,
)


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
    """Читает количество только при явной отметке в конце строки."""
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

        # Исходная строка может содержать фасовку в названии и количество заказа.
        # Значение модели сохраняется только при явном подтверждении в источнике.
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
                # ИИ не должен превращать первую границу диапазона в заказ.
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
            source_item = recovered_from_message[0] if len(recovered_from_message) == 1 else None
            if (
                source_item is not None
                and source_item.quantity is not None
                and source_item.packaging_role in {"catalog_attribute", "user_preference"}
            ):
                item["source_line"] = original_line
                item["quantity"] = source_item.quantity
                item["unit"] = source_item.unit
                item["packaging_text"] = source_item.packaging_text
                item["packaging_role"] = source_item.packaging_role
                item["packaging_confidence"] = source_item.packaging_confidence
                continue
            # При нескольких мерах значение модели безопасно только тогда,
            # когда оно является последним явно названным значением заказа.
            # Более ранние меры относятся к фасовке или характеристикам
            # (например, ``80 г, 3 кг``), а последняя мера — к заказу.  # noqa: RUF003
            # Сохраняем проверенное значение модели, не подменяя его первой  # noqa: RUF003
            # величиной, которую восстановил детерминированный разбор.
            if (
                abs(explicit_terms[-1][0] - model_quantity) <= 1e-9
                and explicit_terms[-1][1] == model_unit
            ):
                item["source_line"] = original_line
                item["quantity"] = model_quantity
                item["unit"] = model_unit
                continue
            if source_item is not None and source_item.quantity is not None:
                item["quantity"] = source_item.quantity
                item["unit"] = source_item.unit
                item["source_line"] = original_line
                if source_item.packaging_role != "none":
                    item["packaging_text"] = source_item.packaging_text
                    item["packaging_role"] = source_item.packaging_role
                    item["packaging_confidence"] = source_item.packaging_confidence
                continue
            item["source_line"] = original_line
            item["quantity"] = model_quantity
            item["unit"] = model_unit
            continue

        # Восстанавливаем каждое явно произнесённое количество даже тогда,
        # когда модель вернула одну общую строку source_line для нескольких товаров.
        # Позиционное восстановление по полному сообщению имеет приоритет,
        # если оба перечисления имеют одинаковую длину. Модель часто копирует  # noqa: RUF003
        # всё сообщение в каждую строку source_line, поэтому разбор этого поля первым
        # присвоил бы количество последнего товара всем предыдущим позициям.
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
            # Исходная строка пользователя имеет приоритет. В частности,  # noqa: RUF003
            # модель не должна придумывать «одну бутылку» для строки,
            # в которой количество отсутствует.
            item["quantity"] = source.quantity
            item["unit"] = source.unit if source.quantity is not None else ""
            if source.quantity is not None:
                continue

        # Словесное «пару яблок» — явное количество этой позиции,
        # а не случайная числовая характеристика товара.  # noqa: RUF003
        query = normalize_text(item.get("product_query"))
        pair = re.search(
            r"\b(?:пару|пара)\s+([a-zа-я][a-zа-я0-9-]*)",
            normalize_text(source_text),
        )
        if pair and query and (pair.group(1) in query or query in pair.group(1)):
            item["quantity"] = 2.0
            item["unit"] = "шт"
    return items


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
    # Детерминированная позиция с явным количеством уже подтверждает,  # noqa: RUF003
    # что в источнике есть количество заказа. В этом случае помощник  # noqa: RUF003
    # фасовки не должен подменять результат всей строкой или очищать количество.
    if recovered.quantity is not None:
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
