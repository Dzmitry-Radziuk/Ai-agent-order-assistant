"""Разбирает товарные строки, количества и фасовку без внешних эффектов."""

from __future__ import annotations

import re

from restaurant_bot.domain.models import CommentSource, ExtractedItem
from restaurant_bot.domain.units import UNIT_ALIASES, normalize_unit
from restaurant_bot.parsing.comment_policy import explicit_supplier_comment
from restaurant_bot.parsing.number_words import NUMBER_WORDS, parse_number_words
from restaurant_bot.parsing.numeric_ranges import numeric_range_spans
from restaurant_bot.parsing.packaging import (
    _is_compact_catalog_measurement,
    _is_packaging_reference_prefix,
    _single_product_packaging_item,
    _spoken_measurement_pair,
)
from restaurant_bot.parsing.quantities import _is_standalone_quantity
from restaurant_bot.text_normalization import clean_text, normalize_text


def _query_with_unmarked_tail(name: str, tail: str) -> str:
    """Возвращает неподтверждённый хвост в возможное название товара."""
    return clean_text(f"{name} {tail}").strip(" .,;:!?-—–")


_ProductLinePatterns = tuple[
    re.Pattern[str],
    re.Pattern[str],
    re.Pattern[str],
    re.Pattern[str],
    re.Pattern[str],
]


def _build_unit_pattern() -> str:
    """Строит единый regex-шаблон поддерживаемых единиц."""
    return "|".join(sorted((re.escape(key) for key in UNIT_ALIASES), key=len, reverse=True))


def _prepare_product_lines(text: str, unit_pattern: str) -> tuple[str, list[str]]:
    """Нормализует ввод и разделяет его на исходные товарные строки."""
    source = str(text or "").replace(";", "\n").strip()
    if not source:
        return source, []
    lines = [clean_text(line) for line in re.split(r"\n+", source) if clean_text(line)]
    if (
        len(lines) == 1
        and source.count(",") >= 2
        and not re.search(r"[.!?]", source)
        and _spoken_measurement_pair(source, unit_pattern) is None
    ):
        comma_parts = [
            clean_text(line) for line in re.split(r"(?<!\d),(?!\d)", source) if clean_text(line)
        ]
        if not any(_is_standalone_quantity(part) for part in comma_parts):
            lines = comma_parts
    return source, lines


def _build_product_line_patterns(unit_pattern: str) -> _ProductLinePatterns:
    """Строит regex-шаблоны для последовательного разбора строки."""
    trailing = re.compile(
        rf"^(.*?)(?:(?:\s+|[-—–:])(?P<qty>\d+(?:[,.]\d+)?)\s*(?P<unit>{unit_pattern})|"
        rf"[-—–:]\s*(?P<bare_qty>\d+(?:[,.]\d+)?))\s*$",
        re.I,
    )
    leading = re.compile(
        rf"^(?P<qty>\d+(?:[,.]\d+)?)\s*(?P<unit>{unit_pattern})?\s+(.*)$",
        re.I,
    )
    packaging = re.compile(
        rf"(?:"
        rf"\d+(?:[,.]\d+)?\s*(?:{unit_pattern})\s*[*xх×]\s*\d+(?:[,.]\d+)?"
        rf"(?:\s*\(\s*~?\s*\d+(?:[,.]\d+)?\s*(?:{unit_pattern})\s*\))?"
        rf"|"
        rf"\d+(?:[,.]\d+)?\s*[*xх×]\s*\d+(?:[,.]\d+)?\s*(?:{unit_pattern})"
        rf")",
        re.I,
    )
    alternative_packaging = re.compile(
        rf"\d+(?:[,.]\d+)?\s*(?:{unit_pattern})\s+"
        rf"(?:или|либо)\s+\d+(?:[,.]\d+)?\s*(?:{unit_pattern})\b",
        re.I,
    )
    number_words_pattern = "|".join(
        sorted((re.escape(word) for word in NUMBER_WORDS), key=len, reverse=True)
    )
    trailing_word_quantity = re.compile(
        rf"(?P<quantity>(?:{number_words_pattern})(?:\s+(?:{number_words_pattern}))*)\s+"
        rf"(?P<unit>{unit_pattern})\s*[.!?]*$",
        re.I,
    )
    return trailing, leading, packaging, alternative_packaging, trailing_word_quantity


