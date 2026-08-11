"""Координирует нейтральную к каналу StateCompatibilityPolicy."""

from __future__ import annotations

from restaurant_bot.conversation.routing.comment_scope import CommentScopePolicyMixin
from restaurant_bot.conversation.routing.contracts import (
    CompatibilityAction,
    CompatibilityContext,
    CompatibilityDecision,
)
from restaurant_bot.conversation.routing.item_resolution import ItemResolutionPolicyMixin
from restaurant_bot.conversation.routing.order_flow import OrderFlowPolicyMixin
from restaurant_bot.domain.models import (
    ConversationState,
    Intent,
    ItemStatus,
    ParsedCommand,
    SessionStage,
)


class StateCompatibilityPolicy(
    ItemResolutionPolicyMixin, OrderFlowPolicyMixin, CommentScopePolicyMixin
):
    """Определяет совместимость intent с поддержанным modal state."""

    _QUANTITY_STAGES = frozenset({SessionStage.AWAIT_UNIT_QUANTITY})
    _CONTINUE_INTENTS = frozenset(
        {
            Intent.EDIT_QUANTITY,
            Intent.ENTER_OTHER_QUANTITY,
            Intent.USE_CATALOG_UNIT,
            Intent.UNIT_EDIT,
            Intent.UNIT_OK,
            Intent.CONTINUE_CURRENT,
        }
    )
    _INTERRUPT_INTENTS = frozenset(
        {
            Intent.REMOVE_ITEM,
            Intent.SHOW_CART,
            Intent.START_NEW_ORDER,
            Intent.CLEAR_CART,
            Intent.BACK,
            Intent.THANKS,
            Intent.GREETING,
            Intent.HELP,
            Intent.SMALL_TALK,
            Intent.ORDER_STATUS,
            Intent.PRODUCT_ADD_LIST,
            Intent.SUBMIT_REQUEST,
            Intent.SHOW_FINAL_REVIEW,
        }
    )

    def evaluate(
        self,
        command: ParsedCommand,
        state: ConversationState,
        context: CompatibilityContext | None = None,
    ) -> CompatibilityDecision:
        """Возвращает решение для поддержанного modal-контекста."""
        context = context or self.context_for(state)
        if context is CompatibilityContext.COMMENT_SCOPE:
            return self._evaluate_comment_scope(command, state)
        if context is CompatibilityContext.MANUAL_DETAILS:
            return self._evaluate_manual_details(command, state)
        if context is CompatibilityContext.PRODUCT_ADD_DETAILS:
            return self._evaluate_product_add_details(command, state)
        if context is CompatibilityContext.ADD_MORE_CONFIRM:
            return self._evaluate_add_more_confirm(command, state)
        if context is CompatibilityContext.SUBMIT_CONFIRM:
            return self._evaluate_submit_confirm(command, state)
        if context is CompatibilityContext.SUBMISSION_FAILED:
            return self._evaluate_submission_failed(command, state)
        if context is CompatibilityContext.NEW_ORDER_CONFIRMATION:
            return self._evaluate_new_order_confirmation(command, state)
        if context is CompatibilityContext.SHEET_REVIEW:
            return self._evaluate_sheet_review(command, state)
        if context is CompatibilityContext.CANDIDATE_SELECTION:
            return self._evaluate_candidate_selection(command, state)
        if context is CompatibilityContext.NOT_FOUND:
            return self._evaluate_not_found(command, state)
        if context is CompatibilityContext.DUPLICATE_PENDING:
            return self._evaluate_duplicate_pending(command, state)
        if context is CompatibilityContext.UNIT_MISMATCH:
            return self._evaluate_unit_mismatch(command, state)
        if context is not CompatibilityContext.QUANTITY:
            return CompatibilityDecision(CompatibilityAction.NOT_APPLICABLE)

        item = state.current_item()
        if (
            item is None
            or item.status is not ItemStatus.MISSING_QTY
            or state.stage not in self._QUANTITY_STAGES
        ):
            return CompatibilityDecision(CompatibilityAction.NOT_APPLICABLE)

        if command.intent is Intent.ADD_ITEMS and self._has_product_items(command):
            return CompatibilityDecision(CompatibilityAction.INTERRUPT)
        if command.intent in self._INTERRUPT_INTENTS:
            return CompatibilityDecision(CompatibilityAction.INTERRUPT)
        if command.intent in self._CONTINUE_INTENTS:
            return CompatibilityDecision(CompatibilityAction.CONTINUE)

        # Неизвестная команда остаётся доступной детерминированному обработчику количества
        # для коротких ответов: отдельного числа или единицы измерения.
        return CompatibilityDecision(CompatibilityAction.CONTINUE)

    def should_try_contextual_fallback(
        self,
        state: ConversationState,
        command: ParsedCommand,
        context: CompatibilityContext,
    ) -> bool:
        """Проверяет, можно ли передать команду contextual parser-у."""
        decision = self.evaluate(command, state, context)
        return decision.action in {
            CompatibilityAction.CONTINUE,
            CompatibilityAction.AMBIGUOUS,
        }

    @staticmethod
    def context_for(state: ConversationState) -> CompatibilityContext | None:
        """Определяет поддержанный modal-контекст по состоянию без разбора текста."""
        if state.stage is SessionStage.SUBMISSION_FAILED:
            return CompatibilityContext.SUBMISSION_FAILED
        if state.pending_new_order_confirmation:
            return CompatibilityContext.NEW_ORDER_CONFIRMATION
        if state.stage is SessionStage.REVIEW and state.review_mode == "sheet_link":
            return CompatibilityContext.SHEET_REVIEW
        if state.pending_comment_items:
            return CompatibilityContext.COMMENT_SCOPE
        item = state.current_item()
        if (
            state.stage is SessionStage.AWAIT_MANUAL_DETAILS
            and state.current_issue_item_id
            and item is not None
        ):
            return CompatibilityContext.MANUAL_DETAILS
        if (
            state.stage is SessionStage.AWAIT_PRODUCT_ADD_DETAILS
            and state.pending_product_add_request_id
            and state.pending_product_add_item_index is not None
            and 0 <= state.pending_product_add_item_index < len(state.cart)
        ):
            return CompatibilityContext.PRODUCT_ADD_DETAILS
        if item is not None and item.status is ItemStatus.AMBIGUOUS and item.candidates:
            return CompatibilityContext.CANDIDATE_SELECTION
        if item is not None and item.status is ItemStatus.NOT_FOUND:
            return CompatibilityContext.NOT_FOUND
        if item is not None and item.status is ItemStatus.DUPLICATE_PENDING:
            return CompatibilityContext.DUPLICATE_PENDING
        if item is not None and item.status is ItemStatus.UNIT_MISMATCH:
            return CompatibilityContext.UNIT_MISMATCH
        if (
            item is not None
            and item.status is ItemStatus.MISSING_QTY
            and state.stage in StateCompatibilityPolicy._QUANTITY_STAGES
        ):
            return CompatibilityContext.QUANTITY
        if StateCompatibilityPolicy._can_use_add_more_context(state):
            return CompatibilityContext.ADD_MORE_CONFIRM
        if StateCompatibilityPolicy._can_use_submit_confirm_context(state):
            return CompatibilityContext.SUBMIT_CONFIRM
        return None
