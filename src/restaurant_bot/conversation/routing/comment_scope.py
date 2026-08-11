"""Содержит policy области комментария поставщику."""

from __future__ import annotations

from restaurant_bot.conversation.routing.contracts import CompatibilityAction, CompatibilityDecision
from restaurant_bot.domain.models import ConversationState, Intent, ParsedCommand


class CommentScopePolicyMixin:
    """Владеет совместимостью pending comment scope."""

    def _evaluate_comment_scope(
        self,
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
            if self._has_comment_scope_product_items(command):
                return CompatibilityDecision(CompatibilityAction.INTERRUPT)
            return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)
        if command.intent in {Intent.UNKNOWN, Intent.CLARIFY_CURRENT, Intent.CANCEL}:
            return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)
        return CompatibilityDecision(CompatibilityAction.INTERRUPT)

    @staticmethod
    def _has_comment_scope_product_items(command: ParsedCommand) -> bool:
        """Считает любую явно извлечённую товарную позицию новым intent."""
        return command.intent is Intent.ADD_ITEMS and any(
            item.product_query.strip() for item in command.items
        )
