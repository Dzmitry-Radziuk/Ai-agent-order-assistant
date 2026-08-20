"""Разбирает команды изменения, удаления и добавления товаров."""

from __future__ import annotations

import re
from collections.abc import Sequence

from restaurant_bot.domain.models import Intent, ParsedCommand
from restaurant_bot.domain.text import clean_text, normalize_text
from restaurant_bot.parsing.commands.normalization import normalize_command_text
from restaurant_bot.parsing.comment_scope import _extract_global_comment
from restaurant_bot.parsing.number_words import NUMBER_WORDS
from restaurant_bot.parsing.products import parse_product_lines
from restaurant_bot.parsing.quantities import parse_quantity_unit

_DRAFT_CONTAINER_RE = r"(?:заявк\w*|заказ\w*|корзин\w*|черновик\w*|списк\w*)"
_DRAFT_MODIFIER_RE = r"(?:мо\w+|наш\w+|текущ\w+|эт\w+|данн\w+)"
_DRAFT_LOCATION_RE = rf"{_DRAFT_CONTAINER_RE}(?:\s+товар\w*)?"
_REMOVE_ACTION_RE = (
    r"(?:убери|убрать|уберем|уберём|убираем|удали|удалить|удаляем|"
    r"исключи|исключить|вычеркни|вычеркнуть|сними|снять|выкинь|выкинуть|"
    r"не\s+добавляй|не\s+добавлять|не\s+заказывай|не\s+заказывать)"
)
_REMOVE_RE = re.compile(
    rf"^(?:(?:пожалуйста\s+)?(?:(?:давай|можешь|можно|нужно|надо)\s+)?"
    rf"{_REMOVE_ACTION_RE}\s+(?:пожалуйста\s+)?(?:(?:мне|у\s+меня)\s+)?"
    rf"(?:(?:из|с)\s+(?:{_DRAFT_MODIFIER_RE}\s+)?{_DRAFT_LOCATION_RE}\s+)?|"
    r"(?:мне\s+)?не\s+(?:нужен|нужна|нужно|нужны)\s+)(.+?)"
    r"(?:\s*,?\s*пожалуйста)?\s*$",
    re.I,
)
_EDIT_ACTION = (
    r"(?:измени|изменить|поменяй|поменять|сделай|сделать|поставь|поставить|"
    r"исправь|исправить|обнови|обновить|замени|заменить)"
)
_NUMBER_START = (
    r"(?:\d+(?:[,.]\d+)?|"
    + "|".join(sorted((re.escape(word) for word in NUMBER_WORDS), key=len, reverse=True))
    + r")"
)
_EDIT_AMOUNT = rf"(?P<amount>{_NUMBER_START}(?:\s+[a-zа-яё.]+){{0,3}})"
_EDIT_PATTERNS = [
    re.compile(
        rf"^{_EDIT_ACTION}\s+у\s+(?P<target>.+?)\s+(?:количество|кол-во)\s+"
        rf"(?:на|до)\s+{_EDIT_AMOUNT}$",
        re.I,
    ),
    re.compile(
        rf"^{_EDIT_ACTION}\s+(?:количество|кол-во)\s+(?:у\s+)?(?P<target>.+?)\s+"
        rf"(?:на|до)\s+{_EDIT_AMOUNT}$",
        re.I,
    ),
    re.compile(
        rf"^для\s+(?P<target>.+?)\s+{_EDIT_ACTION}(?:\s+(?:количество|кол-во))?"
        rf"\s+(?:на\s+)?{_EDIT_AMOUNT}$",
        re.I,
    ),
    re.compile(
        rf"^(?P<target>.+?)\s+{_EDIT_ACTION}(?:\s+(?:количество|кол-во))?"
        rf"\s+(?:на|до)\s+{_EDIT_AMOUNT}$",
        re.I,
    ),
    re.compile(
        rf"^(?P<target>.+?)\s+{_EDIT_ACTION}(?:\s+(?:количество|кол-во))?"
        rf"\s+{_EDIT_AMOUNT}$",
        re.I,
    ),
    re.compile(
        rf"^{_EDIT_ACTION}\s+(?:у\s+)?(?P<target>.+?)\s+(?:на|до)\s+{_EDIT_AMOUNT}$",
        re.I,
    ),
    re.compile(
        rf"^{_EDIT_ACTION}(?:\s+(?:количество|кол-во))?\s+(?:на|до)\s+{_EDIT_AMOUNT}$",
        re.I,
    ),
    re.compile(
        rf"^{_EDIT_ACTION}\s+(?:у\s+)?(?P<target>.+)\s+{_EDIT_AMOUNT}$",
        re.I,
    ),
]
_SELECT_RE = re.compile(r"^(?:вариант|номер|выбери)?\s*([1-5])$", re.I)


def clean_command_target(value: str) -> str:
    """Убирает служебные слова вокруг названия товара в команде."""
    target = clean_text(value)
    target = re.sub(r"^(?:пожалуйста\s+)", "", target, flags=re.I)
    target = re.sub(r"(?:\s*,?\s*пожалуйста)$", "", target, flags=re.I)
    target = target.strip(" ,;:-—–.!?")
    target = re.sub(r"^для\s+", "", target, flags=re.I)
    target = re.sub(
        r"^(?:(?:этот|эту|это|данный|данную|текущий|текущую)\s+)?"
        r"(?:товар|позицию|строку|пункт)\s+",
        "",
        target,
        flags=re.I,
    )
    target = re.sub(
        rf"\s+(?:из|с|в|на)\s+(?:{_DRAFT_MODIFIER_RE}\s+)?{_DRAFT_LOCATION_RE}\s*$",
        "",
        target,
        flags=re.I,
    )
    return target.strip(" ,;:-—–.!?")


