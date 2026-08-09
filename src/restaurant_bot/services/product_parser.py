"""Разбирает товарные строки, количества и фасовку без внешних эффектов."""

from __future__ import annotations

import re

from restaurant_bot.domain.models import CommentSource, ExtractedItem
from restaurant_bot.services.comment_policy import explicit_supplier_comment
from restaurant_bot.services.text import (
    NUMBER_WORDS,
    UNIT_ALIASES,
    clean_text,
    normalize_text,
    normalize_unit,
    numeric_range_spans,
    parse_number_words,
)

_PACKAGING_REFERENCE_PREFIX_RE = re.compile(
    r"(?:\b(?:в|на)\s+)?(?:упаковк\w*|фасовк\w*|бутылк\w*)\s*$",
    flags=re.I,
)


def _is_standalone_quantity(value: str) -> bool:
    """Проверяет, что фрагмент содержит только количество и единицу."""
    tokens = [
        token.strip(" .,:;—–-")
        for token in normalize_text(value).split()
        if token.strip(" .,:;—–-")
    ]
    parsed = parse_number_words(tokens, 0)
    if parsed is None:
        return False
    _, end = parsed
    if end < len(tokens) and tokens[end] in UNIT_ALIASES:
        end += 1
    return end == len(tokens)


def _extract_global_comment(text: str) -> tuple[str, str]:
    """Отделяет явно помеченный общий комментарий от списка товаров."""
    original = str(text or "")
    source = clean_text(original)
    match = re.fullmatch(
        r"(?P<products>.+?)"
        r"(?:[.!?;]\s*|\s*,?\s+(?:и|а)\s+)"
        r"(?:(?:все|всё|всем|для всех)"
        r"(?:\s+(?:товар\w*|позици\w*|это(?:\s+дело)?))?|"
        r"общ(?:ий|его)\s+комментар(?:ий|ия))"
        r"(?:\s*[,;:—–-]\s*|\s+)"
        r"(?P<comment>.+?)\s*[.!?]*",
        source,
        flags=re.I,
    )
    if match is None:
        return original, ""
    products = clean_text(match.group("products")).strip(" ,;:.!?—–-")
    comment = clean_text(match.group("comment")).strip(" ,;:.!?—–-")
    if not products or not comment:
        return original, ""
    return products, comment


def has_explicit_global_comment_scope(text: str) -> bool:
    """Проверяет, что пользователь явно распространил комментарий на всю заявку."""
    normalized = normalize_text(text)
    if not normalized:
        return False
    if re.search(
        r"(?:^|\s)(?:все|всё|всем|для всех)"
        r"(?:\s+(?:товаров|товары|позиций|позиции))?(?:\s|$)",
        normalized,
        flags=re.I,
    ):
        return True
    if re.search(
        r"(?:для|ко|к|на)\s+(?:всей|всю|всего|весь)\s+(?:заявк\w*|заказ\w*)",
        normalized,
        flags=re.I,
    ):
        return True
    return "общ" in normalized and "комментар" in normalized


def _query_with_unmarked_tail(name: str, tail: str) -> str:
    """Возвращает неподтверждённый хвост в возможное название товара."""
    return clean_text(f"{name} {tail}").strip(" .,;:!?-—–")


def _spoken_measurement_pair(
    value: str,
    unit_pattern: str,
) -> tuple[str, str, float] | None:
    """Классифицирует два соседних значения как фасовку, а не заказ."""
    number = r"\d+(?:[,.]\d+)?"
    unit = rf"(?:{unit_pattern})"
    from_to = re.search(
        rf"(?P<pair>\bот\s+{number}(?:\s*{unit})?\s+до\s+{number}\s*{unit}\b)",
        value,
        flags=re.I,
    )
    if from_to is not None:
        return clean_text(from_to.group("pair")), "catalog_attribute", 0.9

    explicit_units = re.search(
        rf"(?P<pair>\b{number}\s*(?P<first_unit>{unit_pattern})\s*"
        rf"(?:,|\bи\b)\s*{number}\s*(?P<second_unit>{unit_pattern})\b)",
        value,
        flags=re.I,
    )
    established_ranges = numeric_range_spans(value)
    same_unit_pair = bool(
        explicit_units is not None
        and normalize_unit(explicit_units.group("first_unit"))
        == normalize_unit(explicit_units.group("second_unit"))
    )
    if (
        explicit_units is not None
        and same_unit_pair
        and not any(
            start < explicit_units.end("pair") and explicit_units.start("pair") < end
            for start, end in established_ranges
        )
    ):
        return clean_text(explicit_units.group("pair")), "ambiguous", 0.6

    shared_unit = re.search(
        rf"(?P<pair>\b{number}\s+(?:и|либо|или)\s+{number}\s*{unit}\b)",
        value,
        flags=re.I,
    )
    if shared_unit is not None:
        return clean_text(shared_unit.group("pair")), "ambiguous", 0.6
    return None


