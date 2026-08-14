"""Классифицирует реальные стадии строк «История» и их релевантность."""

from __future__ import annotations

from restaurant_bot.domain.history import (
    HistoryQuestionType,
    HistoryStatusClass,
    HistoryTemporalScope,
)
from restaurant_bot.domain.text import normalize_text


def classify_status(raw_status: str) -> HistoryStatusClass:
    """Классифицирует только известные по текущим данным значения стадии."""
    value = normalize_text(raw_status)
    if not value:
        return HistoryStatusClass.UNKNOWN
    if any(token in value for token in ("отмен", "аннулиров", "отказ")):
        return HistoryStatusClass.CANCELLED
    if "доставлен" in value:
        return HistoryStatusClass.DELIVERED
    if any(token in value for token in ("заверш", "выполнен", "получен", "закрыт")):
        return HistoryStatusClass.COMPLETED
    if any(
        token in value
        for token in (
            "новая заявка",
            "подтвержд",
            "ожидает",
            "отправлен",
            "отправлено",
            "в пути",
            "собира",
            "обработ",
            "принят",
            "готов",
        )
    ):
        return HistoryStatusClass.ACTIVE
    return HistoryStatusClass.UNKNOWN


def is_relevant_status(
    raw_status: str,
    question_type: HistoryQuestionType,
    scope: HistoryTemporalScope,
) -> bool:
    """Решает, может ли строка участвовать в заданном временном вопросе."""
    status = classify_status(raw_status)
    if scope is HistoryTemporalScope.PAST or question_type is HistoryQuestionType.PAST_DELIVERY:
        return status in {HistoryStatusClass.COMPLETED, HistoryStatusClass.DELIVERED}
    if question_type is HistoryQuestionType.ARRIVAL_STATUS:
        # Для вопроса «уже приехал?» важна последняя зафиксированная стадия,
        # включая доставленную или завершённую поставку.
        return True
    return status not in {HistoryStatusClass.COMPLETED, HistoryStatusClass.DELIVERED}


def status_label(raw_status: str) -> str:
    """Возвращает исходную стадию либо безопасную подпись неизвестного значения."""
    return raw_status.strip() or "Статус не указан"
