"""Преобразует естественный вопрос о поставке в HistoryQuery."""

from __future__ import annotations

import re

from restaurant_bot.domain.history import (
    HistoryDateReference,
    HistoryQuery,
    HistoryQuestionType,
    HistoryTemporalScope,
)
from restaurant_bot.domain.text import normalize_text
from restaurant_bot.parsing.history.dates import date_reference_for
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
}
_PAST_MARKERS = ("последн", "раньше", "был", "приезжал", "привозил")
_ARRIVAL_MARKERS = ("уже приех", "уже привез", "достав", "приехал", "отмен", "задерж", "опаздыва")
_DELIVERY_MARKERS = ("когда", "поставк", "срок", "ждат", "ожид", "приез", "привез", "будет")
_SERVICE_PREFIXES = (
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


def parse_history_query(text: str) -> HistoryQuery | None:
    """Распознаёт общий смысл естественного вопроса о товарной поставке."""
    normalized = normalize_text(text)
    if not normalized or not any(marker in normalized for marker in _QUESTION_MARKERS):
        return None
    if re.search(r"\b(?:добав\w*|закаж\w*|полож\w*|внес\w*|куп\w*)\b", normalized):
        return None
    products = _product_queries(normalized)
    if not products:
        return None
    reference, explicit_date = date_reference_for(normalized)
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
