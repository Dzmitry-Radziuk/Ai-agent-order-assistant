"""Содержит единый внутренний контракт семантических фактов сообщения."""

from restaurant_bot.parsing.semantic.models import (
    SemanticFact,
    SemanticFactKind,
    SemanticItemReference,
)

__all__ = ["SemanticFact", "SemanticFactKind", "SemanticItemReference"]
