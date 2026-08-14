"""Формирует Telegram-ответы, кнопки и read-model карточки диалога."""

from __future__ import annotations

from restaurant_bot.conversation.quantity_resolution import multiple_warnings
from restaurant_bot.domain.models import (
    BotReply,
    Button,
    CartItem,
    ConversationState,
    ExtractedItem,
    ItemStatus,
)
from restaurant_bot.domain.units import normalize_unit
from restaurant_bot.orders.package_suggestions import package_count_suggestion
from restaurant_bot.orders.supplier_minimums import supplier_minimum_warnings
from restaurant_bot.presentation.telegram.formatting import (
    escape,
    format_number,
    heading,
    product_name,
)
from restaurant_bot.presentation.telegram.pagination import (
    CART_PAGE_SIZE,
    FINAL_REVIEW_PAGE_SIZE,
    page_count,
)

ISSUE_STATUSES = {
    ItemStatus.DUPLICATE_PENDING,
    ItemStatus.UNIT_MISMATCH,
    ItemStatus.MISSING_QTY,
    ItemStatus.AMBIGUOUS,
    ItemStatus.NOT_FOUND,
}

_NEW_ORDER_MESSAGE = (
    f"🧾 {heading('Новая заявка')}\n\n"
    "Отправьте товары текстом, голосом или фото — я добавлю их в текущий черновик заказа.\n\n"
    "Можно отправить один товар, список или фото заполненной таблицы. "
    "Я распознаю названия, количество и комментарии.\n\n"
    "Когда закончите, скажите «покажи итог» — я покажу заявку для проверки."
)


def _active_items(state: ConversationState) -> list[CartItem]:
    """Возвращает не пропущенные позиции черновика."""
    return [item for item in state.cart if item.status != ItemStatus.SKIPPED]


def _has_draft_content(state: ConversationState) -> bool:
    """Проверяет наличие данных в черновике."""
    return bool(_active_items(state) or state.product_add_requests)


def _request_count(state: ConversationState) -> int:
    """Возвращает количество запросов снабжению."""
    return sum(
        1
        for request in state.product_add_requests
        if request.get("description") and request.get("status") != "cancelled"
    )


def _candidate_button_label(index: int, name: str, max_length: int = 42) -> str:
    """Формирует краткую подпись кандидата для кнопки."""
    prefix = f"{index}. "
    available = max(1, max_length - len(prefix))
    value = name.strip()
    if len(value) <= available:
        return prefix + value
    ellipsis = "..."
    if available <= len(ellipsis):
        return prefix + ellipsis[:available]
    return prefix + value[: available - len(ellipsis)].rstrip() + ellipsis


def _item_name(item: CartItem) -> str:
    """Возвращает отображаемое название позиции."""
    return item.catalog_name or item.source_query


def _item_unit(item: CartItem) -> str:
    """Возвращает отображаемую единицу измерения."""
    return item.unit or item.catalog_unit or ""


def format_item_comment(comment: str) -> str:
    """Формирует курсивную строку комментария под товаром."""
    return f"  <i>Комментарий: {escape(comment)}</i>"


def welcome_reply(
    state: ConversationState,
    *,
    first_contact: bool | None = None,
) -> BotReply:
    """Формирует приветственное сообщение бота."""
    count = len(_active_items(state))
    requests = _request_count(state)
    if first_contact is None:
        first_contact = not bool(state.metadata.get("onboarding_shown"))
    if first_contact or not (count or requests):
        return BotReply(text=_NEW_ORDER_MESSAGE)
    details = "\n".join(
        part
        for part in (
            f"Товаров в черновике: {count}" if count else "",
            f"Запросов снабженцу: {requests}" if requests else "",
        )
        if part
    )
    return BotReply(
        text=f"🧾 <i>Черновик сохранён</i>\n\n{details}",
        rows=[
            [Button(text="Добавить товары", callback_data="v2:add")],
            [Button(text="Показать черновик", callback_data="v2:back")],
        ],
    )


def new_order_confirmation_reply(state: ConversationState) -> BotReply:
    """Просит подтвердить удаление непустого черновика перед новой заявкой."""
    count = len(_active_items(state))
    last_two = count % 100
    last = count % 10
    if last == 1 and last_two != 11:
        item_word = "позиция"
    elif 2 <= last <= 4 and not 12 <= last_two <= 14:
        item_word = "позиции"
    else:
        item_word = "позиций"
    return BotReply(
        text=(
            f"⚠️ {heading('Начать новую заявку?')}\n\n"
            f"В текущем черновике: {count} {item_word}.\n"
            "Если начать новую заявку, текущий черновик будет очищен."
        ),
        rows=[
            [Button(text="Да, начать новую", callback_data="v2:clear")],
            [Button(text="Нет, оставить черновик", callback_data="v2:back")],
        ],
    )


