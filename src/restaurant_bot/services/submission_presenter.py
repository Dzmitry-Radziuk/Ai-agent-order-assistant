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


def submission_local_saved_reply(state: Any, order_no: str) -> BotReply:
    """Подтверждает запись заявки для последующей ручной отправки."""
    del order_no
    return BotReply(
        text=(
            "✅ <b>Заявка записана</b>\n\n"
            "Товары добавлены в таблицу заказа, расчёты обновлены.\n"
            "Заявку поставщикам отправит ответственный сотрудник."
        ),
        rows=[
            [
                Button(
                    text="Новая заявка",
                    callback_data=_callback_with_revision(state, "v2:clear"),
                )
            ]
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


def submission_dispatch_uncertain_reply(state: Any, order_no: str) -> BotReply:
    """Формирует предупреждение без опасной кнопки повторной отправки."""
    del state
    return BotReply(
        text=(
            "⚠️ <b>Нужно проверить отправку</b>\n\n"
            "Бот передал заявку, но не получил подтверждение от системы закупок.\n\n"
            "<b>Не отправляйте её повторно:</b> поставщики могли уже получить заказ.\n"
            "Сообщите менеджеру по снабжению этот код:\n"
            f"<code>{escape(order_no)}</code>"
        )
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


def _format_history_timestamp(value: Any) -> str:
    """Форматирует дату создания заявки для компактного списка."""
    raw = str(value or "").strip()
    if not raw:
        return "Дата не указана"
    for pattern in (
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            parsed = datetime.strptime(raw[:19], pattern)
            return parsed.strftime("%d.%m.%Y, %H:%M")
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


def group_order_status_rows(
    rows: list[dict[str, Any]],
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Группирует строки «Истории» по номеру заявки, сохраняя порядок."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        order_number = _status_value(row, "Номер заявки", "№ Заявки", "ID заявки", "order_no")
        if order_number:
            groups.setdefault(order_number, []).append(row)
    return list(groups.items())


def _supplier_count(order_rows: list[dict[str, Any]]) -> int:
    """Считает поставщиков одной заявки без повторов."""
    suppliers = {
        _status_value(
            row,
            "Условное название поставщика",
            "Основной поставщик (Условное наз-ие)",
            "Поставщик",
            "supplier",
        )
        for row in order_rows
    }
    suppliers.discard("")
    return len(suppliers) or len(order_rows)


def _supplier_word(count: int) -> str:
    """Согласует слово «поставщик» с количеством."""
    if count % 10 == 1 and count % 100 != 11:
        return "поставщик"
    if count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14}:
        return "поставщика"
    return "поставщиков"


def build_order_status_list_reply(
    rows: list[dict[str, Any]],
    *,
    page: int,
    has_more: bool,
) -> BotReply:
    """Формирует компактный список реальных заявок с навигацией."""
    groups = group_order_status_rows(rows)
    if not groups:
        return BotReply(
            text=(
                "📋 <b>Мои заявки</b>\n\n"
                "У этого заведения пока нет отправленных заявок в листе «История»."
            )
        )

    lines = ["📋 <b>Мои заявки</b>", "", f"Страница {page + 1}:"]
    buttons: list[list[Button]] = []
    for index, (order_number, order_rows) in enumerate(groups, start=1):
        created_at = _status_value(
            order_rows[0],
            "Время создания заявки",
            "Дата создания",
            "created_at",
        )
        supplier_count = _supplier_count(order_rows)
        stages = {
            _status_value(row, "Стадия", "Статус", "status")
            for row in order_rows
            if _status_value(row, "Стадия", "Статус", "status")
        }
        lines.extend(
            [
                "",
                f"{index}. <b>{escape(order_number)}</b>",
                (
                    f"{escape(_format_history_timestamp(created_at))} · "
                    f"{supplier_count} {_supplier_word(supplier_count)}"
                ),
                (
                    f"Статус: {escape(next(iter(stages)))}"
                    if len(stages) == 1
                    else "Статусы различаются по поставщикам"
                ),
            ]
        )
        buttons.append(
            [
                Button(
                    text=f"{index}. {order_number}"[:60],
                    callback_data=f"v2:order:{index}",
                )
            ]
        )

    navigation: list[Button] = []
    if page > 0:
        navigation.append(
            Button(
                text="← Новее",
                callback_data=f"v2:orderspage:{page - 1}",
            )
        )
    if has_more:
        navigation.append(
            Button(
                text="Старее →",
                callback_data=f"v2:orderspage:{page + 1}",
            )
        )
    if navigation:
        buttons.append(navigation)
    lines.extend(["", "Выберите заявку или скажите, например: «Покажи вторую»."])
    return BotReply(text="\n".join(lines), rows=buttons)


def build_order_status_detail_reply(
    text: str,
    *,
    page: int,
    selected_index: int,
) -> BotReply:
    """Добавляет к подробной заявке возврат к списку и обновление."""
    return BotReply(
        text=text,
        rows=[
            [
                Button(
                    text="Обновить",
                    callback_data=f"v2:order:{selected_index}",
                )
            ],
            [
                Button(
                    text="← К списку заявок",
                    callback_data=f"v2:orderspage:{page}",
                )
            ],
        ],
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
