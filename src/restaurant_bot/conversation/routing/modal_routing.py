"""Собирает решения policy для modal-контекстов перед маршрутизацией."""

from __future__ import annotations

from dataclasses import dataclass

from restaurant_bot.conversation.routing.contracts import (
    CompatibilityAction,
    CompatibilityContext,
    CompatibilityDecision,
)
from restaurant_bot.conversation.routing.state_compatibility import StateCompatibilityPolicy
from restaurant_bot.domain.models import ConversationState, ParsedCommand


@dataclass(frozen=True, slots=True)
class ModalRoutingDecision:
    """Хранит решения совместимости активных modal-контекстов."""

    quantity: CompatibilityDecision
    comment_scope: CompatibilityDecision
    manual_details: CompatibilityDecision
    product_add_details: CompatibilityDecision
    add_more_confirm: CompatibilityDecision
    submit_confirm: CompatibilityDecision
    submission_failed: CompatibilityDecision
    new_order_confirmation: CompatibilityDecision
    candidate_selection: CompatibilityDecision
    not_found: CompatibilityDecision
    duplicate: CompatibilityDecision
    unit_mismatch: CompatibilityDecision

    @property
    def quantity_interrupted(self) -> bool:
        """Показывает, прервано ли ожидание количества."""
        return self.quantity.action is CompatibilityAction.INTERRUPT

    @property
    def manual_details_interrupted(self) -> bool:
        """Показывает, прерван ли запрос ручного названия товара."""
        return self.manual_details.action is CompatibilityAction.INTERRUPT

    @property
    def product_add_details_interrupted(self) -> bool:
        """Показывает, прервано ли ожидание описания нового товара."""
        return self.product_add_details.action is CompatibilityAction.INTERRUPT

    @property
    def add_more_confirm_active(self) -> bool:
        """Показывает, что add-more prompt является активным modal-контекстом."""
        return self.add_more_confirm.action is not CompatibilityAction.NOT_APPLICABLE

    @property
    def add_more_confirm_interrupted(self) -> bool:
        """Показывает, прерван ли add-more prompt независимой командой."""
        return self.add_more_confirm.action is CompatibilityAction.INTERRUPT

    @property
    def submit_confirm_active(self) -> bool:
        """Показывает, что обычный финальный review является active modal context."""
        return self.submit_confirm.action is not CompatibilityAction.NOT_APPLICABLE

    @property
    def candidate_interrupted(self) -> bool:
        """Показывает, прерван ли выбор кандидата."""
        return self.candidate_selection.action is CompatibilityAction.INTERRUPT

    @property
    def not_found_interrupted(self) -> bool:
        """Показывает, прерван ли сценарий ненайденного товара."""
        return self.not_found.action is CompatibilityAction.INTERRUPT

    @property
    def duplicate_interrupted(self) -> bool:
        """Показывает, прервано ли ожидание решения по duplicate item."""
        return self.duplicate.action is CompatibilityAction.INTERRUPT

    @property
    def unit_mismatch_interrupted(self) -> bool:
        """Показывает, прервано ли ожидание решения по unit mismatch."""
        return self.unit_mismatch.action is CompatibilityAction.INTERRUPT


def evaluate_modal_routing(
    policy: StateCompatibilityPolicy,
    command: ParsedCommand,
    state: ConversationState,
) -> ModalRoutingDecision:
    """Оценивает все поддержанные modal-контексты одним вызовом."""
    return ModalRoutingDecision(
        quantity=policy.evaluate(command, state, CompatibilityContext.QUANTITY),
        comment_scope=policy.evaluate(command, state, CompatibilityContext.COMMENT_SCOPE),
        manual_details=policy.evaluate(command, state, CompatibilityContext.MANUAL_DETAILS),
        product_add_details=policy.evaluate(
            command,
            state,
            CompatibilityContext.PRODUCT_ADD_DETAILS,
        ),
        add_more_confirm=policy.evaluate(
            command,
            state,
            CompatibilityContext.ADD_MORE_CONFIRM,
        ),
        submit_confirm=policy.evaluate(
            command,
            state,
            CompatibilityContext.SUBMIT_CONFIRM,
        ),
        submission_failed=policy.evaluate(
            command,
            state,
            CompatibilityContext.SUBMISSION_FAILED,
        ),
        new_order_confirmation=policy.evaluate(
            command,
            state,
            CompatibilityContext.NEW_ORDER_CONFIRMATION,
        ),
        candidate_selection=policy.evaluate(
            command,
            state,
            CompatibilityContext.CANDIDATE_SELECTION,
        ),
        not_found=policy.evaluate(command, state, CompatibilityContext.NOT_FOUND),
        duplicate=policy.evaluate(command, state, CompatibilityContext.DUPLICATE_PENDING),
        unit_mismatch=policy.evaluate(command, state, CompatibilityContext.UNIT_MISMATCH),
    )
