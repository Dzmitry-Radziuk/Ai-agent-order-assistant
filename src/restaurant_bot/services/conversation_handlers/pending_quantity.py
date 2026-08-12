"""Обрабатывает короткий ответ количеством на открытой карточке товара."""

from __future__ import annotations

import re
from enum import StrEnum

from restaurant_bot.conversation.routing.item_resolution import has_named_product_items
from restaurant_bot.domain.models import (
    ConversationState,
    InputKind,
    ItemStatus,
    ParsedCommand,
    SessionStage,
    TelegramEvent,
)
from restaurant_bot.services.parser import (
    parse_product_lines,
    parse_quantity_unit,
)
from restaurant_bot.domain.units import UNIT_ALIASES, normalize_unit
from restaurant_bot.services.text import parse_number_words
from restaurant_bot.text_normalization import normalize_text

_SINGLE_CONTAINER_UNITS = {
    "банка",
    "бутылка",
    "пачка",
    "упаковка",
    "коробка",
    "ведро",
    "рулон",
    "пакет",
    "штука",
    "штуку",
    "штуке",
}
_QUANTITY_LEADIN_WORDS = {
    "да",
    "давай",
    "одна",
    "один",
    "одно",
    "одну",
    "возьми",
    "возьмем",
    "закажи",
    "заказать",
    "ладно",
    "мне",
    "нужна",
    "нужен",
    "нужно",
    "ок",
    "окей",
    "поставь",
    "поставить",
    "пусть",
    "тогда",
    "хорошо",
}


class PendingQuantityAction(StrEnum):
    """Сообщает engine следующий детерминированный переход."""

    NOT_HANDLED = "not_handled"
    ADVANCE = "advance"
    CONFIRM_CURRENT = "confirm_current"


class PendingQuantityHandler:
    """Изменяет только открытую позицию с нерешённым количеством."""

    def handle(
        self,
        event: TelegramEvent,
        command: ParsedCommand,
        state: ConversationState,
    ) -> PendingQuantityAction:
        """Применяет короткий ответ и возвращает требуемый переход."""
        item = state.current_item()
        text = event.text or command.text
        if (
            event.input_type == InputKind.CALLBACK
            or item is None
            or item.status
            not in {
                ItemStatus.MISSING_QTY,
                ItemStatus.UNIT_MISMATCH,
                ItemStatus.DUPLICATE_PENDING,
            }
            or state.stage
            not in {
                SessionStage.COLLECTING,
                SessionStage.REVIEW,
                SessionStage.AWAIT_MULTIPLE_QUANTITY,
                SessionStage.AWAIT_UNIT_QUANTITY,
            }
            or not text.strip()
            or self.has_named_product_items(command, text)
        ):
            return PendingQuantityAction.NOT_HANDLED

        quantity, unit = self.spoken_quantity(text)
        if quantity is None:
            return PendingQuantityAction.NOT_HANDLED
        item.quantity = quantity
        item.unit = unit or item.catalog_unit or item.unit
        short_quantity, short_unit = self.spoken_unit_only_quantity(text)
        if (
            item.status == ItemStatus.MISSING_QTY
            and short_quantity is not None
            and short_unit in {"бут", "бан"}
            and item.catalog_unit == "шт"
        ):
            item.unit = item.catalog_unit
        if item.status == ItemStatus.DUPLICATE_PENDING:
            if (
                item.catalog_unit
                and item.unit
                and normalize_unit(item.unit) != normalize_unit(item.catalog_unit)
            ):
                return PendingQuantityAction.ADVANCE
            return PendingQuantityAction.CONFIRM_CURRENT
        if item.catalog_unit and item.unit and item.unit != item.catalog_unit:
            item.status = ItemStatus.UNIT_MISMATCH
            return PendingQuantityAction.ADVANCE
        if item.catalog_product_id or item.catalog_name or item.catalog_unit:
            item.status = ItemStatus.MATCHED
        return PendingQuantityAction.ADVANCE

    @staticmethod
    def spoken_unit_only_quantity(text: str) -> tuple[float | None, str]:
        """Понимает короткое «ладно, бутылка» как одну текущую позицию."""
        words = [
            token
            for token in re.findall(r"[a-zа-яё]+", normalize_text(text), flags=re.I)
            if token not in _QUANTITY_LEADIN_WORDS
        ]
        if len(words) != 1 or words[0] not in _SINGLE_CONTAINER_UNITS:
            return None, ""
        return 1, normalize_unit(words[0])

    @classmethod
    def spoken_quantity(cls, text: str) -> tuple[float | None, str]:
        """Извлекает явно произнесённое количество."""
        quantity, unit = parse_quantity_unit(text)
        if quantity is not None:
            return quantity, unit
        words = [
            cleaned
            for word in normalize_text(text).replace(",", ".").split()
            if (cleaned := re.sub(r"\.+$", "", word))
        ]
        for index in range(len(words)):
            parsed = parse_number_words(words, index)
            if parsed is None:
                continue
            quantity, end = parsed
            unit = (
                normalize_unit(words[end])
                if end < len(words) and words[end] in UNIT_ALIASES
                else ""
            )
            return quantity, unit
        for item in parse_product_lines(text):
            if item.quantity is not None:
                return item.quantity, item.unit
        return cls.spoken_unit_only_quantity(text)

    @staticmethod
    def has_named_product_items(command: ParsedCommand, text: str) -> bool:
        """Отличает товарный запрос от короткого ответа количеством."""
        return has_named_product_items(command, text)
