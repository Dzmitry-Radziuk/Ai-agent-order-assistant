"""Проверяет безопасность выбора товара и совпадение характеристик."""

from __future__ import annotations

import re

from restaurant_bot.catalog.evidence import (
    _canonical_token,
    _catalog_abbreviation_match,
    _token_matches,
    numeric_evidence,
    query_evidence_tokens,
    tokens,
)
from restaurant_bot.domain.models import Candidate
from restaurant_bot.domain.text import normalize_text
from restaurant_bot.domain.units import UNIT_ALIASES, normalize_unit
from restaurant_bot.parsing.number_words import NUMBER_WORDS

_QUALIFIER_IGNORED_WORDS = set(UNIT_ALIASES) | {
    "и",
    "или",
    "либо",
    "а",
    "также",
    "процент",
    "процента",
    "процентов",
    "процентный",
    "процентная",
    "процентное",
    "процентные",
}

_CONTRADICTORY_QUALIFIER_GROUPS = (
    (
        "свеж",
        "копчен",
        "заморож",
        "охлажд",
        "сушен",
        "вялен",
        "солен",
        "маринован",
        "варен",
        "отвар",
        "жарен",
        "обжар",
        "запеч",
        "грил",
        "пропар",
    ),
    ("передн", "задн"),
    ("верхн", "нижн"),
    ("лев", "прав"),
    ("филе", "тушк", "фарш", "кусоч"),
    ("кругл", "квадрат", "длиннозерн", "пропар"),
)

_PRODUCT_VARIANT_QUALIFIER_ROOTS = {
    root for group in _CONTRADICTORY_QUALIFIER_GROUPS for root in group
} | {
    "мрамор",
    "бескост",
    "безкост",
    "мякот",
    "размер",
    "фасов",
    "упаков",
}

_HIGH_RISK_UNSCOPED_VARIANT_ROOTS = {
    "обжар",
    "жарен",
    "копчен",
    "заморож",
    "запеч",
    "грил",
    "квадрат",
    "кругл",
    "длиннозерн",
    "мрамор",
    "зачищ",
    "пропар",
    "передн",
    "задн",
    "верхн",
    "нижн",
    "филе",
    "тушк",
    "фарш",
    "кусоч",
    "бескост",
    "безкост",
}

_OPERATIONAL_COMMENT_ROOTS = (
    "привез",
    "достав",
    "позвон",
    "упаков",
    "фасов",
    "полож",
    "нарез",
    "порез",
    "зачищ",
    "срез",
    "обрез",
    "подрез",
    "очист",
    "раздел",
    "размораж",
    "смеш",
    "замен",
)

# Эти формы могут быть самостоятельным запросом к каталогу («филе», «фарш»),
# в отличие от характеристики качества или обработки вроде «свежий».
# Автоматическая подстановка по ним всё равно запрещена: каталоговый resolver
# решает, показать варианты или выбрать точный товар.
_STANDALONE_PRODUCT_FORM_ROOTS = {
    "филе",
    "тушк",
    "фарш",
    "кусоч",
    "мякот",
}


def _qualifier_tokens(value: str) -> list[str]:
    """Возвращает значимые слова запроса для проверки свойств товара."""
    return [
        token
        for token in re.findall(r"[a-zа-я0-9]+", normalize_text(value), flags=re.I)
        if len(token) > 1
        and token not in _QUALIFIER_IGNORED_WORDS
        and not token.isdigit()
        and not any(char.isdigit() for char in token)
    ]


def catalog_matching_comment_context(value: str) -> str:
    """Оставляет для каталога только комментарий со свойством товара."""
    clauses = [clause.strip() for clause in re.split(r"\s*[;,]\s*", value) if clause.strip()]
    retained_clauses = [
        clause.strip() for clause in clauses if not _contains_operational_instruction(clause)
    ]
    if len(retained_clauses) == len(clauses):
        return value.strip()
    return "; ".join(retained_clauses)


def _contains_operational_instruction(value: str) -> bool:
    """Определяет просьбу к поставщику, а не свойство каталожного товара."""
    return any(
        token.startswith(root)
        for token in _qualifier_tokens(normalize_text(value))
        for root in _OPERATIONAL_COMMENT_ROOTS
    )


def _qualifier_root(token: str) -> str:
    """Сворачивает русскую форму атрибута к корню группы вариантов."""
    normalized = normalize_text(token)
    for group in _CONTRADICTORY_QUALIFIER_GROUPS:
        for root in group:
            if normalized.startswith(root):
                return root
    return ""