def _find_quantity_marks(
    stripped: str,
    unit_pattern: str,
    packaging_pattern: re.Pattern[str],
    alternative_packaging_pattern: re.Pattern[str],
) -> tuple[
    list[tuple[int, int]], list[tuple[int, int]], list[tuple[int, int]], list[re.Match[str]]
]:
    """Находит фасовку, диапазоны и явные количественные маркеры строки."""
    packaging_spans = [match.span() for match in packaging_pattern.finditer(stripped)]
    alternative_packaging_spans = [
        match.span() for match in alternative_packaging_pattern.finditer(stripped)
    ]
    reference_range_spans = numeric_range_spans(stripped)
    quantity_marks = [
        mark
        for mark in re.finditer(
            rf"(\d+(?:[,.]\d+)?)\s*(?P<unit>{unit_pattern})\b",
            stripped,
            flags=re.I,
        )
        if not any(start <= mark.start() < end for start, end in packaging_spans)
        and not any(
            start < mark.end() and mark.start() < end for start, end in reference_range_spans
        )
        and not any(
            start < mark.end() and mark.start() < end for start, end in alternative_packaging_spans
        )
    ]
    return packaging_spans, alternative_packaging_spans, reference_range_spans, quantity_marks


def _catalog_measurement_item(
    stripped: str,
    source_line: str,
    quantity_marks: list[re.Match[str]],
    has_explicit_order_lead: bool,
) -> ExtractedItem | None:
    """Собирает позицию с каталожной мерой вместо количества заказа."""
    if (
        len(quantity_marks) == 1
        and not has_explicit_order_lead
        and _is_compact_catalog_measurement(stripped, quantity_marks[0])
    ):
        mark = quantity_marks[0]
        return ExtractedItem(
            product_query=stripped,
            source_line=source_line,
            packaging_text=clean_text(mark.group(0)),
            packaging_role="catalog_attribute",
            packaging_confidence=0.9,
        )
    if len(quantity_marks) == 1 and _is_packaging_reference_prefix(
        stripped[: quantity_marks[0].start()]
    ):
        mark = quantity_marks[0]
        return ExtractedItem(
            product_query=stripped,
            source_line=source_line,
            packaging_text=clean_text(mark.group(0)),
            packaging_role="catalog_attribute",
            packaging_confidence=0.9,
        )
    return None


def _multiple_quantity_items(
    stripped: str,
    source_line: str,
    quantity_marks: list[re.Match[str]],
) -> list[ExtractedItem] | None:
    """Восстанавливает несколько товаров по явным количествам."""
    if len(quantity_marks) < 2 or re.search(r"[—–-]\s*\d", stripped):
        return None
    recovered: list[ExtractedItem] = []
    start = 0
    for index, mark in enumerate(quantity_marks):
        name = re.sub(
            r"^(?:и|а также|а)\s+",
            "",
            stripped[start : mark.start()].strip(),
            flags=re.I,
        ).strip(" .,;:!?-—–")
        if not name:
            continue
        tail = ""
        if index + 1 < len(quantity_marks):
            between = stripped[mark.end() : quantity_marks[index + 1].start()]
            separators = list(
                re.finditer(
                    r"(?:\s+(?:и|а также|а)\s+|[,.;!?]\s*)",
                    between,
                    flags=re.I,
                )
            )
            if separators:
                strong_separators = [
                    separator
                    for separator in separators
                    if re.match(r"\s*[.;!?]", separator.group())
                ]
                separator = strong_separators[-1] if strong_separators else separators[-1]
                tail = clean_text(between[: separator.start()]).strip(" .,;:!?-—–")
                start = mark.end() + separator.end()
            else:
                start = mark.end()
        else:
            tail = clean_text(stripped[mark.end() :]).strip(" .,;:!?-—–")
        comment = explicit_supplier_comment(tail)
        if tail and not comment:
            name = _query_with_unmarked_tail(name, tail)
        recovered.append(
            ExtractedItem(
                product_query=name,
                quantity=float(mark.group(1).replace(",", ".")),
                unit=normalize_unit(mark.group("unit") or ""),
                comment=comment,
                user_comment_to_supplier=comment,
                comment_source=(CommentSource.EXPLICIT_MARKER if comment else CommentSource.NONE),
                source_line=source_line,
            )
        )
    if len(recovered) == len(quantity_marks):
        return recovered
    quantity_units = {normalize_unit(mark.group("unit") or "") for mark in quantity_marks}
    if len(quantity_units) == 1:
        return [
            ExtractedItem(
                product_query=stripped,
                source_line=source_line,
                packaging_text=clean_text(stripped),
                packaging_role="ambiguous",
                packaging_confidence=0.6,
            )
        ]
    return None


