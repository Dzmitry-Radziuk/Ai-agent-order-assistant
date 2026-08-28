"""Распознаёт измерения и фасовочные связи без присвоения заказа."""

from __future__ import annotations

import re

from restaurant_bot.domain.text import clean_text, normalize_text
from restaurant_bot.domain.units import UNIT_ALIASES, normalize_unit
from restaurant_bot.parsing.number_words import NUMBER_WORDS
from restaurant_bot.parsing.numeric_ranges import numeric_range_spans
from restaurant_bot.parsing.semantic.models import SemanticFact, SemanticFactKind

_NUMBER = r"\d+(?:[,.]\d+)?"
_UNIT = "|".join(sorted((re.escape(value) for value in UNIT_ALIASES), key=len, reverse=True))
_MEASUREMENT_RE = re.compile(
    rf"(?P<number>{_NUMBER})\s*(?P<unit>{_UNIT}|%)(?![A-Za-zА-Яа-яЁё])",
    flags=re.I,
)
_PACKAGING_RE = re.compile(
    rf"(?:"
    rf"{_NUMBER}\s*(?:{_UNIT})\s*/\s*(?:{_NUMBER}\s*)?(?:{_UNIT})|"
    rf"{_NUMBER}\s*(?:{_UNIT})\s+(?:в|на)\s+(?:упаковк\w*|коробк\w*|пачк\w*)|"
    rf"(?:в|на)\s+(?:упаковк\w*|коробк\w*|пачк\w*)\s+{_NUMBER}\s*(?:{_UNIT})|"
    rf"(?:упаковк\w*|коробк\w*|пачк\w*)\s+по\s+{_NUMBER}\s*(?:{_UNIT})"
    rf")",
    flags=re.I,
)
_PACKAGING_WORD_RE = re.compile(
    r"\b(?:упаковк\w*|коробк\w*|пачк\w*|фасовк\w*|бутылк\w*|пакет\w*)\b",
    flags=re.I,
)
_COMMAND_RE = re.compile(
    r"^(?:добавь|добавить|закажи|заказать|нужно|надо)\b[^,;.!?]*",
    flags=re.I,
)
_COMMENT_RE = re.compile(
    r"\b(?:желательно|нужно|надо|без|только|отдельно|покрупнее|покрупней)\b[^,;.!?]*",
    flags=re.I,
)
_COMMENT_MARKERS = {
    "желательно",
    "нужно",
    "надо",
    "без",
    "только",
    "отдельно",
    "покрупнее",
    "покрупней",
}
_GLOBAL_QUANTITY_SCOPE_RE = re.compile(
    r"\b(?:\u0432\u0441\u0435\u0433\u043e|\u0438\u0442\u043e\u0433\u043e|\u043d\u0430\s+\u0432\u0441\u0435\u0445|\u043e\u0431\u0449\u0435\s+\u043a\u043e\u043b\u0438\u0447\u0435\u0441\u0442\u0432\u043e)\b",
    flags=re.I,
)
_WORD_MEASUREMENT_RE = re.compile(
    rf"(?P<number>(?:{'|'.join(sorted((re.escape(word) for word in NUMBER_WORDS), key=len, reverse=True))})"
    rf"(?:\s+(?:{'|'.join(sorted((re.escape(word) for word in NUMBER_WORDS), key=len, reverse=True))}))*)\s+"
    rf"(?P<unit>{_UNIT})\b",
    flags=re.I,
)
_SPOKEN_PAIR_MEASUREMENT_RE = re.compile(
    rf"\b(?:пару|пара)\s+(?P<unit>{_UNIT})\b",
    flags=re.I,
)
_SPOKEN_PAIR_MEASUREMENT_PREFIX_RE = re.compile(
    rf"^\s*(?:пару|пара)\s+(?P<unit>{_UNIT})\b\s+",
    flags=re.I,
)
_SPOKEN_PAIR_MEASUREMENT_SUFFIX_RE = re.compile(
    rf"\s+(?:пару|пара)\s+(?P<unit>{_UNIT})\b\s*[.,;:!?]*\s*$",
    flags=re.I,
)
_SPOKEN_PAIR_SEGMENT_BOUNDARY_RE = re.compile(
    r"[,.;]|\b(?:и|или|а|также|потом|затем)\b",
    flags=re.I,
)
_PRODUCT_TOKEN_RE = re.compile(r"[a-zа-яё0-9-]+", flags=re.I)


