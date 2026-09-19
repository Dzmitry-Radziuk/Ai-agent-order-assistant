"""Формирует Telegram-представление «submission»."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from restaurant_bot.domain.models import BotReply, Button
from restaurant_bot.history.product_list import extract_history_product_name
from restaurant_bot.presentation.telegram.formatting import (
    escape,
    format_status,
    heading,
    product_name,
)

# Канонический Telegram presenter.


def _callback_with_revision(state: Any, callback_data: str) -> str:
    """Добавляет версию интерфейса к callback."""
    revision = max(0, int(getattr(state, "ui_revision", 0) or 0))
    return f"{callback_data}:r{revision}" if revision else callback_data


def submission_success_reply(state: Any, order_no: str) -> BotReply:
    """Формирует подтверждение успешной заявки."""
    return BotReply(
        text=f"<i>Заявка отправлена</i>\n\nНомер заявки: {escape(order_no)}",
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
            "<i>Заявка записана</i>\n\n"
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
            f"🔸 {heading('Отправка не завершена')}\n\n"
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
    """Формирует предупреждение с безопасной проверкой без повторной отправки."""
    return BotReply(
        text=(
            f"🔸 {heading('Нужно проверить отправку')}\n\n"
            "Бот передал заявку, но не получил подтверждение от системы закупок.\n\n"
            "<b>Не отправляйте её повторно:</b> поставщики могли уже получить заказ.\n"
            "Сообщите менеджеру по снабжению этот код:\n"
            f"<code>{escape(order_no)}</code>"
        ),
        rows=[
            [
                Button(
                    text="Проверить отправку",
                    callback_data=_callback_with_revision(state, "v2:check_submission"),
                )
            ],
            [Button(text="К черновику", callback_data=_callback_with_revision(state, "v2:back"))],
        ],
    )


def submission_dispatch_still_uncertain_reply(state: Any, order_no: str) -> BotReply:
    """Сообщает, что подтверждение не найдено и повторять отправку нельзя."""
    return BotReply(
        text=(
            f"🔸 {heading('Подтверждение пока не найдено')}\n\n"
            f"Заявка <code>{escape(order_no)}</code> ещё не появилась в «Истории».\n"
            "Это не означает, что она не была получена поставщиками.\n\n"
            "Не отправляйте заявку повторно. Попробуйте проверить позже или сообщите код "
            "ответственному сотруднику."
        ),
        rows=[
            [
                Button(
                    text="Проверить отправку",
                    callback_data=_callback_with_revision(state, "v2:check_submission"),
                )
            ],
            [Button(text="К черновику", callback_data=_callback_with_revision(state, "v2:back"))],
        ],
    )


def submission_catalog_uncertain_reply(state: Any, order_no: str) -> BotReply:
    """Сообщает о временной остановке без технических терминов и опасного повтора."""
    del order_no
    return BotReply(
        text=(
            f"🔸 {heading('Отправка не завершена')}\n\n"
            "Заявка сохранена, но бот не смог безопасно подтвердить изменение данных.\n\n"
            "Чтобы случайно не изменить заявку повторно, отправка временно остановлена. "
            "Попробуйте позже или обратитесь к ответственному сотруднику."
        ),
        rows=[
            [Button(text="К черновику", callback_data=_callback_with_revision(state, "v2:back"))]
        ],
    )


def submission_recalculation_uncertain_reply(state: Any, order_no: str) -> BotReply:
    """Сообщает о сохранённой заявке и автоматической проверке перерасчёта."""
    del order_no
    return BotReply(
        text=(
            f"🔸 {heading('Заявка сохранена, расчёт ещё проверяется')}\n\n"
            "Бот автоматически проверит и повторит обновление расчётов в фоне.\n\n"
            "Эту заявку повторно оформлять не нужно. Если хотите начать другую, "
            "отправьте команду /reset."
        ),
        rows=[
            [Button(text="К черновику", callback_data=_callback_with_revision(state, "v2:back"))]
        ],
    )


def submission_catalog_conflict_reply(state: Any, order_no: str) -> BotReply:
    """Сообщает об изменении данных заявки без автоматической перезаписи."""
    del order_no
    return BotReply(
        text=(
            f"🔸 {heading('Отправка не завершена')}\n\n"
            "Данные заявки изменились после начала отправки.\n\n"
            "Заявка сохранена. Бот не будет перезаписывать изменения автоматически. "
            "Обратитесь к ответственному сотруднику для проверки."
        ),
        rows=[
            [Button(text="К черновику", callback_data=_callback_with_revision(state, "v2:back"))]
        ],
    )


def submission_recovery_unavailable_reply() -> BotReply:
    """Сообщает о сбое, для которого в состоянии нет безопасного снимка."""
    return BotReply(
        text=(
            f"🔸 {heading('Отправку нельзя безопасно повторить')}\n\n"
            "Снимок заявки не найден. Сохраните этот экран и обратитесь к менеджеру по снабжению."
        )
    )


def submission_in_progress_reply(order_no: str) -> BotReply:
    """Сообщает, что заявка уже находится в процессе отправки."""
    return BotReply(
        text=(
            "<i>Заявка уже обрабатывается</i>\n\n"
            f"Номер заявки: {escape(order_no)}\n"
            "Дождитесь завершения отправки и не запускайте новую заявку сейчас."
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


def _format_order_date(value: Any) -> str:
    """Форматирует дату создания заявки без времени для пользовательского заголовка."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    for pattern in (
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d.%m.%Y",
        "%Y-%m-%d",
    ):
        try:
            parsed = datetime.strptime(raw[:19], pattern)
            return parsed.strftime("%d.%m.%Y")
        except ValueError:
            continue
    return ""


