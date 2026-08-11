"""Содержит чистую policy совместимости области комментария."""

from __future__ import annotations

from restaurant_bot.conversation.routing.contracts import CompatibilityAction, CompatibilityDecision
from restaurant_bot.domain.models import ConversationState, Intent, ParsedCommand


def evaluate_comment_scope(
    command: ParsedCommand,
    state: ConversationState,
) -> CompatibilityDecision:
    """Разделяет ответ области комментария и независимый intent."""
    if not state.pending_comment_items:
        return CompatibilityDecision(CompatibilityAction.NOT_APPLICABLE)

    if command.comment_scope_action in {"items", "order", "cancel"}:
        return CompatibilityDecision(CompatibilityAction.CONTINUE)
    if command.comment_scope_action == "ambiguous":
        return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)
    if command.intent is Intent.ADD_ITEMS:
        if has_comment_scope_product_items(command):
            return CompatibilityDecision(CompatibilityAction.INTERRUPT)
        return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)
    if command.intent in {Intent.UNKNOWN, Intent.CLARIFY_CURRENT, Intent.CANCEL}:
        return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)
    return CompatibilityDecision(CompatibilityAction.INTERRUPT)


def has_comment_scope_product_items(command: ParsedCommand) -> bool:
    """Считает явные товарные позиции новым самостоятельным intent."""
    return command.intent is Intent.ADD_ITEMS and any(
        item.product_query.strip() for item in command.items
    )