def _span_overlaps(span: tuple[int, int], other: tuple[int, int]) -> bool:
    """Проверяет пересечение двух полуинтервалов текста."""
    return span[0] < other[1] and other[0] < span[1]


def extract_semantic_facts(source_text: str) -> tuple[SemanticFact, ...]:
    """Извлекает измерения, диапазоны и фасовочные связи исходной фразы."""
    source = str(source_text or "")
    if not source:
        return ()
    packaging_spans = [match.span() for match in _PACKAGING_RE.finditer(source)]
    facts: list[SemanticFact] = []
    command = _COMMAND_RE.match(source.strip())
    if command is not None:
        start = len(source) - len(source.lstrip())
        facts.append(
            SemanticFact(
                kind=SemanticFactKind.COMMAND_TEXT,
                start=start,
                end=start + len(command.group(0)),
                original_text=clean_text(command.group(0)),
                normalized_value=normalize_text(command.group(0)),
                confidence=0.99,
                provenance="source.command_prefix",
            )
        )
    for separator in re.finditer(r"[,;]", source):
        facts.append(
            SemanticFact(
                kind=SemanticFactKind.SEPARATOR,
                start=separator.start(),
                end=separator.end(),
                original_text=separator.group(0),
                normalized_value=separator.group(0),
                confidence=0.99,
                provenance="source.punctuation",
            )
        )
    for match in _MEASUREMENT_RE.finditer(source):
        span = match.span()
        number = match.group("number")
        unit = match.group("unit")
        normalized = f"{number.replace(',', '.')} {normalize_unit(unit) if unit != '%' else '%'}"
        in_packaging = any(
            _span_overlaps(span, packaging_span) for packaging_span in packaging_spans
        )
        in_range = any(
            _span_overlaps(span, range_span) for range_span in numeric_range_spans(source)
        )
        marker_prefix = source[max(0, match.start() - 24) : match.start()]
        global_quantity_scope = bool(_GLOBAL_QUANTITY_SCOPE_RE.search(marker_prefix))
        local_order_marker = bool(
            re.search(
                r"\b(?:нужно|надо|закаж\w*|заказ\w*|добав\w*|постав\w*)\b",
                marker_prefix,
                re.I,
            )
        )
        terminal_order_quantity = not source[match.end() :].strip(" \t\r\n.,;:!?()[]{}")
        explicit_order = (
            local_order_marker or terminal_order_quantity
        ) and not global_quantity_scope
        kind = (
            SemanticFactKind.CATALOG_ATTRIBUTE
            if in_packaging or in_range
            else (
                SemanticFactKind.ORDER_QUANTITY if explicit_order else SemanticFactKind.MEASUREMENT
            )
        )
        facts.append(
            SemanticFact(
                kind=kind,
                start=match.start(),
                end=match.end(),
                original_text=clean_text(match.group(0)),
                normalized_value=normalized,
                confidence=(
                    0.95 if in_packaging or in_range else (0.9 if explicit_order else 0.45)
                ),
                provenance=(
                    "source.packaging_measurement"
                    if in_packaging
                    else ("source.order_marker" if explicit_order else "source.measurement")
                ),
            )
        )
    for match in _WORD_MEASUREMENT_RE.finditer(source):
        if any(_span_overlaps(match.span(), packaging_span) for packaging_span in packaging_spans):
            continue
        if match.end() < len(source) and source[match.end() :].strip(" .,;:-—–"):
            continue
        facts.append(
            SemanticFact(
                kind=SemanticFactKind.ORDER_QUANTITY,
                start=match.start(),
                end=match.end(),
                original_text=clean_text(match.group(0)),
                normalized_value=normalize_text(match.group(0)),
                confidence=0.8,
                provenance="source.trailing_order_quantity",
            )
        )
    for start, end in packaging_spans:
        facts.append(
            SemanticFact(
                kind=SemanticFactKind.CATALOG_ATTRIBUTE,
                start=start,
                end=end,
                original_text=clean_text(source[start:end]),
                normalized_value=normalize_text(source[start:end]),
                confidence=0.95,
                provenance="source.packaging_relation",
            )
        )
    for match in _PACKAGING_WORD_RE.finditer(source):
        facts.append(
            SemanticFact(
                kind=SemanticFactKind.CATALOG_ATTRIBUTE,
                start=match.start(),
                end=match.end(),
                original_text=clean_text(match.group(0)),
                normalized_value=normalize_text(match.group(0)),
                confidence=0.8,
                provenance="source.packaging_word",
            )
        )
    for match in _COMMENT_RE.finditer(source):
        facts.append(
            SemanticFact(
                kind=SemanticFactKind.COMMENT,
                start=match.start(),
                end=match.end(),
                original_text=clean_text(match.group(0)),
                normalized_value=normalize_text(match.group(0)),
                confidence=0.75,
                provenance="source.comment_marker",
            )
        )
    for segment in re.finditer(r"[^,;]+", source):
        value = clean_text(segment.group(0)).strip(" .,;:-—–")
        words = re.findall(r"[a-zа-яё]+", normalize_text(value), flags=re.I)
        if not words or words[0] in _COMMENT_MARKERS:
            continue
        facts.append(
            SemanticFact(
                kind=SemanticFactKind.PRODUCT,
                start=segment.start(),
                end=segment.end(),
                original_text=value,
                normalized_value=normalize_text(value),
                confidence=0.7,
                provenance="source.product_segment",
            )
        )
    return tuple(sorted(facts, key=lambda fact: (fact.start, fact.end, fact.kind)))


