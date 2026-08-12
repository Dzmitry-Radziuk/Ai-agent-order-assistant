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
) -> ProgressionResult:
    """Переходит к следующей нерешённой позиции с прежней семантикой."""
    if added_count:
        state.pending_added_items_count = added_count

    unresolved = first_unresolved(state)
    if preferred_issue_item_id:
        preferred = next(
            (
                item
                for item in state.cart
                if item.id == preferred_issue_item_id and is_unresolved_status(item.status)
            ),
            None,
        )
        if preferred is not None:
            unresolved = preferred

    if unresolved:
        state.current_issue_item_id = unresolved.id
        state.current_issue_kind = _ISSUE_KINDS.get(unresolved.status)
        return ProgressionResult(kind=ProgressionKind.ISSUE, item=unresolved)

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
