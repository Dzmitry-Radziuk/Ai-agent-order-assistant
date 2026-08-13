"""Содержит provider-specific политику моделей транскрипции OpenAI."""

from __future__ import annotations


def has_distinct_models(openai_service: object) -> bool:
    """Сравнивает основную и уточняющую модели распознавания."""
    settings = getattr(openai_service, "settings", None)
    primary = getattr(settings, "openai_transcribe_model", None)
    fallback = getattr(settings, "openai_transcribe_fallback_model", None)
    if not isinstance(primary, str) or not isinstance(fallback, str):
        return True
    primary_name = primary.strip().casefold()
    fallback_name = fallback.strip().casefold()
    if not primary_name or not fallback_name or primary_name == fallback_name:
        return False
    return not ("mini" in fallback_name and "mini" not in primary_name)
