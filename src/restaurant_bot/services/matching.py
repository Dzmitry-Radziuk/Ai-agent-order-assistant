from __future__ import annotations

import math
import re
from difflib import SequenceMatcher

from restaurant_bot.domain.models import Candidate, CatalogProduct
from restaurant_bot.services.text import (
    UNIT_ALIASES,
    normalize_text,
    normalize_unit,
)

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

# Эти слова не описывают отдельную позицию каталога. Их нужно исключать из
# проверки дополнительных характеристик, иначе единицы измерения и союзы
# будут выглядеть как «лишние» слова запроса.
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

# Attribute groups are independent of the catalog size. Unknown properties are
# sent to semantic review instead of being silently added as comments.
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

# Признаки, которые обычно являются частью наименования или варианта товара.
# Это не словарь каталога: набор защищает неизвестные позиции от подмены,
# когда в каталоге есть только похожая базовая категория.
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


def _compact_voice_name(value: str) -> str:
    """Уплотняет голосовое название товара для поиска."""
    compact = re.sub(r"[^a-zа-я0-9]+", "", normalize_text(value))
    # "Сироп Роза, 1л" must be compared as "сиропроза" when speech
    # recognition merges its words. The order format is not part of the name.
    return re.sub(r"\d+(?:[a-zа-я]+)?$", "", compact)


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


def supplier_matches_hint(supplier: str, supplier_hint: str) -> bool:
    """Сопоставляет полное и сокращённое названия одного поставщика."""
    normalized_supplier = normalize_text(supplier)
    normalized_hint = normalize_text(supplier_hint)
    return bool(
        normalized_supplier
        and normalized_hint
        and (normalized_supplier in normalized_hint or normalized_hint in normalized_supplier)
    )


def _token_matches(query_token: str, product_token: str) -> bool:
    """Проверяет достаточность совпадения поискового слова."""
    if query_token == product_token:
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
        and any(_token_matches(query_ordered[0], product_token) for product_token in product_ordered)
    ):
        # One unmatched trailing word may be an unknown product variant. A
        # leading base product is sufficient for a clarification candidate;
        # a shared adjective in the middle or at the end is not.
        return True

    # Telegram voice recognition sometimes joins adjacent product words:
    # "Сироп Роза" -> "Сыропроза".  n8n sends such a close catalog shortlist
    # to its AI reranker rather than immediately treating it as a different
    # product.  This is deliberately a candidate gate, never an auto-select.
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
        # ASR can merge a multiword product into one token, e.g. «сыропроза».
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


def _identity_token_matches(query_token: str, product_token: str) -> bool:
    """Сопоставляет только безопасные грамматические формы одного слова."""
    if query_token == product_token:
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
    if len(query_tokens) < 2 or len(query_tokens) != len(product_tokens):
        return False
    return all(
        any(_identity_token_matches(query_token, product_token) for product_token in product_tokens)
        for query_token in query_tokens
    ) and all(
        any(_identity_token_matches(query_token, product_token) for query_token in query_tokens)
        for product_token in product_tokens
    )


def _numeric_characteristics(value: str) -> list[tuple[float, float | None, str]]:
    """Извлекает размеры, диапазоны и фасовку из названия товара."""
    normalized = normalize_text(str(value).replace("–", "-").replace("—", "-")).replace(",", ".")
    if not normalized:
        return []
    unit_pattern = "|".join(
        sorted(
            (re.escape(unit) for unit in UNIT_ALIASES),
            key=len,
            reverse=True,
        )
    )
    range_pattern = re.compile(
        rf"(?<!\w)(?P<left>\d+(?:\.\d+)?)\s*(?:--|-)\s*"
        rf"(?P<right>\d+(?:\.\d+)?)(?:\s*(?P<unit>{unit_pattern}))?\b",
        flags=re.IGNORECASE,
    )
    characteristics: list[tuple[float, float | None, str]] = []
    masked = list(normalized)
    for match in range_pattern.finditer(normalized):
        characteristics.append(
            (
                float(match.group("left")),
                float(match.group("right")),
                _normalize_characteristic_unit(match.group("unit") or ""),
            )
        )
        masked[match.start() : match.end()] = [" "] * (match.end() - match.start())

    single_pattern = re.compile(
        rf"(?<![\w-])(?P<value>\d+(?:\.\d+)?)(?:\s*(?P<unit>{unit_pattern}))?\b",
        flags=re.IGNORECASE,
    )
    for match in single_pattern.finditer("".join(masked)):
        characteristics.append(
            (
                float(match.group("value")),
                None,
                _normalize_characteristic_unit(match.group("unit") or ""),
            )
        )
    return characteristics


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