def new_order_started_reply() -> BotReply:
    """Показывает однозначный пустой экран только что начатой заявки."""
    return BotReply(text=_NEW_ORDER_MESSAGE)


def help_reply(state: ConversationState | None = None) -> BotReply:
    """Формирует справку по работе с ботом."""
    rows: list[list[Button]] = []
    if state and _has_draft_content(state):
        rows.append([Button(text="Показать черновик", callback_data="v2:back")])
    rows.extend(
        [
            [Button(text="Добавить товары", callback_data="v2:add")],
            [Button(text="Статус заявок", callback_data="v2:orders")],
        ]
    )
    return BotReply(
        text=f"""ℹ️ {heading("Как пользоваться ботом")}

Отправьте товары текстом, голосом или фото.

Можно:
• добавить или удалить товар;
• изменить количество;
• указать пожелание к товару — напишите его сразу после количества;
• указать общее пожелание — добавьте его в конце сообщения;
• посмотреть черновик;
• отправить готовую заявку.

Пример: <code>Курица 5 кг без кожи, сливки 10 шт. Желательно на завтра, с 9.00 до 14.00</code>

<b>Команды:</b>
/start — начать работу
/help — эта подсказка
/draft — показать черновик
/orders — проверить статус заявок
/reset — очистить черновик
/submit — перейти к проверке заявки""",
        rows=rows,
    )


def thanks_reply(state: ConversationState) -> BotReply:
    """Формирует ответ на благодарность."""
    issues = sum(item.status in ISSUE_STATUSES for item in _active_items(state))
    text = (
        f"<i>Черновик сохранён</i>\n\nНужно уточнить ещё {issues} товар(а)."
        if issues
        else (
            "<i>Черновик сохранён</i>"
            if _active_items(state)
            else "<i>Готово</i>\n\nОтправьте товары текстом, голосом или фото."
        )
    )
    rows = (
        [
            [Button(text="Показать черновик", callback_data="v2:back")],
            [Button(text="Добавить товары", callback_data="v2:add")],
        ]
        if _has_draft_content(state)
        else []
    )
    return BotReply(text=text, rows=rows)


def small_talk_reply(state: ConversationState) -> BotReply:
    """Формирует ответ на бытовую реплику."""
    rows = (
        [
            [
                Button(text="Добавить товары", callback_data="v2:add"),
                Button(text="Показать черновик", callback_data="v2:back"),
            ]
        ]
        if _has_draft_content(state)
        else []
    )
    return BotReply(
        text=f"ℹ️ {heading('Работа с заявкой')}\n\nДобавьте товары, откройте черновик или продолжите текущий шаг.",
        rows=rows,
    )


def unknown_intent_reply(state: ConversationState) -> BotReply:
    """Формирует ответ на неизвестную команду."""
    rows = (
        [
            [
                Button(text="Добавить товары", callback_data="v2:add"),
                Button(text="Показать черновик", callback_data="v2:back"),
            ]
        ]
        if _has_draft_content(state)
        else []
    )
    return BotReply(
        text=f"""⚠️ {heading("К сожалению, мне не удалось распознать сообщение")}

Попробуйте написать или сказать:
• <code>добавь курицу 5 кг</code>;
• <code>убери курицу</code>;
• <code>покажи черновик</code>;
• <code>добавить ещё товары</code>.""",
        rows=rows,
    )


def unrecognized_voice_reply(state: ConversationState) -> BotReply:
    """Формирует ответ на нераспознанное голосовое сообщение."""
    has_draft = _has_draft_content(state)
    return BotReply(
        text=(
            f"⚠️ {heading('К сожалению, мне не удалось распознать голосовое сообщение')}\n\n"
            "Повторите короче или отправьте текстом.\n\n"
            "Пример: <code>сироп роза 3 штуки</code>"
        ),
        rows=[
            [Button(text="Обновить статусы", callback_data="v2:orders")],
            [
                Button(
                    text="Показать черновик" if has_draft else "Добавить товары",
                    callback_data="v2:back" if has_draft else "v2:add",
                )
            ],
        ],
    )


def photo_without_quantities_reply(state: ConversationState) -> BotReply:
    """Объясняет отсутствие заполненных количеств на фотографии."""
    rows = [[Button(text="Черновик", callback_data="v2:cart")]] if _has_draft_content(state) else []
    return BotReply(
        text=(
            f"📷 {heading('Не нашёл заполненных количеств')}\n\n"
            "Фасовку и справочные значения я не добавляю в заявку. "
            "Отправьте фото, на котором видно напечатанное или рукописное количество заказа."
        ),
        rows=rows,
    )


