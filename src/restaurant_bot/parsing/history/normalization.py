"""Нормализует слова history-запроса для русского лексического поиска."""

from __future__ import annotations

import re

from restaurant_bot.domain.text import normalize_text

_TOKEN_RE = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)
_SUFFIXES = (
    "иями",
    "ами",
    "ями",
    "ого",
    "ему",
    "ому",
    "ими",
    "ыми",
    "аго",
    "его",
    "ее",
    "ое",
    "ая",
    "яя",
    "ые",
    "ие",
    "ов",
    "ев",
    "ам",
    "ям",
    "ом",
    "ем",
    "ым",
    "им",
    "ах",
    "ях",
    "ую",
    "юю",
    "ою",
    "ею",
    "ей",
    "ой",
    "ый",
    "ий",
    "ых",
    "их",
    "ы",
    "и",
    "а",
    "я",
    "у",
    "ю",
    "е",
    "о",
    "ь",
)


def history_tokens(value: str) -> list[str]:
    """Возвращает нормализованные слова без пунктуации."""
    return _TOKEN_RE.findall(normalize_text(value))


def history_stem(value: str) -> str:
    """Сводит распространённые русские окончания к основе слова."""
    token = normalize_text(value)
    if len(token) <= 4:
        return token
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)]
    return token


def history_stems(value: str) -> list[str]:
    """Строит уникальные основы слов в исходном порядке."""
    result: list[str] = []
    for token in history_tokens(value):
        stem = history_stem(token)
        if stem and stem not in result:
            result.append(stem)
    return result
