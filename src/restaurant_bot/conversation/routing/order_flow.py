"""Содержит чистые политики подтверждения заявки и review-контекстов."""

from __future__ import annotations

from collections.abc import Callable

from restaurant_bot.conversation.comments import has_pending_comment_scope
from restaurant_bot.conversation.routing.contracts import (
    CompatibilityAction,
    CompatibilityContext,
    CompatibilityDecision,
)
from restaurant_bot.conversation.routing.item_resolution import (
    has_concrete_new_items,
    has_product_items,
)
from restaurant_bot.domain.models import (
    ConversationState,
    DialogueResponse,
    Intent,
    ItemStatus,
    ParsedCommand,
    SessionStage,
)


def evaluate_sheet_review(
    command: ParsedCommand,
    state: ConversationState,
) -> CompatibilityDecision:
    """Разрешает только подтверждённые действия карточки sheet-review."""
    if state.stage is not SessionStage.REVIEW or state.review_mode != "sheet_link":
        return CompatibilityDecision(CompatibilityAction.NOT_APPLICABLE)

    if command.intent is Intent.ADD_ITEMS:
        if command.items:
            return CompatibilityDecision(CompatibilityAction.INTERRUPT)
        return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)

    if command.intent in {
        Intent.REVIEW_ORDER,
        Intent.REVIEW_REFRESH,
        Intent.REVIEW_SUBMIT,
        Intent.REVIEW_CANCEL,
        Intent.SUBMIT_REQUEST,
        Intent.SUBMIT_AS_IS,
        Intent.CONFIRM,
        Intent.CANCEL,
        Intent.BACK,
    }:
        return CompatibilityDecision(CompatibilityAction.CONTINUE)

    if command.intent is Intent.UNKNOWN and command.dialogue_response in {
        DialogueResponse.AFFIRM,
        DialogueResponse.DECLINE,
    }:
        return CompatibilityDecision(CompatibilityAction.CONTINUE)

    if command.intent is Intent.UNKNOWN:
        return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)

    return CompatibilityDecision(CompatibilityAction.INTERRUPT)


def evaluate_new_order_confirmation(
    command: ParsedCommand,
    state: ConversationState,
) -> CompatibilityDecision:
    """Решает судьбу команды поверх подтверждения новой заявки."""
    if not state.pending_new_order_confirmation:
        return CompatibilityDecision(CompatibilityAction.NOT_APPLICABLE)
    if state.stage in {SessionStage.SUBMISSION_FAILED, SessionStage.SUBMITTING} or (
        state.pending_submission is not None
    ):
        return CompatibilityDecision(CompatibilityAction.REJECT, mode="frozen_submission")

    if command.intent is Intent.ADD_ITEMS and has_product_items(command):
        return CompatibilityDecision(CompatibilityAction.INTERRUPT, mode="independent")
    if command.intent is Intent.CLEAR_CART:
        return CompatibilityDecision(CompatibilityAction.CONTINUE, mode="yes")
    if command.intent is Intent.START_NEW_ORDER:
        return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)
    if command.intent in {Intent.CANCEL, Intent.BACK} or (
        command.dialogue_response is DialogueResponse.DECLINE
    ):
        return CompatibilityDecision(CompatibilityAction.CONTINUE, mode="no")
    if command.intent is Intent.CONFIRM and not has_product_items(command):
        return CompatibilityDecision(CompatibilityAction.CONTINUE, mode="yes")
    if command.dialogue_response is DialogueResponse.AFFIRM and not has_product_items(command):
        return CompatibilityDecision(CompatibilityAction.CONTINUE, mode="yes")
    if command.intent is Intent.UNKNOWN and command.quantity_hint is not None:
        return CompatibilityDecision(CompatibilityAction.INTERRUPT, mode="underlying")
    if (
        command.intent
        in {
            Intent.SELECT_CANDIDATE,
            Intent.MANUAL_CURRENT,
            Intent.SKIP_CURRENT,
            Intent.CLARIFY_CURRENT,
            Intent.PRODUCT_ADD,
            Intent.PRODUCT_ADD_RETRY,
            Intent.PRODUCT_ADD_SKIP,
            Intent.SEARCH_ALL_SUPPLIERS,
            Intent.SWITCH_SUPPLIER,
            Intent.USE_CATALOG_UNIT,
            Intent.UNIT_OK,
            Intent.UNIT_EDIT,
            Intent.ENTER_OTHER_QUANTITY,
            Intent.ACCEPT_SUGGESTED_QUANTITY,
            Intent.KEEP_CURRENT_QUANTITY,
            Intent.KEEP_MULTIPLE,
            Intent.FIX_MULTIPLE,
            Intent.EDIT_MULTIPLE,
            Intent.MERGE_DUPLICATE,
            Intent.CONTINUE_CURRENT,
        }
        or command.comment_scope_action
    ):
        return CompatibilityDecision(CompatibilityAction.INTERRUPT, mode="underlying")
    if command.intent in {
        Intent.REMOVE_ITEM,
        Intent.EDIT_QUANTITY,
        Intent.EDIT_COMMENT,
        Intent.SHOW_CART,
        Intent.SHOW_FINAL_REVIEW,
        Intent.ORDER_STATUS,
        Intent.HELP,
        Intent.THANKS,
        Intent.SMALL_TALK,
        Intent.GREETING,
        Intent.PRODUCT_ADD_LIST,
        Intent.ADD_MORE,
        Intent.SUBMIT_REQUEST,
        Intent.SUBMIT_AS_IS,
    }:
        return CompatibilityDecision(CompatibilityAction.INTERRUPT, mode="independent")
    if command.dialogue_response is DialogueResponse.UNCERTAIN or command.intent is Intent.UNKNOWN:
        return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)
    return CompatibilityDecision(CompatibilityAction.INTERRUPT, mode="independent")


