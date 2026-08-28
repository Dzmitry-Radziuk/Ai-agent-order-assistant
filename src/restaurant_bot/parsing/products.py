"""Разбирает товарные строки, количества и фасовку без внешних эффектов."""

from __future__ import annotations

import re

from restaurant_bot.domain.models import CommentSource, ExtractedItem
from restaurant_bot.domain.text import clean_text, normalize_text
from restaurant_bot.domain.units import UNIT_ALIASES, normalize_unit
from restaurant_bot.parsing.comment_policy import explicit_supplier_comment
from restaurant_bot.parsing.number_words import NUMBER_WORDS, parse_number_words
from restaurant_bot.parsing.numeric_ranges import numeric_range_spans
from restaurant_bot.parsing.packaging import (
    _is_compact_catalog_measurement,
    _is_packaging_reference_prefix,
    _packaging_paraphrase_item,
    _single_product_packaging_item,
    _spoken_measurement_pair,
    explicit_packaging_preference,
)
from restaurant_bot.parsing.quantities import (
    _is_standalone_quantity,
    has_explicit_order_marker,
    shared_quantity_phrase,
)
from restaurant_bot.parsing.semantic.measurements import spoken_pair_quantity_for_query

_CATALOG_ATTRIBUTE_WORDS = {
    "в",
    "во",
    "на",
    "по",
    "упаковка",
    "упаковке",
    "упаковки",
    "коробка",
    "коробке",
    "коробки",
    "пачка",
    "пачке",
    "пачки",
    "пакет",
    "пакете",
    "фасовка",
    "фасовке",
    "примерно",
    "приблизительно",
    "около",
}
_COUNTRY_WORDS = {
    "австрия",
    "бельгия",
    "германия",
    "индия",
    "индонезия",
    "италия",
    "китай",
    "нидерланды",
    "польша",
    "россия",
    "таиланд",
    "турция",
    "франция",
    "чехия",
    "южная",
    "корея",
}

_QUALIFIER_FRAGMENT_RE = re.compile(
    r"^(?:(?:и\s+)?чтобы\b|"
    r"(?:желательно|обязательно|пожалуйста|просьб\w*|главное|только|срочно)\b|"
    r"(?:сорт|размер|цвет|марка|бренд|вид|тип|вес)\b)",
    flags=re.I,
)
_QUALIFIER_IDENTITY_RE = re.compile(
    r"^(?P<label>сорт|размер|цвет|марка|бренд|вид|тип)\b\s*"
    r"(?:(?:желательно|обязательно|нужн\w*|требу\w*|лучше|предпочт\w*)\s+)?",
    flags=re.I,
)
_QUALIFIER_COMMENT_RE = re.compile(
    r"^(?:(?:и\s+)?чтобы\b|(?:желательно|обязательно|пожалуйста|просьб\w*|"
    r"главное|только|срочно)\b)",
    flags=re.I,
)
_UNMARKED_WEIGHT_WORDS = {"вес", "весовой", "весовая", "весовое"}


def _query_with_unmarked_tail(name: str, tail: str) -> str:
    """Возвращает неподтверждённый хвост в возможное название товара."""
    return clean_text(f"{name} {tail}").strip(" .,;:!?-—–")


def _tail_comment(tail: str) -> tuple[str, CommentSource, str]:
    """Разделяет пожелание поставщику и хвост, который относится к названию товара."""
    explicit = explicit_supplier_comment(tail)
    if explicit:
        return explicit, CommentSource.EXPLICIT_MARKER, "none"
    packaging = explicit_packaging_preference(tail)
    if packaging:
        return packaging, CommentSource.SEMANTIC, "user_preference"
    return "", CommentSource.NONE, "none"


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
    lines: list[str] = []
    for raw_line in re.split(r"\n+", source):
        line = clean_text(raw_line)
        if not line:
            continue
        lines.extend(_split_comma_product_list(line, unit_pattern))
    return source, lines