def empty_draft_reply() -> BotReply:
    """Формирует ответ для пустого черновика."""
    return BotReply(
        text=f"🧾 {heading('Черновик пуст')}\n\nОтправьте товары текстом, голосом или фото.",
        rows=[[Button(text="Начать", callback_data="v2:add")]],
    )


def added_items_question_reply(_state: ConversationState, added_count: int) -> BotReply:
    """Подтверждает добавление без привязки к асинхронно обработанной позиции."""
    title = (
        "Товар добавлен в черновик заказа"
        if added_count == 1
        else "Товары добавлены в черновик заказа"
    )
    lines = [heading(title), "", "Добавить ещё товары?"]
    return BotReply(
        text="\n".join(lines),
        rows=[
            [Button(text="Да, добавить товары", callback_data="v2:add")],
            [Button(text="Нет, к черновику", callback_data="v2:back")],
        ],
    )


def comment_scope_clarification_reply(
    items: list[ExtractedItem],
    comment: str,
) -> BotReply:
    """Просит безопасно выбрать товары для неоднозначного комментария."""
    visible_items = items[:10]
    lines = [
        f"⚠️ {heading('Уточните комментарий')}",
        "",
        f"К каким товарам относится: <i>{escape(comment)}</i>",
        "",
    ]
    lines.extend(
        f"{index}. {product_name(item.product_query)}"
        for index, item in enumerate(visible_items, start=1)
    )
    if len(items) > len(visible_items):
        lines.append(f"…и ещё {len(items) - len(visible_items)} позиций")
    lines.extend(
        [
            "",
            "Ответьте обычной фразой, например: <code>для всех товаров</code>, "
            "<code>только для огурцов</code>, <code>для второго товара</code> или "
            "<code>для всей заявки</code>.",
        ]
    )
    rows = [[Button(text="Для всех этих товаров", callback_data="v2:comment:all")]]
    if len(items) > 1:
        rows.append([Button(text="Только для последнего", callback_data="v2:comment:last")])
    rows.extend(
        [
            [Button(text="Для всей заявки", callback_data="v2:comment:order")],
            [Button(text="Не добавлять комментарий", callback_data="v2:comment:cancel")],
        ]
    )
    return BotReply(text="\n".join(lines), rows=rows)


def no_current_manual_reply() -> BotReply:
    """Формирует подсказку при отсутствии выбранной позиции."""
    return BotReply(
        text=f"ℹ️ {heading('Нет товара для изменения')}\n\nОткройте черновик или добавьте новый товар.",
        rows=[
            [Button(text="Показать черновик", callback_data="v2:back")],
            [Button(text=" Добавить еще товары", callback_data="v2:add")],
        ],
    )


def submission_retry_reply(order_no: str) -> BotReply:
    """Формирует карточку повторной отправки."""
    return BotReply(
        text=(
            f"⚠️ {heading('Отправка не завершена')}\n\n"
            f"Заявка: {escape(order_no)}\n\n"
            "Нажмите «Повторить отправку». Уже выполненные этапы будут пропущены."
        ),
        rows=[
            [Button(text="Повторить отправку", callback_data="v2:submit")],
            [Button(text="К черновику", callback_data="v2:back")],
        ],
    )


def product_add_requests_reply(state: ConversationState) -> BotReply:
    """Формирует список запросов на новые товары."""
    requests = [
        request
        for request in state.product_add_requests
        if request.get("description") and request.get("status") != "cancelled"
    ]
    if not requests:
        return BotReply(
            text="Запросов снабженцу пока нет.",
            rows=[[Button(text="К черновику", callback_data="v2:back")]],
        )
    labels = {
        "submitted": "передано снабженцу",
        "write_failed": "не отправлено — можно повторить",
        "write_uncertain": "не удалось подтвердить отправку — сообщите менеджеру",
        "pending_write": "отправляется",
        "retry_pending": "повторно отправляется",
        "draft": "ожидает отправки",
    }
    lines = [f"📩 {heading('Запросы снабженцу')}", ""]
    rows: list[list[Button]] = []
    for number, request in enumerate(requests[:20], start=1):
        lines += [
            f"{number}. {product_name(str(request['description']))}",
            f"Статус: {labels.get(str(request.get('status')), 'сохранён')}",
        ]
        if number < min(len(requests), 20):
            lines.append("")
        if request.get("status") == "write_failed" and request.get("request_id"):
            rows.append(
                [
                    Button(
                        text=f"Повторить запрос №{number}",
                        callback_data=f"v2:addreqretry:{request['request_id']}",
                    )
                ]
            )
    if len(requests) > 20:
        lines += ["", f"…и ещё {len(requests) - 20}."]
    rows.append([Button(text="К черновику", callback_data="v2:back")])
    return BotReply(text="\n".join(lines), rows=rows)


