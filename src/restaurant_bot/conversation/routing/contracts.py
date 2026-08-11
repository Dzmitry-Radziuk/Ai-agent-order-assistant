"""Хранит контракты совместимости modal-состояний."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CompatibilityAction(StrEnum):
    """Описывает решение policy для текущего modal state."""

    NOT_APPLICABLE = "not_applicable"
    CONTINUE = "continue"
    INTERRUPT = "interrupt"
    REJECT = "reject"
    AMBIGUOUS = "ambiguous"


class CompatibilityContext(StrEnum):
    """Указывает modal-контекст, для которого принимается решение."""

    QUANTITY = "quantity"
    COMMENT_SCOPE = "comment_scope"
    MANUAL_DETAILS = "manual_details"
    CANDIDATE_SELECTION = "candidate_selection"
    NOT_FOUND = "not_found"
    DUPLICATE_PENDING = "duplicate_pending"
    UNIT_MISMATCH = "unit_mismatch"
    PRODUCT_ADD_DETAILS = "product_add_details"
    ADD_MORE_CONFIRM = "add_more_confirm"
    SUBMIT_CONFIRM = "submit_confirm"
    SUBMISSION_FAILED = "submission_failed"
    NEW_ORDER_CONFIRMATION = "new_order_confirmation"
    SHEET_REVIEW = "sheet_review"


@dataclass(frozen=True, slots=True)
class CompatibilityDecision:
    """Возвращает решение без изменения команды или состояния."""

    action: CompatibilityAction
    mode: str = ""