def _has_independent_product_evidence(value: str, unit_pattern: str) -> bool:
    """Проверяет, содержит ли фрагмент самостоятельное описание товара."""
    fragment = clean_text(value).strip(" .,;:!?-—–")
    if (
        not fragment
        or explicit_supplier_comment(fragment)
        or _is_non_product_qualifier_fragment(fragment)
    ):
        return False
    normalized = normalize_text(fragment)
    has_compact_abbreviation = bool(re.search(r"[a-zа-яё]\.[a-zа-яё]", normalized, flags=re.I))
    has_numeric_range = bool(numeric_range_spans(fragment))
    residual = re.sub(
        rf"\d+(?:[,.]\d+)?\s*(?:{unit_pattern})\b",
        " ",
        normalized,
        flags=re.I,
    )
    residual = re.sub(r"\d+(?:[,.]\d+)?", " ", residual)
    words = re.findall(r"[a-zа-яё]+", residual, flags=re.I)
    product_words = [
        word
        for word in words
        if word not in UNIT_ALIASES
        and word not in NUMBER_WORDS
        and word not in {"и", "а", "или", "либо"}
        and word not in _CATALOG_ATTRIBUTE_WORDS
        and word not in _COUNTRY_WORDS
    ]
    if product_words and all(
        word in _COUNTRY_WORDS or word in _CATALOG_ATTRIBUTE_WORDS for word in words
    ):
        return False
    if (
        normalized.startswith(("примерно ", "приблизительно ", "около "))
        and len(product_words) <= 2
    ):
        return False
    if has_compact_abbreviation and len(product_words) <= 1:
        return False
    if has_numeric_range and not product_words:
        return False
    return bool(product_words)


def _is_non_product_qualifier_fragment(value: str) -> bool:
    """Проверяет, является ли фрагмент уточнением уже названного товара."""
    normalized = normalize_text(value).strip(" .,;:!?-—–")
    return bool(normalized and _QUALIFIER_FRAGMENT_RE.match(normalized))


def _normalize_unmarked_qualifiers(value: str) -> tuple[str, str]:
    """Отделяет признаки товара и пожелание из строки без явного количества."""
    parts = [clean_text(part).strip(" .,;:!?-—–") for part in re.split(r"(?<!\d),(?!\d)", value)]
    if len(parts) < 2 or not all(parts):
        return value, ""

    head_words = parts[0].split()
    while head_words and normalize_text(head_words[-1]) in _UNMARKED_WEIGHT_WORDS:
        head_words.pop()
    head = " ".join(head_words).strip()
    if not head:
        return value, ""

    identity_parts: list[str] = []
    comments: list[str] = []
    for part in parts[1:]:
        normalized = normalize_text(part)
        if _QUALIFIER_IDENTITY_RE.match(normalized):
            identity = _QUALIFIER_IDENTITY_RE.sub("\\g<label> ", part, count=1).strip()
            if identity:
                identity_parts.append(identity)
                continue
        if _QUALIFIER_COMMENT_RE.match(normalized):
            comments.append(re.sub(r"^(?:и\s+)?чтобы\s+", "чтобы ", part, flags=re.I))
            continue
        if normalize_text(part) in _UNMARKED_WEIGHT_WORDS:
            continue
        return value, ""

    query = clean_text(" ".join([head, *identity_parts])).strip(" .,;:!?-—–")
    comment = clean_text(", ".join(comments)).strip(" .,;:!?-—–")
    if not query or query == clean_text(value).strip(" .,;:!?-—–"):
        return value, comment
    return query, comment


