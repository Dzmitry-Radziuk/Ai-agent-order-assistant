"""Формирует компактные HTML-ответы на вопросы по истории поставок."""

from __future__ import annotations

from datetime import date

from restaurant_bot.domain.history import (
    HistoryAnswer,
    HistoryAnswerKind,
    HistoryDeliveryDateRelation,
    HistoryMatch,
    HistoryQuestionType,
)
from restaurant_bot.domain.models import BotReply
from restaurant_bot.presentation.telegram.formatting import (
    escape,
    format_status,
    heading,
    product_name,
)

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
        elif (
            entry.delivery_date and match.delivery_date_relation is HistoryDeliveryDateRelation.PAST
        ):
            lines.append("На эту дату поставка в истории не указана.")
            lines.append(
                f"Последняя указанная дата: <b>{escape(_date_text(entry.delivery_date))}</b> — "
                "она уже прошла."
            )
        elif entry.delivery_date:
            lines.append(
                f"На эту дату поставка не указана. Дата поставки: <b>{escape(_date_text(entry.delivery_date))}</b>."
            )
        else:
            lines.append("Дата поставки пока не указана.")
    elif entry.delivery_date:
        date_text = escape(_date_text(entry.delivery_date))
        if (
            answer.query.question_type is HistoryQuestionType.DELIVERY_DATE
            and match.delivery_date_relation is HistoryDeliveryDateRelation.PAST
        ):
            lines.append(f"В истории указана дата поставки: <b>{date_text}</b> — она уже прошла.")
            lines.append("Новая дата поставки не указана.")
        elif match.delivery_date_relation is HistoryDeliveryDateRelation.PAST:
            lines.append(f"Дата поставки: <b>{date_text}</b> — она уже прошла.")
        else:
            lines.append(f"Дата поставки: <b>{date_text}</b>.")
    else:
        lines.append("Дата поставки пока не указана.")
    lines.append(f"Статус: <b>{escape(format_status(entry.stage))}</b>.")
    if entry.supplier:
        lines.append(f"Поставщик: <b>{escape(entry.supplier)}</b>")
    return lines


def _venue_match_lines(match: HistoryMatch) -> list[str]:
    """Формирует компактную строку доказательств venue-level поставки."""
    entry = match.entry
    date_value = (
        f"<b>{escape(_date_text(entry.delivery_date))}</b>"
        if entry.delivery_date
        else "<b>пока не указана</b>"
    )
    lines = [product_name(entry.product_name), f"Дата поставки: {date_value}."]
    lines.append(f"Статус: <b>{escape(format_status(entry.stage))}</b>.")
    if entry.supplier:
        lines.append(f"Поставщик: <b>{escape(entry.supplier)}</b>")
    return lines


def _venue_reply(answer: HistoryAnswer) -> BotReply:
    """Переводит общий запрос о поставках в безопасный Telegram-ответ."""
    if answer.kind is HistoryAnswerKind.EMPTY:
        text = "В истории этого заведения пока нет данных о поставках."
    elif answer.kind is HistoryAnswerKind.NO_ACTIVE:
        if answer.target_date:
            target = escape(_date_text(answer.target_date))
            text = f"На {target} в истории активных поставок с указанной датой не найдено."
        else:
            text = "В истории этого заведения сейчас не найдено актуальных поставок."
        if answer.active_without_delivery_date_count:
            text += " Есть активные заявки без указанной даты поставки."
    else:
        if answer.target_date:
            title = f"Поставки на {_date_text(answer.target_date)}"
            intro = "В истории найдены поставки с датой на этот день:"
        else:
            title = "Актуальные поставки"
            intro = "В истории найдены актуальные поставки:"
        lines = [heading(f"📦 {title}"), intro]
        for index, match in enumerate(answer.matches, start=1):
            if index > 1:
                lines.append("")
            lines.extend(_venue_match_lines(match))
        if answer.additional_match_count:
            lines.extend(["", f"И ещё {answer.additional_match_count} поставок."])
        text = "\n".join(lines)
    if answer.query.actor_specific:
        caveat = (
            "По истории я могу проверить сами поставки, "
            "но в данных не указано, кто именно их привезёт."
        )
        text = f"{caveat}\n\n{text}"
    return BotReply(text=text)


def history_reply(answer: HistoryAnswer) -> BotReply:
    """Переводит нейтральный history answer в Telegram HTML."""
    if answer.query.question_type is HistoryQuestionType.VENUE_DELIVERIES:
        return _venue_reply(answer)
    query_text = " и ".join(answer.query.product_queries)
    if answer.kind is HistoryAnswerKind.AMBIGUOUS:
        ambiguous_lines = [heading("Нашёл несколько актуальных товаров"), ""]
        for match in answer.alternatives or answer.matches:
            ambiguous_lines.append(f"• {product_name(match.entry.product_name)}")
        ambiguous_lines.extend(["", "Уточните, какой именно товар проверить."])
        return BotReply(text="\n".join(ambiguous_lines))
    if answer.kind is HistoryAnswerKind.NOT_FOUND:
        return BotReply(text=f"В истории этого заведения не нашёл товар «{escape(query_text)}».")
    if answer.kind is HistoryAnswerKind.NO_ACTIVE:
        return BotReply(text=f"{product_name(query_text)}\n\nАктивных поставок сейчас не найдено.")
    if answer.kind is HistoryAnswerKind.EMPTY:
        return BotReply(text="В истории этого заведения пока нет строк с товарами.")
    lines: list[str] = []
    for index, match in enumerate(answer.matches, start=1):
        if index > 1:
            lines.append("")
        lines.extend(_match_lines(answer, match))
    return BotReply(text="\n".join(lines))