def cart_reply(
    state: ConversationState,
    title: str = "Черновик заявки",
    notice: str = "",
) -> BotReply:
    """Формирует карточку черновика заявки."""
    items = _active_items(state)
    issues = [item for item in items if item.status in ISSUE_STATUSES]
    ready = [item for item in items if item.status not in ISSUE_STATUSES]
    paginated = len(items) > CART_PAGE_SIZE
    total_pages = page_count(len(items), CART_PAGE_SIZE)
    page = min(max(0, state.cart_page), total_pages - 1)
    if paginated:
        page_items = (ready + issues)[page * CART_PAGE_SIZE : (page + 1) * CART_PAGE_SIZE]
        page_ready = [item for item in page_items if item.status not in ISSUE_STATUSES]
        page_issues = [item for item in page_items if item.status in ISSUE_STATUSES]
    else:
        page_ready = ready
        page_issues = issues
    lines = [f"🧾 {heading(title)}", ""]
    if notice:
        lines.extend([f"<i>{escape(notice)}</i>", ""])
    if paginated:
        lines.extend([f"Страница {page + 1} из {total_pages}", ""])
    if not ready and not issues:
        lines.append("Товаров пока нет.")
    for item in page_ready if paginated else ready[:25]:
        quantity = (
            f"{format_number(item.quantity)} {escape(_item_unit(item))}"
            if item.quantity
            else "количество не указано"
        )
        lines.append(f"• {product_name(_item_name(item))} — {quantity}")
        if item.comment:
            lines.append(format_item_comment(item.comment))
    if not paginated and len(ready) > 25:
        lines.append(f"…и ещё {len(ready) - 25} поз.")
    if issues:
        if lines[-1] != "":
            lines.append("")
        lines.append(f"⚠️ {heading('Нужно уточнить')}")
        for item in page_issues if paginated else issues[:25]:
            if item.status == ItemStatus.UNIT_MISMATCH:
                lines.append(
                    f"• {product_name(_item_name(item))} — вы указали {format_number(item.quantity)} {escape(item.unit)}; в каталоге заказ в {escape(item.catalog_unit)}"
                )
            elif item.status == ItemStatus.MISSING_QTY:
                lines.append(f"• {product_name(_item_name(item))} — укажите количество")
            else:
                quantity = (
                    f" — {format_number(item.quantity)} {escape(_item_unit(item))}"
                    if item.quantity
                    else ""
                )
                lines.append(f"• {product_name(_item_name(item))}{quantity}")
            if item.comment:
                lines.append(format_item_comment(item.comment))
        if not paginated and len(issues) > 25:
            lines.append(f"…и ещё {len(issues) - 25} поз.")
    request_count = _request_count(state)
    if request_count:
        if lines[-1] != "":
            lines.append("")
        lines += [
            f"📩 {heading(f'Запросы снабженцу: {request_count}')}",
            "Откройте отдельный список кнопкой ниже.",
        ]
    rows: list[list[Button]] = []
    if paginated:
        navigation: list[Button] = []
        if page > 0:
            navigation.append(Button(text="← Назад", callback_data=f"v2:cartpage:{page - 1}"))
        if page < total_pages - 1:
            navigation.append(Button(text="Далее →", callback_data=f"v2:cartpage:{page + 1}"))
        if navigation:
            rows.append(navigation)
    if issues:
        count = len(issues)
        mod10, mod100 = count % 10, count % 100
        word = (
            "товар"
            if mod10 == 1 and mod100 != 11
            else "товара"
            if mod10 in {2, 3, 4} and mod100 not in {12, 13, 14}
            else "товаров"
        )
        rows.append([Button(text=f"Уточнить {count} {word}", callback_data="v2:resolve")])
    else:
        rows.append([Button(text="Отправить в корзину", callback_data="v2:cart")])
    rows.append([Button(text="Добавить ещё товары", callback_data="v2:add")])
    if request_count:
        rows.append(
            [Button(text=f"Запросы снабженцу · {request_count}", callback_data="v2:addreqlist")]
        )
    rows.append([Button(text="Сбросить и начать заново", callback_data="v2:clear")])
    return BotReply(text="\n".join(lines).strip(), rows=rows)


