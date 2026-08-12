"""Содержит переходные текстовые и числовые primitives без presentation-кода."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from restaurant_bot.text_normalization import clean_text, normalize_text


def remove_global_comment_overlap(item_comment: str, global_comment: str) -> str:
    """Удаляет общую часть и разговорные слова охвата из локального комментария."""
    item_text = clean_text(item_comment).strip(" .,;")
    global_text = clean_text(global_comment).strip(" .,;")
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
    source = clean_text(source_text)
    phrase_tokens = [
        normalize_text(token)
        for token in re.findall(r"[a-zа-яё0-9%]+", normalize_text(phrase), flags=re.I)
    ]
    source_matches = list(re.finditer(r"[a-zа-яё0-9%]+", source, flags=re.I))
    source_tokens = [normalize_text(match.group()) for match in source_matches]
    if not source_tokens or not phrase_tokens or len(phrase_tokens) > len(source_tokens):
        return source
    for start in range(len(source_tokens) - len(phrase_tokens) + 1):
        if source_tokens[start : start + len(phrase_tokens)] != phrase_tokens:
            continue
        left = source[: source_matches[start].start()].strip()
        right = source[source_matches[start + len(phrase_tokens) - 1].end() :].strip()
        replacement = clean_text(f"{left} {right}")
        return replacement or source
    return source


def to_float(value: Any) -> float | None:
    """Безопасно преобразует значение в число."""
    if value is None or value == "":
        return None
    raw = clean_text(value).replace(" ", "").replace(",", ".")
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