def _is_packaging_reference_prefix(value: str) -> bool:
    """Проверяет, что перед числом явно названа фасовка, а не заказ."""
    return bool(_PACKAGING_REFERENCE_PREFIX_RE.search(clean_text(value)))


def _single_product_packaging_item(
    text: str,
    source_line: str,
    quantity_marks: list[re.Match[str]],
) -> ExtractedItem | None:
    """Собирает фасовку и заказанное количество в одну позицию."""
    if len(quantity_marks) < 2:
        return None

    first = quantity_marks[0]
    last = quantity_marks[-1]
    between = text[first.end() : last.start()]
    packaging_lead = re.search(
        r"\b(?:упаковк\w*|фасовк\w*)(?:\s+по)?\b",
        between,
        flags=re.I,
    )
    if len(quantity_marks) == 2 and packaging_lead is not None:
        product_query = clean_text(text[: first.start()]).strip(" .,;:!?-—–")
        if product_query:
            return ExtractedItem(
                product_query=product_query,
                quantity=float(first.group(1).replace(",", ".")),
                unit=normalize_unit(first.group("unit") or ""),
                source_line=source_line,
                packaging_text=clean_text(f"{between[packaging_lead.start() :]} {last.group(0)}"),
                packaging_role="catalog_attribute",
                packaging_confidence=0.9,
            )
    order_lead = re.search(
        r"\b(?:нужно|надо|закаж(?:и|ем|у)|постав(?:ь|ить)|возьм(?:и|ем)|"
        r"добав(?:ь|ить)|количеств(?:о|ом)?|мне)\b",
        between,
        flags=re.I,
    )
    number = r"\d+(?:[,.]\d+)?"
    unit = r"(?:г|гр|грамм\w*|кг|килограмм\w*|мл|л|шт|штук\w*|бан\w*|"
    unit += r"бут\w*|упак\w*|короб\w*|ведр\w*)"
    connector = re.search(
        rf"(?P<pack>{number}\s*{unit}\s*(?:на|/|[*xх×])\s*{number}\s*{unit})",
        text,
        flags=re.I,
    )
    has_packaging_connector = connector is not None and connector.start() <= first.start()

    if not has_packaging_connector:
        residual = re.sub(
            rf"(?:{number}\s*{unit})|\b(?:и|а|на|или|либо)\b|[.,;:!?()—–-]",
            " ",
            between,
            flags=re.I,
        )
        residual = re.sub(r"\s+", " ", residual).strip()
        if not order_lead and residual:
            return None
        product_query = text[: first.end()]
        comment_text = between[: order_lead.start()] if order_lead else ""
        comment = explicit_supplier_comment(comment_text)
        return ExtractedItem(
            product_query=clean_text(product_query).strip(" .,;:!?-—–"),
            quantity=float(last.group(1).replace(",", ".")),
            unit=normalize_unit(last.group("unit") or ""),
            comment=comment,
            user_comment_to_supplier=comment,
            comment_source=(CommentSource.EXPLICIT_MARKER if comment else CommentSource.NONE),
            source_line=source_line,
            packaging_text=clean_text(first.group(0)),
            packaging_role="catalog_attribute",
            packaging_confidence=0.85,
        )

    if len(quantity_marks) == 2:
        # Без отдельного заказа сохраняем прежний путь AI-postprocessing:
        # он удаляет ложный connector-item и нормализует исходную строку.
        # Здесь объединяем вариант, если присутствует третье заказанное количество.
        return None

    assert connector is not None
    product_end = last.start()
    if order_lead:
        product_end = first.end() + order_lead.start()
    product_text = text[:product_end]
    product_query = clean_text(
        f"{product_text[: first.start()]} {product_text[first.end() :]}"
    ).strip(" .,;:!?-—–")
    comment_text = between[: order_lead.start()] if order_lead else ""
    comment = explicit_supplier_comment(comment_text)
    return ExtractedItem(
        product_query=product_query,
        quantity=float(last.group(1).replace(",", ".")),
        unit=normalize_unit(last.group("unit") or ""),
        comment=comment,
        user_comment_to_supplier=comment,
        comment_source=(CommentSource.EXPLICIT_MARKER if comment else CommentSource.NONE),
        source_line=source_line,
        packaging_text=clean_text(connector.group("pack")),
        packaging_role="catalog_attribute",
        packaging_confidence=0.9,
    )


