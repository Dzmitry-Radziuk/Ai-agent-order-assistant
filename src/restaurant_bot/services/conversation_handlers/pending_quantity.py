"""Обрабатывает короткий ответ количеством на открытой карточке товара."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from restaurant_bot.application.conversation.contracts import ConversationInteraction
from restaurant_bot.conversation.routing.item_resolution import has_named_product_items
from restaurant_bot.domain.models import (
    CartItem,
    ConversationState,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    SessionStage,
)
from restaurant_bot.domain.text import normalize_text
from restaurant_bot.domain.units import UNIT_ALIASES, normalize_unit
from restaurant_bot.parsing.number_words import NUMBER_WORDS, parse_number_words
from restaurant_bot.parsing.products import parse_product_lines

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
_QUANTITY_PREFIX_WORDS = frozenset(
    {
        "да",
        "давай",
        "добавь",
        "добавить",
        "возьми",
        "возьмем",
        "закажи",
        "заказать",
        "измени",
        "изменить",
        "ладно",
        "мне",
        "на",
        "нужно",
        "нужна",
        "нужны",
        "ок",
        "окей",
        "поставь",
        "поставить",
        "пусть",
        "тогда",
        "укажи",
        "указать",
        "хорошо",
        "хочу",
        "будет",
        "в",
        "введи",
        "ввести",
        "количество",
        "вес",
    }
)


class PendingQuantityAction(StrEnum):
    """Сообщает engine следующий детерминированный переход."""

    NOT_HANDLED = "not_handled"
    ADVANCE = "advance"
    CONFIRM_CURRENT = "confirm_current"


class QuantityReplyStatus(StrEnum):
    """Описывает результат проверки ответа на вопрос о количестве."""

    NO_MATCH = "no_match"
    HANDLED = "handled"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class QuantityReply:
    """Хранит безопасное свидетельство количества для открытой позиции."""

    status: QuantityReplyStatus
    quantity: float | None = None
    unit: str = ""


class PendingQuantityHandler:
    """Изменяет только открытую позицию с нерешённым количеством."""

    def handle(
        self,
        event: ConversationInteraction,
        command: ParsedCommand,
        state: ConversationState,
    ) -> PendingQuantityAction:
        """Применяет короткий ответ и возвращает требуемый переход."""
        item = state.current_item()
        text = event.text or command.text
        if (
            event.kind == InputKind.CALLBACK
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
        previous_quantity = item.quantity
        item.quantity = quantity
        item.quantity_user_edited = True
        state.refresh_department_after_quantity_change(
            item,
            previous_quantity=previous_quantity,
        )
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

    @classmethod
    def modal_reply(cls, text: str, state: ConversationState) -> QuantityReply:
        """Проверяет ответ только при открытом вопросе о количестве."""
        item = state.current_item()
        if (
            item is None
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
        ):
            return QuantityReply(QuantityReplyStatus.NO_MATCH)

        quantity, unit, uncertain = cls._strict_quantity(text)
        if quantity is not None:
            return QuantityReply(QuantityReplyStatus.HANDLED, quantity, unit)
        if uncertain:
            return QuantityReply(QuantityReplyStatus.AMBIGUOUS)
        return QuantityReply(QuantityReplyStatus.NO_MATCH)

    @classmethod
    def modal_command(cls, text: str, state: ConversationState) -> ParsedCommand | None:
        """Возвращает локальную команду количества до глобального маршрутизатора."""
        reply = cls.modal_reply(text, state)
        if reply.status is QuantityReplyStatus.HANDLED:
            item = state.current_item()
            assert item is not None
            return ParsedCommand(
                intent=Intent.EDIT_QUANTITY,
                text=text,
                edit_quantity=reply.quantity,
                edit_unit=reply.unit or item.catalog_unit or item.unit,
            )
        if reply.status is QuantityReplyStatus.AMBIGUOUS:
            return ParsedCommand(intent=Intent.UNKNOWN, text=text)
        return None

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
        reply = cls.modal_reply(
            text,
            ConversationState(
                stage=SessionStage.AWAIT_UNIT_QUANTITY,
                current_issue_item_id="quantity",
                cart=[
                    CartItem(
                        id="quantity",
                        source_query="",
                        status=ItemStatus.MISSING_QTY,
                    )
                ],
            ),
        )
        if reply.status is QuantityReplyStatus.HANDLED:
            return reply.quantity, reply.unit
        for item in parse_product_lines(text):
            if item.quantity is not None:
                return item.quantity, item.unit
        return cls.spoken_unit_only_quantity(text)

    @classmethod
    def _strict_quantity(cls, text: str) -> tuple[float | None, str, bool]:
        """Извлекает количество без поиска первого числа в произвольной фразе."""
        normalized = normalize_text(text).replace(",", ".").strip(" .;:!?—–-")
        if not normalized:
            return None, "", False
        words = [word.strip(".") for word in normalized.split() if word.strip(".")]
        for start in range(len(words)):
            parsed = parse_number_words(words, start)
            if parsed is None:
                continue
            quantity, end = parsed
            unit = ""
            if end < len(words) and words[end] in UNIT_ALIASES:
                unit = normalize_unit(words[end])
                end += 1
            prefix = words[:start]
            suffix = words[end:]
            if all(word in _QUANTITY_PREFIX_WORDS for word in prefix) and all(
                word in UNIT_ALIASES for word in suffix
            ):
                return quantity, unit, False

        unit_only, unit = cls.spoken_unit_only_quantity(normalized)
        if unit_only is not None:
            return unit_only, unit, False

        if cls._has_ambiguous_numeric_fragment(words):
            return None, "", True
        return None, "", False

    @staticmethod
    def _has_ambiguous_numeric_fragment(words: list[str]) -> bool:
        """Находит незавершённое числительное в допустимой оболочке ответа."""
        for index, word in enumerate(words):
            is_digit = bool(re.fullmatch(r"\d+(?:[.,]\d+)?", word))
            is_partial_word = any(
                len(word) >= 2 and number_word.startswith(word) for number_word in NUMBER_WORDS
            )
            if not (is_digit or is_partial_word):
                continue
            prefix = words[:index]
            suffix = words[index + 1 :]
            if all(token in _QUANTITY_PREFIX_WORDS for token in prefix) and all(
                token in UNIT_ALIASES for token in suffix
            ):
                return True
        return False

    @staticmethod
    def has_named_product_items(command: ParsedCommand, text: str) -> bool:
        """Отличает товарный запрос от короткого ответа количеством."""
        return has_named_product_items(command, text)
