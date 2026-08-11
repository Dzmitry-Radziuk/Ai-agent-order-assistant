"""Содержит нейтральную к каналу маршрутизацию modal-состояний."""

from restaurant_bot.conversation.routing.contracts import (
    CompatibilityAction,
    CompatibilityContext,
    CompatibilityDecision,
)
from restaurant_bot.conversation.routing.modal_routing import (
    ModalRoutingDecision,
    evaluate_modal_routing,
)
from restaurant_bot.conversation.routing.state_compatibility import StateCompatibilityPolicy

__all__ = [
    "CompatibilityAction",
    "CompatibilityContext",
    "CompatibilityDecision",
    "ModalRoutingDecision",
    "StateCompatibilityPolicy",
    "evaluate_modal_routing",
]
