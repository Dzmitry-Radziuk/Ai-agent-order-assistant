from __future__ import annotations

from datetime import datetime
from typing import Any

from restaurant_bot.domain.models import BotReply, Button
from restaurant_bot.services.text import escape


def _callback_with_revision(state: Any, callback_data: str) -> str:
    """Добавляет версию интерфейса к callback."""
    revision = max(0, int(getattr(state, "ui_revision", 0) or 0))
    return f"{callback_data}:r{revision}" if revision else callback_data


def submission_success_reply(state: Any, order_no: str) -> BotReply:
    """Формирует подтверждение успешной заявки."""
    return BotReply(
        text=f"✅ <b>Заявка отправлена</b>\n\nНомер заявки: {escape(order_no)}",
        rows=[
            [
                Button(
                    text="Проверить статус",
                    callback_data=_callback_with_revision(state, "v2:orders"),
                )
            ],
            [
                Button(
                    text="Новая заявка",
                    callback_data=_callback_with_revision(state, "v2:clear"),
                )
            ],
        ],
    )


def submission_failure_reply(state: Any, order_no: str) -> BotReply:
    """Формирует безопасную карточку незавершённой отправки."""
    return BotReply(
        text=(
            "⚠️ <b>Отправка не завершена</b>\n\n"
            f"Заявка: {escape(order_no)}\n\n"
            "Нажмите «Повторить отправку». Уже выполненные этапы будут пропущены."
        ),
        rows=[
            [
                Button(
                    text="Повторить отправку",
                    callback_data=_callback_with_revision(state, "v2:submit"),
                )
            ],
            [Button(text="К черновику", callback_data=_callback_with_revision(state, "v2:back"))],
        ],
    )


def _status_value(row: dict[str, Any], *keys: str, default: str = "") -> str:
    """Возвращает первое заполненное поле статуса."""
    for key in keys:
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return default


def _format_delivery_date(value: Any) -> str:
    """Форматирует дату доставки для пользователя."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    months = (
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
    for pattern in ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            parsed = datetime.strptime(raw, pattern)
            return f"{parsed.day} {months[parsed.month - 1]} {parsed.year}"
        except ValueError:
            continue
    return raw


def _escape_multiline(value: Any) -> str:
    """Экранирует многострочный текст, сохраняя переносы строк."""
    return "\n".join(escape(line) for line in str(value or "").splitlines() if line.strip())


def _is_aggregated_status_row(row: dict[str, Any]) -> bool:
    """Определяет строку нового сводного листа «История»."""
    return bool(
        _status_value(
            row,
            "Список товаров",
            "Условное название поставщика",
            "Условное наз-ие заведения",
        )
    )


def _append_aggregated_order_status(lines: list[str], order_rows: list[dict[str, Any]]) -> None:
    """Добавляет в карточку сводные статусы поставщиков одной заявки."""
    for row_index, row in enumerate(order_rows):
        supplier = _status_value(
            row,
            "Условное название поставщика",
            "Основной поставщик (Условное наз-ие)",
            "supplier",
        )
        stage = _status_value(
            row,
            "Стадия",
            "Статус",
            "status",
            default="Ожидает обработки",
        )
        delivery = _format_delivery_date(
            _status_value(
                row,
                "Дата поставки",
                "Дата доставки",
                "Ожидаемая дата доставки",
                "delivery_date",
            )
        )
        product_list = _status_value(
            row,
            "Список товаров",
            "Товары",
            "product_list",
        )
        manager = _status_value(row, "ФИО менеджера Поставщика", "Менеджер", "manager")
        phone = _status_value(row, "Телефон", "Телефон поставщика", "phone")

        if supplier:
            lines.append(f"Поставщик: <b>{escape(supplier)}</b>")
        lines.append(f"Статус: <b>{escape(stage)}</b>")
        if delivery:
            lines.append(f"Дата поставки: <b>{escape(delivery)}</b>")
        if product_list:
            lines.extend(["Товары:", _escape_multiline(product_list)])
        if manager or phone:
            contact = ", ".join(escape(value) for value in (manager, phone) if value)
            lines.append(f"Контакт поставщика: {contact}")
        if row_index < len(order_rows) - 1:
            lines.append("")


def _append_legacy_order_status(lines: list[str], order_rows: list[dict[str, Any]]) -> None:
    """Добавляет старые построчные статусы для обратной совместимости."""
    details = [
        {
            "product": _status_value(
                row,
                "Наименование у поставщика",
                "Наименование у Поставщика",
                "product_name",
                default="Товар",
            ),
            "stage": _status_value(row, "Стадия", "Статус", "status", default="Ожидает обработки"),
            "delivery": _format_delivery_date(
                _status_value(
                    row,
                    "Дата доставки",
                    "Ожидаемая дата доставки",
                    "Дата поставки",
                    "delivery_date",
                )
            ),
        }
        for row in order_rows
    ]
    stages = list(dict.fromkeys(item["stage"] for item in details if item["stage"]))
    deliveries = list(dict.fromkeys(item["delivery"] for item in details if item["delivery"]))
    if len(stages) == 1:
        lines.append(f"Статус: <b>{escape(stages[0])}</b>")
    if len(deliveries) == 1:
        lines.append(f"Дата поставки: <b>{escape(deliveries[0])}</b>")
    lines.append("Товары:")
    for item in details:
        product_line = f"• <b>{escape(item['product'])}</b>"
        if len(stages) > 1:
            product_line += f" — {escape(item['stage'])}"
        if len(deliveries) > 1 and item["delivery"]:
            product_line += f" ({escape(item['delivery'])})"
        lines.append(product_line)


def build_order_status_text(rows: list[dict[str, Any]], state: Any) -> str:
    """Формирует карточку статусов заявок."""
    tracked = list(
        dict.fromkeys(
            [str(getattr(state, "last_order_no", "") or "").strip()]
            + [str(value).strip() for value in getattr(state, "submitted_order_numbers", [])]
        )
    )
    tracked = [value for value in tracked if value][:10]
    if not tracked:
        return "<b>Мои заявки</b>\n\nУ вас пока нет заявок, отправленных через этого бота."
    groups: dict[str, list[dict[str, Any]]] = {order_no: [] for order_no in tracked}
    for row in rows:
        order_no = _status_value(row, "№ Заявки", "Номер заявки", "order_no")
        if order_no in groups:
            groups[order_no].append(row)
    shown = [(order_no, group) for order_no, group in groups.items() if group]
    if not shown:
        return f"<b>Мои заявки</b>\n\nСтатус заявки {escape(tracked[0])} пока не появился в таблице. Попробуйте обновить позже."

    lines = ["<b>Мои заявки</b>", ""]
    for index, (order_no, order_rows) in enumerate(shown):
        lines.append(f"{index + 1}. <b>Заявка {escape(order_no)}</b>")
        if any(_is_aggregated_status_row(row) for row in order_rows):
            _append_aggregated_order_status(lines, order_rows)
        else:
            _append_legacy_order_status(lines, order_rows)
        if index < len(shown) - 1:
            lines.append("")
    return "\n".join(lines)
