"""Формирует Telegram-подсказку для подробностей нового товара."""

from __future__ import annotations

from restaurant_bot.domain.models import CartItem
from restaurant_bot.presentation.telegram.formatting import heading, product_name


def product_add_prompt(item: CartItem) -> str:
    """Формирует понятный запрос подробного описания нового товара."""
    return (
        f"✏️ {heading('Опишите товар одним сообщением')}\n\n"
        "Чтобы менеджеру по снабжению было легче найти этот товар, опишите его максимально подробно: "
        "название, бренд, фасовку или объём и другие важные детали.\n\n"
        "Например: <code>Мисо-паста Genzo, 1 кг</code> или "
        "<code>Краб камчатский М/Л, 6 кг</code>.\n\n"
        f"Товар: {product_name(item.source_query)}"
    )