def parse_product_lines(text: str) -> list[ExtractedItem]:
    """Разбирает список товаров из текста."""
    source = str(text or "").replace(";", "\n").strip()
    if not source:
        return []
    unit_pattern = "|".join(sorted((re.escape(key) for key in UNIT_ALIASES), key=len, reverse=True))
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

    items: list[ExtractedItem] = []
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

    for line in lines:
        stripped = re.sub(
            r"^(?:добавь|добавить|закажи|заказать|нужно|надо)\s+", "", line, flags=re.I
        )
        if _is_standalone_quantity(stripped):
            continue
        spoken_pair = _spoken_measurement_pair(stripped, unit_pattern)
        if spoken_pair is not None:
            packaging_text, packaging_role, packaging_confidence = spoken_pair
            items.append(
                ExtractedItem(
                    product_query=stripped,
                    source_line=line,
                    packaging_text=packaging_text,
                    packaging_role=packaging_role,
                    packaging_confidence=packaging_confidence,
                )
            )
            continue
        packaging_spans = [match.span() for match in packaging.finditer(stripped)]
        alternative_packaging_spans = [
            match.span() for match in alternative_packaging.finditer(stripped)
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
                start < mark.end() and mark.start() < end
                for start, end in alternative_packaging_spans
            )
        ]
        if len(quantity_marks) == 1 and _is_packaging_reference_prefix(
            stripped[: quantity_marks[0].start()]
        ):
            mark = quantity_marks[0]
            items.append(
                ExtractedItem(
                    product_query=stripped,
                    source_line=line,
                    packaging_text=clean_text(mark.group(0)),
                    packaging_role="catalog_attribute",
                    packaging_confidence=0.9,
                )
            )
            continue
        packaged_item = _single_product_packaging_item(stripped, line, quantity_marks)
        if packaged_item is not None:
            items.append(packaged_item)
            continue
        has_explicit_bare_quantity = bool(
            re.search(r"(?:^|\s)[-—–:]\s*\d+(?:[,.]\d+)?\s*$", stripped)
        )
        # Recover several products spoken in one segment by using each
        # explicit quantity as the boundary of the preceding product.
        if len(quantity_marks) >= 2 and not re.search(r"[—–-]\s*\d", stripped):
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
                        comment_source=(
                            CommentSource.EXPLICIT_MARKER if comment else CommentSource.NONE
                        ),
                        source_line=line,
                    )
                )
            if len(recovered) == len(quantity_marks):
                items.extend(recovered)
                continue
            quantity_units = {normalize_unit(mark.group("unit") or "") for mark in quantity_marks}
            if len(quantity_units) == 1:
                items.append(
                    ExtractedItem(
                        product_query=stripped,
                        source_line=line,
                        packaging_text=clean_text(stripped),
                        packaging_role="ambiguous",
                        packaging_confidence=0.6,
                    )
                )
                continue
        if len(quantity_marks) == 1:
            mark = quantity_marks[0]
            # A voice phrase can enumerate products and give a quantity only
            # for the last one: "сироп и говядина 10 кг".  The first product
            # must remain in the draft (with an unanswered quantity), rather
            # than being silently swallowed into a single synthetic name.
            prefix = clean_text(stripped[: mark.start()]).strip(" ,;:-—–")
            enumerated_names = [
                clean_text(part).strip(" ,;:-—–")
                for part in re.split(r"\s+(?:и|а также)\s+", prefix, flags=re.I)
            ]
            if len(enumerated_names) > 1 and all(enumerated_names):
                items.extend(
                    ExtractedItem(product_query=name, source_line=line)
                    for name in enumerated_names[:-1]
                )
                items.append(
                    ExtractedItem(
                        product_query=enumerated_names[-1],
                        quantity=float(mark.group(1).replace(",", ".")),
                        unit=normalize_unit(mark.group("unit") or ""),
                        source_line=line,
                    )
                )
                continue
            name = clean_text(stripped[: mark.start()]).strip(" ,;:-—–")
            tail = clean_text(stripped[mark.end() :]).strip(" .,!?:;-—–")
            if name and not tail:
                items.append(
                    ExtractedItem(
                        product_query=name,
                        quantity=float(mark.group(1).replace(",", ".")),
                        unit=normalize_unit(mark.group("unit") or ""),
                        source_line=line,
                    )
                )
                continue
            # "Сироп Роза 1 л — 12" contains product packaging followed by
            # the ordered quantity.  A number after punctuation is never a
            # supplier comment, so leave this form to the trailing-quantity
            # parser below.
            if name and tail and not re.match(r"^\s*[,;:\-—–]*\s*\d", stripped[mark.end() :]):
                comment = explicit_supplier_comment(tail)
                items.append(
                    ExtractedItem(
                        product_query=(name if comment else _query_with_unmarked_tail(name, tail)),
                        quantity=float(mark.group(1).replace(",", ".")),
                        unit=normalize_unit(mark.group("unit") or ""),
                        comment=comment,
                        user_comment_to_supplier=comment,
                        comment_source=(
                            CommentSource.EXPLICIT_MARKER if comment else CommentSource.NONE
                        ),
                        source_line=line,
                    )
                )
                continue
        if packaging_spans and not quantity_marks:
            items.append(ExtractedItem(product_query=stripped, source_line=line))
            continue
        if reference_range_spans and not quantity_marks and not has_explicit_bare_quantity:
            # Без отдельного маркера диапазон не может быть заказанным
            # Do not pass a range endpoint to the fallback quantity parser.
            items.append(ExtractedItem(product_query=stripped, source_line=line))
            continue
        if alternative_packaging_spans:
            # Alternative package sizes belong to one product. If a spoken
            # order quantity follows them, split only that final quantity.
            word_quantity = trailing_word_quantity.search(stripped)
            if word_quantity:
                quantity_tokens = normalize_text(word_quantity.group("quantity")).split()
                parsed_quantity = parse_number_words(quantity_tokens, 0)
                if parsed_quantity is not None and parsed_quantity[1] == len(quantity_tokens):
                    name = clean_text(stripped[: word_quantity.start()]).strip(" ,;:-—–")
                    if name:
                        items.append(
                            ExtractedItem(
                                product_query=name,
                                quantity=parsed_quantity[0],
                                unit=normalize_unit(word_quantity.group("unit")),
                                source_line=line,
                            )
                        )
                        continue
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
                items.append(
                    ExtractedItem(
                        product_query=name,
                        quantity=float(
                            (match.group("qty") or match.group("bare_qty")).replace(",", ".")
                        ),
                        unit=normalize_unit(match.group("unit") or ""),
                        source_line=line,
                    )
                )
                continue
        match = leading.match(stripped)
        if match:
            name = clean_text(match.group(3)).strip(" -:")
            if name:
                items.append(
                    ExtractedItem(
                        product_query=name,
                        quantity=float(match.group("qty").replace(",", ".")),
                        unit=normalize_unit(match.group("unit") or ""),
                        source_line=line,
                    )
                )
                continue

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
                normalize_unit(tokens[end])
                if end < len(tokens) and tokens[end] in UNIT_ALIASES
                else ""
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
                    items.append(
                        ExtractedItem(
                            product_query=(
                                name if comment else _query_with_unmarked_tail(name, tail)
                            ),
                            quantity=quantity,
                            unit=unit,
                            comment=comment,
                            user_comment_to_supplier=comment,
                            comment_source=(
                                CommentSource.EXPLICIT_MARKER if comment else CommentSource.NONE
                            ),
                            source_line=line,
                        )
                    )
                    break
                continue
            if name:
                items.append(
                    ExtractedItem(
                        product_query=name,
                        quantity=quantity,
                        unit=unit,
                        source_line=line,
                    )
                )
                break
        else:
            if (len(lines) > 1 or len(stripped.split()) <= 8) and re.search(
                r"[a-zа-яё]", stripped, re.I
            ):
                items.append(ExtractedItem(product_query=stripped, source_line=line))
    return items


def parse_quantity_unit(text: str) -> tuple[float | None, str]:
    """Разбирает короткий ответ с количеством и единицей."""
    normalized = normalize_text(text)
    if not normalized:
        return None, ""
    tokens = [
        cleaned
        for token in normalized.replace(",", ".").split()
        if (cleaned := re.sub(r"\.+$", "", token))
    ]
    if len(tokens) >= 1:
        try:
            quantity = float(tokens[0])
            unit = (
                normalize_unit(tokens[1]) if len(tokens) > 1 and tokens[1] in UNIT_ALIASES else ""
            )
            return quantity, unit
        except ValueError:
            pass
    parsed = parse_number_words(tokens, 0)
    if parsed:
        quantity, end = parsed
        unit = (
            normalize_unit(tokens[end]) if end < len(tokens) and tokens[end] in UNIT_ALIASES else ""
        )
        return quantity, unit
    return None, ""
