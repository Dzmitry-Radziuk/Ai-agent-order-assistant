"""Определяет каноническое представление и доказательства товара."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from restaurant_bot.domain.models import CatalogProduct
from restaurant_bot.domain.units import UNIT_ALIASES, normalize_unit
from restaurant_bot.services.text import NUMBER_WORDS, parse_number_words
from restaurant_bot.text_normalization import clean_text, normalize_text

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
            _token_matches(query_ordered[0], product_token) for product_token in product_ordered
        )
    ):
        # Одно неподтверждённое слово в хвосте может быть неизвестным вариантом.
        # Базового товара в начале достаточно для кандидата уточнения; общий
        # признак в середине или конце сам по себе недостаточен.  # noqa: RUF003
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