def order_quantity_facts(source_text: str) -> tuple[SemanticFact, ...]:
    """Возвращает только подтверждённые факты количества заказа из источника."""
    return tuple(
        fact
        for fact in extract_semantic_facts(source_text)
        if fact.kind is SemanticFactKind.ORDER_QUANTITY
    )


def has_order_quantity_evidence(source_text: str) -> bool:
    """Проверяет, содержит ли исходная фраза подтверждённое количество заказа."""
    return bool(order_quantity_facts(source_text))


def has_global_quantity_scope(source_text: str) -> bool:
    """Проверяет, относится ли названное количество ко всему списку товаров."""
    return bool(_GLOBAL_QUANTITY_SCOPE_RE.search(normalize_text(source_text)))


def _same_inflected_product_word(left: str, right: str) -> bool:
    """Сопоставляет слово товара с безопасным учётом окончания."""
    normalized_left = normalize_text(left)
    normalized_right = normalize_text(right)
    if normalized_left == normalized_right:
        return True
    shared_length = 0
    for left_char, right_char in zip(normalized_left, normalized_right, strict=False):
        if left_char != right_char:
            break
        shared_length += 1
    shorter_length = min(len(normalized_left), len(normalized_right))
    required_shared_length = 3 if shorter_length <= 4 else 4
    return (
        shorter_length >= 3
        and shared_length >= required_shared_length
        and len(normalized_left) - shared_length <= 2
        and len(normalized_right) - shared_length <= 2
    )


def _pair_local_segment(source: str, match: re.Match[str], *, after: bool) -> str:
    """Возвращает часть одной разговорной позиции рядом с конструкцией «пару»."""
    if after:
        # Запятая после количества часто отделяет комментарий, а не следующую  # noqa: RUF003
        # позицию. Убираем её до поиска границы, чтобы не потерять товар перед
        # конструкцией «товар, пару килограмм, ...».
        tail = source[match.end() :].lstrip(" \t,;:")
        boundary = _SPOKEN_PAIR_SEGMENT_BOUNDARY_RE.search(tail)
        return tail[: boundary.start()] if boundary is not None else tail
    head = source[: match.start()].rstrip(" \t,;:")
    boundaries = tuple(_SPOKEN_PAIR_SEGMENT_BOUNDARY_RE.finditer(head))
    return head[boundaries[-1].end() :] if boundaries else head