def evaluate_submission_failed(
    command: ParsedCommand,
    state: ConversationState,
) -> CompatibilityDecision:
    """Разрешает восстановление или безопасный сброс неудачной отправки."""
    mode = submission_failure_mode(state)
    if state.stage is not SessionStage.SUBMISSION_FAILED:
        return CompatibilityDecision(CompatibilityAction.NOT_APPLICABLE)
    if state.pending_submission is None:
        if command.intent in {
            Intent.BACK,
            Intent.SHOW_CART,
            Intent.HELP,
            Intent.THANKS,
            Intent.SMALL_TALK,
            Intent.GREETING,
            Intent.ORDER_STATUS,
            Intent.PRODUCT_ADD_LIST,
        }:
            return CompatibilityDecision(CompatibilityAction.CONTINUE, mode="broken")
        return CompatibilityDecision(CompatibilityAction.REJECT, mode="broken")

    if command.intent in {Intent.CLEAR_CART, Intent.START_NEW_ORDER}:
        return CompatibilityDecision(CompatibilityAction.CONTINUE, mode="reset")

    retry = (
        command.retry_requested
        or command.intent in {Intent.SUBMIT_AS_IS, Intent.SUBMIT_REQUEST, Intent.CONFIRM}
        or command.dialogue_response is DialogueResponse.AFFIRM
    )
    if mode == "dispatch_uncertain":
        if retry:
            return CompatibilityDecision(CompatibilityAction.REJECT, mode=mode)
        if command.intent in {
            Intent.BACK,
            Intent.SHOW_CART,
            Intent.HELP,
            Intent.THANKS,
            Intent.SMALL_TALK,
            Intent.GREETING,
            Intent.ORDER_STATUS,
            Intent.PRODUCT_ADD_LIST,
        }:
            return CompatibilityDecision(CompatibilityAction.CONTINUE, mode=mode)
        return CompatibilityDecision(CompatibilityAction.REJECT, mode=mode)
    if retry:
        return CompatibilityDecision(CompatibilityAction.CONTINUE, mode=mode)
    if (
        command.intent
        in {
            Intent.BACK,
            Intent.SHOW_CART,
            Intent.HELP,
            Intent.THANKS,
            Intent.SMALL_TALK,
            Intent.GREETING,
            Intent.ORDER_STATUS,
            Intent.CANCEL,
            Intent.PRODUCT_ADD_LIST,
        }
        or command.dialogue_response is DialogueResponse.DECLINE
    ):
        return CompatibilityDecision(CompatibilityAction.CONTINUE, mode=mode)
    if command.intent is Intent.UNKNOWN or command.dialogue_response is DialogueResponse.UNCERTAIN:
        return CompatibilityDecision(CompatibilityAction.AMBIGUOUS, mode=mode)
    return CompatibilityDecision(CompatibilityAction.REJECT, mode=mode)


def submission_failure_mode(state: ConversationState) -> str:
    """Определяет structured-режим сбоя без анализа текста ошибки."""
    pending = state.pending_submission
    if state.status == "dispatch_uncertain" or (
        pending is not None and pending.failed_stage == "dispatch_uncertain"
    ):
        return "dispatch_uncertain"
    return "retryable"


