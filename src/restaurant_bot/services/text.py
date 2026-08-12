from __future__ import annotations

import html
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from restaurant_bot.text_normalization import (
    clean_text as _clean_text,
)
from restaurant_bot.text_normalization import (
    normalize_text as _normalize_text,
)

UNIT_ALIASES: dict[str, str] = {
    "шт": "шт",
    "штук": "шт",
    "штука": "шт",
    "штуки": "шт",
    "штуку": "шт",
    "штуке": "шт",
    "штуках": "шт",
    "штуками": "шт",
    "ед": "шт",
    "кг": "кг",
    "кило": "кг",
    "килограмм": "кг",
    "килограмма": "кг",
    "килограммов": "кг",
    "килограмму": "кг",
    "килограммы": "кг",
    "килограмме": "кг",
    "килограммах": "кг",
    "килограммами": "кг",
    "г": "г",
    "гр": "г",
    "грам": "г",
    "грамм": "г",
    "грамма": "г",
    "граммов": "г",
    "грамму": "г",
    "граммы": "г",
    "грамме": "г",
    "граммах": "г",
    "граммами": "г",
    "л": "л",
    "литр": "л",
    "литра": "л",
    "литров": "л",
    "литру": "л",
    "литры": "л",
    "литре": "л",
    "литрах": "л",
    "литрами": "л",
    "мл": "мл",
    "миллилитр": "мл",
    "миллилитра": "мл",
    "миллилитров": "мл",
    "миллилитру": "мл",
    "миллилитры": "мл",
    "миллилитре": "мл",
    "миллилитрах": "мл",
    "миллилитрами": "мл",
    "уп": "уп",
    "упак": "уп",
    "упаковка": "уп",
    "упаковки": "уп",
    "упаковок": "уп",
    "кор": "кор",
    "короб": "кор",
    "коробка": "кор",
    "коробки": "кор",
    "коробок": "кор",
    "пач": "пач",
    "пачка": "пач",
    "пачки": "пач",
    "пачек": "пач",
    "бан": "бан",
    "банка": "бан",
    "банки": "бан",
    "банок": "бан",
    "бут": "бут",
    "бутылка": "бут",
    "бутылки": "бут",
    "бутылок": "бут",
    "ведро": "ведро",
    "ведра": "ведро",
    "ведер": "ведро",
    "вёдра": "ведро",
    "вёдер": "ведро",
}

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
}

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


def remove_global_comment_overlap(item_comment: str, global_comment: str) -> str:
    """Удаляет общую часть и разговорные слова охвата из локального комментария."""
    item_text = _clean_text(item_comment).strip(" .,;")
    global_text = _clean_text(global_comment).strip(" .,;")
    if not item_text or not global_text:
        return item_text
    match = re.search(re.escape(global_text), item_text, flags=re.I)
    if match is None:
        return item_text
    remaining = f"{item_text[: match.start()]} {item_text[match.end() :]}"
    scope_residue = (
        r"(?:(?:все|всё|всем)"
        r"(?:\s+(?:это(?:\s+дело)?|эти\w*"
        r"(?:\s+(?:товар\w*|позици\w*))?|товар\w*|позици\w*))?"
        r"|для\s+всех(?:\s+(?:товар\w*|позици\w*))?)"
    )
    remaining = re.sub(
        rf"(?:\b(?:и|а)\s+)?{scope_residue}\s*(?=$|[,;:—–-])",
        " ",
        remaining,
        flags=re.I,
    )
    remaining = re.sub(r"\s+", " ", remaining)
    return remaining.strip(" .,;:-—–")