def _split_comma_product_list(line: str, unit_pattern: str) -> list[str]:
    """Разделяет запятую только при подтверждённом списке независимых товаров."""
    # Запятая внутри десятичного числа остаётся частью измерения, остальные
    # запятые рассматриваются как возможные границы списка.
    parts = [clean_text(part) for part in re.split(r"(?<!\d),(?!\d)", line)]
    if len(parts) < 2 or not all(parts):
        return [line]
    if re.search(r"[a-zа-яё]\.[ \t]+[a-zа-яё]", line, flags=re.I):
        return [line]
    if not all(_has_independent_product_evidence(part, unit_pattern) for part in parts):
        return [line]
    # Уточнение с числом после первой части обычно относится к уже названному  # noqa: RUF003
    # товару (например, «срез корня до 5 см»), а не начинает новую позицию.  # noqa: RUF003
    if any(
        re.search(r"\b(?:до|от|около|примерно|приблизительно)\s+\d", part, flags=re.I)
        for part in parts[1:]
    ):
        return [line]
    quantity_pattern = re.compile(
        rf"(?:\d+(?:[,.]\d+)?\s*(?:{unit_pattern})\b)",
        flags=re.I,
    )
    word_quantity_pattern = re.compile(
        rf"(?:({'|'.join(re.escape(word) for word in NUMBER_WORDS)})\s+"
        rf"(?:{unit_pattern})\b)",
        flags=re.I,
    )
    broad_quantity_pattern = re.compile(r"\d+(?:[,.]\d+)?\s+\w+", flags=re.I)
    part_has_quantity = [
        bool(
            quantity_pattern.search(part)
            or word_quantity_pattern.search(part)
            or broad_quantity_pattern.search(part)
        )
        for part in parts
    ]
    if any(part_has_quantity) and not all(part_has_quantity):
        return [line]
    # Если количество есть только у первой части, последующие слова чаще  # noqa: RUF003
    # являются хвостовым уточнением или комментарием этой позиции. Разделяем
    # только полноценный список без количеств либо список, где количество
    # подтверждено у каждого товара.  # noqa: RUF003
    if all(part_has_quantity) or (len(parts) >= 2 and not any(part_has_quantity)):
        return parts
    return [line]


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
        rf"\d+(?:[,.]\d+)?\s*(?:{unit_pattern})\s*/\s*(?:"
        rf"\d+(?:[,.]\d+)?\s*(?:{unit_pattern})|"
        rf"(?:кор\w*|короб\w*|упак\w*|ящик\w*)"
        rf")"
        rf"(?:\s*/\s*(?:\d+(?:[,.]\d+)?\s*(?:{unit_pattern})|"
        rf"(?:кор\w*|короб\w*|упак\w*|ящик\w*)))*"
        rf"|"
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
    has_packaging_span: bool,
    packaging_spans: list[tuple[int, int]],
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
    if len(quantity_marks) == 1 and has_packaging_span and not has_explicit_order_lead:
        mark = quantity_marks[0]
        if any(mark.start() > end for _, end in packaging_spans):
            packaging_text = clean_text(
                " ".join(stripped[start:end] for start, end in packaging_spans)
            )
            product_query = clean_text(stripped[: mark.start()]).strip(" .,;:!?-—–")
            if product_query:
                return ExtractedItem(
                    product_query=product_query,
                    quantity=float(mark.group(1).replace(",", ".")),
                    unit=normalize_unit(mark.group("unit") or ""),
                    source_line=source_line,
                    packaging_text=packaging_text,
                    packaging_role="catalog_attribute",
                    packaging_confidence=0.9,
                )
        return ExtractedItem(
            product_query=stripped,
            source_line=source_line,
            packaging_text=clean_text(mark.group(0)),
            packaging_role="catalog_attribute",
            packaging_confidence=0.85,
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
        comment, comment_source, packaging_role = _tail_comment(tail)
        if tail and not comment:
            name = _query_with_unmarked_tail(name, tail)
        recovered.append(
            ExtractedItem(
                product_query=name,
                quantity=float(mark.group(1).replace(",", ".")),
                unit=normalize_unit(mark.group("unit") or ""),
                comment=comment,
                user_comment_to_supplier=comment,
                comment_source=comment_source,
                source_line=source_line,
                packaging_role=packaging_role,
                packaging_text=comment if packaging_role == "user_preference" else "",
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
        comment, comment_source, packaging_role = _tail_comment(tail)
        return [
            ExtractedItem(
                product_query=(name if comment else _query_with_unmarked_tail(name, tail)),
                quantity=float(mark.group(1).replace(",", ".")),
                unit=normalize_unit(mark.group("unit") or ""),
                comment=comment,
                user_comment_to_supplier=comment,
                comment_source=comment_source,
                source_line=source_line,
                packaging_role=packaging_role,
                packaging_text=comment if packaging_role == "user_preference" else "",
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
                comment, comment_source, packaging_role = _tail_comment(tail)
                return ExtractedItem(
                    product_query=(name if comment else _query_with_unmarked_tail(name, tail)),
                    quantity=quantity,
                    unit=unit,
                    comment=comment,
                    user_comment_to_supplier=comment,
                    comment_source=comment_source,
                    source_line=source_line,
                    packaging_role=packaging_role,
                    packaging_text=comment if packaging_role == "user_preference" else "",
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


def _spoken_pair_quantity_item(
    stripped: str,
    source_line: str,
    unit_pattern: str,
) -> ExtractedItem | None:
    """Разбирает разговорное количество «пару/пара единиц» перед или после товара."""
    if len(re.findall(r"\b(?:пару|пара)\b", normalize_text(stripped), flags=re.I)) != 1:
        return None
    prefix = re.match(
        rf"^(?:пару|пара)\s+(?P<unit>{unit_pattern})\b\s+",
        stripped,
        flags=re.I,
    )
    if prefix is not None:
        query = clean_text(stripped[prefix.end() :]).strip(" .,;:!?-—–")
        quantity = spoken_pair_quantity_for_query(stripped, query)
        if (
            query
            and quantity is not None
            and _has_independent_product_evidence(query, unit_pattern)
        ):
            return ExtractedItem(
                product_query=query,
                quantity=quantity[0],
                unit=quantity[1],
                source_line=source_line,
            )
    suffix = re.search(
        rf"\s+(?:пару|пара)\s+(?P<unit>{unit_pattern})\b\s*[.,;:!?]*$",
        stripped,
        flags=re.I,
    )
    if suffix is not None:
        query = clean_text(stripped[: suffix.start()]).strip(" .,;:!?-—–")
        quantity = spoken_pair_quantity_for_query(stripped, query)
        if (
            query
            and quantity is not None
            and _has_independent_product_evidence(query, unit_pattern)
        ):
            return ExtractedItem(
                product_query=query,
                quantity=quantity[0],
                unit=quantity[1],
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
    ) or has_explicit_order_marker(line)
    stripped = re.sub(
        r"^(?:(?:добавь|добавьте|дабавь|дабавьте|добавить|закажи|закажите|заказать|"
        r"хочу|хотим|нужно|надо|мне\s+(?:нуж\w*|надо)|нам\s+(?:нуж\w*|надо))\s+)+",
        "",
        line,
        flags=re.I,
    )
    if _is_standalone_quantity(stripped):
        return []
    spoken_pair_item = _spoken_pair_quantity_item(stripped, line, unit_pattern)
    if spoken_pair_item is not None:
        return [spoken_pair_item]
    spoken_pair = _spoken_measurement_pair(stripped, unit_pattern)
    if spoken_pair is not None:
        if not _has_independent_product_evidence(stripped, unit_pattern):
            return []
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
    paraphrase_item = _packaging_paraphrase_item(stripped, line)
    if paraphrase_item is not None:
        if not _has_independent_product_evidence(stripped, unit_pattern):
            return []
        return [paraphrase_item]
    packaging_spans, alternative_packaging_spans, reference_range_spans, quantity_marks = (
        _find_quantity_marks(stripped, unit_pattern, packaging, alternative_packaging)
    )
    catalog_item = _catalog_measurement_item(
        stripped,
        line,
        quantity_marks,
        has_explicit_order_lead,
        any("/" in stripped[start:end] for start, end in packaging_spans),
        packaging_spans,
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
        if _has_independent_product_evidence(stripped, unit_pattern):
            return [ExtractedItem(product_query=stripped, source_line=line)]
        return []
    if reference_range_spans and not quantity_marks and not has_explicit_bare_quantity:
        # Без отдельного маркера диапазон не может быть заказанным.
        if _has_independent_product_evidence(stripped, unit_pattern):
            return [ExtractedItem(product_query=stripped, source_line=line)]
        return []
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
    product_query, comment = _normalize_unmarked_qualifiers(stripped)
    if (
        (lines_count > 1 or len(stripped.split()) <= 8 or product_query != stripped)
        and re.search(r"[a-zа-яё]", stripped, re.I)
        and _has_independent_product_evidence(stripped, unit_pattern)
    ):
        return [
            ExtractedItem(
                product_query=product_query,
                comment=comment,
                user_comment_to_supplier=comment,
                comment_source=CommentSource.SEMANTIC if comment else CommentSource.NONE,
                source_line=line,
            )
        ]
    return []


def parse_product_lines(text: str) -> list[ExtractedItem]:
    """Разбирает список товаров из текста."""
    shared = shared_quantity_phrase(text)
    if shared is not None:
        names, quantity, unit, tail = shared
        return [
            ExtractedItem(
                product_query=name,
                quantity=quantity,
                unit=unit,
                comment=tail,
                user_comment_to_supplier=tail,
                comment_source=CommentSource.SEMANTIC if tail else CommentSource.NONE,
                source_line=clean_text(text),
            )
            for name in names
        ]
    unit_pattern = _build_unit_pattern()
    source, lines = _prepare_product_lines(text, unit_pattern)
    if not source:
        return []
    patterns = _build_product_line_patterns(unit_pattern)
    items: list[ExtractedItem] = []
    for line in lines:
        items.extend(_parse_product_line(line, len(lines), unit_pattern, patterns))
    return items


def has_multiple_explicit_order_items(text: str) -> bool:
    """Проверяет, содержит ли исходная фраза несколько позиций с явным количеством заказа."""
    items = parse_product_lines(text)
    return len(items) > 1 and all(item.quantity is not None and bool(item.unit) for item in items)