def evaluate_add_more_confirm(
    command: ParsedCommand,
    state: ConversationState,
    context_for: Callable[[ConversationState], CompatibilityContext | None],
) -> CompatibilityDecision:
    """Разрешает ответ на вопрос о продолжении сбора товаров."""
    if context_for(state) is not CompatibilityContext.ADD_MORE_CONFIRM:
        return CompatibilityDecision(CompatibilityAction.NOT_APPLICABLE)

    if command.dialogue_response is DialogueResponse.AFFIRM:
        return CompatibilityDecision(CompatibilityAction.CONTINUE)
    if command.dialogue_response is DialogueResponse.DECLINE:
        return CompatibilityDecision(CompatibilityAction.CONTINUE)
    if command.dialogue_response is DialogueResponse.UNCERTAIN:
        return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)
    if command.intent in {Intent.ADD_MORE, Intent.CONFIRM}:
        # Текстовые команды заранее нормализуются в dialogue_response.
        # Непустая команда без этого признака не продолжает modal-вопрос.
        if not command.text:
            return CompatibilityDecision(CompatibilityAction.CONTINUE)
        return CompatibilityDecision(CompatibilityAction.INTERRUPT)
    if command.intent is Intent.ADD_ITEMS:
        return CompatibilityDecision(CompatibilityAction.CONTINUE)
    if command.intent in {Intent.BACK, Intent.CANCEL, Intent.SHOW_CART}:
        return CompatibilityDecision(CompatibilityAction.CONTINUE)
    if command.intent is Intent.UNKNOWN:
        return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)
    return CompatibilityDecision(CompatibilityAction.INTERRUPT)


def evaluate_submit_confirm(
    command: ParsedCommand,
    state: ConversationState,
) -> CompatibilityDecision:
    """Разрешает подтверждение отправки только в валидном review-контексте."""
    if not can_use_submit_confirm_context(state):
        return CompatibilityDecision(CompatibilityAction.NOT_APPLICABLE)
    if command.intent is Intent.ADD_ITEMS:
        if has_concrete_new_items(command):
            return CompatibilityDecision(CompatibilityAction.INTERRUPT)
        return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)
    if command.intent is Intent.ADD_MORE:
        return CompatibilityDecision(CompatibilityAction.INTERRUPT)
    if command.dialogue_response is DialogueResponse.UNCERTAIN:
        return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)
    if command.dialogue_response in {DialogueResponse.AFFIRM, DialogueResponse.DECLINE}:
        return CompatibilityDecision(CompatibilityAction.CONTINUE)
    if command.intent in {
        Intent.CONFIRM,
        Intent.SUBMIT_REQUEST,
        Intent.SUBMIT_AS_IS,
        Intent.BACK,
        Intent.CANCEL,
        Intent.SHOW_CART,
        Intent.SHOW_FINAL_REVIEW,
        Intent.CHECK_MIN_SUM,
        Intent.CHOOSE_SUPPLIER_WARNING,
        Intent.ADD_SUPPLIER_ITEMS,
        Intent.SEARCH_ALL_SUPPLIERS,
        Intent.SWITCH_SUPPLIER,
        Intent.SELECT_DEPARTMENT,
    }:
        return CompatibilityDecision(CompatibilityAction.CONTINUE)
    if command.intent is Intent.UNKNOWN:
        return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)
    return CompatibilityDecision(CompatibilityAction.INTERRUPT)


def can_use_add_more_context(state: ConversationState) -> bool:
    """Проверяет, что add-more prompt не смешан с конкретным modal state."""
    if state.stage is not SessionStage.AWAIT_ADD_MORE_CONFIRM:
        return False
    if (
        has_pending_comment_scope(state)
        or state.current_issue_item_id
        or state.pending_product_add_request_id
        or state.manual_item_index is not None
        or state.unit_item_index is not None
        or state.edit_multiple_index is not None
    ):
        return False
    return not any(
        item.status
        in {
            ItemStatus.AMBIGUOUS,
            ItemStatus.NOT_FOUND,
            ItemStatus.DUPLICATE_PENDING,
            ItemStatus.UNIT_MISMATCH,
            ItemStatus.MISSING_QTY,
            ItemStatus.AI_PENDING,
        }
        for item in state.cart
    )


def can_use_submit_confirm_context(state: ConversationState) -> bool:
    """Проверяет инвариант обычного финального review без raw-текста."""
    if state.stage is not SessionStage.AWAIT_SUBMIT_CONFIRM or state.review_mode != "cart":
        return False
    if (
        has_pending_comment_scope(state)
        or state.current_issue_item_id
        or state.pending_product_add_request_id
        or state.manual_item_index is not None
        or state.unit_item_index is not None
        or state.edit_multiple_index is not None
        or state.pending_new_order_confirmation
    ):
        return False
    unresolved_statuses = {
        ItemStatus.AMBIGUOUS,
        ItemStatus.NOT_FOUND,
        ItemStatus.DUPLICATE_PENDING,
        ItemStatus.UNIT_MISMATCH,
        ItemStatus.MISSING_QTY,
        ItemStatus.NEW,
        ItemStatus.AI_PENDING,
    }
    if any(item.status in unresolved_statuses for item in state.cart):
        return False
    return any(item.status is ItemStatus.MATCHED for item in state.cart)
