"""Содержит разбор числительных, записанных словами."""

from __future__ import annotations

import re

NUMBER_WORDS: dict[str, float] = {
    "ноль": 0,
    "один": 1,
    "одна": 1,
    "одно": 1,
    "одну": 1,
    "раз": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
    "тринадцать": 13,
    "четырнадцать": 14,
    "пятнадцать": 15,
    "шестнадцать": 16,
    "семнадцать": 17,
    "восемнадцать": 18,
    "девятнадцать": 19,
    "двадцать": 20,
    "тридцать": 30,
    "сорок": 40,
    "пятьдесят": 50,
    "шестьдесят": 60,
    "семьдесят": 70,
    "восемьдесят": 80,
    "девяносто": 90,
    "сто": 100,
    "двести": 200,
    "триста": 300,
    "четыреста": 400,
    "пятьсот": 500,
    "шестьсот": 600,
    "семьсот": 700,
    "восемьсот": 800,
    "девятьсот": 900,
    "полтора": 1.5,
    "полторы": 1.5,
    "половина": 0.5,
    "четверть": 0.25,
}


def parse_number_words(tokens: list[str], start: int) -> tuple[float, int] | None:
    """Преобразует числительное словами в число."""
    if start >= len(tokens):
        return None
    raw = tokens[start].replace(",", ".")
    if re.fullmatch(r"\d+(?:\.\d+)?", raw):
        return float(raw), start + 1

    total = 0.0
    used = False
    index = start
    while index < len(tokens):
        token = tokens[index].replace("ё", "е")
        if token not in NUMBER_WORDS:
            break
        total += NUMBER_WORDS[token]
        used = True
        index += 1
    if not used or total <= 0:
        return None
    return total, index
