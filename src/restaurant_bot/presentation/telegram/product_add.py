"""Формирует Telegram-подсказку для подробностей нового товара."""

from __future__ import annotations

from restaurant_bot.domain.models import CartItem


def product_add_prompt(item: CartItem) -> str:
    """Формирует понятный запрос подробного описания нового товара."""
    return (
        "✏️ <b>Опишите товар одним сообщением</b>\n\n"
        "Чтобы менеджеру по снабжению было легче найти этот товар, опишите его максимально подробно: "
        "название, бренд, фасовку или объём и другие важные детали.\n\n"
        "Например: <code>Мисо-паста Genzo, 1 кг</code> или "
        "<code>Краб камчатский М/Л, 6 кг</code>.\n\n"
        f"Товар: {item.source_query}"
    )
