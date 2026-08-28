"""Содержит восстановление количества и измерений из исходной фразы."""

from __future__ import annotations

import re
from typing import Any

from restaurant_bot.domain.models import CommentSource, ExtractedItem
from restaurant_bot.domain.text import clean_text, normalize_text
from restaurant_bot.domain.units import UNIT_ALIASES, normalize_unit
from restaurant_bot.parsing.number_words import parse_number_words
from restaurant_bot.parsing.numeric import to_float
from restaurant_bot.parsing.numeric_ranges import numeric_range_spans
from restaurant_bot.parsing.packaging import explicit_packaging_preference
from restaurant_bot.parsing.products import parse_product_lines
from restaurant_bot.parsing.quantities import has_explicit_order_quantity, parse_quantity_unit
from restaurant_bot.parsing.semantic.measurements import (
    extract_semantic_facts,
    product_query_has_anchor,
    spoken_pair_quantity_for_query,
)
from restaurant_bot.parsing.semantic.models import SemanticFactKind

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
            authorized_quantity, authorized_unit = _explicit_order_quantity_from_source(
                source_line,
                quantity,
                unit,
            )
            if (
                authorized_quantity is not None
                and abs(authorized_quantity - quantity) <= 1e-9
                and authorized_unit == unit
            ):
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


def _word_quantity_for_item(
    source_line: str,
    proposed_quantity: float | None,
    proposed_unit: str,
) -> tuple[float | None, str]:
    """Подтверждает количество, произнесённое словом, в локальном фрагменте позиции."""
    if proposed_quantity is None:
        return None, ""
    words = [
        normalize_text(match.group(0)).strip(" .,:;—–-")
        for match in re.finditer(r"\d+|[a-zа-яё]+", source_line, flags=re.IGNORECASE)
    ]
    expected_unit = normalize_unit(proposed_unit)
    matches: list[tuple[float, str]] = []
    for index in range(len(words)):
        parsed = parse_number_words(words, index)
        if parsed is None:
            continue
        quantity, end = parsed
        if abs(quantity - proposed_quantity) > 1e-9:
            continue
        actual_unit = (
            normalize_unit(words[end]) if end < len(words) and words[end] in UNIT_ALIASES else ""
        )
        if expected_unit and actual_unit and expected_unit != actual_unit:
            continue
        if expected_unit and not actual_unit and not (expected_unit == "шт" and index == 0):
            continue
        matches.append((quantity, actual_unit))
    if not matches:
        return None, ""
    quantity, unit = matches[0]
    return quantity, unit or expected_unit


def _spoken_pair_quantity_for_item(
    source_text: str, product_query: str
) -> tuple[float, str] | None:
    """Находит локальную конструкцию «пару <товар>» без переноса единицы соседа."""
    measured_pair = spoken_pair_quantity_for_query(source_text, product_query)
    if measured_pair is not None:
        return measured_pair
    query_tokens = re.findall(r"[a-zа-яё0-9-]+", normalize_text(product_query), flags=re.I)
    if not query_tokens:
        return None
    for pair_match in re.finditer(r"\b(?:пару|пара)\b", normalize_text(source_text), flags=re.I):
        phrase = re.split(r"\bи\b|[,.;]", source_text[pair_match.end() :], maxsplit=1)[0]
        phrase_tokens = re.findall(r"[a-zа-яё0-9-]+", normalize_text(phrase), flags=re.I)
        if not product_query_has_anchor(product_query, phrase):
            continue
        unit = normalize_unit(phrase_tokens[0]) if phrase_tokens[0] in UNIT_ALIASES else "шт"
        return 2.0, unit
    return None


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