def _segment_has_product_anchor(segment: str, query_tokens: tuple[str, ...]) -> bool:
    """Проверяет, что локальный фрагмент содержит якорь конкретного товара."""
    segment_tokens = _PRODUCT_TOKEN_RE.findall(segment)
    return any(
        _same_inflected_product_word(query_token, segment_token)
        for query_token in query_tokens
        for segment_token in segment_tokens
    )


def product_query_has_anchor(product_query: str, source_fragment: str) -> bool:
    """Проверяет, что фрагмент содержит слово указанного товара."""
    query_tokens = tuple(_PRODUCT_TOKEN_RE.findall(normalize_text(product_query)))
    return bool(query_tokens) and _segment_has_product_anchor(source_fragment, query_tokens)


def spoken_pair_quantity_for_query(
    source_text: str, product_query: str
) -> tuple[float, str] | None:
    """Находит «пару <единиц>» только в локальном фрагменте указанного товара."""
    source = normalize_text(source_text)
    if not source or not _PRODUCT_TOKEN_RE.search(normalize_text(product_query)):
        return None
    for match in _SPOKEN_PAIR_MEASUREMENT_RE.finditer(source):
        before_segment = _pair_local_segment(source, match, after=False)
        after_segment = _pair_local_segment(source, match, after=True)
        if product_query_has_anchor(product_query, before_segment):
            return 2.0, normalize_unit(match.group("unit"))
        if not product_query_has_anchor(product_query, after_segment):
            continue
        prefix = source[: match.start()].rstrip(" \t,;:")
        if prefix and not re.search(
            r"\b(?:и|или|а|также|нужно|надо|хочу|закаж\w*|добав\w*)\s*$",
            prefix,
            flags=re.I,
        ):
            continue
        return 2.0, normalize_unit(match.group("unit"))
    return None


def _source_has_spoken_pair_measurement(source_text: str, unit: str) -> bool:
    """Проверяет наличие в источнике той же разговорной единицы количества."""
    normalized_unit = normalize_unit(unit)
    return any(
        normalize_unit(match.group("unit")) == normalized_unit
        for match in _SPOKEN_PAIR_MEASUREMENT_RE.finditer(normalize_text(source_text))
    )


def strip_order_quantity_from_query(query: str, source_text: str) -> str:
    """Удаляет из поискового названия только подтверждённый заказной хвост."""
    value = clean_text(query)
    if not value:
        return ""
    spoken_pair_prefix = _SPOKEN_PAIR_MEASUREMENT_PREFIX_RE.match(value)
    if spoken_pair_prefix is not None:
        pair_unit = normalize_unit(spoken_pair_prefix.group("unit"))
        source_has_same_pair = _source_has_spoken_pair_measurement(source_text, pair_unit)
        query_tail = value[spoken_pair_prefix.end() :].strip(" .,;:-—–")
        if source_has_same_pair and re.search(r"[a-zа-яё]", normalize_text(query_tail), flags=re.I):
            return query_tail
    spoken_pair_suffix = _SPOKEN_PAIR_MEASUREMENT_SUFFIX_RE.search(value)
    if spoken_pair_suffix is not None:
        pair_unit = normalize_unit(spoken_pair_suffix.group("unit"))
        query_head = value[: spoken_pair_suffix.start()].strip(" .,;:-—–")
        if _source_has_spoken_pair_measurement(source_text, pair_unit) and re.search(
            r"[a-zа-яё]", normalize_text(query_head), flags=re.I
        ):
            return query_head
    normalized_value = normalize_text(value)
    source_has_global_scope = has_global_quantity_scope(source_text)
    for fact in extract_semantic_facts(source_text):
        if fact.kind is not SemanticFactKind.ORDER_QUANTITY and not (
            source_has_global_scope and fact.kind is SemanticFactKind.MEASUREMENT
        ):
            continue
        if (
            fact.kind is SemanticFactKind.ORDER_QUANTITY
            and not re.search(r"\s", fact.original_text)
            and not re.search(
                r"\b(?:\u043d\u0443\u0436\u043d\u043e|\u043d\u0430\u0434\u043e)\b",
                source_text,
                re.I,
            )
        ):
            continue
        term = normalize_text(fact.original_text)
        term_match = re.search(
            rf"(?<![\w.,]){re.escape(term)}(?![\w.])",
            normalized_value,
        )
        if term_match is None:
            continue
        position = term_match.start()
        prefix = normalized_value[:position]
        marker = re.search(
            r"(?:\b(?:\u0432\u0441\u0435\u0433\u043e|\u0438\u0442\u043e\u0433\u043e)\b\s+)?"
            r"(?:\b(?:\u043d\u0443\u0436\u043d\u043e|\u043d\u0430\u0434\u043e|\u0437\u0430\u043a\u0430\u0436\w*|\u0434\u043e\u0431\u0430\u0432\w*)\b\s*)$",
            prefix,
            flags=re.I,
        )
        cut = marker.start() if marker is not None else position
        return value[:cut].strip(" .,;:-—–")
    marker = re.search(
        r"(?:\b(?:\u0432\u0441\u0435\u0433\u043e|\u0438\u0442\u043e\u0433\u043e)\b\s+)?"
        r"\b(?:\u043d\u0443\u0436\u043d\u043e|\u043d\u0430\u0434\u043e|\u0437\u0430\u043a\u0430\u0436\w*|\u0434\u043e\u0431\u0430\u0432\w*)\b.*$",
        normalized_value,
        flags=re.I,
    )
    if marker is not None:
        return value[: marker.start()].strip(" .,;:-—–")
    return value


