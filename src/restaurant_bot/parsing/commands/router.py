"""Маршрутизирует разбор текстовых команд без привязки к каналу."""

from __future__ import annotations

import re

from restaurant_bot.domain.models import Intent, ParsedCommand
from restaurant_bot.domain.text import clean_text, normalize_text
from restaurant_bot.domain.units import UNIT_ALIASES
from restaurant_bot.parsing.commands.comment_commands import _parse_edit_comment
from restaurant_bot.parsing.commands.item_commands import (
    _AFFIRM_NEW_ORDER_RE,
    _REMOVE_RE,
    _SELECT_RE,
    _is_whole_draft_target,
    _parse_edit_quantity,
    _parse_mixed_add_items,
    clean_command_target,
    has_explicit_add_items,
)
from restaurant_bot.parsing.commands.navigation import (
    _infer_free_form_navigation,
    _parse_order_status_navigation,
)
from restaurant_bot.parsing.commands.normalization import (
    _has_word_stem,
    has_negated_action,
    has_negation,
    is_explicit_item_rejection,
    normalize_command_text,
)
from restaurant_bot.parsing.commands.patterns import _COMMANDS, _NATURAL_COMMANDS
from restaurant_bot.parsing.comment_scope import _extract_global_comment
from restaurant_bot.parsing.history import parse_history_query, requires_history_context
from restaurant_bot.parsing.products import has_multiple_explicit_order_items, parse_product_lines


def _infer_negated_command(normalized: str) -> Intent | None:
    """Блокирует мутации, если пользователь явно отрицает действие."""
    if not has_negation(normalized):
        return None
    if is_explicit_item_rejection(normalized):
        return Intent.SKIP_CURRENT
    words = re.findall(r"[a-zа-яё0-9-]+", normalized, flags=re.I)
    has_new_order_target = _has_word_stem(words, "заявк", "заказ", "черновик")
    has_new_order_marker = _has_word_stem(
        words,
        "нов",
        "друг",
        "следующ",
        "повторн",
        "занов",
    )
    if has_new_order_target and has_new_order_marker:
        return Intent.CANCEL
    if has_negated_action(normalized, "исправ", "поправ", "измен", "поменя") and (
        "колич" in normalized or "как есть" in normalized
    ):
        return Intent.KEEP_CURRENT_QUANTITY
    if any(
        has_negated_action(normalized, *stems)
        for stems in (
            ("отправ", "оформ", "подтверж", "переда", "запиш"),
            ("очист", "очищ", "почист", "сброс", "сбрасы", "обнул", "удал", "убер"),
            ("использ", "остав", "перевед", "конверт"),
            ("выбер", "выбир", "возьм", "бери"),
            ("объедин", "суммир", "прибав", "слож", "увелич"),
        )
    ):
        return Intent.CANCEL
    if has_negated_action(normalized, "добав", "внес", "докин", "попол", "продолж"):
        generic_targets = {
            "товары",
            "товаров",
            "позиции",
            "позиций",
            "продукты",
            "продуктов",
        }
        removal = _REMOVE_RE.match(normalized)
        if removal:
            target = normalize_text(removal.group(1))
            if target and not any(word in generic_targets for word in target.split()):
                return None
        return Intent.CANCEL
    return None