def remove_phrase_overlap(source_text: str, phrase: str) -> str:
    """Убирает подтверждённую фразу из поисковой копии, не меняя исходные данные."""
    source = _clean_text(source_text)
    phrase_tokens = [
        _normalize_text(token)
        for token in re.findall(r"[a-zа-яё0-9%]+", _normalize_text(phrase), flags=re.I)
    ]
    source_matches = list(re.finditer(r"[a-zа-яё0-9%]+", source, flags=re.I))
    source_tokens = [_normalize_text(match.group()) for match in source_matches]
    if not source_tokens or not phrase_tokens or len(phrase_tokens) > len(source_tokens):
        return source
    for start in range(len(source_tokens) - len(phrase_tokens) + 1):
        if source_tokens[start : start + len(phrase_tokens)] != phrase_tokens:
            continue
        left = source[: source_matches[start].start()].strip()
        right = source[source_matches[start + len(phrase_tokens) - 1].end() :].strip()
        replacement = _clean_text(f"{left} {right}")
        return replacement or source
    return source


def normalize_unit(value: Any) -> str:
    """Нормализует единицу измерения."""
    text = _normalize_text(value)
    return UNIT_ALIASES.get(text, _clean_text(value))


def normalize_department(value: Any) -> str:
    """Приводит название отдела к заголовку листа заявки."""
    text = _normalize_text(value)
    return DEPARTMENT_ALIASES.get(text, _clean_text(value))


def numeric_range_spans(value: Any) -> list[tuple[int, int]]:
    """Находит цифровые и словесные диапазоны характеристик товара."""
    text = _clean_text(value)
    if not text:
        return []
    unit_pattern = "|".join(
        sorted((re.escape(unit) for unit in UNIT_ALIASES), key=len, reverse=True)
    )
    pattern = re.compile(
        rf"(?<!\w)\d+(?:[,.]\d+)?\s*(?:--|[-–—]|/|на|x|х)\s*\d+(?:[,.]\d+)?"
        rf"(?:\s*(?:{unit_pattern}))?\b",
        flags=re.IGNORECASE,
    )
    spans = [match.span() for match in pattern.finditer(text)]
    number_word = "|".join(
        sorted((re.escape(word) for word in NUMBER_WORDS), key=len, reverse=True)
    )
    word_phrase = rf"(?:{number_word})(?:\s+(?:{number_word}))*"
    spoken_pattern = re.compile(
        rf"(?<!\w){word_phrase}\s*[-–—]\s*{word_phrase}"
        rf"(?:\s*(?:{unit_pattern}))?\b",
        flags=re.IGNORECASE,
    )
    spans.extend(match.span() for match in spoken_pattern.finditer(text))
    return sorted(spans)


def to_float(value: Any) -> float | None:
    """Безопасно преобразует значение в число."""
    if value is None or value == "":
        return None
    raw = _clean_text(value).replace(" ", "").replace(",", ".")
    raw = re.sub(r"[^0-9.\-]", "", raw)
    if raw.count(".") > 1:
        sign = "-" if raw.startswith("-") else ""
        parts = raw.lstrip("-").split(".")
        # Google Sheets может добавлять к российской валюте букву и точку.
        # Последние одна-две цифры считаются десятичной частью, предыдущие
        # точки — визуальными разделителями. Более длинная последняя группа
        # означает, что все точки являются разделителями.
        if parts[-1] and len(parts[-1]) <= 2:
            raw = sign + "".join(parts[:-1]) + "." + parts[-1]
        else:
            raw = sign + "".join(parts)
    try:
        number = float(Decimal(raw))
    except (InvalidOperation, ValueError):
        return None
    return number if number > 0 else None


def escape(value: Any) -> str:
    """Экранирует текст для безопасного HTML Telegram."""
    return html.escape(_clean_text(value), quote=False)


def format_number(value: float | None) -> str:
    """Форматирует число для сообщения пользователю."""
    if value is None:
        return "-"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.3f}".rstrip("0").rstrip(".").replace(".", ",")


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


def convert_quantity(quantity: float, source_unit: str, target_unit: str) -> float | None:
    """Преобразует количество между совместимыми единицами."""
    source = normalize_unit(source_unit)
    target = normalize_unit(target_unit)
    if source == target or not source or not target:
        return quantity
    factors = {
        ("г", "кг"): 0.001,
        ("кг", "г"): 1000,
        ("мл", "л"): 0.001,
        ("л", "мл"): 1000,
    }
    factor = factors.get((source, target))
    return quantity * factor if factor else None