def catalog_packaging_span(source_text: str) -> tuple[int, int] | None:
    """Возвращает границы явной фасовочной связи, если она есть."""
    match = _PACKAGING_RE.search(str(source_text or ""))
    return match.span() if match is not None else None


def is_catalog_measurement(source_text: str, start: int, end: int) -> bool:
    """Проверяет, относится ли измерение к фасовке или диапазону каталога."""
    facts = extract_semantic_facts(source_text)
    return any(
        fact.kind is SemanticFactKind.CATALOG_ATTRIBUTE and fact.start <= start and end <= fact.end
        for fact in facts
    )


def is_catalog_tail_text(fragment: str, source_text: str) -> bool:
    """Проверяет хвост после явного обозначения фасовки товара."""
    value = normalize_text(fragment).strip(" .,;:-—–")
    source = normalize_text(source_text)
    start = source.find(value)
    if not value or start < 0:
        return False
    facts = extract_semantic_facts(source_text)
    fragment_end = start + len(value)
    first_order_start = min(
        (
            fact.start
            for fact in facts
            if fact.kind is SemanticFactKind.ORDER_QUANTITY and fact.start >= start
        ),
        default=len(source),
    )
    if any(
        fact.provenance == "source.packaging_relation" and fact.start <= start < fact.end
        for fact in facts
    ):
        return fragment_end <= first_order_start
    packaging_ends = [
        fact.end
        for fact in facts
        if fact.kind is SemanticFactKind.CATALOG_ATTRIBUTE
        and fact.provenance in {"source.packaging_relation", "source.packaging_word"}
        and fact.end <= start
    ]
    if not packaging_ends:
        return False
    return fragment_end <= first_order_start and "," in source[max(packaging_ends) : start]


def catalog_attribute_text(source_text: str) -> str:
    """Собирает канонический текст фасовочных фактов для карточки товара."""
    source = str(source_text or "")
    span = catalog_packaging_span(source)
    if span is not None:
        relation = clean_text(source[span[0] : span[1]])
        if "/" in relation or "*" in relation or "×" in relation:
            return relation
        measurements = [
            fact.original_text
            for fact in extract_semantic_facts(source)
            if fact.provenance in {"source.measurement", "source.packaging_measurement"}
            and span[0] <= fact.start
            and fact.end <= span[1]
        ]
        if measurements:
            return clean_text(" ".join(measurements))
        return relation
    return ""
