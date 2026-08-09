"""Проверяет совместимость команды с первым поддержанным modal state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from restaurant_bot.domain.models import (
    CartItem,
    ConversationState,
    Intent,
    ItemStatus,
    ParsedCommand,
    SessionStage,
)
from restaurant_bot.services.conversation_handlers.pending_quantity import (
    PendingQuantityHandler,
)
from restaurant_bot.services.text import normalize_text


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


@dataclass(frozen=True, slots=True)
class CompatibilityDecision:
    """Возвращает решение без изменения команды или состояния."""

    action: CompatibilityAction


class StateCompatibilityPolicy:
    """Определяет, совместим ли intent с поддержанным modal state."""

    _QUANTITY_STAGES = frozenset({SessionStage.AWAIT_UNIT_QUANTITY})
    _CONTINUE_INTENTS = frozenset(
        {
            Intent.EDIT_QUANTITY,
            Intent.ENTER_OTHER_QUANTITY,
            Intent.USE_CATALOG_UNIT,
            Intent.UNIT_EDIT,
            Intent.UNIT_OK,
            Intent.CONTINUE_CURRENT,
        }
    )
    _INTERRUPT_INTENTS = frozenset(
        {
            Intent.REMOVE_ITEM,
            Intent.SHOW_CART,
            Intent.START_NEW_ORDER,
            Intent.CLEAR_CART,
            Intent.BACK,
            Intent.THANKS,
            Intent.GREETING,
            Intent.HELP,
            Intent.SMALL_TALK,
            Intent.ORDER_STATUS,
            Intent.PRODUCT_ADD_LIST,
            Intent.SUBMIT_REQUEST,
            Intent.SHOW_FINAL_REVIEW,
        }
    )

    def evaluate(
        self,
        command: ParsedCommand,
        state: ConversationState,
        context: CompatibilityContext | None = None,
    ) -> CompatibilityDecision:
        """Возвращает решение для поддержанного modal-контекста."""
        context = context or self.context_for(state)
        if context is CompatibilityContext.COMMENT_SCOPE:
            return self._evaluate_comment_scope(command, state)
        if context is CompatibilityContext.MANUAL_DETAILS:
            return self._evaluate_manual_details(command, state)
        if context is CompatibilityContext.CANDIDATE_SELECTION:
            return self._evaluate_candidate_selection(command, state)
        if context is CompatibilityContext.NOT_FOUND:
            return self._evaluate_not_found(command, state)
        if context is CompatibilityContext.DUPLICATE_PENDING:
            return self._evaluate_duplicate_pending(command, state)
        if context is CompatibilityContext.UNIT_MISMATCH:
            return self._evaluate_unit_mismatch(command, state)
        if context is not CompatibilityContext.QUANTITY:
            return CompatibilityDecision(CompatibilityAction.NOT_APPLICABLE)

        item = state.current_item()
        if (
            item is None
            or item.status is not ItemStatus.MISSING_QTY
            or state.stage not in self._QUANTITY_STAGES
        ):
            return CompatibilityDecision(CompatibilityAction.NOT_APPLICABLE)

        if command.intent is Intent.ADD_ITEMS and self._has_concrete_new_items(command):
            return CompatibilityDecision(CompatibilityAction.INTERRUPT)
        if command.intent in self._INTERRUPT_INTENTS:
            return CompatibilityDecision(CompatibilityAction.INTERRUPT)
        if command.intent in self._CONTINUE_INTENTS:
            return CompatibilityDecision(CompatibilityAction.CONTINUE)

        # UNKNOWN remains available to the existing deterministic quantity
        # handler for short answers such as a bare number or unit phrase.
        return CompatibilityDecision(CompatibilityAction.CONTINUE)

    def _evaluate_manual_details(
        self,
        command: ParsedCommand,
        state: ConversationState,
    ) -> CompatibilityDecision:
        """Разделяет новое намерение и ответ на запрос нового названия."""
        if (
            state.stage is not SessionStage.AWAIT_MANUAL_DETAILS
            or not state.current_issue_item_id
            or state.current_item() is None
        ):
            return CompatibilityDecision(CompatibilityAction.NOT_APPLICABLE)

        if command.intent is Intent.ADD_ITEMS:
            if self._has_concrete_new_items(command):
                return CompatibilityDecision(CompatibilityAction.INTERRUPT)
            if len(command.items) == 1 and normalize_text(
                command.items[0].product_query
            ):
                return CompatibilityDecision(CompatibilityAction.CONTINUE)
            return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)

        if command.intent in {Intent.MANUAL_CURRENT, Intent.SKIP_CURRENT}:
            return CompatibilityDecision(CompatibilityAction.CONTINUE)
        if command.intent is Intent.UNKNOWN:
            return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)
        if command.intent in self._INTERRUPT_INTENTS:
            return CompatibilityDecision(CompatibilityAction.INTERRUPT)
        return CompatibilityDecision(CompatibilityAction.INTERRUPT)

    def _evaluate_candidate_selection(
        self,
        command: ParsedCommand,
        state: ConversationState,
    ) -> CompatibilityDecision:
        """Разрешает выбор кандидата только в открытом контексте AMBIGUOUS."""
        if state.stage is SessionStage.AWAIT_MANUAL_DETAILS:
            return CompatibilityDecision(CompatibilityAction.NOT_APPLICABLE)
        item = state.current_item()
        if (
            item is None
            or item.status is not ItemStatus.AMBIGUOUS
            or not item.candidates
        ):
            return CompatibilityDecision(CompatibilityAction.NOT_APPLICABLE)

        if command.intent is Intent.SELECT_CANDIDATE:
            candidate_index = (command.selected_index or 0) - 1
            if command.selection_query or 0 <= candidate_index < len(item.candidates):
                return CompatibilityDecision(CompatibilityAction.CONTINUE)
            return CompatibilityDecision(CompatibilityAction.REJECT)

        if command.intent is Intent.ADD_ITEMS:
            if self._has_concrete_new_items(command):
                return CompatibilityDecision(CompatibilityAction.INTERRUPT)
            return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)

        if command.intent in {Intent.UNKNOWN, Intent.CLARIFY_CURRENT, Intent.CANCEL}:
            return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)
        return CompatibilityDecision(CompatibilityAction.INTERRUPT)

    def _evaluate_not_found(
        self,
        command: ParsedCommand,
        state: ConversationState,
    ) -> CompatibilityDecision:
        """Разрешает продолжение только открытого сценария ненайденного товара."""
        item = state.current_item()
        if item is None or item.status is not ItemStatus.NOT_FOUND:
            return CompatibilityDecision(CompatibilityAction.NOT_APPLICABLE)

        if (
            state.stage is SessionStage.AWAIT_PRODUCT_ADD_DETAILS
            and command.intent is Intent.CANCEL
        ):
            return CompatibilityDecision(CompatibilityAction.CONTINUE)
        if command.intent is Intent.ADD_ITEMS:
            if self._has_concrete_new_items(command):
                return CompatibilityDecision(CompatibilityAction.INTERRUPT)
            if state.stage in {
                SessionStage.AWAIT_MANUAL_DETAILS,
                SessionStage.AWAIT_PRODUCT_ADD_DETAILS,
            }:
                return CompatibilityDecision(CompatibilityAction.CONTINUE)
            return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)

        if command.intent in self._INTERRUPT_INTENTS:
            return CompatibilityDecision(CompatibilityAction.INTERRUPT)
        if command.intent in {
            Intent.CLARIFY_CURRENT,
            Intent.CONTINUE_CURRENT,
            Intent.MANUAL_CURRENT,
            Intent.PRODUCT_ADD,
            Intent.PRODUCT_ADD_RETRY,
            Intent.PRODUCT_ADD_SKIP,
            Intent.SELECT_CANDIDATE,
            Intent.SEARCH_ALL_SUPPLIERS,
            Intent.SKIP_CURRENT,
            Intent.SWITCH_SUPPLIER,
        }:
            return CompatibilityDecision(CompatibilityAction.CONTINUE)
        if command.intent is Intent.UNKNOWN:
            return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)
        return CompatibilityDecision(CompatibilityAction.INTERRUPT)

    def _evaluate_duplicate_pending(
        self,
        command: ParsedCommand,
        state: ConversationState,
    ) -> CompatibilityDecision:
        """Разрешает ответ на duplicate prompt или прерывает его новым intent."""
        item = state.current_item()
        if item is None or item.status is not ItemStatus.DUPLICATE_PENDING:
            return CompatibilityDecision(CompatibilityAction.NOT_APPLICABLE)

        if command.intent is Intent.ADD_ITEMS:
            if PendingQuantityHandler.has_named_product_items(command, command.text):
                return CompatibilityDecision(CompatibilityAction.INTERRUPT)
            if self._has_concrete_new_items(command):
                # Existing quantity-flow accepts spoken forms such as
                # ``давай 3 штуки`` as an increment for the open duplicate.
                return CompatibilityDecision(CompatibilityAction.CONTINUE)
            return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)

        if command.intent is Intent.REMOVE_ITEM:
            # The deterministic parser can classify a negative duplicate
            # answer such as ``не добавляй повторно`` as REMOVE_ITEM with a
            # non-product target. Let the existing negative rewrite handle it;
            # a real cart target remains an independent removal intent.
            if not self._has_matching_cart_target(command, state):
                return CompatibilityDecision(CompatibilityAction.CONTINUE)
            return CompatibilityDecision(CompatibilityAction.INTERRUPT)

        if command.intent in {
            Intent.MERGE_DUPLICATE,
            Intent.CONFIRM,
            Intent.SKIP_CURRENT,
            Intent.CANCEL,
            Intent.UNIT_EDIT,
            Intent.UNIT_OK,
            Intent.ENTER_OTHER_QUANTITY,
            Intent.KEEP_CURRENT_QUANTITY,
            Intent.KEEP_MULTIPLE,
        }:
            return CompatibilityDecision(CompatibilityAction.CONTINUE)
        if command.intent is Intent.SELECT_CANDIDATE:
            # The deterministic parser may classify a bare spoken number as a
            # candidate index. PendingQuantityHandler still owns its quantity
            # interpretation while this duplicate card is open.
            return CompatibilityDecision(CompatibilityAction.CONTINUE)
        if command.intent is Intent.UNKNOWN:
            # The quantity handler may still prove this to be a short numeric
            # reply; otherwise the engine returns the unchanged duplicate card.
            return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)
        if command.intent in self._INTERRUPT_INTENTS:
            return CompatibilityDecision(CompatibilityAction.INTERRUPT)
        return CompatibilityDecision(CompatibilityAction.INTERRUPT)

    def _evaluate_unit_mismatch(
        self,
        command: ParsedCommand,
        state: ConversationState,
    ) -> CompatibilityDecision:
        """Разрешает только подтверждённые ответы на unit mismatch."""
        item = state.current_item()
        if item is None or item.status is not ItemStatus.UNIT_MISMATCH:
            return CompatibilityDecision(CompatibilityAction.NOT_APPLICABLE)

        if command.intent is Intent.ADD_ITEMS:
            if PendingQuantityHandler.has_named_product_items(command, command.text):
                return CompatibilityDecision(CompatibilityAction.INTERRUPT)
            return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)

        if command.intent is Intent.REMOVE_ITEM:
            if self._has_matching_cart_target(command, state):
                return CompatibilityDecision(CompatibilityAction.INTERRUPT)
            return CompatibilityDecision(CompatibilityAction.CONTINUE)

        if command.intent is Intent.EDIT_QUANTITY:
            target = normalize_text(command.target_query)
            if not target or self._target_matches_item(target, item):
                return CompatibilityDecision(CompatibilityAction.CONTINUE)
            return CompatibilityDecision(CompatibilityAction.INTERRUPT)

        if command.intent in {
            Intent.USE_CATALOG_UNIT,
            Intent.UNIT_OK,
            Intent.UNIT_EDIT,
            Intent.ENTER_OTHER_QUANTITY,
            Intent.SKIP_CURRENT,
            Intent.CONFIRM,
            Intent.CONTINUE_CURRENT,
            Intent.CLARIFY_CURRENT,
        }:
            return CompatibilityDecision(CompatibilityAction.CONTINUE)
        if command.intent is Intent.UNKNOWN:
            # Existing contextual recovery may prove this to be a quantity or
            # catalog-unit answer before the decision is evaluated again.
            return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)
        if command.intent in self._INTERRUPT_INTENTS:
            return CompatibilityDecision(CompatibilityAction.INTERRUPT)
        return CompatibilityDecision(CompatibilityAction.INTERRUPT)

    @staticmethod
    def _target_matches_item(target: str, item: CartItem) -> bool:
        """Проверяет, что структурированный target указывает на текущий item."""
        return any(
            candidate and (target in candidate or candidate in target)
            for candidate in (
                normalize_text(item.source_query),
                normalize_text(item.catalog_name),
            )
        )

    @staticmethod
    def _has_matching_cart_target(command: ParsedCommand, state: ConversationState) -> bool:
        """Проверяет, указывает ли REMOVE_ITEM на реальную активную строку корзины."""
        target = normalize_text(command.target_query)
        if not target:
            return False
        return any(
            target in candidate
            or candidate in target
            for item in state.cart
            if item.status is not ItemStatus.SKIPPED
            for candidate in (
                normalize_text(item.source_query),
                normalize_text(item.catalog_name),
            )
            if candidate
        )

    def should_try_contextual_fallback(
        self,
        state: ConversationState,
        command: ParsedCommand,
        context: CompatibilityContext,
    ) -> bool:
        """Проверяет, можно ли передать команду contextual parser-у."""
        decision = self.evaluate(command, state, context)
        return decision.action in {
            CompatibilityAction.CONTINUE,
            CompatibilityAction.AMBIGUOUS,
        }

    @staticmethod
    def context_for(state: ConversationState) -> CompatibilityContext | None:
        """Определяет поддержанный modal-контекст по состоянию без разбора текста."""
        if state.pending_comment_items:
            return CompatibilityContext.COMMENT_SCOPE
        item = state.current_item()
        if (
            state.stage is SessionStage.AWAIT_MANUAL_DETAILS
            and state.current_issue_item_id
            and item is not None
        ):
            return CompatibilityContext.MANUAL_DETAILS
        if item is not None and item.status is ItemStatus.AMBIGUOUS and item.candidates:
            return CompatibilityContext.CANDIDATE_SELECTION
        if item is not None and item.status is ItemStatus.NOT_FOUND:
            return CompatibilityContext.NOT_FOUND
        if item is not None and item.status is ItemStatus.DUPLICATE_PENDING:
            return CompatibilityContext.DUPLICATE_PENDING
        if item is not None and item.status is ItemStatus.UNIT_MISMATCH:
            return CompatibilityContext.UNIT_MISMATCH
        if (
            item is not None
            and item.status is ItemStatus.MISSING_QTY
            and state.stage in StateCompatibilityPolicy._QUANTITY_STAGES
        ):
            return CompatibilityContext.QUANTITY
        return None

    def _evaluate_comment_scope(
        self,
        command: ParsedCommand,
        state: ConversationState,
    ) -> CompatibilityDecision:
        """Разделяет ответ области комментария и независимый intent."""
        if not state.pending_comment_items:
            return CompatibilityDecision(CompatibilityAction.NOT_APPLICABLE)

        if command.comment_scope_action in {"items", "order", "cancel"}:
            return CompatibilityDecision(CompatibilityAction.CONTINUE)
        if command.comment_scope_action == "ambiguous":
            return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)
        if command.intent is Intent.ADD_ITEMS:
            if self._has_comment_scope_product_items(command):
                return CompatibilityDecision(CompatibilityAction.INTERRUPT)
            return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)
        if command.intent in {Intent.UNKNOWN, Intent.CLARIFY_CURRENT, Intent.CANCEL}:
            return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)
        return CompatibilityDecision(CompatibilityAction.INTERRUPT)

    @staticmethod
    def _has_comment_scope_product_items(command: ParsedCommand) -> bool:
        """Считает любую явно извлечённую товарную позицию новым intent."""
        return command.intent is Intent.ADD_ITEMS and any(
            item.product_query.strip() for item in command.items
        )

    @staticmethod
    def _has_concrete_new_items(command: ParsedCommand) -> bool:
        """Отличает структурированную новую позицию от неуточнённого voice ответа."""
        if not command.items:
            return False
        command_text = normalize_text(command.text)
        return len(command.items) > 1 or any(
            item.quantity is not None
            or all(
                normalize_text(item.product_query) != candidate_text
                for candidate_text in (
                    command_text,
                    normalize_text(item.source_line),
                )
                if candidate_text
            )
            for item in command.items
        )
