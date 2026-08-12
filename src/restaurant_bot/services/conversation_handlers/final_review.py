"""Управляет переходами к финальной проверке без внешних эффектов."""

from __future__ import annotations

from dataclasses import dataclass

from restaurant_bot.conversation.quantity_resolution import multiple_warnings
from restaurant_bot.conversation.state.queries import (
    first_unresolved,
    item_index,
)
from restaurant_bot.conversation.state.transitions import normalize_final_review_page
from restaurant_bot.domain.models import (
    ConversationState,
    EngineResult,
    Intent,
    ItemStatus,
    ParsedCommand,
    SessionStage,
)
from restaurant_bot.presentation.telegram.pagination import FINAL_REVIEW_PAGE_SIZE, page_count
from restaurant_bot.presentation.telegram.replies import (
    empty_draft_reply,
    final_review_reply,
    issue_reply,
)


@dataclass(frozen=True, slots=True)
class FinalReviewOutcome:
    """Возвращает готовый ответ либо запрос на подготовку отправки."""

    result: EngineResult | None = None
    prepare_submission: bool = False


class FinalReviewHandler:
    """Проверяет guards финального экрана и отправки черновика."""

    def handle(
        self,
        command: ParsedCommand,
        state: ConversationState,
    ) -> FinalReviewOutcome | None:
        """Обрабатывает только intent финальной проверки и подтверждения."""
        if command.intent in {
            Intent.SUBMIT_REQUEST,
            Intent.SHOW_FINAL_REVIEW,
            Intent.CHECK_MIN_SUM,
        }:
            unresolved = first_unresolved(state)
            if unresolved:
                state.current_issue_item_id = unresolved.id
                return FinalReviewOutcome(
                    result=EngineResult(
                        state=state,
                        reply=issue_reply(unresolved, item_index(state, unresolved)),
                    )
                )
            if not any(item.status == ItemStatus.MATCHED for item in state.cart):
                return FinalReviewOutcome(
                    result=EngineResult(state=state, reply=empty_draft_reply())
                )
            state.current_issue_item_id = ""
            state.current_issue_kind = None
            if command.intent == Intent.SHOW_FINAL_REVIEW:
                state.final_review_page = self._requested_page(command, state)
            normalize_final_review_page(state, page_size=FINAL_REVIEW_PAGE_SIZE)
            state.stage = SessionStage.AWAIT_SUBMIT_CONFIRM
            return FinalReviewOutcome(
                result=EngineResult(state=state, reply=final_review_reply(state))
            )
        if command.intent != Intent.SUBMIT_AS_IS:
            return None

        unresolved = first_unresolved(state)
        if unresolved:
            state.current_issue_item_id = unresolved.id
            return FinalReviewOutcome(
                result=EngineResult(
                    state=state,
                    reply=issue_reply(unresolved, item_index(state, unresolved)),
                )
            )
        has_multiple_warning = bool(multiple_warnings(state))
        if has_multiple_warning:
            state.stage = SessionStage.AWAIT_SUBMIT_CONFIRM
            return FinalReviewOutcome(
                result=EngineResult(state=state, reply=final_review_reply(state))
            )
        return FinalReviewOutcome(prepare_submission=True)

    @staticmethod
    def _requested_page(command: ParsedCommand, state: ConversationState) -> int:
        """Вычисляет страницу финальной проверки заявки."""
        target = command.callback_target
        if target.startswith("page:"):
            try:
                requested = max(0, int(target.partition(":")[2]))
            except ValueError:
                return 0
            active_count = sum(item.status != ItemStatus.SKIPPED for item in state.cart)
            total_pages = page_count(active_count, FINAL_REVIEW_PAGE_SIZE)
            return min(requested, total_pages - 1)
        requested = max(0, state.final_review_page)
        active_count = sum(item.status != ItemStatus.SKIPPED for item in state.cart)
        total_pages = page_count(active_count, FINAL_REVIEW_PAGE_SIZE)
        return min(requested, total_pages - 1)
