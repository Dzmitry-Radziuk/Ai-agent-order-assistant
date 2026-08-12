"""Содержит разбор фасовки и каталожных измерений."""

from __future__ import annotations

import re

from restaurant_bot.domain.models import CommentSource, ExtractedItem
from restaurant_bot.parsing.comment_policy import explicit_supplier_comment
from restaurant_bot.services.text import (
    normalize_unit,
    numeric_range_spans,
)
from restaurant_bot.text_normalization import clean_text, normalize_text

_PACKAGING_REFERENCE_PREFIX_RE = re.compile(
    r"(?:\b(?:в|на)\s+)?(?:упаковк\w*|фасовк\w*|бутылк(?:а|е|у|ой))\s*$",
    flags=re.I,
)


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


def _is_compact_catalog_measurement(value: str, mark: re.Match[str]) -> bool:
    """Отличает слитную фасовочную меру от явного заказа."""
    if re.search(r"\s", mark.group(0)):
        return False
    prefix = normalize_text(value[: mark.start()])
    return not bool(
        re.search(
            r"\b(?:\u043d\u0443\u0436\u043d\u043e|\u043d\u0430\u0434\u043e|\u0437\u0430\u043a\u0430\u0436\w*|"
            r"\u043f\u043e\u0441\u0442\u0430\u0432\w*|\u0434\u043e\u0431\u0430\u0432\w*)\b",
            prefix,
            flags=re.I,
        )
    )


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
        # Без отдельного заказа сохраняем прежний путь постобработки AI:
        # он удаляет ложную позицию-связку и нормализует исходную строку.
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