def product_add_sending_reply() -> BotReply:
    """Показывает процесс записи нового товара."""
    return BotReply(text="<b>Отправляю запрос менеджеру…</b>")


def issue_reply(item: CartItem, item_index: int | None = None) -> BotReply:
    """Формирует карточку проблемы товарной позиции."""
    index = 0 if item_index is None else item_index
    name = product_name(_item_name(item))
    if item.status == ItemStatus.MISSING_QTY:
        return BotReply(
            text=f"✏️ {heading('Укажите количество')}\n\n{name}\n\nОтправьте число текстом или голосом.\n\nПример: <code>5</code>",
            rows=[[Button(text="Не добавлять", callback_data=f"v2:skip:{index}")]],
        )
    if item.status == ItemStatus.UNIT_MISMATCH:
        if item.duplicate_existing_quantity is not None and item.issue_message:
            existing_unit = item.duplicate_existing_unit or item.catalog_unit or "шт"
            incoming_unit = item.unit or item.catalog_unit or existing_unit
            return BotReply(
                text=(
                    f"⚠️ {heading('Товар уже есть в черновике')}\n\n{name}\n\n"
                    f"В черновике: <b>{format_number(item.duplicate_existing_quantity)} "
                    f"{escape(existing_unit)}</b>\n"
                    f"Вы указали: <b>{format_number(item.quantity)} "
                    f"{escape(incoming_unit)}</b>.\n\n"
                    f"Этот товар заказывается <b>в {escape(item.catalog_unit)}</b>.\n"
                    f"Чтобы объединить позиции, укажите количество в {escape(item.catalog_unit)}."
                ),
                rows=[
                    [
                        Button(
                            text=f"Ввести количество в {item.catalog_unit}",
                            callback_data=f"v2:unitedit:{index}",
                        )
                    ],
                    [Button(text="Не добавлять повторно", callback_data=f"v2:skip:{index}")],
                ],
            )
        unit_rows: list[list[Button]] = []
        package_suggestion = package_count_suggestion(item)
        if package_suggestion is not None:
            count, approximate_quantity, approximate_unit = package_suggestion
            unit_rows.append(
                [
                    Button(
                        text=(
                            f"Заказать {count} {item.catalog_unit} "
                            f"(≈ {format_number(approximate_quantity)} {approximate_unit})"
                        )[:64],
                        callback_data=f"v2:qty:{item.id}:{count}",
                    )
                ]
            )
        unit_rows.extend(
            [
                [
                    Button(
                        text=f"Ввести количество в {item.catalog_unit}",
                        callback_data=f"v2:unitedit:{index}",
                    )
                ],
                [Button(text="Не добавлять", callback_data=f"v2:skip:{index}")],
            ]
        )
        return BotReply(
            text=(
                f"⚠️ {heading('Уточните количество')}\n\n{name}\n\n"
                f"Вы указали: <b>{format_number(item.quantity)} {escape(item.unit)}</b>.\n"
                f"Этот товар заказывается <b>в {escape(item.catalog_unit)}</b>.\n\n"
                f"Выберите вариант или напишите, сколько {escape(item.catalog_unit)} нужно."
            ),
            rows=unit_rows,
        )
    if item.status == ItemStatus.DUPLICATE_PENDING:
        unit = _item_unit(item) or item.duplicate_existing_unit or "шт"
        existing_quantity = item.duplicate_existing_quantity
        if (
            item.catalog_unit
            and item.unit
            and normalize_unit(item.unit) != normalize_unit(item.catalog_unit)
        ):
            existing_unit = item.duplicate_existing_unit or item.catalog_unit
            return BotReply(
                text=(
                    f"⚠️ {heading('Товар уже есть в черновике')}\n\n{name}\n\n"
                    f"В черновике: <b>{format_number(existing_quantity)} "
                    f"{escape(existing_unit)}</b>\n"
                    f"Вы указали: <b>{format_number(item.quantity)} "
                    f"{escape(item.unit)}</b>.\n\n"
                    f"Этот товар заказывается <b>в {escape(item.catalog_unit)}</b>.\n"
                    f"Чтобы объединить позиции, укажите количество в {escape(item.catalog_unit)}."
                ),
                rows=[
                    [
                        Button(
                            text=f"Ввести количество в {item.catalog_unit}",
                            callback_data=f"v2:unitedit:{index}",
                        )
                    ],
                    [Button(text="Не добавлять повторно", callback_data=f"v2:skip:{index}")],
                ],
            )
        if not item.quantity:
            return BotReply(
                text=(
                    f"⚠️ {heading('Товар уже есть в черновике')}\n\n{name}\n"
                    f"В черновике: {format_number(existing_quantity)} {escape(unit)}\n\n"
                    "Напишите или скажите голосом, сколько добавить. Если повторно добавлять товар не нужно — нажмите на кнопку ниже."
                ),
                rows=[[Button(text="Не добавлять повторно", callback_data=f"v2:skip:{index}")]],
            )
        total = existing_quantity + item.quantity
        return BotReply(
            text=(
                f"⚠️ {heading('Товар уже есть в черновике')}\n\n{name}\n\n"
                f"В черновике: {format_number(existing_quantity)} {escape(unit)}\n"
                f"Вы добавляете: {format_number(item.quantity)} {escape(unit)}\n"
                f"После добавления будет: {format_number(total)} {escape(unit)}"
            ),
            rows=[
                [
                    Button(
                        text=f"Добавить — будет {format_number(total)} {unit}",
                        callback_data=f"v2:dupmerge:{index}:0",
                    )
                ],
                [Button(text="Не добавлять повторно", callback_data=f"v2:skip:{index}")],
            ],
        )
    if item.status == ItemStatus.AMBIGUOUS:
        if len(item.candidates) == 1:
            lines = [
                f"🔎 {heading('Точного совпадения не найдено')}",
                "",
                f"По запросу «{product_name(item.source_query)}» найден похожий товар.",
                "",
                "Возможно, вы имели в виду:",
                "",
            ]
        else:
            lines = [
                f"По запросу «{product_name(item.source_query)}» найдено несколько вариантов.",
                "",
                "Уточните, какой товар вы имели в виду:",
                "",
            ]
        rows: list[list[Button]] = []
        for candidate_index, candidate in enumerate(item.candidates[:5]):
            lines.append(f"{candidate_index + 1}. {product_name(candidate.name)}")
            if candidate_index < len(item.candidates[:5]) - 1:
                lines.append("")
            rows.append(
                [
                    Button(
                        text=_candidate_button_label(candidate_index + 1, candidate.name),
                        callback_data=f"v2:sel:{index}:{candidate_index}",
                    )
                ]
            )
        lines += [
            "",
            "Не нашли нужный вариант? Отправьте запрос менеджеру по снабжению.",
        ]
        rows += [
            [Button(text="Отправить запрос снабженцу", callback_data=f"v2:addreq:{index}")],
            [Button(text="Изменить название", callback_data=f"v2:rename:{index}")],
            [Button(text="Не добавлять", callback_data=f"v2:skip:{index}")],
        ]
        return BotReply(text="\n".join(lines).strip(), rows=rows)
    if item.status == ItemStatus.NOT_FOUND and item.supplier_search_locked and item.supplier_hint:
        lines = [
            f"⚠️ {heading('Товар не найден у выбранного поставщика')}",
            "",
            f"Поставщик: <b>{escape(item.supplier_hint)}</b>",
            f"По запросу: {escape(item.source_query)}",
        ]
        supplier_rows: list[list[Button]] = []
        suggestions = item.candidates[:5]
        if suggestions:
            lines += ["", "<b>Похожие товары у других поставщиков:</b>", ""]
            for candidate_index, candidate in enumerate(suggestions):
                lines.append(f"{candidate_index + 1}. {product_name(candidate.name)}")
                if candidate.supplier:
                    lines.append(f"   Поставщик: {escape(candidate.supplier)}")
                if candidate_index < len(suggestions) - 1:
                    lines.append("")
                supplier_rows.append(
                    [
                        Button(
                            text=_candidate_button_label(candidate_index + 1, candidate.name),
                            callback_data=f"v2:sel:{index}:{candidate_index}",
                        )
                    ]
                )
        else:
            lines += ["", "Введите другое название или выполните поиск у всех поставщиков."]
        supplier_rows += [
            [Button(text="Ввести другое название", callback_data=f"v2:rename:{index}")],
            [Button(text="Искать у всех поставщиков", callback_data=f"v2:searchall:{index}")],
            [Button(text="Выбрать другого поставщика", callback_data=f"v2:switchsupplier:{index}")],
            [Button(text="Не добавлять", callback_data=f"v2:skip:{index}")],
        ]
        return BotReply(text="\n".join(lines), rows=supplier_rows)
    if item.status == ItemStatus.NOT_FOUND and item.rename_attempted:
        return BotReply(
            text=(
                f"⚠️ {heading('Товар не обнаружен в вашем списке товаров')}\n\n"
                f"• Название товара: {product_name(item.source_query)}\n\n"
                "Отправить заявку менеджеру по снабжению АвтоСнаб на добавление этого товара "
                "в ваш список товаров (таблицу)?"
            ),
            rows=[
                [Button(text="Да, отправить", callback_data=f"v2:addreq:{index}")],
                [Button(text="Нет", callback_data=f"v2:addreqskip:{index}")],
            ],
        )
    return BotReply(
        text=(
            f"⚠️ {heading('Товар не найден')}\n\n"
            f"По вашему запросу «{product_name(item.source_query)}» ничего не найдено.\n\n"
            "Вы можете изменить название, отправить запрос менеджеру по снабжению "
            "или не добавлять товар."
        ),
        rows=[
            [Button(text="Отправить запрос снабженцу", callback_data=f"v2:addreq:{index}")],
            [Button(text="Изменить название", callback_data=f"v2:rename:{index}")],
            [Button(text="Не добавлять", callback_data=f"v2:skip:{index}")],
        ],
    )


