"""Формирует Telegram-представление «order review»."""

from __future__ import annotations

from restaurant_bot.application.order_review.contracts import ReviewItem, ReviewSnapshot
from restaurant_bot.domain.models import BotReply, Button
from restaurant_bot.presentation.telegram.formatting import escape
from restaurant_bot.presentation.telegram.replies import format_item_comment


def format_quantity(value: float) -> str:
    """Форматирует количество без лишних нулей."""
    return str(int(value)) if value.is_integer() else f"{value:.3f}".rstrip("0").rstrip(".")


def preview_reply(
    snapshot: ReviewSnapshot,
    token: str,
    *,
    changed: bool = False,
    edit_message_id: int | None = None,
) -> BotReply:
    """Формирует Telegram-карточку проверки заявки с кнопками."""
    if not snapshot.items:
        return BotReply(
            text=(
                "🛒 <b>Текущая заявка пуста</b>\n\n"
                "В таблице пока нет товаров с указанным количеством."
            ),
            rows=[[Button(text="↩️ Закрыть", callback_data=f"v2:review_cancel:{token}")]],
            edit_message_id=edit_message_id,
        )
    lines = [
        "🛒 <b>Проверьте текущую заявку</b>",
        f"Заведение: {escape(snapshot.venue_name)}",
        f"Товаров: {len(snapshot.items)}",
        f"Поставщиков: {snapshot.supplier_count}",
    ]
    if changed:
        lines += ["", "⚠️ Таблица изменилась. Проверьте обновлённый состав заявки."]
    lines += ["", "<b>Товары по поставщикам:</b>"]
    grouped: dict[str, list[ReviewItem]] = {}
    for item in snapshot.items:
        supplier = item.supplier.strip() or "Поставщик не указан"
        grouped.setdefault(supplier, []).append(item)

    rendered_count = 0
    truncated = False
    for supplier, items in grouped.items():
        supplier_header = f"<b>{escape(supplier)}</b>"
        for item_index, item in enumerate(items):
            item_lines = [
                f"• {escape(item.name)} — {format_quantity(item.quantity)} {escape(item.unit)}"
            ]
            if item.comment:
                item_lines.append(format_item_comment(item.comment))
            item_block = "\n".join(item_lines)
            prefix = supplier_header if item_index == 0 else ""
            separator = "\n\n" if prefix else "\n"
            candidate = (
                "\n".join(lines) + separator + (f"{prefix}\n{item_block}" if prefix else item_block)
            )
            if len(candidate) > 3600:
                truncated = True
                break
            if prefix:
                lines.extend(["", prefix])
            lines.extend(item_lines)
            rendered_count += 1
        if truncated:
            break
    if truncated:
        lines.append(
            f"… и ещё {len(snapshot.items) - rendered_count} поз. "
            "Все товары повторно проверяются перед отправкой."
        )
    rows = [
        [Button(text="✅ Отправить заявку", callback_data=f"v2:review_submit:{token}")],
        [Button(text="↩️ Отмена", callback_data=f"v2:review_cancel:{token}")],
    ]
    return BotReply(text="\n".join(lines), rows=rows, edit_message_id=edit_message_id)


def submission_disabled_reply(venue_code: str, edit_message_id: int | None = None) -> BotReply:
    """Формирует ответ при отключённой внешней отправке заявки."""
    return BotReply(
        text=(
            "ℹ️ <b>Отправка пока отключена</b>\n\n"
            "Заявка проверена, но поставщикам ничего не отправлено. "
            "Данные таблицы не изменены."
        ),
        rows=[
            [
                Button(
                    text="🔄 Обновить заявку",
                    callback_data=f"v2:review:{venue_code}",
                )
            ]
        ],
        edit_message_id=edit_message_id,
    )


def submission_failure_reply(token: str, edit_message_id: int | None = None) -> BotReply:
    """Формирует ответ о временной ошибке отправки заявки."""
    return BotReply(
        text=(
            "⚠️ <b>Не удалось отправить заявку</b>\n\nТаблица не изменена. Попробуйте ещё раз позже."
        ),
        rows=[
            [
                Button(
                    text="🔄 Повторить",
                    callback_data=f"v2:review_submit:{token}",
                )
            ]
        ],
        edit_message_id=edit_message_id,
    )


def submission_success_reply(
    order_number: str,
    base_rows: int,
    edit_message_id: int | None = None,
) -> BotReply:
    """Формирует ответ об успешной отправке заявки."""
    return BotReply(
        text=(
            f"<i>Заявка отправлена</i>\n\nНомер заявки: "
            f"{escape(order_number)}\nТоваров: {base_rows}"
        ),
        edit_message_id=edit_message_id,
    )
