"""Сохраняет старый путь импорта modal routing."""

from restaurant_bot.conversation.routing.modal_routing import (
    ModalRoutingDecision,
    evaluate_modal_routing,
)

__all__ = ["ModalRoutingDecision", "evaluate_modal_routing"]