def _escape_multiline(value: Any) -> str:
    """Экранирует многострочный текст, сохраняя переносы строк."""
    return "\n".join(escape(line) for line in str(value or "").splitlines() if line.strip())


_PRODUCT_LIST_PREFIX = re.compile(r"^(?P<prefix>\s*(?:(?:•|\d+[.)])\s*)?)")


def _escape_product_list(value: Any) -> str:
    """Экранирует список товаров и выделяет только названия позиций."""
    rendered: list[str] = []
    for line in str(value or "").splitlines():
        if not line.strip():
            continue
        prefix_match = _PRODUCT_LIST_PREFIX.match(line)
        prefix = prefix_match.group("prefix") if prefix_match else ""
        body = line[len(prefix) :]
        name = extract_history_product_name(body)
        if name and body.startswith(name) and "<" not in name and ">" not in name:
            tail = body[len(name) :]
            separator = " " if tail[:1].isspace() else ""
            rendered.append(f"{prefix}{product_name(name)}{separator}{escape(tail.lstrip())}")
        else:
            rendered.append(escape(line))
    return "\n".join(rendered)


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


def _order_title(order_rows: list[dict[str, Any]], display_index: int) -> str:
    """Формирует короткий заголовок заявки по дате создания."""
    created_at = _status_value(
        order_rows[0],
        "Время создания заявки",
        "Дата создания",
        "created_at",
    )
    date_text = _format_order_date(created_at)
    return f"{display_index}. Заявка от {date_text}" if date_text else f"{display_index}. Заявка"


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
                f"📋 {heading('Мои заявки')}\n\n"
                "У этого заведения пока нет отправленных заявок в листе «История»."
            )
        )

    lines = [f"📋 {heading('Мои заявки')}", "", f"Страница {page + 1}:"]
    buttons: list[list[Button]] = []
    for index, (_order_number, order_rows) in enumerate(groups, start=1):
        supplier_count = _supplier_count(order_rows)
        stages = {
            format_status(_status_value(row, "Стадия", "Статус", "status"))
            for row in order_rows
            if _status_value(row, "Стадия", "Статус", "status")
        }
        order_title = _order_title(order_rows, index)
        created_at_available = bool(
            _format_order_date(
                _status_value(
                    order_rows[0],
                    "Время создания заявки",
                    "Дата создания",
                    "created_at",
                )
            )
        )
        lines.extend(
            [
                "",
                f"<b>{escape(order_title)}</b>",
                *([] if created_at_available else ["Дата создания не указана"]),
                f"Поставщиков: <b>{supplier_count}</b>",
                (
                    f"Статус: <b>{escape(next(iter(stages)))}</b>"
                    if len(stages) == 1
                    else "Статусы различаются по поставщикам"
                ),
            ]
        )
        buttons.append(
            [
                Button(
                    text=order_title[:60],
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
    detail_page: int = 0,
    detail_page_count: int = 1,
) -> BotReply:
    """Добавляет к подробной заявке возврат к списку и обновление."""
    rows: list[list[Button]] = []
    if detail_page_count > 1:
        navigation: list[Button] = []
        if detail_page > 0:
            navigation.append(
                Button(
                    text="← Назад",
                    callback_data=f"v2:orderitems:{selected_index}:{detail_page - 1}",
                )
            )
        if detail_page < detail_page_count - 1:
            navigation.append(
                Button(
                    text="Далее →",
                    callback_data=f"v2:orderitems:{selected_index}:{detail_page + 1}",
                )
            )
        if navigation:
            rows.append(navigation)
    rows.extend(
        [
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
        ]
    )
    return BotReply(
        text=text,
        rows=rows,
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
        stage = format_status(
            _status_value(
                row,
                "Стадия",
                "Статус",
                "status",
                default="Ожидает обработки",
            )
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
            lines.extend(["Товары:", _escape_product_list(product_list)])
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
            "stage": format_status(
                _status_value(row, "Стадия", "Статус", "status", default="Ожидает обработки")
            ),
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
        product_line = f"• {product_name(item['product'])}"
        if len(stages) > 1:
            product_line += f" — {escape(item['stage'])}"
        if len(deliveries) > 1 and item["delivery"]:
            product_line += f" ({escape(item['delivery'])})"
        lines.append(product_line)


_STATUS_DETAIL_PRODUCTS_PER_BLOCK = 8
_STATUS_DETAIL_BLOCKS_PER_PAGE = 3


def _tracked_status_groups(
    rows: list[dict[str, Any]], state: Any
) -> tuple[list[str], list[tuple[str, list[dict[str, Any]]]]]:
    """Возвращает отслеживаемые номера заявок и относящиеся к ним строки."""
    tracked = list(
        dict.fromkeys(
            [str(getattr(state, "last_order_no", "") or "").strip()]
            + [str(value).strip() for value in getattr(state, "submitted_order_numbers", [])]
        )
    )
    tracked = [value for value in tracked if value][:10]
    groups: dict[str, list[dict[str, Any]]] = {order_no: [] for order_no in tracked}
    for row in rows:
        order_no = _status_value(row, "№ Заявки", "Номер заявки", "order_no")
        if order_no in groups:
            groups[order_no].append(row)
    return tracked, [(order_no, group) for order_no, group in groups.items() if group]


def _aggregated_detail_blocks(
    order_no: str,
    order_rows: list[dict[str, Any]],
    *,
    display_index: int = 1,
) -> list[list[str]]:
    """Делит строки поставщика и длинные списки на безопасные блоки Telegram."""
    del order_no
    title_block = [f"<b>{escape(_order_title(order_rows, display_index))}</b>"]
    if not _format_order_date(
        _status_value(order_rows[0], "Время создания заявки", "Дата создания", "created_at")
    ):
        title_block.append("Дата создания не указана")
    blocks: list[list[str]] = [title_block]
    for row in order_rows:
        supplier = _status_value(
            row,
            "Условное название поставщика",
            "Основной поставщик (Условное наз-ие)",
            "supplier",
        )
        stage = format_status(
            _status_value(row, "Стадия", "Статус", "status", default="Ожидает обработки")
        )
        delivery = _format_delivery_date(
            _status_value(
                row, "Дата поставки", "Дата доставки", "Ожидаемая дата доставки", "delivery_date"
            )
        )
        product_list = _status_value(row, "Список товаров", "Товары", "product_list")
        manager = _status_value(row, "ФИО менеджера Поставщика", "Менеджер", "manager")
        phone = _status_value(row, "Телефон", "Телефон поставщика", "phone")
        product_lines = _escape_product_list(product_list).splitlines() or ["—"]
        for chunk_index in range(0, len(product_lines), _STATUS_DETAIL_PRODUCTS_PER_BLOCK):
            chunk = product_lines[chunk_index : chunk_index + _STATUS_DETAIL_PRODUCTS_PER_BLOCK]
            section: list[str] = []
            if chunk_index == 0:
                if supplier:
                    section.append(f"Поставщик: <b>{escape(supplier)}</b>")
                section.append(f"Статус: <b>{escape(stage)}</b>")
                if delivery:
                    section.append(f"Дата поставки: <b>{escape(delivery)}</b>")
                section.append("Товары:")
            else:
                section.append("Товары (продолжение):")
            section.extend(chunk)
            if chunk_index + _STATUS_DETAIL_PRODUCTS_PER_BLOCK >= len(product_lines) and (
                manager or phone
            ):
                contact = ", ".join(escape(value) for value in (manager, phone) if value)
                section.append(f"Контакт поставщика: {contact}")
            blocks.append(section)
    return blocks


def _legacy_detail_blocks(
    order_no: str,
    order_rows: list[dict[str, Any]],
    *,
    display_index: int = 1,
) -> list[list[str]]:
    """Делит старую историю с одной позицией в строке на блоки."""
    del order_no
    details = [
        (
            _status_value(
                row,
                "Наименование у поставщика",
                "Наименование у Поставщика",
                "product_name",
                default="Товар",
            ),
            format_status(
                _status_value(row, "Стадия", "Статус", "status", default="Ожидает обработки")
            ),
            _format_delivery_date(
                _status_value(
                    row,
                    "Дата доставки",
                    "Ожидаемая дата доставки",
                    "Дата поставки",
                    "delivery_date",
                )
            ),
        )
        for row in order_rows
    ]
    stages = list(dict.fromkeys(stage for _, stage, _ in details if stage))
    deliveries = list(dict.fromkeys(delivery for _, _, delivery in details if delivery))
    blocks: list[list[str]] = []
    for offset in range(0, len(details), _STATUS_DETAIL_PRODUCTS_PER_BLOCK):
        chunk = details[offset : offset + _STATUS_DETAIL_PRODUCTS_PER_BLOCK]
        section: list[str] = []
        if offset == 0:
            section.append(f"<b>{escape(_order_title(order_rows, display_index))}</b>")
            if not _format_order_date(
                _status_value(
                    order_rows[0],
                    "Время создания заявки",
                    "Дата создания",
                    "created_at",
                )
            ):
                section.append("Дата создания не указана")
            if len(stages) == 1:
                section.append(f"Статус: <b>{escape(stages[0])}</b>")
            if len(deliveries) == 1:
                section.append(f"Дата поставки: <b>{escape(deliveries[0])}</b>")
            section.append("Товары:")
        else:
            section.append("Товары (продолжение):")
        for product, stage, delivery in chunk:
            product_line = f"• {product_name(product)}"
            if len(stages) > 1:
                product_line += f" — {escape(stage)}"
            if len(deliveries) > 1 and delivery:
                product_line += f" ({escape(delivery)})"
            section.append(product_line)
        blocks.append(section)
    return blocks


def _build_order_status_detail_pages(
    rows: list[dict[str, Any]],
    state: Any,
    *,
    display_index: int = 1,
) -> list[str]:
    """Строит страницы, сохраняя границы поставщиков и товаров читаемыми."""
    _, shown = _tracked_status_groups(rows, state)
    if len(shown) != 1:
        return []
    blocks: list[list[str]] = []
    for order_no, order_rows in shown:
        if any(_is_aggregated_status_row(row) for row in order_rows):
            blocks.extend(
                _aggregated_detail_blocks(
                    order_no,
                    order_rows,
                    display_index=display_index,
                )
            )
        else:
            blocks.extend(
                _legacy_detail_blocks(
                    order_no,
                    order_rows,
                    display_index=display_index,
                )
            )
    if not blocks:
        return []
    pages: list[str] = []
    for offset in range(0, len(blocks), _STATUS_DETAIL_BLOCKS_PER_PAGE):
        page_blocks = blocks[offset : offset + _STATUS_DETAIL_BLOCKS_PER_PAGE]
        pages.append(
            f"{heading('Мои заявки')}\n\n" + "\n\n".join("\n".join(block) for block in page_blocks)
        )
    return pages


def order_status_detail_page_count(
    rows: list[dict[str, Any]],
    state: Any,
    *,
    display_index: int = 1,
) -> int:
    """Возвращает число страниц карточки состояния заявки."""
    return max(
        1,
        len(
            _build_order_status_detail_pages(
                rows,
                state,
                display_index=display_index,
            )
        ),
    )


def build_order_status_text(
    rows: list[dict[str, Any]],
    state: Any,
    *,
    detail_page: int = 0,
    display_index: int = 1,
) -> str:
    """Формирует карточку статусов заявок."""
    tracked, shown = _tracked_status_groups(rows, state)
    if not tracked:
        return f"{heading('Мои заявки')}\n\nУ вас пока нет заявок, отправленных через этого бота."
    if not shown:
        return (
            f"{heading('Мои заявки')}\n\n"
            "Статус заявки пока не появился в таблице. Попробуйте обновить позже."
        )

    pages = _build_order_status_detail_pages(rows, state, display_index=display_index)
    if len(pages) > 1:
        page = min(max(0, detail_page), len(pages) - 1)
        return f"{pages[page]}\n\nСтраница {page + 1} из {len(pages)}"

    lines = [heading("Мои заявки"), ""]
    for index, (_order_no, order_rows) in enumerate(shown):
        number = display_index if len(shown) == 1 else index + 1
        lines.append(f"<b>{escape(_order_title(order_rows, number))}</b>")
        if any(_is_aggregated_status_row(row) for row in order_rows):
            _append_aggregated_order_status(lines, order_rows)
        else:
            _append_legacy_order_status(lines, order_rows)
        if index < len(shown) - 1:
            lines.append("")
    return "\n".join(lines)