def _is_whole_draft_target(value: str) -> bool:
    """Отличает название всего черновика от названия отдельного товара."""
    target = normalize_command_text(value)
    if re.fullmatch(rf"(?:{_DRAFT_MODIFIER_RE}\s+)?{_DRAFT_LOCATION_RE}", target, re.I):
        return True

    all_quantifier = r"(?:все|всё|всю|весь|полностью|целиком)"
    item_word = r"(?:товар\w*|позици\w*|продукт\w*)"
    draft_location = rf"(?:{_DRAFT_MODIFIER_RE}\s+)?{_DRAFT_LOCATION_RE}"
    return bool(
        re.fullmatch(
            rf"{all_quantifier}(?:\s+{item_word})?\s+(?:из|с|в|на)\s+{draft_location}",
            target,
            re.I,
        )
        or re.fullmatch(
            rf"{item_word}\s+(?:из|с|в|на)\s+{draft_location}\s+{all_quantifier}",
            target,
            re.I,
        )
    )


def _parse_edit_quantity(text: str) -> ParsedCommand | None:
    """Разбирает изменение количества при разном порядке слов."""
    for pattern in _EDIT_PATTERNS:
        match = pattern.fullmatch(text)
        if match is None:
            continue
        quantity, unit = parse_quantity_unit(match.group("amount"))
        if quantity is None:
            continue
        target = clean_command_target(match.groupdict().get("target") or "")
        return ParsedCommand(
            intent=Intent.EDIT_QUANTITY,
            text=text,
            target_query=target,
            edit_quantity=quantity,
            edit_unit=unit,
        )
    return None


def is_product_add_request_phrase(text: str) -> bool:
    """Распознаёт команду отправки товара снабжению."""
    words = normalize_text(text).split()
    if not words:
        return False

    exact_short_commands = {
        "отправь",
        "отправить",
        "отправьте",
        "запрос",
        "запрос снабженцу",
    }
    normalized = " ".join(words)
    if normalized in exact_short_commands:
        return True

    has_action = any(
        word.startswith(("отправ", "переда", "созда", "оформ", "добав")) for word in words
    )
    has_procurement_target = any(
        word.startswith(("снабжен", "менеджер", "запрос", "заявк")) for word in words
    )
    has_product_target = any(word.startswith(("товар", "позици")) for word in words)
    return has_action and (has_procurement_target or has_product_target)


_EXPLICIT_ADD_ITEMS_RE = re.compile(
    r"^(?:(?:мне\s+нужно|мне\s+надо|я\s+хочу|хочу|давай(?:те)?|пожалуйста)\s+)?"
    r"(?:добав(?:ь|ить|им)|закаж(?:и|ем|ать)|полож(?:и|ить)|возьм(?:и|ем)|постав(?:ь|ить))\s+"
    r"(?P<target>.+)$",
    re.IGNORECASE,
)
_MIXED_ADD_ITEMS_RE = re.compile(
    r"^(?:да|нет)\s*(?:[,;:—–-]\s*)?"
    r"(?:добав(?:ь|ить|им)|закаж(?:и|ем|ать)|полож(?:и|ить)|"
    r"возьм(?:и|ем)|постав(?:ь|ить))\s+(?P<target>.+)$",
    re.IGNORECASE,
)
_AFFIRM_NEW_ORDER_RE = re.compile(
    r"^да\s*(?:[,;:—–-]\s*)?(?:начинай|начать|начн(?:ем|ём)|"
    r"сделай|создай|оформи)\s+(?:новую(?:\s+заявку)?|новый\s+заказ)$",
    re.IGNORECASE,
)
_NON_PRODUCT_ADD_TARGET_RE = re.compile(
    r"^(?:ещ[её]\s+)?(?:товар(?:ы|а|ов)?|позици(?:я|и|й)|продукт(?:ы|а|ов)?|"
    r"что(?:-нибудь|\s+нибудь)?)(?:\s+ещ[её])?$",
    re.IGNORECASE,
)


def has_explicit_add_items(text: str, items: Sequence[object] | None = None) -> bool:
    """Определяет явную команду добавления новой товарной позиции."""
    if items is not None:
        has_product = any(
            (
                item.get("product_query", "")
                if isinstance(item, dict)
                else getattr(item, "product_query", "")
            ).strip()
            for item in items
        )
        if not has_product:
            return False
    normalized = normalize_command_text(text)
    match = _EXPLICIT_ADD_ITEMS_RE.fullmatch(normalized)
    if match is None:
        return False
    target = clean_command_target(match.group("target"))
    if not target or _NON_PRODUCT_ADD_TARGET_RE.fullmatch(target):
        return False
    return not bool(re.fullmatch(r"(?:в|во)\s+(?:корзин\w*|заявк\w*)", target, re.IGNORECASE))


def _parse_mixed_add_items(text: str) -> ParsedCommand | None:
    """Сохраняет явное добавление после вводного да/нет-маркера."""
    match = _MIXED_ADD_ITEMS_RE.fullmatch(normalize_command_text(text))
    if match is None:
        return None
    target = clean_command_target(match.group("target"))
    if not target or _NON_PRODUCT_ADD_TARGET_RE.fullmatch(target):
        return None
    product_text, global_comment = _extract_global_comment(target)
    items = parse_product_lines(product_text)
    if not items:
        return None
    return ParsedCommand(
        intent=Intent.ADD_ITEMS,
        explicit_add_items=True,
        text=text,
        items=items,
        global_comment=global_comment,
    )