def match_score(query: str, product: CatalogProduct, supplier_hint: str = "") -> float:
    """Рассчитывает оценку совпадения товара."""
    q = normalize_text(query)
    name = normalize_text(product.name)
    if not q or not name:
        return 0
    if q == name:
        base = 100.0
    else:
        q_tokens = tokens(q)
        n_tokens = tokens(name)
        intersection = len(q_tokens & n_tokens)
        union = len(q_tokens | n_tokens) or 1
        jaccard = intersection / union
        containment = intersection / max(1, len(q_tokens))
        sequence = SequenceMatcher(None, q, name).ratio()
        fuzzy_token = max(
            (
                SequenceMatcher(None, query_token, name_token).ratio()
                for query_token in q_tokens
                for name_token in n_tokens
            ),
            default=0.0,
        )
        substring = 1.0 if q in name or name in q else 0.0
        compact_query = _compact_voice_name(q)
        compact_name = _compact_voice_name(name)
        compact_similarity = (
            SequenceMatcher(None, compact_query, compact_name).ratio()
            if len(compact_query) >= 6 and len(compact_name) >= 6
            else 0.0
        )
        base = 45 * containment + 25 * jaccard + 25 * sequence + 15 * fuzzy_token + 5 * substring
        # Enough to retain a joined-word candidate for the AI resolver, but
        # below the automatic-selection threshold.
        if compact_similarity >= 0.77:
            base = max(base, 55 * compact_similarity)
    if supplier_matches_hint(product.supplier, supplier_hint):
        base += 8
    return min(100.0, base)


def rank_candidates(
    query: str,
    catalog: list[CatalogProduct],
    supplier_hint: str = "",
    limit: int = 5,
) -> list[Candidate]:
    """Ранжирует кандидатов из каталога."""
    supplier_matches = [
        product for product in catalog if supplier_matches_hint(product.supplier, supplier_hint)
    ]
    scoped_catalog = supplier_matches or catalog
    ranked: list[Candidate] = []
    for product in scoped_catalog:
        score = match_score(query, product, supplier_hint)
        # n8n never shows a candidate based on a weak aggregate similarity
        # alone.  A real token match is required before the clarification UI.
        if score < 20 or not has_catalog_search_evidence(query, product):
            continue
        ranked.append(
            Candidate(
                product_id=product.product_id,
                name=product.name,
                supplier=product.supplier,
                unit=product.unit,
                score=round(score, 2),
                reason="deterministic",
            )
        )
    ranked.sort(key=lambda item: (-item.score, item.name))
    return ranked[:limit]


def can_auto_select(candidates: list[Candidate]) -> bool:
    """Проверяет безопасность автоматического выбора товара."""
    if not candidates:
        return False
    top = candidates[0]
    second_score = candidates[1].score if len(candidates) > 1 else 0
    gap = top.score - second_score
    # An exact product phrase is still unsafe when two catalog rows are
    # effectively tied (for example, the same syrup at two suppliers).  n8n
    # keeps that choice with the user rather than silently selecting row one.
    return (
        (top.score >= 82 and gap >= 5)
        or (top.score >= 67 and gap >= 14)
        or (top.score >= 58 and gap >= 25)
    )


def is_broad_category_query(query: str, candidates: list[Candidate]) -> bool:
    """Определяет слишком общий категорийный запрос."""
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

    if len(query_tokens) == 1:
        category = next(iter(query_tokens))
        for candidate in candidates:
            candidate_tokens = tokens(candidate.name)
            if len(candidate_tokens) <= len(query_tokens):
                continue
            if any(_token_matches(category, token) for token in candidate_tokens):
                return True
        return False

    # A voice model can invent a qualifier such as "мраморная" after the
    # user said only "говядина". If the leading candidates support exactly
    # the same subset of query words and another word is unsupported by both,
    # that qualifier provides no evidence for silently choosing either row.
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


def nearest_valid_multiple(quantity: float, multiple: float | None) -> float | None:
    """Возвращает ближайшее допустимое кратное."""
    if not multiple or multiple <= 0:
        return None
    ratio = quantity / multiple
    nearest = math.ceil(ratio - 1e-9) * multiple
    if abs(nearest - quantity) < 1e-9:
        return None
    return round(nearest, 6)
