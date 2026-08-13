"""Прикладные контракты обработки диалога."""

from restaurant_bot.application.conversation.contracts import (
    ConversationEffectPlan,
    ConversationInput,
    ConversationInteraction,
    ConversationResult,
    ConversationView,
    SemanticAction,
)
from restaurant_bot.application.conversation.use_case import ConversationApplication

__all__ = [
    "ConversationApplication",
    "ConversationEffectPlan",
    "ConversationInput",
    "ConversationInteraction",
    "ConversationResult",
    "ConversationView",
    "SemanticAction",
]