def has_conflicting_catalog_qualifiers(query: str, product_name: str) -> bool:
    """Проверяет явное противоречие свойств запроса и строки каталога.

    Неизвестные слова не считаются конфликтом сами по себе: они могут быть
    произвольным пожеланием поставщику. Но если пользователь явно назвал
    взаимоисключающий атрибут, например «копчёные», а каталог предлагает
    «свежие», автоматический выбор запрещается.
    """
    query_qualifiers = _qualifier_tokens(query)
    product_qualifiers = _qualifier_tokens(product_name)
    query_roots = {_qualifier_root(token) for token in query_qualifiers} - {""}
    product_roots = {_qualifier_root(token) for token in product_qualifiers} - {""}
    for group in _CONTRADICTORY_QUALIFIER_GROUPS:
        query_group = query_roots.intersection(group)
        product_group = product_roots.intersection(group)
        if query_group and product_group and query_group.isdisjoint(product_group):
            return True
    return False


def has_product_variant_qualifier(value: str) -> bool:
    """Определяет признак, который нельзя молча считать комментарием.

    Такой признак должен либо совпасть с названием каталога, либо привести к
    уточнению. Это предотвращает выбор базового товара вместо нужного варианта
    для произвольных названий и не требует перечислять весь каталог.
    """
    return any(
        token.startswith(root)
        for token in _qualifier_tokens(value)
        for root in _PRODUCT_VARIANT_QUALIFIER_ROOTS
    )


def is_standalone_product_form_query(value: str) -> bool:
    """Определяет самостоятельный запрос формы товара для поиска в каталоге."""
    query_tokens = tokens(value)
    return len(query_tokens) == 1 and any(
        token.startswith(root) for token in query_tokens for root in _STANDALONE_PRODUCT_FORM_ROOTS
    )


def has_unscoped_product_variant_qualifier(value: str) -> bool:
    """Проверяет риск подмены, когда AI ошибочно назвал признак комментарием."""
    normalized = normalize_text(value)
    if not normalized or re.search(
        r"(?:^|\s)(?:только|обязательно|желательно|нужн\w*|просьб\w*|"
        r"пожалуйста|без|не|на\s+завтра|достав\w*|привез\w*)\b",
        normalized,
    ):
        return False
    return any(
        token.startswith(root)
        for token in _qualifier_tokens(normalized)
        for root in _HIGH_RISK_UNSCOPED_VARIANT_ROOTS
    )


def _identity_token_matches(query_token: str, product_token: str) -> bool:
    """Сопоставляет только безопасные грамматические формы одного слова."""
    query_token = _canonical_token(query_token)
    product_token = _canonical_token(product_token)
    if query_token == product_token:
        return True
    if _catalog_abbreviation_match(query_token, product_token):
        return True

    short, long = sorted((query_token, product_token), key=len)
    if len(short) >= 3 and long.startswith(short) and len(long) - len(short) <= 3:
        return True

    common_prefix = 0
    for left_char, right_char in zip(query_token, product_token, strict=False):
        if left_char != right_char:
            break
        common_prefix += 1
    return (
        min(len(query_token), len(product_token)) >= 4
        and abs(len(query_token) - len(product_token)) <= 1
        and common_prefix >= min(len(query_token), len(product_token)) - 1
    )


def is_safe_catalog_name_equivalent(query: str, product_name: str) -> bool:
    """Проверяет, что запрос и каталог называют один товар, а не похожую категорию."""
    if not has_compatible_numeric_characteristics(query, product_name):
        return False
    query_tokens = {
        token
        for token in tokens(query)
        if not any(char.isdigit() for char in token) and token not in UNIT_ALIASES
    }
    product_tokens = {
        token
        for token in tokens(product_name)
        if not any(char.isdigit() for char in token) and token not in UNIT_ALIASES
    }
    product_tokens.update(
        token
        for token in re.findall(r"[a-z]", normalize_text(product_name), flags=re.I)
        if token.isascii()
    )
    query_tokens = {_canonical_token(token) for token in query_tokens if token not in NUMBER_WORDS}
    product_tokens = {
        _canonical_token(token) for token in product_tokens if token not in NUMBER_WORDS
    }
    if len(query_tokens) < 2 or not product_tokens:
        return False
    return all(
        any(_identity_token_matches(query_token, product_token) for product_token in product_tokens)
        for query_token in query_tokens
    )


def _numeric_characteristics(value: str) -> list[tuple[float, float | None, str]]:
    """Извлекает размеры, диапазоны и фасовку из названия товара."""
    return [
        (entry.value, entry.upper_value, _normalize_characteristic_unit(entry.unit))
        for entry in numeric_evidence(value)
    ]


def _normalize_characteristic_unit(value: str) -> str:
    """Нормализует единицу характеристики без зависимости от каталога."""
    return normalize_unit(value)


def _same_numeric_characteristic(
    left: tuple[float, float | None, str],
    right: tuple[float, float | None, str],
) -> bool:
    """Сравнивает одиночный размер или диапазон товара."""
    return (
        abs(left[0] - right[0]) <= 1e-9
        and (
            (left[1] is None and right[1] is None)
            or (left[1] is not None and right[1] is not None and abs(left[1] - right[1]) <= 1e-9)
        )
        and (not left[2] or left[2] == right[2])
    )


