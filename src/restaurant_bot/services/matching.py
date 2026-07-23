from __future__ import annotations

import math
import re
from difflib import SequenceMatcher

from restaurant_bot.domain.models import Candidate, CatalogProduct
from restaurant_bot.services.text import normalize_text

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


def _token_matches(query_token: str, product_token: str) -> bool:
    """Проверяет достаточность совпадения поискового слова."""
    if query_token == product_token:
        return True
    short, long = sorted((query_token, product_token), key=len)
    if len(short) >= 4 and short in long and len(short) / len(long) >= 0.6:
        return True
    if len(query_token) < 4 or len(product_token) < 4:
        return False
    if query_token[0] != product_token[0]:
        return False
    return SequenceMatcher(None, query_token, product_token).ratio() >= 0.72


def has_catalog_search_evidence(query: str, product: CatalogProduct) -> bool:
    """Проверяет наличие достаточных оснований для показа кандидата."""
    query_tokens = tokens(query)
    product_tokens = tokens(product.name)
    if not query_tokens or not product_tokens:
        return False
    if any(
        _token_matches(query_token, product_token)
        for query_token in query_tokens
        for product_token in product_tokens
    ):
        return True

    # Telegram voice recognition sometimes joins adjacent product words:
    # "Сироп Роза" -> "Сыропроза".  n8n sends such a close catalog shortlist
    # to its AI reranker rather than immediately treating it as a different
    # product.  This is deliberately a candidate gate, never an auto-select.
    compact_query = _compact_voice_name(query)
    compact_name = _compact_voice_name(product.name)
    return (
        len(compact_query) >= 6
        and len(compact_name) >= 6
        and compact_query[0] == compact_name[0]
        and SequenceMatcher(None, compact_query, compact_name).ratio() >= 0.77
    )


def has_complete_query_evidence(query: str, product_name: str) -> bool:
    """Проверяет совпадение всех значимых слов запроса."""
    query_tokens = tokens(query)
    product_tokens = tokens(product_name)
    return bool(query_tokens and product_tokens) and all(
        any(_token_matches(query_token, product_token) for product_token in product_tokens)
        for query_token in query_tokens
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
    if supplier_hint and normalize_text(supplier_hint) in normalize_text(product.supplier):
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
        product
        for product in catalog
        if supplier_hint and normalize_text(supplier_hint) in normalize_text(product.supplier)
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
        product_tokens = tokens(candidate.name)
        return {
            query_token
            for query_token in query_tokens
            if any(_token_matches(query_token, product_token) for product_token in product_tokens)
        }

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