def _single_quantity_items(
    stripped: str,
    source_line: str,
    quantity_marks: list[re.Match[str]],
) -> list[ExtractedItem] | None:
    """Собирает одну позицию с единственным явным количеством."""
    if len(quantity_marks) != 1:
        return None
    mark = quantity_marks[0]
    # Голосовая фраза может перечислить товары и дать количество только последнему.
    prefix = clean_text(stripped[: mark.start()]).strip(" ,;:-—–")
    enumerated_names = [
        clean_text(part).strip(" ,;:-—–")
        for part in re.split(r"\s+(?:и|а также)\s+", prefix, flags=re.I)
    ]
    if len(enumerated_names) > 1 and all(enumerated_names):
        return [
            *(
                ExtractedItem(product_query=name, source_line=source_line)
                for name in enumerated_names[:-1]
            ),
            ExtractedItem(
                product_query=enumerated_names[-1],
                quantity=float(mark.group(1).replace(",", ".")),
                unit=normalize_unit(mark.group("unit") or ""),
                source_line=source_line,
            ),
        ]
    name = clean_text(stripped[: mark.start()]).strip(" ,;:-—–")
    tail = clean_text(stripped[mark.end() :]).strip(" .,!?:;-—–")
    if name and not tail:
        return [
            ExtractedItem(
                product_query=name,
                quantity=float(mark.group(1).replace(",", ".")),
                unit=normalize_unit(mark.group("unit") or ""),
                source_line=source_line,
            )
        ]
    # Число после пунктуации оставляем для последующего разбора хвостового количества.
    if name and tail and not re.match(r"^\s*[,;:\-—–]*\s*\d", stripped[mark.end() :]):
        comment = explicit_supplier_comment(tail)
        return [
            ExtractedItem(
                product_query=(name if comment else _query_with_unmarked_tail(name, tail)),
                quantity=float(mark.group(1).replace(",", ".")),
                unit=normalize_unit(mark.group("unit") or ""),
                comment=comment,
                user_comment_to_supplier=comment,
                comment_source=(CommentSource.EXPLICIT_MARKER if comment else CommentSource.NONE),
                source_line=source_line,
            )
        ]
    return None


def _alternative_packaging_quantity(
    stripped: str,
    source_line: str,
    alternative_packaging_spans: list[tuple[int, int]],
    trailing_word_quantity: re.Pattern[str],
) -> ExtractedItem | None:
    """Обрабатывает заказанное количество после альтернативной фасовки."""
    if not alternative_packaging_spans:
        return None
    word_quantity = trailing_word_quantity.search(stripped)
    if not word_quantity:
        return None
    quantity_tokens = normalize_text(word_quantity.group("quantity")).split()
    parsed_quantity = parse_number_words(quantity_tokens, 0)
    if parsed_quantity is None or parsed_quantity[1] != len(quantity_tokens):
        return None
    name = clean_text(stripped[: word_quantity.start()]).strip(" ,;:-—–")
    if not name:
        return None
    return ExtractedItem(
        product_query=name,
        quantity=parsed_quantity[0],
        unit=normalize_unit(word_quantity.group("unit")),
        source_line=source_line,
    )


def _numeric_quantity_item(
    stripped: str,
    source_line: str,
    reference_range_spans: list[tuple[int, int]],
    trailing: re.Pattern[str],
    leading: re.Pattern[str],
) -> ExtractedItem | None:
    """Обрабатывает числовое количество после упаковки и диапазонов."""
    match = trailing.match(stripped)
    if match:
        quantity_span = match.span("qty") if match.group("qty") else match.span("bare_qty")
        if any(
            start < quantity_span[1] and quantity_span[0] < end
            for start, end in reference_range_spans
        ):
            match = None
    if match:
        name = clean_text(match.group(1)).strip(" -:—–")
        if name:
            return ExtractedItem(
                product_query=name,
                quantity=float((match.group("qty") or match.group("bare_qty")).replace(",", ".")),
                unit=normalize_unit(match.group("unit") or ""),
                source_line=source_line,
            )
    match = leading.match(stripped)
    if match:
        name = clean_text(match.group(3)).strip(" -:")
        if name:
            return ExtractedItem(
                product_query=name,
                quantity=float(match.group("qty").replace(",", ".")),
                unit=normalize_unit(match.group("unit") or ""),
                source_line=source_line,
            )
    return None


