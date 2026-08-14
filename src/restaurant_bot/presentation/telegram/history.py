"""Формирует компактные HTML-ответы на вопросы по истории поставок."""

from __future__ import annotations

from datetime import date

from restaurant_bot.domain.history import (
    HistoryAnswer,
    HistoryAnswerKind,
    HistoryMatch,
    HistoryQuestionType,
)
from restaurant_bot.domain.models import BotReply
from restaurant_bot.presentation.telegram.formatting import escape, heading, product_name

_MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def _date_text(value: date | None) -> str:
    """Форматирует дату истории без обещания фактической доставки."""
    if value is None:
        return ""
    return f"{value.day} {_MONTHS[value.month - 1]} {value.year}"


def _match_lines(answer: HistoryAnswer, match: HistoryMatch) -> list[str]:
    """Формирует доказательства одной найденной товарной позиции."""
    entry = match.entry
    lines = [product_name(entry.product_name)]
    if answer.query.question_type is HistoryQuestionType.DELIVERY_ON_DATE:
        if answer.target_date and entry.delivery_date == answer.target_date:
            lines.append("В истории указана поставка на эту дату.")
        elif entry.delivery_date:
            lines.append(
                f"На эту дату поставка не указана. Дата поставки: <b>{escape(_date_text(entry.delivery_date))}</b>."
            )
        else:
            lines.append("Дата поставки пока не указана.")
    elif entry.delivery_date:
        lines.append(f"Дата поставки: <b>{escape(_date_text(entry.delivery_date))}</b>.")
    else:
        lines.append("Дата поставки пока не указана.")
    lines.append(f"Статус: <b>{escape(entry.stage or 'не указан')}</b>.")
    if entry.supplier:
        lines.append(f"Поставщик: {escape(entry.supplier)}")
    return lines


def history_reply(answer: HistoryAnswer) -> BotReply:
    """Переводит нейтральный history answer в Telegram HTML."""
    query_text = " и ".join(answer.query.product_queries)
    if answer.kind is HistoryAnswerKind.AMBIGUOUS:
        lines = [heading("Нашёл несколько актуальных товаров"), ""]
        for match in answer.alternatives or answer.matches:
            lines.append(f"• {product_name(match.entry.product_name)}")
        lines.extend(["", "Уточните, какой именно товар проверить."])
        return BotReply(text="\n".join(lines))
    if answer.kind is HistoryAnswerKind.NOT_FOUND:
        return BotReply(text=f"В истории этого заведения не нашёл товар «{escape(query_text)}».")
    if answer.kind is HistoryAnswerKind.NO_ACTIVE:
        return BotReply(text=f"{product_name(query_text)}\n\nАктивных поставок сейчас не найдено.")
    if answer.kind is HistoryAnswerKind.EMPTY:
        return BotReply(text="В истории этого заведения пока нет строк с товарами.")
    lines = [heading("Проверка истории"), ""]
    for index, match in enumerate(answer.matches, start=1):
        if index > 1:
            lines.append("")
        lines.extend(_match_lines(answer, match))
    return BotReply(text="\n".join(lines))