def _explicit_order_quantity_from_source(
    source_line: str,
    proposed_quantity: float | None,
    proposed_unit: str,
) -> tuple[float | None, str]:
    """Находит заказанное количество только среди source-фактов роли order_quantity."""
    candidates: list[tuple[float, str]] = []
    for fact in extract_semantic_facts(source_line):
        if fact.kind is not SemanticFactKind.ORDER_QUANTITY:
            continue
        quantity, unit = parse_quantity_unit(fact.original_text)
        if quantity is not None:
            candidates.append((quantity, normalize_unit(unit)))
    if not candidates:
        return None, ""
    normalized_proposed_unit = normalize_unit(proposed_unit)
    for quantity, unit in candidates:
        if (
            proposed_quantity is not None
            and abs(quantity - proposed_quantity) <= 1e-9
            and (not normalized_proposed_unit or not unit or normalized_proposed_unit == unit)
        ):
            return proposed_quantity, normalized_proposed_unit or unit
    return candidates[-1]


def _mark_item_source(item: dict[str, Any], source: str) -> None:
    """Сохраняет локальный semantic span, не перезаписывая полный source_line."""
    if not source:
        return
    item["source_span"] = source
    if not clean_text(item.get("source_line")):
        item["source_line"] = source


def restore_explicit_order_terms(
    items: list[dict[str, Any]], source_text: str = ""
) -> list[dict[str, Any]]:
    """Восстанавливает явно указанное количество или единицу."""
    recovered_from_message = parse_product_lines(source_text)
    for index, item in enumerate(items):
        original_line = clean_text(item.get("source_span")) or clean_text(item.get("source_line"))
        if not original_line and len(items) == 1:
            original_line = clean_text(source_text)
        terminal_quantity, terminal_unit = _terminal_order_quantity(original_line)
        if (
            terminal_quantity is not None
            and terminal_unit
            and not any(
                fact.kind is SemanticFactKind.ORDER_QUANTITY
                and abs((parse_quantity_unit(fact.original_text)[0] or -1) - terminal_quantity)
                <= 1e-9
                for fact in extract_semantic_facts(original_line)
            )
        ):
            terminal_quantity, terminal_unit = None, ""
        shared_source = len(items) > 1 and (
            "\n" in str(item.get("source_line") or "")
            or normalize_text(original_line) == normalize_text(clean_text(source_text))
        )
        if terminal_quantity is not None and not shared_source:
            _mark_item_source(item, original_line)
            item["quantity"] = terminal_quantity
            item["unit"] = terminal_unit
            continue
        outside_quantity, outside_unit = _quantity_outside_packaged_query(
            clean_text(item.get("product_query")),
            original_line,
        )
        if outside_quantity is not None:
            _mark_item_source(item, original_line)
            item["quantity"] = outside_quantity
            item["unit"] = outside_unit
            continue

        # Исходная строка может содержать фасовку в названии и количество заказа.
        # Значение модели сохраняется только при явном подтверждении в источнике.
        explicit_terms = _quantities_with_units(original_line)
        model_quantity = to_float(item.get("quantity"))
        model_unit = normalize_unit(item.get("unit"))
        # Общий fallback допустим только для единственной позиции: в списке он
        # не принадлежит ни одной строке и не может передавать фасовку соседям.
        source_item = (
            recovered_from_message[0]
            if len(items) == 1 and len(recovered_from_message) == 1
            else None
        )

        source_facts = extract_semantic_facts(original_line)
        source_order_facts = [
            fact for fact in source_facts if fact.kind is SemanticFactKind.ORDER_QUANTITY
        ]
        has_competing_order_quantity = (
            any(
                (parsed_quantity := parse_quantity_unit(fact.original_text)[0]) is not None
                and abs(parsed_quantity - model_quantity) > 1e-9
                for fact in source_order_facts
            )
            if model_quantity is not None
            else False
        )
        proposed_is_packaging = (
            any(
                fact.kind is SemanticFactKind.CATALOG_ATTRIBUTE
                and (parsed_quantity := parse_quantity_unit(fact.original_text)[0]) is not None
                and abs(parsed_quantity - model_quantity) <= 1e-9
                and (
                    not model_unit
                    or normalize_unit(parse_quantity_unit(fact.original_text)[1]) == model_unit
                )
                for fact in source_facts
            )
            if model_quantity is not None
            else False
        )
        proposed_is_measurement = (
            any(
                fact.kind is SemanticFactKind.MEASUREMENT
                and (parsed_quantity := parse_quantity_unit(fact.original_text)[0]) is not None
                and abs(parsed_quantity - model_quantity) <= 1e-9
                and (
                    not model_unit
                    or normalize_unit(parse_quantity_unit(fact.original_text)[1]) == model_unit
                )
                for fact in source_facts
            )
            if model_quantity is not None
            else False
        )

        word_quantity, word_unit = _word_quantity_for_item(
            original_line,
            model_quantity,
            model_unit,
        )
        if (
            word_quantity is not None
            and not proposed_is_packaging
            and not has_competing_order_quantity
            and not (
                source_item is not None
                and source_item.packaging_role in {"catalog_attribute", "user_preference"}
            )
        ):
            if not shared_source:
                _mark_item_source(item, original_line)
            item["quantity"] = word_quantity
            item["unit"] = word_unit
            continue

        # Словесное «пару яблок» — явное количество этой позиции. Его нужно  # noqa: RUF003
        # применить до fallback по общей source_line, иначе единица соседа
        # («две бутылки ... и пару яблок») переносится на этот товар.
        pair_quantity = _spoken_pair_quantity_for_item(original_line, item.get("product_query", ""))
        if pair_quantity is None and normalize_text(original_line) != normalize_text(source_text):
            pair_quantity = _spoken_pair_quantity_for_item(
                source_text, item.get("product_query", "")
            )
        if pair_quantity is not None:
            item["quantity"], item["unit"] = pair_quantity
            continue

        # До выбора товара каталог ещё не подтвердил, является ли мера частью
        # названия или количеством заказа. Поэтому не удаляем предложенную ИИ
        # локальную меру: если она не похожа на явную фасовку или другое
        # количество заказа, окончательное решение остаётся за
        # владельцем catalog provenance после выбора товара.
        if (
            model_quantity is not None
            and model_unit
            and not has_competing_order_quantity
            and not proposed_is_packaging
            and (
                has_explicit_order_quantity(original_line, model_quantity)
                or proposed_is_measurement
            )
        ):
            if not shared_source:
                _mark_item_source(item, original_line)
            if source_item is not None:
                item["packaging_text"] = (
                    clean_text(item.get("packaging_text")) or source_item.packaging_text
                )
                item["packaging_role"] = (
                    clean_text(item.get("packaging_role")) or source_item.packaging_role
                )
                item["packaging_confidence"] = (
                    item.get("packaging_confidence") or source_item.packaging_confidence
                )
            item["quantity"] = model_quantity
            item["unit"] = model_unit
            continue
        if len(items) == 1 and (
            (
                source_item is not None
                and source_item.packaging_role in {"catalog_attribute", "user_preference"}
            )
            or len(source_order_facts) == 1
        ):
            source_quantity, source_unit = _explicit_order_quantity_from_source(
                original_line,
                model_quantity,
                model_unit,
            )
            if (
                source_quantity is None
                and source_item is not None
                and source_item.quantity is not None
            ):
                source_quantity = float(source_item.quantity)
                source_unit = normalize_unit(source_item.unit)
            _mark_item_source(item, original_line)
            if source_item is not None:
                item["packaging_text"] = (
                    clean_text(item.get("packaging_text")) or source_item.packaging_text
                )
                item["packaging_role"] = (
                    clean_text(item.get("packaging_role")) or source_item.packaging_role
                )
                item["packaging_confidence"] = (
                    item.get("packaging_confidence") or source_item.packaging_confidence
                )
            item["quantity"] = source_quantity
            item["unit"] = source_unit if source_quantity is not None else ""
            continue
        if numeric_range_spans(original_line):
            trailing_quantity, trailing_unit = _trailing_quantity_with_unit(original_line)
            if trailing_quantity is None:
                # Вариант «... 0,9-1,3 кг - 2» содержит явно заказанное
                # количество без единицы. Концы диапазона к нему не относятся.
                trailing_quantity, trailing_unit = _terminal_order_quantity(original_line)
            if len(explicit_terms) == 1 and trailing_quantity is not None:
                # После справочного диапазона безопасным количеством является
                # только отдельно названное значение заказа.
                _mark_item_source(item, original_line)
                item["quantity"], item["unit"] = trailing_quantity, trailing_unit
                continue
            if len(explicit_terms) == 1 and model_quantity is not None:
                explicit_quantity, explicit_unit = explicit_terms[0]
                if abs(model_quantity - explicit_quantity) <= 1e-9:
                    # Разрешаем комментарий после явно распознанного заказа,
                    # например ``... 10 кг, желательно завтра``.
                    _mark_item_source(item, original_line)
                    item["quantity"], item["unit"] = explicit_quantity, explicit_unit
                    continue
            if not explicit_terms or len(explicit_terms) == 1:
                # ИИ не должен превращать первую границу диапазона в заказ.
                # Дальше обычный диалог попросит пользователя уточнить объём.
                _mark_item_source(item, original_line)
                item["quantity"] = None
                item["unit"] = ""
                continue
        if len(items) == 1 and len(explicit_terms) == 1 and model_quantity is not None:
            explicit_quantity, explicit_unit = explicit_terms[0]
            normalized_model_unit = normalize_unit(model_unit)
            normalized_explicit_unit = normalize_unit(explicit_unit)
            if abs(model_quantity - explicit_quantity) <= 1e-9 and (
                not normalized_model_unit
                or not normalized_explicit_unit
                or normalized_model_unit == normalized_explicit_unit
            ):
                _mark_item_source(item, original_line)
                item["quantity"] = model_quantity
                item["unit"] = model_unit or explicit_unit
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
                _mark_item_source(item, original_line)
                item["quantity"] = model_quantity
                item["unit"] = model_unit
                continue
            if source_item is not None and source_item.quantity is not None:
                item["quantity"] = source_item.quantity
                item["unit"] = source_item.unit
                _mark_item_source(item, original_line)
                if source_item.packaging_role != "none":
                    item["packaging_text"] = source_item.packaging_text
                    item["packaging_role"] = source_item.packaging_role
                    item["packaging_confidence"] = source_item.packaging_confidence
                continue
            _mark_item_source(item, original_line)
            item["quantity"] = model_quantity
            item["unit"] = model_unit
            continue

        # Восстанавливаем каждое явно произнесённое количество даже тогда,
        # когда модель вернула одну общую строку source_line для нескольких товаров.
        # Позиционное восстановление по полному сообщению имеет приоритет,
        # если оба перечисления имеют одинаковую длину. Модель часто копирует  # noqa: RUF003
        # всё сообщение в каждую строку source_line, поэтому разбор этого поля первым
        # присвоил бы количество последнего товара всем предыдущим позициям.
        source_line = clean_text(item.get("source_span")) or clean_text(item.get("source_line"))
        source = None
        if len(items) == len(recovered_from_message):
            source = recovered_from_message[index]
        if source is None:
            recovered = parse_product_lines(source_line)
            source = recovered[0] if len(recovered) == 1 else None
        if source is not None:
            if not clean_text(item.get("source_line")):
                _mark_item_source(item, source.source_line or clean_text(source_text))
            source_context = (
                source_line
                if clean_text(item.get("source_span"))
                else (source.source_line or source_line)
            )
            source_terminal_quantity, source_terminal_unit = _terminal_order_quantity(
                source_context
            )
            if source_terminal_quantity is not None:
                item["quantity"] = source_terminal_quantity
                item["unit"] = source_terminal_unit
                continue
            # Исходная строка пользователя имеет приоритет. В частности,  # noqa: RUF003
            # модель не должна придумывать «одну бутылку» для строки,
            # в которой количество отсутствует.
            if (
                not clean_text(item.get("source_span"))
                and source.quantity is not None
                and normalize_text(source.source_line) == normalize_text(source_line)
            ):
                source_quantity = float(source.quantity)
                source_unit = normalize_unit(source.unit)
            else:
                source_quantity, source_unit = _explicit_order_quantity_from_source(
                    source_context,
                    source.quantity,
                    source.unit,
                )
            item["quantity"] = source_quantity
            item["unit"] = source_unit if source_quantity is not None else ""
            if source_quantity is not None:
                continue

    _remove_confirmed_order_quantities_from_catalog_packaging(items, source_text)
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


