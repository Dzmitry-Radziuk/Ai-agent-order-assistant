from __future__ import annotations

import re

from restaurant_bot.text_normalization import normalize_text


def has_supported_voice_letters(transcript: str) -> bool:
    """Проверяет наличие латинских или русских букв в транскрипте."""
    return bool(re.search(r"[A-Za-zА-Яа-яЁё]", transcript))


def select_transcription_result(primary: str, retry: str) -> str:
    """Выбирает более полный и пригодный результат повторной транскрипции."""
    primary_normalized = normalize_text(primary)
    retry_normalized = normalize_text(retry)
    if primary_normalized in {"тестовый товар", "тест товар", "test product"}:
        return retry
    if not has_supported_voice_letters(retry):
        return primary
    if not has_supported_voice_letters(primary):
        return retry
    primary_words = re.findall(r"[a-zа-яё0-9]+", primary_normalized, flags=re.I)
    retry_words = re.findall(r"[a-zа-яё0-9]+", retry_normalized, flags=re.I)
    primary_has_list_structure = bool(
        re.search(r"[,;\n]", primary)
        or len(re.findall(r"\d+(?:[,.]\d+)?", primary_normalized)) >= 2
    )
    if (
        len(primary_words) >= 6
        and len(retry_words) * 2 < len(primary_words)
        and primary_has_list_structure
    ):
        return primary
    return retry