def final_review_reply(state: ConversationState) -> BotReply:
    """Формирует карточку финальной проверки."""
    items = _active_items(state)
    total_pages = page_count(len(items), FINAL_REVIEW_PAGE_SIZE)
    page = min(max(0, state.final_review_page), total_pages - 1)
    page_items = items[page * FINAL_REVIEW_PAGE_SIZE : (page + 1) * FINAL_REVIEW_PAGE_SIZE]
    paginated = total_pages > 1
    lines = [f"📦 {heading('Финальная проверка')}", ""]
    if paginated:
        lines.extend([f"Страница {page + 1} из {total_pages}", ""])
    for index, item in enumerate(
        page_items,
        start=page * FINAL_REVIEW_PAGE_SIZE + 1,
    ):
        lines.append(
            f"{index}. {product_name(_item_name(item))} — {format_number(item.quantity)} {escape(_item_unit(item) or 'шт')}"
        )
        if item.comment:
            lines.append(format_item_comment(item.comment))
    multiple = multiple_warnings(state)
    warnings = supplier_minimum_warnings(state)
    rows: list[list[Button]] = []
    if paginated:
        navigation: list[Button] = []
        if page > 0:
            navigation.append(Button(text="← Назад", callback_data=f"v2:finalpage:{page - 1}"))
        if page < total_pages - 1:
            navigation.append(Button(text="Далее →", callback_data=f"v2:finalpage:{page + 1}"))
        if navigation:
            rows.append(navigation)
    if len(multiple) == 1:
        item = multiple[0]
        unit = escape(_item_unit(item) or "шт")
        batch = format_number(item.minimum_multiple)
        current = format_number(item.quantity)
        suggested = format_number(item.suggested_quantity)
        lines += [
            "",
            f"⚠️ {heading('Проверьте количество')}",
            product_name(_item_name(item)),
            (
                f"Этот товар заказывают партиями по <b>{batch} {unit}</b>."
                if item.minimum_multiple
                else "Для этого товара доступны определённые варианты количества."
            ),
            f"Вы указали: <b>{current} {unit}</b>.",
            f"Ближайший подходящий вариант: <b>{suggested} {unit}</b>.",
        ]
        rows += [
            [Button(text="Выбрать количество", callback_data="v2:mulone")],
            [Button(text=f"Оставить {current} {unit}", callback_data="v2:keepwarn")],
        ]
    elif len(multiple) > 1:
        lines += [
            "",
            f"⚠️ {heading(f'Проверьте количество у {len(multiple)} товаров')}",
            "Эти товары заказывают партиями определённого размера.",
            "Выберите количество для каждого товара.",
        ]
        rows += [
            [Button(text="Выбрать количество", callback_data="v2:mulone")],
        ]
    else:
        if warnings:
            lines += ["", f"⚠️ {heading('Минимальная сумма поставщика')}"]
            lines += [
                f"{escape(warning.supplier)}: {format_number(warning.current_amount + warning.added_amount)} ₽ из {format_number(warning.minimum_amount)} ₽"
                for warning in warnings
            ]
            rows.append(
                [
                    Button(
                        text="Показать товары поставщика"
                        if len(warnings) == 1
                        else "Проверить минимальные суммы",
                        callback_data="v2:minsum",
                    )
                ]
            )
        rows.append([Button(text="Отправить в таблицу заказа", callback_data="v2:submit")])
    rows.append([Button(text="К черновику", callback_data="v2:back")])
    return BotReply(text="\n".join(lines), rows=rows)


