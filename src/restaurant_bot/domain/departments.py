"""Содержит каноническую лексику отделов заявки."""

from __future__ import annotations

from typing import Any

from restaurant_bot.domain.text import clean_text, normalize_text

DEPARTMENT_ALIASES: dict[str, str] = {
    "зал": "Зал",
    "зала": "Зал",
    "залу": "Зал",
    "зале": "Зал",
    "бар": "Бар",
    "бара": "Бар",
    "бару": "Бар",
    "баре": "Бар",
    "кухня": "Кухня",
    "кухни": "Кухня",
    "кухню": "Кухня",
    "кухне": "Кухня",
    "hall": "Зал",
    "bar": "Бар",
    "kitchen": "Кухня",
    "condiments": "Кухня",
}


def normalize_department(value: Any) -> str:
    """Приводит название отдела к заголовку листа заявки."""
    text = normalize_text(value)
    return DEPARTMENT_ALIASES.get(text, clean_text(value))
