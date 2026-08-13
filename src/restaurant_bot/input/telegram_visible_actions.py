"""Извлекает Telegram-специфичные цели страниц из видимых действий."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping


def available_cart_pages(visible_actions: Iterable[Mapping[str, str]]) -> set[int]:
    """Возвращает номера страниц черновика из callback-данных Telegram."""
    return _available_pages(visible_actions, "cartpage")


def available_final_review_pages(visible_actions: Iterable[Mapping[str, str]]) -> set[int]:
    """Возвращает номера страниц финальной проверки из callback-данных Telegram."""
    return _available_pages(visible_actions, "finalpage")


def _available_pages(
    visible_actions: Iterable[Mapping[str, str]],
    action_name: str,
) -> set[int]:
    """Извлекает номера страниц для одного Telegram callback-типа."""
    pattern = re.compile(rf"v2:{re.escape(action_name)}:(\d+)(?::r\d+)?")
    pages: set[int] = set()
    for action in visible_actions:
        match = pattern.fullmatch(action.get("action_id", ""))
        if match:
            pages.add(int(match.group(1)))
    return pages
