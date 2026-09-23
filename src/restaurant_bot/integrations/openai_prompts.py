"""Повторно экспортирует неизменённые системные контракты OpenAI."""

from restaurant_bot.integrations.openai_decision_prompts import (
    _COMMENT_SCOPE_SYSTEM,
    _MATCH_SYSTEM,
    _VISIBLE_ACTION_SYSTEM,
)
from restaurant_bot.integrations.openai_photo_prompts import (
    _PHOTO_OBSERVATION_CONTRACT,
    _PHOTO_SYSTEM,
)
from restaurant_bot.integrations.openai_text_prompt import _TEXT_SYSTEM

__all__ = [
    "_COMMENT_SCOPE_SYSTEM",
    "_MATCH_SYSTEM",
    "_PHOTO_OBSERVATION_CONTRACT",
    "_PHOTO_SYSTEM",
    "_TEXT_SYSTEM",
    "_VISIBLE_ACTION_SYSTEM",
]