def multiple_quantity_choice_reply(item: CartItem) -> BotReply:
    """Предлагает понятные варианты количества для одного товара."""
    unit = escape(_item_unit(item) or "шт")
    current = format_number(item.quantity)
    suggested = format_number(item.suggested_quantity)
    batch = format_number(item.minimum_multiple)
    lines = [
        f"⚖️ {heading('Выберите количество')}",
        "",
        product_name(_item_name(item)),
        (
            f"Этот товар заказывают партиями по <b>{batch} {unit}</b>."
            if item.minimum_multiple
            else "Для этого товара доступны определённые варианты количества."
        ),
        f"Вы указали: <b>{current} {unit}</b>.",
        f"Ближайший подходящий вариант: <b>{suggested} {unit}</b>.",
        "",
        "Как поступить?",
    ]
    return BotReply(
        text="\n".join(lines),
        rows=[
            [
                Button(
                    text=f"Выбрать {suggested} {unit}",
                    callback_data="v2:accept_multiple",
                )
            ],
            [Button(text="Ввести другое количество", callback_data="v2:enter_quantity")],
            [
                Button(
                    text=f"Оставить {current} {unit}",
                    callback_data="v2:keep_current",
                )
            ],
            [Button(text="К финальной проверке", callback_data="v2:final_review")],
        ],
    )


