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
    DepartmentQuantities,
    EngineResult,
    Intent,
    ItemStatus,
    ParsedCommand,
    SessionStage,
)
from restaurant_bot.presentation.telegram.pagination import FINAL_REVIEW_PAGE_SIZE, page_count
from restaurant_bot.presentation.telegram.replies import (
    department_selection_reply,
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
        if command.intent is Intent.SELECT_DEPARTMENT:
            if command.callback_target.startswith("page:"):
                if self._needs_department_confirmation(state):
                    requested_page = command.callback_target.partition(":")[2]
                    if requested_page.isdecimal():
                        state.department_selection_page = int(requested_page)
                return FinalReviewOutcome(
                    result=EngineResult(state=state, reply=department_selection_reply(state))
                )
            if not self._apply_department_selection(command.callback_target, state):
                return FinalReviewOutcome(
                    result=EngineResult(state=state, reply=department_selection_reply(state))
                )
            state.stage = SessionStage.AWAIT_SUBMIT_CONFIRM
            state.status = "await_submit_confirm"
            return FinalReviewOutcome(
                result=EngineResult(state=state, reply=final_review_reply(state))
            )
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
            if self._needs_department_confirmation(state):
                state.stage = SessionStage.AWAIT_SUBMIT_CONFIRM
                state.status = "await_department_confirmation"
                return FinalReviewOutcome(
                    result=EngineResult(state=state, reply=department_selection_reply(state))
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
        if self._needs_department_confirmation(state):
            state.stage = SessionStage.AWAIT_SUBMIT_CONFIRM
            state.status = "await_department_confirmation"
            return FinalReviewOutcome(
                result=EngineResult(state=state, reply=department_selection_reply(state))
            )
        has_multiple_warning = bool(multiple_warnings(state))
        if has_multiple_warning:
            state.stage = SessionStage.AWAIT_SUBMIT_CONFIRM
            return FinalReviewOutcome(
                result=EngineResult(state=state, reply=final_review_reply(state))
            )
        return FinalReviewOutcome(prepare_submission=True)

    @staticmethod
    def _needs_department_confirmation(state: ConversationState) -> bool:
        """Требует явный выбор подразделения для нового или дополненного черновика."""
        return state.department_confirmation_required and not state.department_confirmed

    @staticmethod
    def _apply_department_selection(target: str, state: ConversationState) -> bool:
        """Подтверждает распределение с фото или назначает весь заказ одному отделу."""
        if not state.department_confirmation_required or state.department_confirmed:
            return False
        if target == "preserve":
            if not any(
                any(
                    value is not None and value > 0
                    for value in item.department_quantities.model_dump().values()
                )
                for item in state.cart
                if item.status is not ItemStatus.SKIPPED
            ):
                return False
            state.department_confirmed = True
            state.department_selection_page = 0
            return True
        departments = {"hall": "Зал", "bar": "Бар", "kitchen": "Кухня"}
        department = departments.get(target)
        if department is None:
            return False
        state.department = department
        for item in state.cart:
            if item.status is ItemStatus.SKIPPED:
                continue
            item.department = department
            item.department_quantities = DepartmentQuantities()
        state.department_confirmed = True
        state.department_selection_page = 0
        return True

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
