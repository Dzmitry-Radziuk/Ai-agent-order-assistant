"""Нормализует текст и распознаёт отрицание в командах."""

from __future__ import annotations

import re

from restaurant_bot.text_normalization import normalize_text


def normalize_command_text(value: str) -> str:
    """Нормализует пунктуацию и разговорные слова команды."""
    text = re.sub(r"[.,!?;:]+", " ", normalize_text(value))
    text = re.sub(r"\s+", " ", text).strip()
    for _ in range(3):
        cleaned = re.sub(r"^(?:ну|это|так|ладно|хорошо|ок|окей|ага)\s+", "", text, flags=re.I)
        if cleaned == text:
            break
        text = cleaned.strip()
    return re.sub(r"\s+пожалуйста$", "", text, flags=re.I).strip()


def _has_word_stem(words: list[str], *stems: str) -> bool:
    """Проверяет наличие слова с одним из заданных корней."""
    return any(word.startswith(stems) for word in words)


def has_negation(text: str) -> bool:
    """Определяет явное отрицание в пользовательской фразе."""
    words = re.findall(r"[a-zа-яё0-9-]+", normalize_text(text), flags=re.I)
    return any(word in {"не", "нет", "никогда", "никак"} for word in words)


def has_negated_action(text: str, *action_stems: str) -> bool:
    """Находит действие, отрицаемое в пределах короткой разговорной фразы."""
    words = re.findall(r"[a-zа-яё0-9-]+", normalize_text(text), flags=re.I)
    for index, word in enumerate(words):
        if word not in {"не", "нет", "никогда", "никак"}:
            continue
        # «не хочу сейчас добавлять», «не надо больше отправлять» и похожие
        # формы сохраняют отрицание, даже когда между частицей и глаголом
        # стоят модальные или вводные слова.
        window = words[index + 1 : index + 7]
        if any(candidate.startswith(action_stems) for candidate in window):
            return True
    return False


def is_explicit_item_rejection(text: str) -> bool:
    """Распознаёт однозначный отказ от текущей товарной позиции."""
    normalized = normalize_command_text(text)
    words = re.findall(r"[a-zа-яё0-9-]+", normalized, flags=re.I)
    if not words:
        return False

    has_current_marker = _has_word_stem(
        words,
        "этот",
        "эту",
        "это",
        "данн",
        "текущ",
    )
    has_item_target = _has_word_stem(words, "товар", "позици", "строк", "пункт")
    if _has_word_stem(words, "пропуст", "пропуск", "скип") or (
        _has_word_stem(words, "исключ", "вычерк", "откаж")
        and has_current_marker
        and has_item_target
    ):
        return True
    if _has_word_stem(words, "убер", "удал") and has_current_marker and has_item_target:
        return True
    if re.search(r"\bне\s+(?:нужен|нужна|нужно|нужны)\b", normalized) and (
        has_item_target or has_current_marker
    ):
        return True
    return bool(
        has_negated_action(normalized, "добав")
        and (
            has_current_marker
            or re.fullmatch(
                r"(?:не\s+)?добав\w*(?:\s+(?:товар|позицию|строку|пункт))?",
                normalized,
            )
        )
    )
