"""Формирует пользовательский результат ручного выбора кандидата."""

from __future__ import annotations

from dataclasses import dataclass

from restaurant_bot.conversation.selection import (
    SelectionFailure,
    resolve_candidate_selection,
)
from restaurant_bot.conversation.state.queries import item_index
from restaurant_bot.domain.models import (
    Candidate,
    CartItem,
    ConversationState,
    EngineResult,
    ParsedCommand,
)
from restaurant_bot.services.replies import cart_reply, issue_reply


@dataclass(frozen=True, slots=True)
class CandidateSelectionOutcome:
    """Возвращает выбранную пару или готовый ответ обработчика."""

    item: CartItem | None = None
    candidate: Candidate | None = None
    result: EngineResult | None = None


class CandidateSelectionHandler:
    """Адаптирует чистое ядро выбора к ответу ConversationEngine."""

    def resolve(
        self,
        command: ParsedCommand,
        state: ConversationState,
    ) -> CandidateSelectionOutcome:
        """Выбирает кандидата и формирует прежний ответ при ошибке."""
        selection = resolve_candidate_selection(command, state)
        if selection.failure is SelectionFailure.NO_CURRENT_ITEM:
            return CandidateSelectionOutcome(
                result=EngineResult(state=state, reply=cart_reply(state))
            )
        if selection.failure is SelectionFailure.INVALID_CANDIDATE:
            assert selection.item is not None
            return CandidateSelectionOutcome(
                result=EngineResult(
                    state=state,
                    reply=issue_reply(selection.item, item_index(state, selection.item)),
                )
            )
        return CandidateSelectionOutcome(
            item=selection.item,
            candidate=selection.candidate,
        )