def parse_text_command(text: str) -> ParsedCommand:
    """Определяет намерение пользователя."""
    command_match = re.fullmatch(r"/([a-z][a-z0-9_]*)(?:@[a-z0-9_]+)?", clean_text(text), re.I)
    if command_match:
        command_intents = {
            "start": Intent.GREETING,
            "help": Intent.HELP,
            "draft": Intent.SHOW_CART,
            "reset": Intent.CLEAR_CART,
            "submit": Intent.SUBMIT_REQUEST,
            "orders": Intent.ORDER_STATUS,
            "cancel": Intent.CANCEL,
            "back": Intent.BACK,
        }
        intent = command_intents.get(command_match.group(1).lower())
        if intent:
            return ParsedCommand(intent=intent, text=text)

    # Распознавание речи часто добавляет точку в конце. Команда является
    # законченной фразой, поэтому точка не должна превращать «Добавить еще
    # товары» в товар под названием «товары».
    normalized = normalize_command_text(text)
    history_query = parse_history_query(text)
    if history_query is not None and not has_multiple_explicit_order_items(text):
        return ParsedCommand(intent=Intent.HISTORY_QUERY, text=text, history_query=history_query)
    if requires_history_context(text):
        return ParsedCommand(intent=Intent.UNKNOWN, text=text)
    if status_command := _parse_order_status_navigation(normalized, text):
        return status_command
    if _AFFIRM_NEW_ORDER_RE.fullmatch(normalized):
        return ParsedCommand(intent=Intent.CONFIRM, text=text)
    for intent, pattern in (*_COMMANDS, *_NATURAL_COMMANDS):
        if pattern.fullmatch(normalized):
            return ParsedCommand(intent=intent, text=text)

    if is_explicit_item_rejection(normalized):
        return ParsedCommand(intent=Intent.SKIP_CURRENT, text=text)

    if mixed_add := _parse_mixed_add_items(text):
        return mixed_add

    if negated_intent := _infer_negated_command(normalized):
        return ParsedCommand(intent=negated_intent, text=text)

    if edit_command := _parse_edit_quantity(normalized):
        return edit_command.model_copy(update={"text": text})

    if comment_command := _parse_edit_comment(text):
        return comment_command

    if match := _REMOVE_RE.match(normalized):
        target = clean_command_target(match.group(1))
        if target and _is_whole_draft_target(target):
            return ParsedCommand(intent=Intent.CLEAR_CART, text=text)
        if target:
            return ParsedCommand(
                intent=Intent.REMOVE_ITEM, text=text, target_query=target, target_queries=[target]
            )

    if free_form_intent := _infer_free_form_navigation(normalized):
        return ParsedCommand(intent=free_form_intent, text=text)

    if re.fullmatch(
        r"(?:да\s*,?\s*)?добавляй|пусть\s+будет\s+вместе",
        normalized,
        flags=re.IGNORECASE,
    ):
        return ParsedCommand(intent=Intent.ADD_ITEMS, text=text)

    if match := _SELECT_RE.match(normalized):
        return ParsedCommand(
            intent=Intent.SELECT_CANDIDATE, text=text, selected_index=int(match.group(1))
        )

    ordinal_patterns = {
        1: r"(?:1|один|перв(?:ый|ого|ая|ую|ое))",
        2: r"(?:2|два|втор(?:ой|ого|ая|ую|ое))",
        3: r"(?:3|три|трет(?:ий|ьего|ья|ью|ье))",
        4: r"(?:4|четыре|четверт(?:ый|ого|ая|ую|ое))",
        5: r"(?:5|пять|пят(?:ый|ого|ая|ую|ое))",
    }
    explicit_choice = bool(
        re.search(
            r"(?:^|\s)(?:вариант|выбери|выбрать|выбираю|беру|возьми|взять|"
            r"подходит|подойдёт|подойдет|нужен|нужна|номер)(?:\s|$)",
            normalized,
        )
    )
    polite_choice = bool(re.match(r"^(?:давай|мне|хочу|нужен|нужна|возьми)\s+", normalized))
    contains_measurement = any(word in UNIT_ALIASES for word in normalized.split())
    for index, token in ordinal_patterns.items():
        bare = re.fullmatch(token, normalized)
        mentioned = re.search(rf"(?:^|\s){token}(?:\s|$)", normalized)
        if mentioned and not contains_measurement and (bare or explicit_choice or polite_choice):
            return ParsedCommand(
                intent=Intent.SELECT_CANDIDATE,
                text=text,
                selected_index=index,
            )

    ordinal_text = normalized.removesuffix(" вариант").strip()
    ordinal_matches = {"первый": 1, "второй": 2, "третий": 3, "четвертый": 4, "пятый": 5}
    if ordinal_text in ordinal_matches:
        return ParsedCommand(
            intent=Intent.SELECT_CANDIDATE,
            text=text,
            selected_index=ordinal_matches[ordinal_text],
        )

    if re.fullmatch(
        r"(?:пропусти|не нужен|не добавляй)(?: эту позицию| этот товар| товар)?", normalized
    ):
        return ParsedCommand(intent=Intent.SKIP_CURRENT, text=text)
    if re.fullmatch(r"(?:ни один|ничего не подходит|нужного нет|поищи иначе)", normalized):
        return ParsedCommand(intent=Intent.MANUAL_CURRENT, text=text)
    if re.fullmatch(r"(?:продолжить|продолжай|дальше|поехали)", normalized):
        return ParsedCommand(intent=Intent.CONTINUE_CURRENT, text=text)
    if re.fullmatch(
        r"(?:отправь как есть|отправить как есть|отправь поставщику|отправить поставщику|отправь заказ поставщику|отправить заказ поставщику|передай поставщикам|передать поставщикам|не будем добирать|риск принимаю)",
        normalized,
    ):
        return ParsedCommand(intent=Intent.SUBMIT_AS_IS, text=text)

    product_text, global_comment = _extract_global_comment(text)
    items = parse_product_lines(product_text)
    if items:
        return ParsedCommand(
            intent=Intent.ADD_ITEMS,
            explicit_add_items=has_explicit_add_items(text, items),
            text=text,
            items=items,
            global_comment=global_comment,
        )
    return ParsedCommand(intent=Intent.UNKNOWN, text=text)
