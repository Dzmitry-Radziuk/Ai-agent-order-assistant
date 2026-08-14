"""Содержит channel-neutral переход к следующей нерешённой позиции."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from restaurant_bot.conversation.state.queries import first_unresolved, is_unresolved_status
from restaurant_bot.domain.models import (
    CartItem,
    ConversationState,
    IssueKind,
    ItemStatus,
    SessionStage,
)


class ProgressionKind(StrEnum):
    """Описывает результат перехода без presentation-данных."""

    ISSUE = "issue"
    ADD_MORE_CONFIRM = "add_more_confirm"
    DRAFT = "draft"


@dataclass(frozen=True, slots=True)
class ProgressionResult:
    """Возвращает состояние перехода и данные для presentation adapter."""

    kind: ProgressionKind
    item: CartItem | None = None
    prompt_count: int = 0
    added_count: int = 0
    resumed: bool = False


_ISSUE_KINDS = {
    ItemStatus.AMBIGUOUS: IssueKind.CANDIDATE,
    ItemStatus.NOT_FOUND: IssueKind.NOT_FOUND,
    ItemStatus.MISSING_QTY: IssueKind.QUANTITY,
    ItemStatus.UNIT_MISMATCH: IssueKind.UNIT,
    ItemStatus.DUPLICATE_PENDING: IssueKind.DUPLICATE,
}


def advance(
    state: ConversationState,
    added_count: int = 0,
    preferred_issue_item_id: str = "",
    issue_context_item_ids: list[str] | None = None,
) -> ProgressionResult:
    """Переходит внутри текущего контекста и возобновляет прерванный вопрос."""
    if added_count:
        state.pending_added_items_count = added_count

    if issue_context_item_ids is not None:
        if not state.issue_context_stack and state.current_issue_item_id:
            current = state.current_item()
            if current is not None and is_unresolved_status(current.status):
                state.issue_context_stack.append([current.id])
        state.issue_context_stack.append(list(issue_context_item_ids))

    if not state.issue_context_stack and state.current_issue_item_id:
        current = state.current_item()
        if current is not None and is_unresolved_status(current.status):
            state.issue_context_stack.append([current.id])

    resumed = False
    unresolved = None
    while state.issue_context_stack:
        context = state.issue_context_stack[-1]
        context[:] = [
            item_id
            for item_id in context
            if any(item.id == item_id and is_unresolved_status(item.status) for item in state.cart)
        ]
        if preferred_issue_item_id:
            preferred = next(
                (item for item in state.cart if item.id == preferred_issue_item_id),
                None,
            )
            if preferred is not None and is_unresolved_status(preferred.status):
                unresolved = preferred
                break
        if context:
            unresolved = next(
                item
                for item in state.cart
                if item.id == context[0] and is_unresolved_status(item.status)
            )
            break
        state.issue_context_stack.pop()
        if state.issue_context_stack:
            resumed = True

    if unresolved is None:
        unresolved = first_unresolved(state)
        if unresolved is not None:
            state.issue_context_stack = [[unresolved.id]]

    if unresolved:
        state.current_issue_item_id = unresolved.id
        state.current_issue_kind = _ISSUE_KINDS.get(unresolved.status)
        return ProgressionResult(
            kind=ProgressionKind.ISSUE,
            item=unresolved,
            resumed=resumed,
        )

    state.issue_context_stack = []
    state.current_issue_item_id = ""
    state.current_issue_kind = None
    if state.pending_added_items_count:
        prompt_count = state.pending_added_items_count
        state.pending_added_items_count = 0
        state.stage = SessionStage.AWAIT_ADD_MORE_CONFIRM
        state.status = "await_add_more_confirm"
        return ProgressionResult(
            kind=ProgressionKind.ADD_MORE_CONFIRM,
            prompt_count=prompt_count,
        )

    state.stage = SessionStage.REVIEW if state.cart else SessionStage.COLLECTING
    return ProgressionResult(kind=ProgressionKind.DRAFT, added_count=added_count)
