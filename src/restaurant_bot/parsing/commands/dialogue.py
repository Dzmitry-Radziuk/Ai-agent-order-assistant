"""Обогащает команды диалоговыми признаками и короткими ответами."""

from __future__ import annotations

import re
from collections.abc import Sequence

from restaurant_bot.domain.models import DialogueResponse, ExtractedItem, Intent
from restaurant_bot.parsing.commands.normalization import normalize_command_text
from restaurant_bot.parsing.quantities import parse_quantity_unit
from restaurant_bot.services.text import NUMBER_WORDS, UNIT_ALIASES, normalize_unit

_STANDALONE_RETRY_RE = re.compile(
    r"(?:повтори|повторить|повтори отправку|повторить отправку|"
    r"отправь еще раз|отправь ещё раз|попробуй снова|отправляй)",
    re.IGNORECASE,
)


def retry_requested_for(text: str) -> bool:
    """Распознаёт только самостоятельную просьбу повторить отправку."""
    return bool(_STANDALONE_RETRY_RE.fullmatch(normalize_command_text(text)))


_QUANTITY_HINT_LEADINS = {
    "да",
    "давай",
    "ладно",
    "мне",
    "нужен",
    "нужна",
    "нужно",
    "ок",
    "окей",
    "поставь",
    "поставить",
    "пусть",
    "тогда",
    "хорошо",
    "возьми",
    "возьмем",
    "закажи",
    "заказать",
}


def _standalone_quantity_hint(text: str) -> tuple[float | None, str]:
    """Извлекает количество только из короткой standalone-фразы."""
    normalized = normalize_command_text(text)
    quantity, unit = parse_quantity_unit(normalized)
    if quantity is None:
        return None, ""
    tokens = normalized.replace(",", " ").split()
    allowed = set(UNIT_ALIASES) | set(NUMBER_WORDS) | _QUANTITY_HINT_LEADINS
    if not all(token in allowed or re.fullmatch(r"\d+(?:\.\d+)?", token) for token in tokens):
        return None, ""
    return quantity, normalize_unit(unit)


def dialogue_response_for(
    text: str,
    intent: Intent,
    items: Sequence[ExtractedItem] | None = None,
) -> DialogueResponse:
    """Определяет общий короткий ответ без привязки к modal state."""
    normalized = normalize_command_text(text)
    if normalized in {"ну", "не знаю", "может быть", "ладно"}:
        return DialogueResponse.UNCERTAIN
    if normalized in {"хватит", "достаточно"} or re.fullmatch(
        r"нет(?:\s+.*)?(?:не\s+надо|не\s+нужно|хватит|достаточно)",
        normalized,
    ):
        return DialogueResponse.DECLINE
    if intent is Intent.CONFIRM and (
        not normalized or not re.search(r"\b(?:отправ|переда|оформ)\w*\b", normalized)
    ):
        return DialogueResponse.AFFIRM
    if intent is Intent.CANCEL:
        return DialogueResponse.DECLINE
    if intent is Intent.ADD_MORE and not normalized:
        return DialogueResponse.AFFIRM
    if intent is Intent.ADD_MORE and re.fullmatch(
        r"(?:да\s+)?(?:давай\s+)?(?:добавим|добавить|добавь)\s+ещ[её]"
        r"(?:\s+(?:товар\w*|позици\w*|что[- ]?нибудь))?",
        normalized,
        flags=re.IGNORECASE,
    ):
        return DialogueResponse.AFFIRM
    if intent is not Intent.ADD_ITEMS or not normalized:
        return DialogueResponse.NONE

    # Эти фразы попадают в детерминированный разбор как условные товары.
    # Сохраняем их смысл в метаданных, чтобы modal handler не разбирал исходный текст.
    if re.fullmatch(
        r"(?:да\s+)?(?:давай\s+)?(?:добавим|добавить|добавь)\s+ещ[её]",
        normalized,
        flags=re.IGNORECASE,
    ) or re.fullmatch(r"давай\s+ещ[её]", normalized, flags=re.IGNORECASE):
        return DialogueResponse.AFFIRM
    return DialogueResponse.NONE