def _remove_confirmed_order_quantity_from_catalog_packaging(
    item: dict[str, Any],
    source_line: str,
) -> None:
    """Не оставляет подтверждённый заказ внутри фасовки каталога."""
    packaging_text = clean_text(item.get("packaging_text"))
    if clean_text(item.get("packaging_role")) != "catalog_attribute" or not packaging_text:
        return
    quantity = to_float(item.get("quantity"))
    unit = normalize_unit(item.get("unit"))
    if quantity is None or not unit or not has_explicit_order_quantity(source_line, quantity):
        return
    cleaned_packaging, removed = _remove_matching_quantity(packaging_text, quantity, unit)
    if not removed:
        return
    item["packaging_text"] = clean_text(cleaned_packaging)
    if item["packaging_text"]:
        return
    item["packaging_role"] = "none"
    item["packaging_confidence"] = 0


def _remove_confirmed_order_quantities_from_catalog_packaging(
    items: list[dict[str, Any]],
    source_text: str,
) -> None:
    """Очищает фасовку от количества заказа только в локальном source span."""
    normalized_source = normalize_text(source_text)
    for item in items:
        source_span = clean_text(item.get("source_span"))
        source_line = source_span or clean_text(item.get("source_line"))
        if not source_line and len(items) == 1:
            source_line = clean_text(source_text)
        if not source_line:
            continue
        source_is_shared = len(items) > 1 and normalize_text(source_line) == normalized_source
        if source_is_shared:
            continue
        _remove_confirmed_order_quantity_from_catalog_packaging(item, source_line)


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
    normalized_source = normalize_text(source)
    if explicit_packaging_preference(source):
        return "user_preference", 0.9
    if role == "catalog_attribute" and confidence >= _PACKAGING_ROLE_CONFIDENCE_THRESHOLD:
        return role, confidence
    if role == "ambiguous":
        return "ambiguous", max(confidence, 0.5)
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
        source_span = clean_text(item.get("source_span"))
        source_line = source_span or clean_text(item.get("source_line"))
        ranges = _source_numeric_ranges(source_line)
        if not ranges and len(items) == 1:
            ranges = _source_numeric_ranges(source_text)
        if not ranges and not source_span and len(items) == len(deterministic):
            ranges = _source_numeric_ranges(deterministic[index].source_line)
        if len(ranges) != 1:
            continue
        range_text = ranges[0]
        role, confidence = _packaging_role_from_context(
            item, source_line or (source_text if len(items) == 1 else ""), range_text
        )
        item["packaging_text"] = range_text
        item["packaging_role"] = role
        item["packaging_confidence"] = confidence
        if role == "catalog_attribute":
            _append_reference_range_to_query(item, range_text)
            _remove_reference_range_from_comment(item, range_text)
        elif role == "user_preference" and not clean_text(item.get("comment")):
            preference_source = source_line or (source_text if len(items) == 1 else "")
            preference_match = _PACKAGING_PREFERENCE_RE.search(preference_source)
            if preference_match:
                item["comment"] = clean_text(preference_source[preference_match.start() :])
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
    source = clean_text(item.get("source_span")) or clean_text(item.get("source_line"))
    model_quantity = to_float(item.get("quantity"))
    model_unit = normalize_unit(item.get("unit"))
    source_quantity, source_unit = _explicit_order_quantity_from_source(
        source,
        model_quantity,
        model_unit,
    )
    if (
        source_quantity is not None
        and model_quantity is not None
        and abs(source_quantity - model_quantity) <= 1e-9
        and (not model_unit or not source_unit or model_unit == source_unit)
    ) or spoken_pair_quantity_for_query(source, clean_text(item.get("product_query"))) is not None:
        return
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
