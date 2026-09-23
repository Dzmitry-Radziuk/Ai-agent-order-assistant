"""Владеет аналитикой пользовательского запроса в UpdateOrchestrator."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

from restaurant_bot.domain.models import (
    CartItem,
    ConversationState,
    Intent,
    ItemStatus,
    ParsedCommand,
    SessionStage,
    TelegramEvent,
)
from restaurant_bot.observability import sanitize_log_value
from restaurant_bot.services.engine import ConversationEngine

if TYPE_CHECKING:
    from restaurant_bot.services.orchestrator import UpdateOrchestrator


class OrchestratorAnalyticsService:
    """Формирует безопасный итог запроса и отправляет его в наблюдаемость."""

    def __init__(self, owner: UpdateOrchestrator) -> None:
        """Подключает сервис к настройкам и трассировщику координатора."""
        self._owner = owner

    @property
    def settings(self) -> Any:
        """Возвращает настройки координатора для политики содержимого логов."""
        return self._owner.settings

    @property
    def tracer(self) -> Any:
        """Возвращает трассировщик координатора."""
        return self._owner.tracer

    @staticmethod
    def _scenario_for_intent(intent: Intent) -> str:
        """Возвращает короткое название пользовательского сценария."""
        if intent in {Intent.GREETING, Intent.HELP}:
            return "onboarding"
        if intent == Intent.ORDER_STATUS:
            return "order_status"
        if intent is Intent.HISTORY_QUERY:
            return "history_query"
        if intent is Intent.VENUE_STATUS:
            return "venue_status"
        if intent in {
            Intent.REVIEW_ORDER,
            Intent.REVIEW_REFRESH,
            Intent.REVIEW_SUBMIT,
            Intent.REVIEW_CANCEL,
        }:
            return "order_review"
        if intent in {
            Intent.PRODUCT_ADD,
            Intent.PRODUCT_ADD_RETRY,
            Intent.PRODUCT_ADD_SKIP,
            Intent.PRODUCT_ADD_LIST,
        }:
            return "product_request"
        if intent in {Intent.SUBMIT_REQUEST, Intent.SUBMIT_AS_IS}:
            return "order_submission"
        if intent in {
            Intent.SHOW_CART,
            Intent.SHOW_FINAL_REVIEW,
            Intent.CHECK_MIN_SUM,
            Intent.CHOOSE_SUPPLIER_WARNING,
            Intent.ADD_SUPPLIER_ITEMS,
        }:
            return "order_review"
        if intent in {Intent.THANKS, Intent.SMALL_TALK}:
            return "conversation"
        if intent == Intent.UNKNOWN:
            return "unrecognized_request"
        return "draft_management"

    @staticmethod
    def _effective_analytics_command(
        event: TelegramEvent,
        command: ParsedCommand,
        state: ConversationState,
        previous_issue_item_id: str,
    ) -> ParsedCommand:
        """Восстанавливает контекстное уточнение количества для аналитики."""
        if command.intent is not Intent.UNKNOWN or not previous_issue_item_id:
            return command

        quantity, unit = ConversationEngine._spoken_quantity(event.text or command.text)
        if quantity is None:
            return command

        item = next(
            (candidate for candidate in state.cart if candidate.id == previous_issue_item_id),
            None,
        )
        if (
            item is None
            or item.quantity is None
            or item.status not in {ItemStatus.MATCHED, ItemStatus.UNIT_MISMATCH}
        ):
            return command

        return command.model_copy(
            update={
                "intent": Intent.EDIT_QUANTITY,
                "edit_quantity": quantity,
                "edit_unit": unit,
                "target_query": "",
            }
        )

    def request_analytics(
        self,
        event: TelegramEvent,
        command: ParsedCommand,
        state: ConversationState,
        *,
        previous_stage: str,
        previous_cart_count: int,
        previous_issue_item_id: str,
    ) -> dict[str, Any]:
        """Формирует компактный и обезличенный результат пользовательского запроса."""
        command = self._effective_analytics_command(
            event,
            command,
            state,
            previous_issue_item_id,
        )
        relevant_items: list[CartItem] = []
        if command.intent == Intent.ADD_ITEMS:
            relevant_items = state.cart[previous_cart_count:]
        elif command.intent in {
            Intent.SUBMIT_REQUEST,
            Intent.SUBMIT_AS_IS,
            Intent.SHOW_FINAL_REVIEW,
            Intent.CHECK_MIN_SUM,
        }:
            if current := state.current_item():
                relevant_items = [current]
        elif command.intent in {
            Intent.SELECT_CANDIDATE,
            Intent.MANUAL_CURRENT,
            Intent.SEARCH_ALL_SUPPLIERS,
            Intent.SWITCH_SUPPLIER,
            Intent.EDIT_QUANTITY,
            Intent.ENTER_OTHER_QUANTITY,
            Intent.USE_CATALOG_UNIT,
            Intent.UNIT_EDIT,
            Intent.UNIT_OK,
            Intent.MERGE_DUPLICATE,
        }:
            current = next(
                (item for item in state.cart if item.id == previous_issue_item_id),
                None,
            )
            if current:
                relevant_items = [current]

        issue_statuses = {
            ItemStatus.NOT_FOUND,
            ItemStatus.AMBIGUOUS,
            ItemStatus.MISSING_QTY,
            ItemStatus.UNIT_MISMATCH,
            ItemStatus.DUPLICATE_PENDING,
            ItemStatus.AI_PENDING,
        }
        issue_counts = Counter(
            item.status.value for item in relevant_items if item.status in issue_statuses
        )
        matched_count = sum(item.status == ItemStatus.MATCHED for item in relevant_items)
        outcome = "success"
        failure_reason = ""

        if command.comment_clarification:
            outcome = "needs_clarification"
            failure_reason = "ambiguous_comment_scope"
        elif command.intent == Intent.UNKNOWN:
            outcome = "failed"
            failure_reason = "unrecognized_request"
        elif command.intent == Intent.ADD_ITEMS and not command.items:
            outcome = "failed"
            failure_reason = "no_items_recognized"
        elif state.stage == SessionStage.SUBMISSION_FAILED and command.intent in {
            Intent.SUBMIT_REQUEST,
            Intent.SUBMIT_AS_IS,
        }:
            outcome = "failed"
            failure_reason = "submission_failed"
        elif issue_counts:
            reason_by_status = (
                (ItemStatus.NOT_FOUND, "product_not_found"),
                (ItemStatus.AMBIGUOUS, "ambiguous_product"),
                (ItemStatus.MISSING_QTY, "missing_quantity"),
                (ItemStatus.UNIT_MISMATCH, "unit_mismatch"),
                (ItemStatus.DUPLICATE_PENDING, "duplicate_product"),
                (ItemStatus.AI_PENDING, "ai_match_pending"),
            )
            failure_reason = next(
                reason for status, reason in reason_by_status if issue_counts.get(status.value, 0)
            )
            if matched_count:
                outcome = "partial"
            elif failure_reason == "product_not_found":
                outcome = "failed"
            else:
                outcome = "needs_clarification"

        analytics: dict[str, Any] = {
            "status": "done",
            "scenario": self._scenario_for_intent(command.intent),
            "intent": command.intent.value,
            "outcome": outcome,
            "failure_reason": failure_reason,
            "input_type": event.input_type.value,
            "stage_from": previous_stage,
            "stage_to": state.stage.value,
            "item_count": len(command.items),
            "cart_count": len(state.cart),
            "issue_count": sum(issue_counts.values()),
            "issue_types": sorted(issue_counts),
        }
        if command.confidence is not None:
            analytics["confidence"] = command.confidence
        if outcome != "success":
            request_text = self._analytics_request_text(event, command)
            if request_text:
                request_key = (
                    "user_text" if self.settings.log_user_content else "request_fingerprint"
                )
                analytics[request_key] = sanitize_log_value(
                    request_text,
                    key="user_text",
                    include_user_content=self.settings.log_user_content,
                    max_content_length=self.settings.log_content_max_length,
                )
        return analytics

    @staticmethod
    def _analytics_request_text(event: TelegramEvent, command: ParsedCommand) -> str:
        """Выбирает наиболее полезную часть неуспешного запроса для диагностики."""
        if command.comment_clarification:
            return event.text or command.text or command.comment_clarification
        if command.target_query:
            return command.target_query
        product_queries = [item.product_query for item in command.items if item.product_query]
        if product_queries:
            return " | ".join(product_queries)
        return command.text or event.text

    def record_request_outcome(
        self,
        trace: Any,
        log: Any,
        analytics: dict[str, Any],
        *,
        chat_id: str = "",
        venue_code: str = "",
    ) -> None:
        """Записывает один итог запроса в обычный лог и текущую трассу Langfuse."""
        metadata = {
            key: analytics[key]
            for key in (
                "scenario",
                "intent",
                "outcome",
                "failure_reason",
                "input_type",
            )
        }
        if chat_id:
            metadata["chat_hash"] = self.tracer.anonymized_chat_id(chat_id)
        if venue_code:
            metadata["venue_hash"] = self.tracer.anonymized_chat_id(venue_code)
        log.info("user_request_outcome", **analytics)
        trace.update(output=analytics, metadata=metadata)
