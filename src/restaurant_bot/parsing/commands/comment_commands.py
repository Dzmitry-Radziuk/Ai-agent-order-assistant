"""Разбирает команды изменения комментариев в черновике."""

from __future__ import annotations

import re

from restaurant_bot.domain.models import Intent, ParsedCommand
from restaurant_bot.domain.text import clean_text
from restaurant_bot.parsing.commands.item_commands import clean_command_target
from restaurant_bot.parsing.commands.normalization import normalize_command_text
from restaurant_bot.parsing.comment_policy import strip_comment_label
from restaurant_bot.parsing.comment_scope import (
    has_explicit_group_comment_scope,
    has_explicit_order_comment_scope,
    strip_comment_scope_suffix,
)
from restaurant_bot.parsing.semantic.measurements import has_order_quantity_evidence

_COMMENT_NOUN_RE = r"(?:комментар\w*|примечан\w*)"
_COMMENT_ACTION_RE = r"(?:добав\w*|внес\w*|запиш\w*|укаж\w*|измени\w*|поправ\w*)"
_COMMENT_REMOVE_ACTION_RE = r"(?:убер\w*|удал\w*|сотр\w*|очист\w*)"
_COMMENT_GLOBAL_SCOPE_RE = (
    r"(?:для\s+всех\s+(?:товар\w*|позици\w*)|"
    r"всем\s+(?:товар\w*|позици\w*)|"
    r"ко?\s+всем\s+(?:товар\w*|позици\w*)|"
    r"у\s+всех\s+(?:товар\w*|позици\w*)|"
    r"для\s+всей\s+(?:заявк\w*|заказ\w*)|"
    r"ко?\s+всей\s+(?:заявк\w*|заказ\w*))"
)
_COMMENT_WISH_RE = re.compile(
    r"\b(?:желательн\w*|нужн\w*|обязательн\w*|только|именно|"
    r"пожалуйста|просьб\w*|привез\w*|достав\w*|полож\w*|упаков\w*|"
    r"не\s+(?:замен\w*|смешива\w*|размораж\w*))\b",
    re.I,
)


def _build_edit_comment(
    target: str,
    comment: str,
    source: str,
    *,
    action: str = "add",
    scope: str = "item",
    scope_action: str = "",
    require_wish: bool = True,
) -> ParsedCommand | None:
    """Создаёт команду изменения комментария только с явным товаром и пожеланием."""
    target = clean_command_target(target)
    comment = strip_comment_label(comment)
    if action == "add" and scope_action in {"items", "order"}:
        comment = strip_comment_scope_suffix(comment, scope_action)
    if action == "add" and scope == "order":
        comment = re.sub(r"(?:[,;:]|\s+)\s*общ\w*$", "", comment, flags=re.I).strip(" ,;:-—–.!?")
        comment = re.sub(
            r"\s+(?:для|ко|на)\s+(?:всей|всю|всего|весь)\s+(?:заявк\w*|заказ\w*)$",
            "",
            comment,
            flags=re.I,
        ).strip(" ,;:-—–.!?")
    if scope == "item" and not target and scope_action != "items":
        return None
    if action == "add" and (not comment or (require_wish and not _COMMENT_WISH_RE.search(comment))):
        return None
    if action in {"remove", "clear_all"}:
        comment = ""
    if scope not in {"item", "order"} or action not in {"add", "remove", "clear_all"}:
        return None
    if scope_action not in {"", "items", "order"}:
        return None
    if scope_action == "items" and (scope != "item" or action != "add"):
        return None
    return ParsedCommand(
        intent=Intent.EDIT_COMMENT,
        text=source,
        comment_target_query=target,
        comment_text=comment,
        comment_action=action,
        comment_scope=scope,
        comment_scope_action=scope_action,
    )