def supplier_warning_details_reply(state: ConversationState) -> BotReply:
    """Формирует подробности минимума поставщика."""
    warnings = supplier_minimum_warnings(state)
    if not warnings:
        return BotReply(
            text="<i>Минимальная сумма набрана</i>",
            rows=[[Button(text="К финальной проверке", callback_data="v2:cart")]],
        )
    lines = [f"⚠️ {heading('Минимальная сумма не набрана')}"]
    for warning in warnings[:8]:
        lines += [
            "",
            f"<b>Поставщик: {escape(warning.supplier)}</b>",
            "Товары в этой заявке:",
        ]
        for item in warning.items[:8]:
            lines.append(
                f"• {product_name(_item_name(item))} — {format_number(item.quantity)} {escape(_item_unit(item) or 'шт')} · {format_number(item.amount)} ₽"
            )
        lines += [
            f"В заказе этого поставщика: {format_number(warning.current_amount + warning.added_amount)} ₽ из {format_number(warning.minimum_amount)} ₽",
            f"<b>Не хватает: {format_number(warning.missing_amount)} ₽</b>",
        ]
    lines += ["", "Можно добавить товары этого поставщика или отправить заявку как есть."]
    rows = (
        [
            [
                Button(
                    text=(f"Добавить товары: {warnings[0].supplier}")[:62],
                    callback_data="v2:minsumadd:0",
                )
            ]
        ]
        if len(warnings) == 1
        else [[Button(text="Выбрать поставщика", callback_data="v2:minsumchoose")]]
    )
    rows += [
        [Button(text="Отправить как есть", callback_data="v2:submit")],
        [Button(text="К финальной проверке", callback_data="v2:cart")],
    ]
    return BotReply(text="\n".join(lines), rows=rows)


def supplier_warning_choose_reply(state: ConversationState) -> BotReply:
    """Формирует выбор поставщика с недобранным минимумом."""
    warnings = supplier_minimum_warnings(state)
    if not warnings:
        return BotReply(
            text="<i>Минимальная сумма набрана</i>\n\nДополнительная проверка не требуется.",
            rows=[[Button(text="К финальной проверке", callback_data="v2:cart")]],
        )
    rows = [
        [
            Button(
                text=f"{warning.supplier} · не хватает {format_number(warning.missing_amount)} ₽"[
                    :62
                ],
                callback_data=f"v2:minsumadd:{index}",
            )
        ]
        for index, warning in enumerate(warnings[:10])
    ]
    rows += [
        [Button(text="К минимальной сумме", callback_data="v2:minsum")],
        [Button(text="К финальной проверке", callback_data="v2:cart")],
    ]
    return BotReply(
        text=f"🚚 {heading('Выберите поставщика')}\n\nСледующие товары будут искаться только у выбранного поставщика.",
        rows=rows,
    )


def start_adding_supplier_reply(supplier: str) -> BotReply:
    """Формирует сообщение для добавления товаров выбранного поставщика."""
    return BotReply(
        text=f"🚚 {heading('Добавьте товары поставщика')}\n\nПоставщик: {escape(supplier)}\n\nОтправьте товары текстом, голосом или фото. Поиск будет выполнен только у этого поставщика.",
        rows=[
            [Button(text="Выбрать другого поставщика", callback_data="v2:minsumchoose")],
            [Button(text="Показать черновик", callback_data="v2:back")],
            [Button(text="К минимальной сумме", callback_data="v2:minsum")],
        ],
    )
