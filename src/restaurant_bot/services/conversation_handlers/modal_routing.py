"""Собирает решения policy для modal-контекстов перед маршрутизацией."""

from __future__ import annotations

from dataclasses import dataclass

from restaurant_bot.domain.models import ConversationState, ParsedCommand
from restaurant_bot.services.conversation_handlers.state_compatibility import (
    CompatibilityAction,
    CompatibilityContext,
    CompatibilityDecision,
    StateCompatibilityPolicy,
)


@dataclass(frozen=True, slots=True)
class ModalRoutingDecision:
    """Хранит решения совместимости активных modal-контекстов."""

    quantity: CompatibilityDecision
    comment_scope: CompatibilityDecision
    candidate_selection: CompatibilityDecision
    not_found: CompatibilityDecision
    duplicate: CompatibilityDecision
    unit_mismatch: CompatibilityDecision

    @property
    def quantity_interrupted(self) -> bool:
        """Показывает, прервано ли ожидание количества."""
        return self.quantity.action is CompatibilityAction.INTERRUPT

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
        candidate_selection=policy.evaluate(
            command,
            state,
            CompatibilityContext.CANDIDATE_SELECTION,
        ),
        not_found=policy.evaluate(command, state, CompatibilityContext.NOT_FOUND),
        duplicate=policy.evaluate(command, state, CompatibilityContext.DUPLICATE_PENDING),
        unit_mismatch=policy.evaluate(command, state, CompatibilityContext.UNIT_MISMATCH),
    )