def _word_quantity_item(
    stripped: str,
    source_line: str,
    has_explicit_bare_quantity: bool,
) -> ExtractedItem | None:
    """Обрабатывает количество, записанное словами."""
    tokens = [
        token.strip(" .,:;—–-")
        for token in normalize_text(stripped).split()
        if token.strip(" .,:;—–-")
    ]
    for index in range(len(tokens)):
        parsed = parse_number_words(tokens, index)
        if not parsed:
            continue
        quantity, end = parsed
        unit = (
            normalize_unit(tokens[end]) if end < len(tokens) and tokens[end] in UNIT_ALIASES else ""
        )
        if not unit and not has_explicit_bare_quantity:
            continue
        if end < len(tokens) and unit:
            end += 1
        if index == 0:
            name = " ".join(tokens[end:])
        elif end == len(tokens):
            name = " ".join(tokens[:index])
        else:
            name = " ".join(tokens[:index])
            tail = " ".join(tokens[end:])
            if name:
                comment = explicit_supplier_comment(tail)
                return ExtractedItem(
                    product_query=(name if comment else _query_with_unmarked_tail(name, tail)),
                    quantity=quantity,
                    unit=unit,
                    comment=comment,
                    user_comment_to_supplier=comment,
                    comment_source=(
                        CommentSource.EXPLICIT_MARKER if comment else CommentSource.NONE
                    ),
                    source_line=source_line,
                )
            continue
        if name:
            return ExtractedItem(
                product_query=name,
                quantity=quantity,
                unit=unit,
                source_line=source_line,
            )
    return None


def _parse_product_line(
    line: str,
    lines_count: int,
    unit_pattern: str,
    patterns: _ProductLinePatterns,
) -> list[ExtractedItem]:
    """Применяет последовательность правил к одной товарной строке."""
    trailing, leading, packaging, alternative_packaging, trailing_word_quantity = patterns
    has_explicit_order_lead = bool(
        re.match(
            r"^(?:\u0434\u043e\u0431\u0430\u0432\w*|\u0437\u0430\u043a\u0430\u0436\w*|"
            r"\u043d\u0443\u0436\u043d\u043e|\u043d\u0430\u0434\u043e)\s+",
            normalize_text(line),
            flags=re.I,
        )
    )
    stripped = re.sub(r"^(?:добавь|добавить|закажи|заказать|нужно|надо)\s+", "", line, flags=re.I)
    if _is_standalone_quantity(stripped):
        return []
    spoken_pair = _spoken_measurement_pair(stripped, unit_pattern)
    if spoken_pair is not None:
        packaging_text, packaging_role, packaging_confidence = spoken_pair
        return [
            ExtractedItem(
                product_query=stripped,
                source_line=line,
                packaging_text=packaging_text,
                packaging_role=packaging_role,
                packaging_confidence=packaging_confidence,
            )
        ]
    packaging_spans, alternative_packaging_spans, reference_range_spans, quantity_marks = (
        _find_quantity_marks(stripped, unit_pattern, packaging, alternative_packaging)
    )
    catalog_item = _catalog_measurement_item(
        stripped,
        line,
        quantity_marks,
        has_explicit_order_lead,
    )
    if catalog_item is not None:
        return [catalog_item]
    packaged_item = _single_product_packaging_item(stripped, line, quantity_marks)
    if packaged_item is not None:
        return [packaged_item]
    has_explicit_bare_quantity = bool(re.search(r"(?:^|\s)[-—–:]\s*\d+(?:[,.]\d+)?\s*$", stripped))
    multiple_items = _multiple_quantity_items(stripped, line, quantity_marks)
    if multiple_items is not None:
        return multiple_items
    single_items = _single_quantity_items(stripped, line, quantity_marks)
    if single_items is not None:
        return single_items
    if packaging_spans and not quantity_marks:
        return [ExtractedItem(product_query=stripped, source_line=line)]
    if reference_range_spans and not quantity_marks and not has_explicit_bare_quantity:
        # Без отдельного маркера диапазон не может быть заказанным.
        return [ExtractedItem(product_query=stripped, source_line=line)]
    alternative_item = _alternative_packaging_quantity(
        stripped,
        line,
        alternative_packaging_spans,
        trailing_word_quantity,
    )
    if alternative_item is not None:
        return [alternative_item]
    numeric_item = _numeric_quantity_item(
        stripped,
        line,
        reference_range_spans,
        trailing,
        leading,
    )
    if numeric_item is not None:
        return [numeric_item]
    word_item = _word_quantity_item(
        stripped,
        line,
        has_explicit_bare_quantity,
    )
    if word_item is not None:
        return [word_item]
    if (lines_count > 1 or len(stripped.split()) <= 8) and re.search(r"[a-zа-яё]", stripped, re.I):
        return [ExtractedItem(product_query=stripped, source_line=line)]
    return []


def parse_product_lines(text: str) -> list[ExtractedItem]:
    """Разбирает список товаров из текста."""
    unit_pattern = _build_unit_pattern()
    source, lines = _prepare_product_lines(text, unit_pattern)
    if not source:
        return []
    patterns = _build_product_line_patterns(unit_pattern)
    items: list[ExtractedItem] = []
    for line in lines:
        items.extend(_parse_product_line(line, len(lines), unit_pattern, patterns))
    return items