def has_compatible_numeric_characteristics(query: str, product_name: str) -> bool:
    """Проверяет наличие в каталоге всех явно названных размеров и фасовок."""
    query_characteristics = _numeric_characteristics(query)
    product_characteristics = _numeric_characteristics(product_name)
    return not query_characteristics or all(
        any(
            _same_numeric_characteristic(query_value, product_value)
            for product_value in product_characteristics
        )
        for query_value in query_characteristics
    )


def can_auto_select(candidates: list[Candidate]) -> bool:
    """Проверяет безопасность автоматического выбора товара."""
    if not candidates:
        return False
    top = candidates[0]
    second_score = candidates[1].score if len(candidates) > 1 else 0
    gap = top.score - second_score
    # Даже точная фраза небезопасна, если две строки каталога практически
    # равны (например, один сироп у двух поставщиков). В этом случае выбор  # noqa: RUF003
    # остаётся пользователю, а первая строка не подставляется молча.  # noqa: RUF003
    return (
        (top.score >= 82 and gap >= 5)
        or (top.score >= 67 and gap >= 14)
        or (top.score >= 58 and gap >= 25)
    )


def _is_known_variant_qualifier(token: str) -> bool:
    """Проверяет, что слово описывает вариант, а не идентичность товара."""
    return any(token.startswith(root) for root in _PRODUCT_VARIANT_QUALIFIER_ROOTS)


def _has_unmatched_concrete_identity(
    query: str,
    query_tokens: set[str],
    candidates: list[Candidate],
) -> bool:
    """Находит названный товарный признак, которого нет ни у одного кандидата."""
    supported_tokens = set().union(
        *(query_evidence_tokens(query, candidate.name) for candidate in candidates)
    )
    return any(
        token not in supported_tokens
        and token not in _QUALIFIER_IGNORED_WORDS
        and not _is_known_variant_qualifier(token)
        for token in query_tokens
    )


def is_broad_category_query(query: str, candidates: list[Candidate]) -> bool:
    """Определяет общий запрос без неподтверждённой идентичности товара."""
    query_tokens = tokens(query)
    if not query_tokens or not candidates:
        return False

    def name_key(value: str) -> str:
        """Возвращает ключ полного названия без знаков оформления."""
        return re.sub(r"[^a-zа-я0-9]+", "", normalize_text(value))

    query_key = name_key(query)
    exact_matches = [candidate for candidate in candidates if name_key(candidate.name) == query_key]
    if exact_matches:
        # Одно полное совпадение безопасно. Одинаковые строки от нескольких
        # поставщиков всё равно требуют явного выбора пользователя.
        return len(exact_matches) > 1

    # Общая форма товара (например, «филе») может требовать выбора варианта.
    # Но «филе лосося» уже содержит конкретную идентичность. Если ни один  # noqa: RUF003
    # кандидат её не подтверждает, нельзя показывать форель, судака или щуку
    # как равноправные варианты: решение должно пройти через семантическую
    # проверку и при необходимости стать not_found.
    #
    # Неподтверждённый известный qualifier («говядина мраморная») остаётся
    # безопасным общим запросом: он описывает вариант базового товара, а не  # noqa: RUF003
    # отдельную товарную идентичность.
    if _has_unmatched_concrete_identity(query, query_tokens, candidates):
        return False

    if len(query_tokens) == 1:
        category = next(iter(query_tokens))
        for candidate in candidates:
            candidate_tokens = tokens(candidate.name)
            if len(candidate_tokens) <= len(query_tokens):
                continue
            if any(_token_matches(category, token) for token in candidate_tokens):
                return True
        return False

    # Голосовая модель может добавить признак вроде «мраморная», хотя пользователь
    # сказал только «говядина». Если первые кандидаты подтверждают один и тот же
    # набор слов, неподтверждённый обоими признак не даёт права выбирать строку.
    if len(candidates) < 2:
        return False

    def evidence(candidate: Candidate) -> set[str]:
        """Возвращает подтверждённые совпадения кандидата."""
        return query_evidence_tokens(query, candidate.name)

    complete_candidates = [
        candidate for candidate in candidates if evidence(candidate) == query_tokens
    ]
    if len(complete_candidates) >= 2:
        # Запрос из нескольких слов тоже может быть лишь названием линейки:
        # «Кордиал ЛЬЮ» полностью входит во множество разных вкусов. Ни
        # разница в баллах, ни ИИ не должны выбирать первый товар вместо
        # пользователя.
        return True

    first_evidence = evidence(candidates[0])
    second_evidence = evidence(candidates[1])
    return (
        bool(first_evidence)
        and first_evidence == second_evidence
        and len(first_evidence) < len(query_tokens)
    )
