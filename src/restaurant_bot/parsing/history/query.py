"""Преобразует естественный вопрос о поставке в HistoryQuery."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date

from restaurant_bot.domain.history import (
    HistoryDateReference,
    HistoryQuery,
    HistoryQuestionType,
    HistoryTemporalScope,
)
from restaurant_bot.domain.text import normalize_text
from restaurant_bot.parsing.history.dates import business_today, date_reference_for
from restaurant_bot.parsing.history.normalization import history_stem, history_tokens

_QUESTION_MARKERS = (
    "когда",
    "сегодня",
    "завтра",
    "статус",
    "поставк",
    "приезж",
    "привез",
    "достав",
    "ждат",
    "будет",
    "задерж",
    "отмен",
    "опазды",
    "новост",
    "что",
    "как",
    "где",
    "почему",
)
_STOP_STEMS = {
    "когда",
    "сегодн",
    "завтр",
    "этот",
    "эт",
    "мо",
    "мне",
    "моя",
    "мое",
    "моё",
    "моей",
    "мою",
    "моим",
    "там",
    "вообщ",
    "у",
    "нас",
    "мои",
    "что",
    "как",
    "где",
    "по",
    "на",
    "с",
    "в",
    "из",
    "к",
    "от",
    "до",
    "уже",
    "еще",
    "или",
    "нет",
    "не",
    "пожалуйст",
    "подскаж",
    "слуша",
    "ну",
    "поставк",
    "срок",
    "ожида",
    "приезж",
    "приед",
    "приедет",
    "приедут",
    "приех",
    "привез",
    "достав",
    "будет",
    "будут",
    "буд",
    "ждат",
    "ожидат",
    "задерж",
    "опаздыва",
    "отмен",
    "статус",
    "новост",
    "дел",
    "должн",
    "нам",
    "это",
    "вообще",
    "почему",
    "остав",
    "все",
    "был",
    "было",
    "бы",
    "пусть",
    "ладн",
    "хорош",
    "так",
    "тогда",
    "да",
    "просто",
    "можно",
    "ли",
    "есть",
    "раз",
    "он",
    "она",
    "оно",
    "они",
    "эта",
    "эту",
    "этой",
    "этим",
}
_PAST_MARKERS = ("последн", "раньше", "был", "приезжал", "привозил")
_ARRIVAL_MARKERS = ("уже приех", "уже привез", "достав", "приехал", "отмен", "задерж", "опаздыва")
_DELIVERY_MARKERS = (
    "когда",
    "поставк",
    "срок",
    "ждат",
    "ожид",
    "приед",
    "приез",
    "привез",
    "будет",
)
_SERVICE_PREFIXES = (
    "хоч",
    "узна",
    "добав",
    "закаж",
    "полож",
    "внес",
    "куп",
    "приед",
    "приех",
    "привез",
    "достав",
    "вез",
    "буд",
    "ждат",
    "ожид",
    "задерж",
    "опазды",
    "отмен",
    "должн",
    "поставк",
    "статус",
    "новост",
    "срок",
    "почему",
    "силе",
    "сил",
)
_CONTEXT_PRONOUNS = ("он", "она", "оно", "они")
_HISTORY_VERB_RE = re.compile(
    r"\b(?:приед\w*|приех\w*|привез\w*|достав\w*|ожида\w*|"
    r"задерж\w*|опаздыва\w*|буд(?:ет|ут|у|ем|ешь|ете)|ждат\w*)\b"
)
_HISTORY_PHRASE_PATTERNS = (
    re.compile(r"\bподскаж\w*\s+по\b"),
    re.compile(r"\bчто\s+там\s+по\b"),
    re.compile(r"\bпоставк\w*\s+по\b"),
    re.compile(r"\bмне\s+.+\s+ждат\w*\b"),
)


def _contains_stem(text: str, stems: tuple[str, ...]) -> bool:
    """Проверяет наличие смыслового корня в нормализованной фразе."""
    normalized = normalize_text(text)
    return any(stem in normalized for stem in stems)


def _product_queries(text: str) -> list[str]:
    """Удаляет вопросительную оболочку и оставляет названия товаров."""
    normalized = normalize_text(text)
    normalized = re.sub(r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b", " ", normalized)
    tokens = history_tokens(normalized)
    kept = [token for token in tokens if not _is_service_token(token)]
    if not kept:
        return []
    groups: list[list[str]] = [[]]
    for token in kept:
        if token == "и" and groups[-1]:
            groups.append([])
            continue
        groups[-1].append(token)
    groups = [group for group in groups if group]
    return [" ".join(group) for group in groups]


def _is_service_token(token: str) -> bool:
    """Отличает глаголы и разговорную оболочку от названия товара."""
    stem = history_stem(token)
    return (
        token in _STOP_STEMS
        or stem in _STOP_STEMS
        or any(stem.startswith(prefix) or token.startswith(prefix) for prefix in _SERVICE_PREFIXES)
    )


def _has_history_signal(normalized: str, *, raw_text: str = "") -> bool:
    """Проверяет смысловые признаки вопроса о поставке."""
    return bool(
        any(marker in normalized for marker in _QUESTION_MARKERS)
        or _HISTORY_VERB_RE.search(normalized)
        or ("?" in raw_text and re.search(r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b", normalized))
        or any(pattern.search(normalized) for pattern in _HISTORY_PHRASE_PATTERNS)
    )


def _context_product_query(
    normalized: str,
    context_product_queries: Sequence[str],
) -> list[str]:
    """Возвращает единственный безопасный товар для местоимённого вопроса."""
    if not any(
        re.search(rf"\b{re.escape(pronoun)}\b", normalized) for pronoun in _CONTEXT_PRONOUNS
    ):
        return []
    names = [str(name).strip() for name in context_product_queries if str(name).strip()]
    unique_names: dict[str, str] = {}
    for name in names:
        unique_names.setdefault(normalize_text(name), name)
    return list(unique_names.values()) if len(unique_names) == 1 else []


def requires_history_context(text: str) -> bool:
    """Определяет, требует ли вопрос истории безопасной ссылки на текущий товар."""
    normalized = normalize_text(text)
    return bool(
        _has_history_signal(normalized, raw_text=text)
        and not _product_queries(normalized)
        and any(
            re.search(rf"\b{re.escape(pronoun)}\b", normalized) for pronoun in _CONTEXT_PRONOUNS
        )
    )


def parse_history_query(
    text: str,
    *,
    context_product_queries: Sequence[str] = (),
    today: date | None = None,
    timezone_name: str = "Europe/Minsk",
) -> HistoryQuery | None:
    """Распознаёт общий смысл естественного вопроса о товарной поставке."""
    normalized = normalize_text(text)
    if not normalized or not _has_history_signal(normalized, raw_text=text):
        return None
    if re.search(r"\b(?:добав\w*|закаж\w*|полож\w*|внес\w*|куп\w*)\b", normalized):
        return None
    products = _product_queries(normalized)
    if not products:
        products = _context_product_query(normalized, context_product_queries)
    if not products:
        return None
    reference, explicit_date = date_reference_for(
        normalized,
        today=today or business_today(timezone_name),
    )
    if _contains_stem(normalized, ("отмен", "в силе")):
        question_type = HistoryQuestionType.CURRENT_STATUS
        scope = HistoryTemporalScope.ACTIVE
    elif _contains_stem(normalized, _PAST_MARKERS):
        question_type = HistoryQuestionType.PAST_DELIVERY
        scope = HistoryTemporalScope.PAST
    elif _contains_stem(normalized, _ARRIVAL_MARKERS):
        question_type = HistoryQuestionType.ARRIVAL_STATUS
        scope = HistoryTemporalScope.ACTIVE
    elif reference is not HistoryDateReference.NONE:
        question_type = HistoryQuestionType.DELIVERY_ON_DATE
        scope = HistoryTemporalScope.ACTIVE
    elif _contains_stem(normalized, _DELIVERY_MARKERS):
        question_type = HistoryQuestionType.DELIVERY_DATE
        scope = HistoryTemporalScope.ACTIVE
    else:
        question_type = HistoryQuestionType.CURRENT_STATUS
        scope = HistoryTemporalScope.ACTIVE
    return HistoryQuery(
        product_queries=products,
        question_type=question_type,
        temporal_scope=scope,
        date_reference=reference,
        explicit_date=explicit_date,
        original_text=text,
    )
