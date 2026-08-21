"""Определяет каноническое представление и доказательства товара."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from restaurant_bot.domain.models import CatalogProduct
from restaurant_bot.domain.text import clean_text, normalize_text
from restaurant_bot.domain.units import UNIT_ALIASES, normalize_unit
from restaurant_bot.parsing.number_words import NUMBER_WORDS, parse_number_words


@dataclass(frozen=True, slots=True)
class NumericEvidence:
    """Описывает одно числовое свидетельство в исходной фразе."""

    value: float
    upper_value: float | None = None
    unit: str = ""
    raw: str = ""
    start: int = 0
    end: int = 0


_FRACTION_DENOMINATORS = {
    "одного": 1,
    "одной": 1,
    "одному": 1,
    "одну": 1,
    "один": 1,
    "одна": 1,
    "вторая": 2,
    "вторые": 2,
    "вторых": 2,
    "две": 2,
    "третья": 3,
    "третьи": 3,
    "третьих": 3,
    "три": 3,
    "четвертая": 4,
    "четвертые": 4,
    "четвертых": 4,
    "четыре": 4,
    "пятая": 5,
    "пятые": 5,
    "пятых": 5,
    "пять": 5,
    "шестая": 6,
    "шестые": 6,
    "шестых": 6,
    "шести": 6,
    "шесть": 6,
    "седьмая": 7,
    "седьмые": 7,
    "седьмых": 7,
    "семи": 7,
    "семь": 7,
    "восьмая": 8,
    "восьмые": 8,
    "восьмых": 8,
    "восьми": 8,
    "восемь": 8,
    "девятая": 9,
    "девятые": 9,
    "девятых": 9,
    "девяти": 9,
    "девять": 9,
    "десятая": 10,
    "десятые": 10,
    "десятых": 10,
    "десяти": 10,
    "десять": 10,
    "двенадцатая": 12,
    "двенадцатые": 12,
    "двенадцатых": 12,
    "двенадцати": 12,
    "двенадцать": 12,
}

_STOP_WORDS = {
    "и",
    "в",
    "на",
    "для",
    "из",
    "с",
    "по",
    "шт",
    "кг",
    "г",
    "л",
    "мл",
    "уп",
    "кор",
    "бан",
    "бут",
    "пач",
    "свежий",
    "свежая",
    "свежее",
}

_CYRILLIC_TO_LATIN = str.maketrans(
    {
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "g",
        "д": "d",
        "е": "e",
        "ж": "zh",
        "з": "z",
        "и": "i",
        "й": "i",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",
        "х": "h",
        "ц": "c",
        "ч": "ch",
        "ш": "sh",
        "щ": "shch",
        "ъ": "",
        "ы": "y",
        "ь": "",
        "э": "e",
        "ю": "yu",
        "я": "ya",
    }
)


def _compact_voice_name(value: str) -> str:
    """Уплотняет голосовое название товара для поиска."""
    compact = re.sub(r"[^a-zа-я0-9]+", "", normalize_text(value))
    # При слиянии слов голосовым распознаванием «Сироп Роза, 1л»
    # сравнивается как «сиропроза». Формат фасовки не является частью названия.
    return re.sub(r"\d+(?:[a-zа-я]+)?$", "", compact)


def _canonical_token(value: str) -> str:
    """Сводит кириллические и латинские варианты одного слова к общей форме."""
    return normalize_text(value).translate(_CYRILLIC_TO_LATIN)


def _catalog_abbreviation_match(left: str, right: str) -> bool:
    """Распознаёт короткое каталожное сокращение длинного слова."""
    short, long = sorted(
        (_canonical_token(left), _canonical_token(right)),
        key=len,
    )
    return (
        3 <= len(short) <= 4
        and len(long) >= 7
        and long.startswith(short)
        and len(long) - len(short) >= 3
    )


def _spoken_range_pattern() -> re.Pattern[str]:
    """Создаёт ограниченный шаблон словесного диапазона с единицей."""
    number_word = "|".join(
        sorted((re.escape(word) for word in NUMBER_WORDS), key=len, reverse=True)
    )
    phrase = rf"(?:{number_word})(?:\s+(?:{number_word}))*"
    unit_pattern = "|".join(
        sorted((re.escape(unit) for unit in UNIT_ALIASES), key=len, reverse=True)
    )
    return re.compile(
        rf"(?P<left>{phrase})\s*[-–—]\s*(?P<right>{phrase})"
        rf"(?:\s*(?P<unit>{unit_pattern}))?\b",
        flags=re.IGNORECASE,
    )


def canonical_search_query(value: str) -> str:
    """Возвращает временное каноническое представление поискового запроса."""
    normalized = normalize_text(value).replace(",", ".")
    normalized = re.sub(
        r"(?P<left>\d+(?:\.\d+)?)\s+на\s+(?P<right>\d+(?:\.\d+)?)",
        r"\g<left>/\g<right>",
        normalized,
        flags=re.IGNORECASE,
    )
    pattern = _spoken_range_pattern()
    replacements: list[tuple[int, int, str]] = []
    for match in pattern.finditer(normalized):
        left = parse_number_words(match.group("left").split(), 0)
        right = parse_number_words(match.group("right").split(), 0)
        if left is None or right is None:
            continue
        unit = normalize_unit(match.group("unit") or "")
        suffix = f" {unit}" if unit else ""
        replacements.append((match.start(), match.end(), f"{left[0]:g}-{right[0]:g}{suffix}"))
    for start, end, replacement in reversed(replacements):
        normalized = f"{normalized[:start]}{replacement}{normalized[end:]}"
    return normalized


def _numeric_unit_pattern() -> str:
    """Строит шаблон единиц для числового свидетельства."""
    return "|".join(sorted((re.escape(unit) for unit in UNIT_ALIASES), key=len, reverse=True))


def _spoken_number_phrase_pattern() -> str:
    """Строит шаблон словесного числа без привязки к конкретному товару."""
    words = set(NUMBER_WORDS) | set(_FRACTION_DENOMINATORS)
    number_word = "|".join(sorted((re.escape(word) for word in words), key=len, reverse=True))
    return rf"(?:{number_word})(?:\s+(?:{number_word}))*"


def _fraction_value(left: str, right: str) -> tuple[float, float] | None:
    """Возвращает числитель и знаменатель словесной дроби."""
    numerator = NUMBER_WORDS.get(normalize_text(left))
    denominator = _FRACTION_DENOMINATORS.get(normalize_text(right))
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator, float(denominator)


def _spoken_scalar(value: str) -> float | None:
    """Преобразует обычное числительное с учётом падежной формы."""
    tokens = normalize_text(value).split()
    parsed = parse_number_words(tokens, 0)
    if parsed is not None and parsed[1] == len(tokens):
        return parsed[0]
    if len(tokens) == 1:
        return _FRACTION_DENOMINATORS.get(tokens[0])
    return None


def _spoken_numeric_pairs(value: str) -> list[NumericEvidence]:
    """\u0420\u0430\u0441\u043f\u043e\u0437\u043d\u0430\u0451\u0442 \u0441\u043b\u043e\u0432\u0435\u0441\u043d\u044b\u0435 \u0434\u0440\u043e\u0431\u0438 \u0438 \u0434\u0438\u0430\u043f\u0430\u0437\u043e\u043d\u044b."""
    number_phrase = _spoken_number_phrase_pattern()
    denominator_phrase = "|".join(
        sorted(
            (re.escape(word) for word in _FRACTION_DENOMINATORS if word not in NUMBER_WORDS),
            key=len,
            reverse=True,
        )
    )
    result: list[NumericEvidence] = []
    occupied: list[tuple[int, int]] = []

    fraction_pattern = re.compile(
        rf"(?<!\w)(?P<numerator>{number_phrase})\s+(?P<denominator>{denominator_phrase})\b",
        flags=re.IGNORECASE,
    )
    for match in fraction_pattern.finditer(value):
        fraction = _fraction_value(match.group("numerator"), match.group("denominator"))
        if fraction is None:
            continue
        occupied.append(match.span())
        result.append(
            NumericEvidence(
                value=fraction[0],
                upper_value=fraction[1],
                raw=match.group(0),
                start=match.start(),
                end=match.end(),
            )
        )

    ratio_pattern = re.compile(
        rf"(?<!\w)(?P<numerator>{number_phrase})\s+(?:\u043a|\u043d\u0430)\s+"
        rf"(?P<denominator>{number_phrase})\b",
        flags=re.IGNORECASE,
    )
    for match in ratio_pattern.finditer(value):
        if any(start < match.end() and match.start() < end for start, end in occupied):
            continue
        numerator = _spoken_scalar(match.group("numerator"))
        denominator = _spoken_scalar(match.group("denominator"))
        if numerator is None or denominator is None or denominator == 0:
            continue
        occupied.append(match.span())
        result.append(
            NumericEvidence(
                value=numerator,
                upper_value=denominator,
                raw=match.group(0),
                start=match.start(),
                end=match.end(),
            )
        )

    range_pattern = re.compile(
        rf"(?<!\w)(?P<left>{number_phrase})\s*(?:\u043d\u0430|\u0434\u043e|[-])\s*"
        rf"(?P<right>{number_phrase})(?:\s*(?P<unit>{_numeric_unit_pattern()}))?\b",
        flags=re.IGNORECASE,
    )
    for match in range_pattern.finditer(value):
        if any(start < match.end() and match.start() < end for start, end in occupied):
            continue
        left = _spoken_scalar(match.group("left"))
        right = _spoken_scalar(match.group("right"))
        if left is None or right is None:
            continue
        occupied.append(match.span())
        result.append(
            NumericEvidence(
                value=left,
                upper_value=right,
                unit=normalize_unit(match.group("unit") or ""),
                raw=match.group(0),
                start=match.start(),
                end=match.end(),
            )
        )
    return result


def numeric_evidence(value: str) -> list[NumericEvidence]:
    """Возвращает нормализованные числовые свидетельства с исходными позициями."""
    normalized = normalize_text(str(value or "").replace("\u2013", "-").replace("\u2014", "-"))
    if not normalized:
        return []
    unit_pattern = _numeric_unit_pattern()
    result: list[NumericEvidence] = []
    occupied: list[tuple[int, int]] = []
    range_pattern = re.compile(
        rf"(?<!\w)(?P<left>\d+(?:[.,]\d+)?)\s*(?:--|[-/]|на|x|х)\s*"
        rf"(?P<right>\d+(?:[.,]\d+)?)(?:\s*(?P<unit>{unit_pattern}))?\b",
        flags=re.IGNORECASE,
    )
    for match in range_pattern.finditer(normalized):
        occupied.append(match.span())
        result.append(
            NumericEvidence(
                value=float(match.group("left").replace(",", ".")),
                upper_value=float(match.group("right").replace(",", ".")),
                unit=normalize_unit(match.group("unit") or ""),
                raw=match.group(0),
                start=match.start(),
                end=match.end(),
            )
        )

    compact_pattern = re.compile(
        rf"(?<!\w)(?P<value>\d+(?:[.,]\d+)?)(?:\s*)(?P<unit>{unit_pattern})\b",
        flags=re.IGNORECASE,
    )
    for match in compact_pattern.finditer(normalized):
        if any(start < match.end() and match.start() < end for start, end in occupied):
            continue
        occupied.append(match.span())
        result.append(
            NumericEvidence(
                value=float(match.group("value").replace(",", ".")),
                unit=normalize_unit(match.group("unit")),
                raw=match.group(0),
                start=match.start(),
                end=match.end(),
            )
        )

    spoken_pairs = _spoken_numeric_pairs(normalized)
    for evidence in spoken_pairs:
        if any(start < evidence.end and evidence.start < end for start, end in occupied):
            continue
        occupied.append((evidence.start, evidence.end))
        result.append(evidence)

    masked = list(normalized)
    for start, end in occupied:
        masked[start:end] = [" "] * (end - start)
    token_matches = list(re.finditer(r"[a-zа-яё0-9%]+", "".join(masked), flags=re.I))
    token_values = [normalize_text(match.group(0)) for match in token_matches]
    for index, token in enumerate(token_values):
        parsed = parse_number_words(token_values, index)
        if parsed is None:
            continue
        quantity, end_index = parsed
        raw_unit = token_values[end_index] if end_index < len(token_values) else ""
        if raw_unit not in UNIT_ALIASES and raw_unit != "%":
            if not re.fullmatch(r"\d+(?:\.\d+)?", token):
                continue
            raw_unit = ""
            span_end_index = end_index
        else:
            span_end_index = end_index
        start = token_matches[index].start()
        end = token_matches[span_end_index - 1].end()
        if any(start < existing.end and existing.start < end for existing in result):
            continue
        result.append(
            NumericEvidence(
                value=quantity,
                unit=normalize_unit(raw_unit) if raw_unit != "%" else "%",
                raw=normalized[start:end],
                start=start,
                end=end,
            )
        )
    return sorted(result, key=lambda evidence: (evidence.start, evidence.end))


def remove_phrase_overlap(source_text: str, phrase: str) -> str:
    """Убирает подтверждённую фразу из временного поискового запроса."""
    source = clean_text(source_text)
    phrase_tokens = [
        normalize_text(token)
        for token in re.findall(r"[a-zа-яё0-9%]+", normalize_text(phrase), flags=re.I)
    ]
    source_matches = list(re.finditer(r"[a-zа-яё0-9%]+", source, flags=re.I))
    source_tokens = [normalize_text(match.group()) for match in source_matches]
    if not source_tokens or not phrase_tokens or len(phrase_tokens) > len(source_tokens):
        return source
    for start in range(len(source_tokens) - len(phrase_tokens) + 1):
        if source_tokens[start : start + len(phrase_tokens)] != phrase_tokens:
            continue
        left = source[: source_matches[start].start()].strip()
        right = source[source_matches[start + len(phrase_tokens) - 1].end() :].strip()
        replacement = clean_text(f"{left} {right}")
        return replacement or source
    return source


def tokens(value: str) -> set[str]:
    """Возвращает нормализованные слова для поиска."""
    return {
        token
        for token in re.findall(r"[a-zа-я0-9]+", normalize_text(value))
        if len(token) > 1 and token not in _STOP_WORDS
    }


def _product_identity_tokens(value: str) -> set[str]:
    """Возвращает слова товара без фасовки и числовых признаков.

    Числа и единицы измерения проверяются отдельными правилами. Иначе запрос
    «Сироп Роза, 1л» совпадает с любым товаром, где встречается «1л».
    """
    return {
        token
        for token in tokens(value)
        if token not in UNIT_ALIASES and not any(char.isdigit() for char in token)
    }


def has_sufficient_photo_identity(query: str, product_name: str) -> bool:
    """Проверяет совместимость прочитанного с фото названия со строкой каталога."""
    observed = _product_identity_tokens(query)
    if not observed:
        return False
    matched = observed & query_evidence_tokens(query, product_name)
    if len(observed) == 1:
        return matched == observed
    required = max(2, (len(observed) * 3 + 4) // 5)
    return len(matched) >= required


def _product_identity_tokens_in_order(value: str) -> list[str]:
    """Возвращает значимые слова товара в исходном порядке."""
    return [
        token
        for token in re.findall(r"[a-zа-яё0-9]+", normalize_text(value), flags=re.I)
        if len(token) > 1
        and token not in _STOP_WORDS
        and token not in UNIT_ALIASES
        and not any(char.isdigit() for char in token)
    ]


def _token_matches(query_token: str, product_token: str) -> bool:
    """Проверяет достаточность совпадения поискового слова."""
    query_token = _canonical_token(query_token)
    product_token = _canonical_token(product_token)
    if query_token == product_token:
        return True
    if _catalog_abbreviation_match(query_token, product_token):
        return True
    short, long = sorted((query_token, product_token), key=len)
    if len(short) >= 4 and short in long and len(short) / len(long) >= 0.6:
        return True
    if len(query_token) < 3 or len(product_token) < 3:
        return False
    if query_token[0] != product_token[0]:
        return False
    similarity = SequenceMatcher(None, query_token, product_token).ratio()
    threshold = 0.8 if min(len(query_token), len(product_token)) == 3 else 0.72
    return similarity >= threshold


def _has_strong_token_match(query_token: str, product_token: str) -> bool:
    """Проверяет точное или морфологически расширенное совпадение слова."""
    query_token = _canonical_token(query_token)
    product_token = _canonical_token(product_token)
    if query_token == product_token:
        return True
    short = min(query_token, product_token, key=len)
    common_prefix = 0
    for left, right in zip(query_token, product_token, strict=False):
        if left != right:
            break
        common_prefix += 1
    return len(short) >= 4 and common_prefix >= 4 and common_prefix / len(short) >= 0.6


def _compound_query_matches(
    query_tokens_in_order: list[str],
    product_tokens_in_order: list[str],
) -> set[str]:
    """Находит слова запроса, объединённые в одно слово в названии каталога."""
    canonical_query = [_canonical_token(token) for token in query_tokens_in_order]
    canonical_product = [_canonical_token(token) for token in product_tokens_in_order]
    matched: set[str] = set()
    for start in range(len(canonical_query)):
        for end in range(start + 2, len(canonical_query) + 1):
            joined = "".join(canonical_query[start:end])
            if any(
                (joined == product_token or joined in product_token or product_token in joined)
                and min(len(joined), len(product_token)) / max(len(joined), len(product_token))
                >= 0.8
                and _token_matches(joined, product_token)
                for product_token in canonical_product
            ):
                matched.update(query_tokens_in_order[start:end])
    return matched


def has_catalog_search_evidence(query: str, product: CatalogProduct) -> bool:
    """Проверяет наличие достаточных оснований для показа кандидата."""
    normalized_query = normalize_text(query)
    normalized_name = normalize_text(product.name)
    if normalized_query and normalized_query == normalized_name:
        return True

    query_tokens = _product_identity_tokens(query)
    product_tokens = _product_identity_tokens(product.name)
    if not query_tokens or not product_tokens:
        return False

    query_ordered = _product_identity_tokens_in_order(query)
    product_ordered = _product_identity_tokens_in_order(product.name)
    matched_pairs = [
        (query_token, product_token)
        for query_token in query_ordered
        for product_token in product_ordered
        if _token_matches(query_token, product_token)
    ]
    matched_query_tokens = {query_token for query_token, _ in matched_pairs}
    matched_query_tokens.update(_compound_query_matches(query_ordered, product_ordered))
    if len(query_ordered) == 1 and matched_query_tokens:
        return True
    if len(matched_query_tokens) >= 2:
        return True

    if (
        len(matched_query_tokens) == 1
        and query_ordered
        and query_ordered[0] in matched_query_tokens
        and product_ordered
        and any(
            _has_strong_token_match(query_ordered[0], product_token)
            for product_token in product_ordered
        )
    ):
        # Для неизвестного хвоста достаточно точного базового слова товара.
        # Нечёткое совпадение базового слова не должно создавать кандидата.
        return True

    # Голосовое распознавание Telegram иногда сливает соседние слова:
    # «Сироп Роза» превращается в «Сыропроза». Такой близкий список
    # передаётся на переранжирование ИИ, но не выбирается автоматически.
    compact_query = _compact_voice_name(query)
    compact_name = _compact_voice_name(product.name)
    return (
        " " not in normalize_text(query)
        and " " in normalize_text(product.name)
        and len(compact_query) >= 6
        and len(compact_name) >= 6
        and compact_query[0] == compact_name[0]
        and SequenceMatcher(None, compact_query, compact_name).ratio() >= 0.77
    )


def has_complete_query_evidence(query: str, product_name: str) -> bool:
    """Проверяет совпадение всех значимых слов запроса."""
    query_tokens = tokens(query)
    return bool(query_tokens) and query_evidence_tokens(query, product_name) == query_tokens


def query_evidence_tokens(query: str, product_name: str) -> set[str]:
    """Возвращает слова запроса, подтверждённые названием товара."""
    product_tokens = tokens(product_name)
    return {
        query_token
        for query_token in tokens(query)
        if any(_token_matches(query_token, product_token) for product_token in product_tokens)
    }


def unverified_product_terms(query: str, product_name: str) -> list[str]:
    """Возвращает длинные признаки запроса, которых нет в названии каталога.

    Короткий фрагмент до четырёх символов допускается только как вероятная
    ошибка распознавания речи рядом с похожим словом каталога. Числа и единицы
    проверяются отдельными правилами.
    """
    query_tokens = tokens(query)
    if not query_tokens:
        return []
    normalized_query = normalize_text(query)
    normalized_name = normalize_text(product_name)
    compact_query = _compact_voice_name(normalized_query)
    compact_name = _compact_voice_name(normalized_name)
    if (
        normalized_query
        and " " not in normalized_query
        and len(compact_query) >= 6
        and len(compact_name) >= 6
        and compact_query[0] == compact_name[0]
        and SequenceMatcher(None, compact_query, compact_name).ratio() >= 0.77
    ):
        # ASR может слить многословный товар в один токен, например «сыропроза».
        return []
    evidence = query_evidence_tokens(query, product_name)
    product_tokens = tokens(product_name)
    return [
        token
        for token in re.findall(r"[a-zа-яё0-9]+", normalize_text(query), flags=re.I)
        if token in query_tokens
        and token not in evidence
        and token not in UNIT_ALIASES
        and not token.isdigit()
        and not any(char.isdigit() for char in token)
        and (
            len(token) > 4
            or not any(
                len(product_token) >= 3
                and token[0] == product_token[0]
                and SequenceMatcher(None, token, product_token).ratio() >= 0.7
                for product_token in product_tokens
            )
        )
    ]


def supplier_matches_hint(supplier: str, supplier_hint: str) -> bool:
    """Сопоставляет полное и сокращённое названия одного поставщика."""
    normalized_supplier = normalize_text(supplier)
    normalized_hint = normalize_text(supplier_hint)
    return bool(
        normalized_supplier
        and normalized_hint
        and (normalized_supplier in normalized_hint or normalized_hint in normalized_supplier)
    )