def _parse_edit_comment(text: str) -> ParsedCommand | None:
    """Распознаёт изменение комментария существующего товара без создания позиции."""
    source = clean_text(text)
    normalized = normalize_command_text(source)
    if not normalized:
        return None

    remove_action = rf"{_COMMENT_REMOVE_ACTION_RE}\s+"
    noun = rf"{_COMMENT_NOUN_RE}\s*"
    global_scope = rf"{_COMMENT_GLOBAL_SCOPE_RE}"

    # Полная очистка пользовательских комментариев отделена от удаления
    # только order-фрагментов.
    if re.fullmatch(
        rf"{remove_action}(?:(?:все|всё)\s+{noun}(?:(?:у\s+всех\s+(?:товар\w*|позиц\w*))|(?:из\s+(?:заявк\w*|заказ\w*)))?|{noun}у\s+всех\s+(?:товар\w*|позиц\w*))",
        normalized,
        re.I,
    ):
        return _build_edit_comment("", "", source, action="clear_all", scope="order")

    # Явное удаление общего комментария не должно становиться товаром.
    if re.fullmatch(
        rf"(?:{remove_action}общ\w*\s+{noun}(?:\s+{global_scope})?|"
        rf"{remove_action}(?:все\s+)?{noun}(?:\s+{global_scope})?)",
        normalized,
        re.I,
    ):
        return _build_edit_comment("", "", source, action="remove", scope="order")

    # Явное удаление комментария конкретной позиции.
    remove_patterns = (
        re.compile(
            rf"^{remove_action}(?:все\s+)?{noun}(?:у|к|для|о|об|про)\s+(?P<target>.+)$",
            re.I,
        ),
        re.compile(
            rf"^{remove_action}(?:у|к|для|о|об|про)\s+(?P<target>.+?)\s+{noun}$",
            re.I,
        ),
    )
    for pattern in remove_patterns:
        match = pattern.fullmatch(normalized)
        if match is not None:
            return _build_edit_comment(
                match.group("target"), "", source, action="remove", scope="item"
            )

    # Явный общий комментарий: только при словах общего охвата.
    global_patterns = (
        re.compile(
            rf"^(?:{_COMMENT_ACTION_RE}\s+)?{noun}{global_scope}\s+(?P<comment>.+)$",
            re.I,
        ),
        re.compile(
            rf"^{_COMMENT_ACTION_RE}\s+(?:в\s+)?общ\w*\s+{noun}(?::\s*|\s+)(?P<comment>.+)$",
            re.I,
        ),
        re.compile(
            rf"^{_COMMENT_ACTION_RE}\s+(?:в\s+)?{noun}(?::\s*|\s+)(?P<comment>.+)$",
            re.I,
        ),
    )
    for pattern in global_patterns:
        match = pattern.fullmatch(normalized)
        if match is not None:
            scope_action = (
                "order"
                if has_explicit_order_comment_scope(source)
                else "items"
                if has_explicit_group_comment_scope(source)
                else ""
            )
            if not scope_action:
                continue
            return _build_edit_comment(
                "",
                match.group("comment"),
                source,
                scope="order" if scope_action == "order" else "item",
                scope_action=scope_action,
                require_wish=False,
            )

    # Голос часто опускает слово «комментарий»: явная конструкция «к/для
    # товара + пожелание» всё равно безопасна, потому что ищет только черновик.
    wish_match = _COMMENT_WISH_RE.search(normalized)
    if wish_match is not None and not re.search(_COMMENT_NOUN_RE, normalized, re.I):
        if has_order_quantity_evidence(source):
            return None
        prefix = normalized[: wish_match.start()].strip(" ,;:-—–")
        comment = normalized[wish_match.start() :]
        match = re.match(
            rf"^(?:{_COMMENT_ACTION_RE}\s+)?(?:к|для)\s+(?P<target>.+)$",
            prefix,
            re.I,
        )
        if match is not None:
            return _build_edit_comment(match.group("target"), comment, source)
        return None
    if not re.search(_COMMENT_NOUN_RE, normalized, re.I):
        return None

    action = rf"{_COMMENT_ACTION_RE}\s+"
    direct_target_match = re.fullmatch(
        rf"^{noun}(?:к|для)\s+(?P<target>.+?)\s*:\s*(?P<comment>.+)$",
        clean_text(source),
        re.I,
    )
    if direct_target_match is not None:
        result = _build_edit_comment(
            direct_target_match.group("target"),
            direct_target_match.group("comment"),
            source,
            require_wish=False,
        )
        if result is not None:
            return result
    patterns = (
        re.compile(
            rf"^{action}{noun}(?:к|для)\s+(?P<target>.+?)(?:\s*[,;:—–-]\s*|\s+)(?P<comment>.+)$",
            re.I,
        ),
        re.compile(
            rf"^{action}(?:к|для)\s+(?P<target>.+?)\s+{noun}(?:[:—–-]\s*|\s+)(?P<comment>.+)$",
            re.I,
        ),
        re.compile(
            rf"^(?:к|для)\s+(?P<target>.+?)\s+{noun}(?:[:—–-]\s*|\s+)(?P<comment>.+)$",
            re.I,
        ),
        re.compile(
            rf"^{noun}(?:к|для)\s+(?P<target>.+?)\s*[:—–-]\s*(?P<comment>.+)$",
            re.I,
        ),
    )
    for pattern in patterns:
        match = pattern.fullmatch(normalized)
        if match is not None:
            result = _build_edit_comment(
                match.group("target"),
                match.group("comment"),
                source,
                require_wish=False,
            )
            if result is not None:
                return result
    # Явный маркер комментария без понятной цели — это не удаление товара и
    # не новая товарная позиция. Оставляем команду безопасно нераспознанной.
    return ParsedCommand(intent=Intent.UNKNOWN, text=source)
