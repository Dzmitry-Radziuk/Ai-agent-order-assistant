"""Координирует сервис «engine»."""

from __future__ import annotations

import re
from collections import defaultdict
from copy import deepcopy
from datetime import UTC, datetime

from restaurant_bot.application.conversation.contracts import ConversationInteraction
from restaurant_bot.catalog.evidence import (
    query_evidence_tokens,
)
from restaurant_bot.catalog.resolver import CatalogResolver
from restaurant_bot.config import Settings
from restaurant_bot.conversation.comments import (
    append_item_comment,
    apply_global_comment,
    clear_all_active_comments,
    clear_item_comment,
    comment_scope_items,
    remove_cart_comment_shadows,
)
from restaurant_bot.conversation.draft import (
    find_duplicate,
    has_active_draft_items,
    remove_exact_cart_duplicates,
)
from restaurant_bot.conversation.draft_actions import (
    DraftActionOutcome,
    confirm_duplicate_item,
    skip_current_item,
)
from restaurant_bot.conversation.draft_actions import (
    remove_item as remove_draft_item,
)
from restaurant_bot.conversation.item_intake import build_cart_item
from restaurant_bot.conversation.product_add import clear_product_add_pending
from restaurant_bot.conversation.progression import ProgressionKind
from restaurant_bot.conversation.progression import advance as advance_progression
from restaurant_bot.conversation.quantity_resolution import (
    multiple_warnings,
    select_multiple_warning,
    suggested_quantity_for_multiple,
)
from restaurant_bot.conversation.routing.contextual_commands import ContextualCommandPolicy
from restaurant_bot.conversation.routing.contracts import (
    CompatibilityAction,
    CompatibilityContext,
    CompatibilityDecision,
)
from restaurant_bot.conversation.routing.modal_routing import (
    evaluate_modal_routing,
)
from restaurant_bot.conversation.routing.state_compatibility import (
    StateCompatibilityPolicy,
)
from restaurant_bot.conversation.selection import find_cart_item
from restaurant_bot.conversation.state.queries import (
    first_unresolved as first_unresolved_item,
)
from restaurant_bot.conversation.state.queries import item_index as state_item_index
from restaurant_bot.conversation.state.transitions import (
    clear_transient_dialog_state,
    normalize_cart_page,
)
from restaurant_bot.domain.departments import normalize_department
from restaurant_bot.domain.models import (
    BotReply,
    Button,
    CartItem,
    CatalogProduct,
    ConversationState,
    DialogueResponse,
    EngineResult,
    ExtractedItem,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    PendingSubmission,
    SearchScope,
    SessionStage,
)
from restaurant_bot.domain.text import normalize_text
from restaurant_bot.domain.unit_conversion import convert_quantity
from restaurant_bot.domain.units import normalize_unit
from restaurant_bot.input.telegram_visible_actions import (
    available_cart_pages,
    available_final_review_pages,
)
from restaurant_bot.orders.catalog_resolution import CatalogResolutionService
from restaurant_bot.orders.product_add import new_product_add_request_id
from restaurant_bot.orders.supplier_minimums import supplier_minimum_warnings
from restaurant_bot.parsing.commands.api import infer_intent
from restaurant_bot.parsing.commands.dialogue import dialogue_response_for
from restaurant_bot.parsing.commands.item_commands import (
    clean_command_target,
    is_product_add_request_phrase,
)
from restaurant_bot.parsing.commands.normalization import (
    has_negated_action,
)
from restaurant_bot.presentation.telegram.formatting import escape
from restaurant_bot.presentation.telegram.pagination import CART_PAGE_SIZE
from restaurant_bot.presentation.telegram.product_add import product_add_prompt
from restaurant_bot.presentation.telegram.progression import render_progression
from restaurant_bot.presentation.telegram.replies import (
    added_items_question_reply,
    comment_scope_clarification_reply,
    final_review_reply,
    issue_reply,
    multiple_quantity_choice_reply,
    new_order_confirmation_reply,
    new_order_started_reply,
    no_current_manual_reply,
    photo_without_quantities_reply,
    product_add_requests_reply,
    product_add_sending_reply,
    start_adding_supplier_reply,
    submission_retry_reply,
    supplier_warning_choose_reply,
    supplier_warning_details_reply,
    unknown_intent_reply,
    unrecognized_voice_reply,
)
from restaurant_bot.presentation.telegram.replies import (
    cart_reply as render_cart_reply,
)
from restaurant_bot.presentation.telegram.submission import (
    submission_dispatch_uncertain_reply,
    submission_failure_reply,
    submission_in_progress_reply,
    submission_recovery_unavailable_reply,
)
from restaurant_bot.services.conversation_handlers.candidate_selection import (
    CandidateSelectionHandler,
)
from restaurant_bot.services.conversation_handlers.comment_scope import CommentScopeHandler
from restaurant_bot.services.conversation_handlers.final_review import FinalReviewHandler
from restaurant_bot.services.conversation_handlers.navigation import (
    OrderStatusHandler,
    PassiveIntentHandler,
)
from restaurant_bot.services.conversation_handlers.pending_quantity import (
    PendingQuantityAction,
    PendingQuantityHandler,
)


def cart_reply(
    state: ConversationState,
    title: str = "Черновик заявки",
    notice: str = "",
) -> BotReply:
    """Нормализует страницу в state boundary и делегирует Telegram-рендеринг."""
    normalize_cart_page(state, page_size=CART_PAGE_SIZE)
    return render_cart_reply(state, title=title, notice=notice)


class ConversationEngine:
    """Применяет бизнес-правила к состоянию диалога."""

    def __init__(
        self,
        settings: Settings,
        catalog_resolver: CatalogResolver | None = None,
        pending_quantity_handler: PendingQuantityHandler | None = None,
        final_review_handler: FinalReviewHandler | None = None,
        passive_intent_handler: PassiveIntentHandler | None = None,
        order_status_handler: OrderStatusHandler | None = None,
        candidate_selection_handler: CandidateSelectionHandler | None = None,
        comment_scope_handler: CommentScopeHandler | None = None,
        state_compatibility_policy: StateCompatibilityPolicy | None = None,
    ):
        """Инициализирует компонент."""
        self.settings = settings
        self.catalog_resolver = catalog_resolver or CatalogResolver()
        self.catalog_resolution = CatalogResolutionService(self.catalog_resolver)
        self.pending_quantity_handler = pending_quantity_handler or PendingQuantityHandler()
        self.final_review_handler = final_review_handler or FinalReviewHandler()
        self.passive_intent_handler = passive_intent_handler or PassiveIntentHandler()
        self.order_status_handler = order_status_handler or OrderStatusHandler()
        self.candidate_selection_handler = (
            candidate_selection_handler or CandidateSelectionHandler()
        )
        self.comment_scope_handler = comment_scope_handler or CommentScopeHandler()
        self.state_compatibility_policy = state_compatibility_policy or StateCompatibilityPolicy()
        self.contextual_command_policy = ContextualCommandPolicy(
            self.pending_quantity_handler.spoken_quantity
        )

    def handle(
        self,
        event: ConversationInteraction,
        command: ParsedCommand,
        state: ConversationState,
        catalog: list[CatalogProduct],
    ) -> EngineResult:
        """Обрабатывает входные данные текущего компонента."""
        if (
            event.kind in {InputKind.TEXT, InputKind.VOICE}
            and command.dialogue_response is DialogueResponse.NONE
        ):
            command = command.model_copy(
                update={
                    "dialogue_response": dialogue_response_for(
                        event.text or command.text,
                        command.intent,
                        command.items,
                    )
                }
            )
        # Сохраняем существующую нормализацию голоса как шаг до policy
        # для legacy/LLM-команд, которые принимают фразу отправки за add-more.
        if event.kind is InputKind.VOICE and (
            command.intent in {Intent.ADD_MORE, Intent.CONFIRM}
            or state.stage is SessionStage.AWAIT_SUBMIT_CONFIRM
        ):
            command = self.contextual_command_policy.normalize_pre_modal_voice(
                command,
                input_kind=event.kind,
                raw_text=event.text or "",
                state=state,
            )
        modal_decision = evaluate_modal_routing(
            self.state_compatibility_policy,
            command,
            state,
        )
        # Устаревший callback нужно отклонить до любого modal-перехода или
        # очистки, способных изменить текущий черновик.
        if (
            event.kind == InputKind.CALLBACK
            and command.callback_revision is not None
            and command.callback_revision != state.ui_revision
        ):
            unresolved = first_unresolved_item(state)
            reply = (
                issue_reply(unresolved, state_item_index(state, unresolved))
                if unresolved
                else cart_reply(state)
            )
            return EngineResult(state=state, reply=reply)

        state.last_input_text = event.text or command.text
        submission_failed_decision = modal_decision.submission_failed
        if submission_failed_decision.action is not CompatibilityAction.NOT_APPLICABLE:
            return self._handle_submission_failed_recovery(
                event,
                command,
                state,
                submission_failed_decision,
            )
        new_order_decision = modal_decision.new_order_confirmation
        if new_order_decision.action is CompatibilityAction.REJECT:
            pending = state.pending_submission
            if pending is not None and pending.order_no:
                return EngineResult(
                    state=state,
                    reply=submission_in_progress_reply(pending.order_no),
                )
            return EngineResult(state=state, reply=submission_recovery_unavailable_reply())
        if new_order_decision.action is CompatibilityAction.AMBIGUOUS:
            return EngineResult(state=state, reply=new_order_confirmation_reply(state))
        new_order_interrupted = False
        if new_order_decision.action is CompatibilityAction.CONTINUE:
            if new_order_decision.mode == "yes":
                return self._start_new_order(state)
            if new_order_decision.mode == "no":
                state.pending_new_order_confirmation = False
                return self._resume_after_new_order_confirmation(state)
        elif new_order_decision.action is CompatibilityAction.INTERRUPT:
            state.pending_new_order_confirmation = False
            new_order_interrupted = True
        submit_confirm_decision = modal_decision.submit_confirm
        if (
            not new_order_interrupted
            and submit_confirm_decision.action is CompatibilityAction.AMBIGUOUS
        ):
            return EngineResult(state=state, reply=final_review_reply(state))
        if (
            not new_order_interrupted
            and submit_confirm_decision.action is CompatibilityAction.INTERRUPT
        ):
            state.stage = SessionStage.REVIEW
            state.status = "review"
        elif (
            not new_order_interrupted
            and submit_confirm_decision.action is CompatibilityAction.CONTINUE
        ):
            if command.dialogue_response is DialogueResponse.UNCERTAIN:
                return EngineResult(state=state, reply=final_review_reply(state))
            if command.dialogue_response is DialogueResponse.AFFIRM or command.intent in {
                Intent.CONFIRM,
                Intent.SUBMIT_REQUEST,
            }:
                command = command.model_copy(update={"intent": Intent.SUBMIT_AS_IS})
            elif command.intent is Intent.SHOW_CART:
                if self.contextual_command_policy.is_generic_show_products_command(command.text):
                    return EngineResult(
                        state=state,
                        reply=supplier_warning_details_reply(state),
                    )
                state.stage = SessionStage.REVIEW
                state.status = "review"
                return EngineResult(state=state, reply=cart_reply(state))
            elif command.intent is Intent.BACK:
                state.stage = SessionStage.REVIEW
                state.status = "review"
                return EngineResult(state=state, reply=cart_reply(state))
            elif (
                command.intent is Intent.CANCEL
                or command.dialogue_response is DialogueResponse.DECLINE
            ):
                state.stage = SessionStage.REVIEW
                state.status = "review"
                return EngineResult(
                    state=state,
                    reply=cart_reply(state, title="Отправка отменена"),
                )
        add_more_decision = modal_decision.add_more_confirm
        if add_more_decision.action is not CompatibilityAction.NOT_APPLICABLE:
            if add_more_decision.action is CompatibilityAction.AMBIGUOUS:
                return EngineResult(state=state, reply=self._repeat_add_more_prompt(state))
            if command.dialogue_response is DialogueResponse.AFFIRM or command.intent in {
                Intent.ADD_MORE,
                Intent.CONFIRM,
            }:
                command = command.model_copy(update={"intent": Intent.ADD_MORE, "items": []})
            elif command.dialogue_response is DialogueResponse.DECLINE or command.intent in {
                Intent.BACK,
                Intent.CANCEL,
                Intent.SHOW_CART,
            }:
                state.pending_added_items_count = 0
                state.stage = SessionStage.REVIEW
                state.status = "review"
                return EngineResult(state=state, reply=cart_reply(state))
            elif command.intent is Intent.ADD_ITEMS:
                state.pending_added_items_count = 0
                state.stage = SessionStage.COLLECTING
                state.status = "collecting"
            elif add_more_decision.action is CompatibilityAction.INTERRUPT:
                state.pending_added_items_count = 0
                state.stage = SessionStage.REVIEW
                state.status = "review"
        if (
            not new_order_interrupted
            and state.pending_comment_items
            and modal_decision.comment_scope.action
            in {
                CompatibilityAction.CONTINUE,
                CompatibilityAction.AMBIGUOUS,
            }
        ):
            return self._resolve_pending_comment_scope(event, command, state, catalog)
        if (
            not new_order_interrupted
            and modal_decision.manual_details.action is CompatibilityAction.AMBIGUOUS
        ):
            current = state.current_item()
            if current is not None:
                return EngineResult(
                    state=state,
                    reply=issue_reply(current, state_item_index(state, current)),
                )
        if (
            not new_order_interrupted
            and modal_decision.not_found.action is CompatibilityAction.AMBIGUOUS
            and command.intent is Intent.ADD_ITEMS
            and command.items
        ):
            return EngineResult(state=state, reply=unknown_intent_reply(state))
        self._remove_navigation_command_items(state)
        remove_cart_comment_shadows(state)
        remove_exact_cart_duplicates(state)
        current = state.current_item()
        if (
            not modal_decision.quantity_interrupted
            and not modal_decision.candidate_interrupted
            and not modal_decision.not_found_interrupted
            and not modal_decision.duplicate_interrupted
            and not modal_decision.unit_mismatch_interrupted
            and not modal_decision.manual_details_interrupted
            and not modal_decision.product_add_details_interrupted
            and not modal_decision.add_more_confirm_active
        ):
            command = self.contextual_command_policy.reinterpret_contextual_command(
                command,
                input_kind=event.kind,
                raw_text=event.text or "",
                state=state,
                available_cart_pages=available_cart_pages(state.visible_actions),
                available_final_review_pages=available_final_review_pages(state.visible_actions),
            )

        duplicate_decision = self.state_compatibility_policy.evaluate(
            command,
            state,
            CompatibilityContext.DUPLICATE_PENDING,
        )
        if (
            not new_order_interrupted
            and duplicate_decision.action is CompatibilityAction.AMBIGUOUS
            and command.intent is Intent.ADD_ITEMS
        ):
            current = state.current_item()
            if current is not None:
                return EngineResult(
                    state=state,
                    reply=issue_reply(current, state_item_index(state, current)),
                )
        unit_mismatch_decision = self.state_compatibility_policy.evaluate(
            command,
            state,
            CompatibilityContext.UNIT_MISMATCH,
        )
        if (
            not new_order_interrupted
            and unit_mismatch_decision.action is CompatibilityAction.AMBIGUOUS
            and command.intent in {Intent.ADD_ITEMS, Intent.UNKNOWN}
        ):
            current = state.current_item()
            if current is not None:
                return EngineResult(
                    state=state,
                    reply=issue_reply(current, state_item_index(state, current)),
                )
        if command.intent not in {
            Intent.ORDER_STATUS,
            Intent.SMALL_TALK,
            Intent.THANKS,
            Intent.UNKNOWN,
        }:
            state.order_status_view_active = False
        if command.intent in {
            Intent.SUBMIT_REQUEST,
            Intent.SHOW_FINAL_REVIEW,
            Intent.CHECK_MIN_SUM,
        }:
            self.catalog_resolution.refresh_cart_order_values(state, catalog)
        if (
            current
            and current.status in {ItemStatus.NOT_FOUND, ItemStatus.AMBIGUOUS}
            and command.intent
            not in {
                Intent.CANCEL,
                Intent.PRODUCT_ADD_SKIP,
                Intent.SKIP_CURRENT,
                Intent.MANUAL_CURRENT,
            }
            and not has_negated_action(
                event.text or command.text,
                "отправ",
                "переда",
                "созда",
                "оформ",
                "добав",
            )
            and is_product_add_request_phrase(event.text or command.text)
            and not modal_decision.product_add_details_interrupted
        ):
            command = ParsedCommand(
                intent=Intent.PRODUCT_ADD,
                text=event.text or command.text,
                callback_target=str(state_item_index(state, current)),
            )

        # n8n treats an unambiguous spoken/textual candidate name as a
        # контекстный выбор при открытой карточке кандидатов. Нельзя превращать
        # слабое совпадение категории (например, только «сироп») в тихий
        # выбор: разрешён только один строго лучший кандидат.
        candidate_decision = self.state_compatibility_policy.evaluate(
            command,
            state,
            CompatibilityContext.CANDIDATE_SELECTION,
        )
        if (
            current
            and current.status == ItemStatus.AMBIGUOUS
            and not new_order_interrupted
            and candidate_decision.action
            in {CompatibilityAction.CONTINUE, CompatibilityAction.AMBIGUOUS}
            and command.intent in {Intent.UNKNOWN, Intent.ADD_ITEMS}
        ):
            selection_query = normalize_text(event.text or command.text)
            if selection_query:
                scores = [
                    len(query_evidence_tokens(selection_query, candidate.name))
                    for candidate in current.candidates
                ]
                best_score = max(scores, default=0)
                if best_score >= 1 and scores.count(best_score) == 1:
                    command = ParsedCommand(
                        intent=Intent.SELECT_CANDIDATE,
                        text=event.text or command.text,
                        selection_query=event.text or command.text,
                        callback_target=str(state_item_index(state, current)),
                    )
                elif event.kind == InputKind.VOICE and not command.items:
                    # В открытой карточке кандидатов любая неясная голосовая фраза  # noqa: RUF003
                    # может быть выбором, но не новым товаром. Сохранить ту же карточку
                    # безопаснее, чем засорить черновик ошибочной
                    # transcription such as "Был вариант".
                    command = ParsedCommand(
                        intent=Intent.CONTINUE_CURRENT, text=event.text or command.text
                    )
                elif candidate_decision.action is CompatibilityAction.AMBIGUOUS:
                    # A tied category-like phrase is contextual clarification,
                    # это не вторая строка товара и не неявный кандидат.
                    command = ParsedCommand(
                        intent=Intent.CONTINUE_CURRENT, text=event.text or command.text
                    )

        # Повтор доставки сведений о добавлении товара нельзя считать новой  # noqa: RUF003
        # строкой после создания устойчивого запроса. Входящий ящик orchestration
        # также устраняет дубликаты, но это сохраняет контракт n8n на границе
        # state machine.
        if any(
            str(request.get("description_event_key") or "") == str(event.interaction_id)
            for request in state.product_add_requests
        ):
            return EngineResult(state=state, reply=cart_reply(state))

        pending_quantity_action = (
            PendingQuantityAction.NOT_HANDLED
            if modal_decision.quantity_interrupted
            or modal_decision.duplicate.action is CompatibilityAction.INTERRUPT
            or modal_decision.unit_mismatch_interrupted
            else self.pending_quantity_handler.handle(event, command, state)
        )
        if pending_quantity_action is PendingQuantityAction.CONFIRM_CURRENT:
            return self._confirm_current(state)
        if pending_quantity_action is PendingQuantityAction.ADVANCE:
            return self._advance(state)
        if (
            duplicate_decision.action is CompatibilityAction.AMBIGUOUS
            and pending_quantity_action is PendingQuantityAction.NOT_HANDLED
        ):
            current = state.current_item()
            if current is not None:
                return EngineResult(
                    state=state,
                    reply=issue_reply(current, state_item_index(state, current)),
                )

        passive_result = self.passive_intent_handler.handle(command, state)
        if passive_result is not None:
            return passive_result
        order_status_result = self.order_status_handler.handle(event, command, state)
        if order_status_result is not None:
            return order_status_result
        if command.intent == Intent.SHOW_CART:
            if (
                state.stage == SessionStage.AWAIT_SUBMIT_CONFIRM
                and self.contextual_command_policy.is_generic_show_products_command(command.text)
            ):
                return EngineResult(state=state, reply=supplier_warning_details_reply(state))
            state.cart_page = self._cart_page(command, state)
            return EngineResult(state=state, reply=cart_reply(state))
        if command.intent == Intent.START_NEW_ORDER:
            if state.stage == SessionStage.SUBMITTED or not has_active_draft_items(state):
                return self._start_new_order(state)
            state.pending_new_order_confirmation = True
            return EngineResult(state=state, reply=new_order_confirmation_reply(state))
        if command.intent == Intent.CLEAR_CART:
            return self._start_new_order(state)
        if command.intent == Intent.PRODUCT_ADD:
            index = self._callback_item_index(command, state)
            item = state.cart[index] if index is not None and index < len(state.cart) else None
            if item is None or item.status not in {ItemStatus.AMBIGUOUS, ItemStatus.NOT_FOUND}:
                return EngineResult(
                    state=state,
                    reply=BotReply(
                        text="Запрос на добавление доступен только после повторного поиска."
                    ),
                )
            item.product_add_request_id = item.product_add_request_id or new_product_add_request_id(
                item
            )
            state.pending_product_add_item_index = index
            state.pending_product_add_request_id = item.product_add_request_id
            state.stage = SessionStage.AWAIT_PRODUCT_ADD_DETAILS
            state.status = "await_product_add_details"
            return EngineResult(state=state, reply=BotReply(text=product_add_prompt(item)))
        if command.intent == Intent.PRODUCT_ADD_SKIP:
            index = self._callback_item_index(command, state)
            if index is not None and index < len(state.cart):
                state.cart[index].status = ItemStatus.SKIPPED
            clear_product_add_pending(state)
            state.stage = SessionStage.REVIEW
            state.status = "review"
            return self._advance(state)
        if command.intent == Intent.PRODUCT_ADD_RETRY:
            request = next(
                (
                    row
                    for row in state.product_add_requests
                    if row.get("request_id") == command.callback_target
                ),
                None,
            )
            if request and request.get("status") == "write_failed":
                request["status"] = "retry_pending"
                request["updated_at"] = datetime.now(UTC).isoformat()
                state.pending_product_add_request_id = command.callback_target
                state.product_add_write_in_progress = True
                return EngineResult(
                    state=state,
                    reply=BotReply(text="Повторяю отправку запроса с тем же ID."),
                    enqueue_product_add=True,
                )
            if request and request.get("status") == "write_uncertain":
                return EngineResult(
                    state=state,
                    reply=BotReply(
                        text="Не удалось подтвердить отправку. Повторно отправлять запрос не нужно."
                    ),
                )
            return EngineResult(state=state, reply=product_add_requests_reply(state))
        if command.intent == Intent.PRODUCT_ADD_LIST:
            return EngineResult(state=state, reply=product_add_requests_reply(state))
        if command.intent == Intent.SEARCH_ALL_SUPPLIERS:
            index = self._callback_item_index(command, state)
            item = (
                state.cart[index]
                if index is not None and 0 <= index < len(state.cart)
                else state.current_item()
            )
            if item is None:
                return EngineResult(state=state, reply=cart_reply(state))
            state.search_scope = SearchScope.ANY_SUPPLIER
            state.supplier_search_locked = False
            item.supplier_hint = ""
            item.supplier_search_locked = False
            item.catalog_product_id = ""
            item.catalog_name = ""
            item.supplier = ""
            item.catalog_unit = ""
            item.candidates = []
            item.status = ItemStatus.NEW
            self.catalog_resolution.match_item(item, catalog, state.search_scope)
            return self._advance(state)
        if command.intent == Intent.SWITCH_SUPPLIER:
            index = self._callback_item_index(command, state)
            if index is not None and 0 <= index < len(state.cart):
                state.cart[index].status = ItemStatus.SKIPPED
            state.supplier_hint_context = ""
            state.supplier_search_locked = False
            return EngineResult(state=state, reply=supplier_warning_choose_reply(state))
        if command.intent == Intent.CHECK_MIN_SUM:
            return EngineResult(state=state, reply=supplier_warning_details_reply(state))
        if command.intent == Intent.CHOOSE_SUPPLIER_WARNING:
            return EngineResult(state=state, reply=supplier_warning_choose_reply(state))
        if command.intent == Intent.ADD_SUPPLIER_ITEMS:
            try:
                index = int(command.callback_target)
            except ValueError:
                return EngineResult(state=state, reply=supplier_warning_choose_reply(state))
            warnings = supplier_minimum_warnings(state)
            if index < 0 or index >= len(warnings):
                return EngineResult(state=state, reply=supplier_warning_choose_reply(state))
            supplier = warnings[index].supplier
            state.stage = SessionStage.COLLECTING
            state.status = "collecting"
            state.supplier_hint_context = supplier
            state.supplier_search_locked = True
            return EngineResult(state=state, reply=start_adding_supplier_reply(supplier))

        if command.comment_clarification:
            state.pending_comment_items = [item.model_copy(deep=True) for item in command.items]
            state.pending_comment_existing_item_ids = [
                item.id for item in state.cart if item.status != ItemStatus.SKIPPED
            ]
            state.pending_comment_text = command.comment_clarification
            state.pending_comment_global_comment = command.global_comment
            state.stage = SessionStage.AWAIT_COMMENT_SCOPE
            state.status = "await_comment_scope"
            return EngineResult(
                state=state,
                reply=comment_scope_clarification_reply(
                    comment_scope_items(state),
                    state.pending_comment_text,
                ),
            )
        if command.intent == Intent.BACK:
            if event.kind == InputKind.CALLBACK or command.text.startswith("v2:back"):
                return EngineResult(state=state, reply=cart_reply(state))
            clear_transient_dialog_state(state)
            state.stage = SessionStage.COLLECTING
            return EngineResult(state=state, reply=cart_reply(state))
        if command.intent == Intent.ADD_MORE:
            clear_transient_dialog_state(state)
            state.stage = SessionStage.COLLECTING
            return EngineResult(
                state=state,
                reply=BotReply(
                    text="Отправьте товары текстом, голосом или фото — я добавлю их в текущий черновик заказа.",
                    rows=[[Button(text="📦 Показать черновик", callback_data="v2:back")]],
                ),
            )
        if command.intent == Intent.REMOVE_ITEM:
            return self._remove_item(command, state)
        if command.intent == Intent.EDIT_COMMENT:
            return self._edit_existing_comment(command, state)
        if command.intent == Intent.EDIT_QUANTITY:
            return self._edit_quantity(command, state)
        if command.intent == Intent.SELECT_CANDIDATE:
            return self._select_candidate(command, state, catalog)
        if command.intent == Intent.USE_CATALOG_UNIT:
            return self._use_catalog_unit(state)
        if command.intent == Intent.UNIT_OK:
            return self._use_catalog_unit(state)
        if command.intent == Intent.UNIT_EDIT:
            return self._enter_other_quantity(state)
        if command.intent == Intent.FIX_MULTIPLE:
            return self._show_multiple_quantity_choice(state)
        if command.intent == Intent.ACCEPT_SUGGESTED_QUANTITY:
            select_multiple_warning(state)
            return self._accept_suggested_quantity(state)
        if command.intent in {Intent.KEEP_CURRENT_QUANTITY, Intent.KEEP_MULTIPLE}:
            select_multiple_warning(state)
            return self._keep_current_quantity(state)
        if command.intent in {Intent.ENTER_OTHER_QUANTITY, Intent.EDIT_MULTIPLE}:
            select_multiple_warning(state)
            return self._enter_other_quantity(state)
        if command.intent == Intent.SKIP_CURRENT:
            return self._skip_current(state)
        if command.intent == Intent.MANUAL_CURRENT:
            index = self._callback_item_index(command, state)
            if index is not None and 0 <= index < len(state.cart):
                state.current_issue_item_id = state.cart[index].id
            state.stage = SessionStage.AWAIT_MANUAL_DETAILS
            current = state.current_item()
            if current is None:
                return EngineResult(state=state, reply=no_current_manual_reply())
            return EngineResult(
                state=state,
                reply=BotReply(
                    text=(
                        "✏️ <b>Измените название</b>\n\n"
                        f"Текущий запрос: {current.source_query}\n\n"
                        "Отправьте новое название товара."
                    ),
                    rows=[
                        [
                            Button(
                                text="Не добавлять",
                                callback_data=f"v2:skip:{state_item_index(state, current)}",
                            )
                        ],
                        [Button(text="К черновику", callback_data="v2:back")],
                    ],
                ),
            )
        if command.intent == Intent.CONFIRM:
            return self._confirm_current(state)
        if command.intent == Intent.MERGE_DUPLICATE:
            return self._confirm_current(state)
        if command.intent in {Intent.CONTINUE_CURRENT, Intent.CLARIFY_CURRENT}:
            return self._advance(state)
        final_review_outcome = self.final_review_handler.handle(command, state)
        if final_review_outcome is not None:
            if final_review_outcome.prepare_submission:
                return self._prepare_submission(event, state)
            assert final_review_outcome.result is not None
            return final_review_outcome.result
        if command.intent == Intent.CANCEL:
            clear_transient_dialog_state(state)
            state.stage = SessionStage.COLLECTING
            return EngineResult(state=state, reply=cart_reply(state, title="Отправка отменена"))

        if (
            state.stage == SessionStage.AWAIT_PRODUCT_ADD_DETAILS
            and modal_decision.product_add_details.action is CompatibilityAction.CONTINUE
            and command.intent in {Intent.ADD_ITEMS, Intent.UNKNOWN}
        ):
            description = (command.text or event.text).strip()
            if not description and command.intent is Intent.ADD_ITEMS:
                description = ", ".join(
                    item.source_line or item.product_query for item in command.items
                ).strip()
            if description:
                return self._submit_product_add_description(event, state, description)
        if (
            state.stage == SessionStage.AWAIT_PRODUCT_ADD_DETAILS
            and modal_decision.product_add_details.action is CompatibilityAction.AMBIGUOUS
            and not (event.kind is InputKind.PHOTO and command.intent is Intent.ADD_ITEMS)
        ):
            index = state.pending_product_add_item_index
            item = state.cart[index] if index is not None and index < len(state.cart) else None
            if item is not None:
                return EngineResult(state=state, reply=BotReply(text=product_add_prompt(item)))

        if command.intent == Intent.ADD_ITEMS:
            if command.global_comment:
                apply_global_comment(state, command.global_comment)
            if command.global_comment and not command.items:
                active_count = sum(item.status != ItemStatus.SKIPPED for item in state.cart)
                return EngineResult(
                    state=state,
                    reply=BotReply(
                        text=(
                            "<i>Общий комментарий добавлен</i>\n\n"
                            f"{escape(command.global_comment)}\n\n"
                            f"Применён ко всем товарам: {active_count}."
                        )
                    ),
                )
            if event.kind == InputKind.VOICE and not command.items:
                return EngineResult(state=state, reply=unrecognized_voice_reply(state))
            if event.kind == InputKind.PHOTO and not command.items:
                return EngineResult(state=state, reply=photo_without_quantities_reply(state))
            if (
                modal_decision.manual_details.action is CompatibilityAction.CONTINUE
                and state.stage == SessionStage.AWAIT_MANUAL_DETAILS
                and state.current_issue_item_id
            ):
                current = state.current_item()
                if current and command.items:
                    current.source_query = command.items[0].product_query
                    current.quantity = command.items[0].quantity or current.quantity
                    current.unit = command.items[0].unit or current.unit
                    current.status = ItemStatus.NEW
                    current.candidates = []
                    current.rename_attempted = True
                    state.stage = SessionStage.COLLECTING
                    self.catalog_resolution.match_item(current, catalog)
                    return self._advance(state)
            selected_supplier = state.supplier_hint_context
            newly_unresolved_ids: list[str] = []
            for extracted in command.items:
                if selected_supplier:
                    extracted = extracted.model_copy(update={"supplier_hint": selected_supplier})
                else:
                    extracted = self._validate_supplier_hint(extracted, catalog)
                item = self._build_item(extracted, command.global_comment)
                item.supplier_search_locked = bool(selected_supplier)
                self.catalog_resolution.match_item(item, catalog)
                same_missing = next(
                    (
                        existing
                        for existing in state.cart
                        if existing.status == ItemStatus.MISSING_QTY
                        and item.catalog_product_id
                        and existing.catalog_product_id == item.catalog_product_id
                    ),
                    None,
                )
                if same_missing is not None:
                    # n8n считает повтор товара без количества тем же открытым вопросом,
                    # а не новой строкой в черновике.  # noqa: RUF003
                    if item.quantity is not None:
                        same_missing.quantity = item.quantity
                        same_missing.unit = item.unit or same_missing.catalog_unit
                        same_missing.suggested_quantity = suggested_quantity_for_multiple(
                            same_missing
                        )
                        same_missing.status = ItemStatus.MATCHED
                    state.current_issue_item_id = same_missing.id
                    continue
                duplicate = find_duplicate(state, item)
                if duplicate and item.status in {
                    ItemStatus.MATCHED,
                    ItemStatus.MISSING_QTY,
                    ItemStatus.UNIT_MISMATCH,
                }:
                    item.status = ItemStatus.DUPLICATE_PENDING
                    item.issue_message = duplicate.id
                    item.duplicate_existing_quantity = duplicate.quantity or 0
                    item.duplicate_existing_unit = duplicate.unit or duplicate.catalog_unit
                state.cart.append(item)
                if item.status in {
                    ItemStatus.DUPLICATE_PENDING,
                    ItemStatus.UNIT_MISMATCH,
                    ItemStatus.MISSING_QTY,
                    ItemStatus.AMBIGUOUS,
                    ItemStatus.NOT_FOUND,
                    ItemStatus.NEW,
                    ItemStatus.AI_PENDING,
                }:
                    newly_unresolved_ids.append(item.id)
            if selected_supplier:
                state.supplier_hint_context = ""
            preferred_issue_item_id = next(iter(newly_unresolved_ids), "")
            return self._advance(
                state,
                added_count=len(command.items),
                preferred_issue_item_id=preferred_issue_item_id,
            )

        if event.kind == InputKind.VOICE:
            return EngineResult(state=state, reply=unrecognized_voice_reply(state))
        return EngineResult(state=state, reply=unknown_intent_reply(state))

    def _resolve_pending_comment_scope(
        self,
        event: ConversationInteraction,
        command: ParsedCommand,
        state: ConversationState,
        catalog: list[CatalogProduct],
    ) -> EngineResult:
        """Применяет ожидающий комментарий только после надёжного выбора области."""
        outcome = self.comment_scope_handler.handle(command, state)
        if outcome.result is not None:
            return outcome.result
        assert outcome.reprocess_command is not None
        safe_event = event.model_copy(update={"text": ""}) if outcome.clear_event_text else event
        return self.handle(safe_event, outcome.reprocess_command, state, catalog)

    @staticmethod
    def _remove_navigation_command_items(state: ConversationState) -> None:
        """Удаляет ранее ошибочно добавленные голосовые команды из черновика."""
        navigation_intents = {
            Intent.ADD_MORE,
            Intent.BACK,
            Intent.CHECK_MIN_SUM,
            Intent.EDIT_COMMENT,
            Intent.CLARIFY_CURRENT,
            Intent.CLEAR_CART,
            Intent.ENTER_OTHER_QUANTITY,
            Intent.START_NEW_ORDER,
            Intent.GREETING,
            Intent.HELP,
            Intent.ORDER_STATUS,
            Intent.PRODUCT_ADD_SKIP,
            Intent.PRODUCT_ADD_LIST,
            Intent.SHOW_CART,
            Intent.SHOW_FINAL_REVIEW,
            Intent.SUBMIT_REQUEST,
        }
        removable_statuses = {
            ItemStatus.NEW,
            ItemStatus.MISSING_QTY,
            ItemStatus.NOT_FOUND,
            ItemStatus.AMBIGUOUS,
            ItemStatus.AI_PENDING,
        }
        removed_ids = {
            item.id
            for item in state.cart
            if item.quantity is None
            and item.status in removable_statuses
            and (
                infer_intent(item.source_query).intent in navigation_intents
                or not re.search(r"[a-zа-яё]", item.source_query, re.I)
            )
        }
        if not removed_ids:
            return
        state.cart = [item for item in state.cart if item.id not in removed_ids]
        if state.current_issue_item_id in removed_ids:
            state.current_issue_item_id = ""
        if state.stage in {
            SessionStage.AWAIT_UNIT_QUANTITY,
            SessionStage.AWAIT_MULTIPLE_QUANTITY,
        }:
            state.stage = SessionStage.COLLECTING

    def _build_item(self, extracted: ExtractedItem, global_comment: str = "") -> CartItem:
        """Создаёт позицию черновика из распознанного товара."""
        return build_cart_item(
            extracted,
            default_department=self.settings.default_department,
            global_comment=global_comment,
        )

    def _validate_supplier_hint(
        self,
        extracted: ExtractedItem,
        catalog: list[CatalogProduct],
    ) -> ExtractedItem:
        """Оставляет поставщика только при подтверждении живым каталогом."""
        hint = extracted.supplier_hint.strip()
        if not hint:
            return extracted
        canonical_hint = self.catalog_resolver.canonical_supplier_hint(hint, catalog)
        return extracted.model_copy(update={"supplier_hint": canonical_hint})

    @staticmethod
    def _spoken_quantity(text: str) -> tuple[float | None, str]:
        """Сохраняет аналитический контракт восстановления голоса для старых вызывающих сторон."""
        return PendingQuantityHandler.spoken_quantity(text)

    @staticmethod
    def _cart_page(command: ParsedCommand, state: ConversationState) -> int:
        """Определяет запрошенную страницу текущего черновика."""
        target = command.callback_target
        if target.startswith("page:"):
            try:
                return max(0, int(target.partition(":")[2]))
            except ValueError:
                return 0
        return max(0, state.cart_page)

    @staticmethod
    def _fresh_order_state(state: ConversationState) -> ConversationState:
        """Создаёт пустую заявку, сохраняя регистрацию и историю пользователя."""
        return ConversationState(
            last_order_no=state.last_order_no,
            submitted_order_numbers=list(state.submitted_order_numbers),
            restaurant=state.restaurant,
            telegram_user_id=state.telegram_user_id,
            telegram_chat_id=state.telegram_chat_id,
            venue_code=state.venue_code,
            venue_name=state.venue_name,
            spreadsheet_id=state.spreadsheet_id,
            spreadsheet_url=state.spreadsheet_url,
            role=state.role,
            metadata=deepcopy(state.metadata),
            department=state.department,
            product_add_requests=deepcopy(state.product_add_requests),
        )

    def _start_new_order(self, state: ConversationState) -> EngineResult:
        """Безопасно начинает новую заявку без потери истории и регистрации."""
        fresh = self._fresh_order_state(state)
        fresh.metadata["onboarding_shown"] = True
        return EngineResult(state=fresh, reply=new_order_started_reply())

    def _submit_product_add_description(
        self,
        event: ConversationInteraction,
        state: ConversationState,
        description: str,
    ) -> EngineResult:
        """Сохраняет подтверждённое описание товара для отдельного запроса снабженцу."""
        index = state.pending_product_add_item_index
        item = state.cart[index] if index is not None and 0 <= index < len(state.cart) else None
        request_id = state.pending_product_add_request_id or new_product_add_request_id(item)
        existing = next(
            (row for row in state.product_add_requests if row.get("request_id") == request_id),
            None,
        )
        if existing is None:
            now = datetime.now(UTC).isoformat()
            state.product_add_requests.append(
                {
                    "request_id": request_id,
                    "source_item_id": item.id if item else "",
                    "original_query": item.source_query if item else "",
                    "description": description[:1000],
                    "description_event_key": str(event.interaction_id),
                    "telegram_user_id": event.actor_id or event.conversation_id,
                    "telegram_username": str(event.metadata.get("username", "")),
                    "telegram_first_name": str(event.metadata.get("first_name", "")),
                    "telegram_last_name": str(event.metadata.get("last_name", "")),
                    "status": "pending_write",
                    "created_at": now,
                    "updated_at": now,
                    "submitted_at": "",
                    "sheets_error": "",
                }
            )
        if item is not None:
            item.status = ItemStatus.SKIPPED
        state.current_issue_item_id = ""
        state.pending_product_add_request_id = request_id
        state.product_add_write_in_progress = True
        state.stage = SessionStage.REVIEW
        state.status = "review"
        return EngineResult(
            state=state,
            reply=product_add_sending_reply(),
            enqueue_product_add=True,
        )

    @staticmethod
    def _callback_item_index(command: ParsedCommand, state: ConversationState) -> int | None:
        """Безопасно определяет номер позиции из callback."""
        try:
            if command.callback_target:
                return int(command.callback_target)
        except ValueError:
            pass
        if state.current_issue_item_id:
            return next(
                (i for i, item in enumerate(state.cart) if item.id == state.current_issue_item_id),
                None,
            )
        return None

    @staticmethod
    def _repeat_add_more_prompt(state: ConversationState) -> BotReply:
        """Повторяет существующий вопрос о добавлении товаров без мутации state."""
        fallback = added_items_question_reply(state, 1)
        return BotReply(
            text=state.ui_message_text or fallback.text,
            rows=fallback.rows,
        )

    def _advance(
        self,
        state: ConversationState,
        added_count: int = 0,
        preferred_issue_item_id: str = "",
    ) -> EngineResult:
        """Переходит к следующей нерешённой позиции."""
        progression = advance_progression(
            state,
            added_count=added_count,
            preferred_issue_item_id=preferred_issue_item_id,
        )
        if progression.kind is ProgressionKind.DRAFT:
            normalize_cart_page(state, page_size=CART_PAGE_SIZE)
        return EngineResult(state=state, reply=render_progression(state, progression))

    def _resume_after_new_order_confirmation(self, state: ConversationState) -> EngineResult:
        """Показывает сохранённый underlying modal context после отказа."""
        if state.pending_comment_items:
            return EngineResult(
                state=state,
                reply=comment_scope_clarification_reply(
                    comment_scope_items(state),
                    state.pending_comment_text,
                ),
            )
        if state.stage is SessionStage.AWAIT_ADD_MORE_CONFIRM:
            return EngineResult(state=state, reply=self._repeat_add_more_prompt(state))
        if state.stage is SessionStage.AWAIT_SUBMIT_CONFIRM:
            return EngineResult(state=state, reply=final_review_reply(state))
        if state.stage is SessionStage.AWAIT_PRODUCT_ADD_DETAILS:
            index = state.pending_product_add_item_index
            item = state.cart[index] if index is not None and 0 <= index < len(state.cart) else None
            if item is not None:
                return EngineResult(state=state, reply=BotReply(text=product_add_prompt(item)))
        current = state.current_item()
        if current is not None and current.status in {
            ItemStatus.AMBIGUOUS,
            ItemStatus.NOT_FOUND,
            ItemStatus.DUPLICATE_PENDING,
            ItemStatus.UNIT_MISMATCH,
            ItemStatus.MISSING_QTY,
        }:
            return self._advance(state)
        return EngineResult(state=state, reply=cart_reply(state))

    def _select_candidate(
        self,
        command: ParsedCommand,
        state: ConversationState,
        catalog: list[CatalogProduct],
    ) -> EngineResult:
        """Выбирает указанный товар из списка кандидатов."""
        outcome = self.candidate_selection_handler.resolve(command, state)
        if outcome.result is not None:
            return outcome.result
        assert outcome.item is not None and outcome.candidate is not None
        item = outcome.item
        self.catalog_resolution.apply_catalog(item, outcome.candidate, catalog)
        duplicate = find_duplicate(
            ConversationState(cart=[current for current in state.cart if current.id != item.id]),
            item,
        )
        if duplicate and item.status in {
            ItemStatus.MATCHED,
            ItemStatus.MISSING_QTY,
            ItemStatus.UNIT_MISMATCH,
        }:
            item.status = ItemStatus.DUPLICATE_PENDING
            item.issue_message = duplicate.id
            item.duplicate_existing_quantity = duplicate.quantity or 0
            item.duplicate_existing_unit = duplicate.unit or duplicate.catalog_unit
        return self._advance(state)

    def _use_catalog_unit(self, state: ConversationState) -> EngineResult:
        """Принимает единицу измерения из каталога."""
        item = state.current_item()
        if item is None:
            return EngineResult(state=state, reply=cart_reply(state))
        if item.catalog_unit:
            converted = convert_quantity(item.quantity or 0, item.unit, item.catalog_unit)
            if converted is not None:
                item.quantity = converted
            item.unit = item.catalog_unit
        item.status = ItemStatus.MATCHED if item.quantity else ItemStatus.MISSING_QTY
        return self._advance(state)

    def _accept_suggested_quantity(self, state: ConversationState) -> EngineResult:
        """Принимает рекомендуемое количество товара."""
        item = state.current_item()
        if item is None:
            return EngineResult(state=state, reply=cart_reply(state))
        if item.suggested_quantity is not None:
            item.quantity = item.suggested_quantity
        item.status = ItemStatus.MATCHED if item.quantity else ItemStatus.MISSING_QTY
        return self._advance_multiple_quantity_choice(state)

    def _keep_current_quantity(self, state: ConversationState) -> EngineResult:
        """Сохраняет текущее количество позиции."""
        item = state.current_item()
        if item is None:
            return EngineResult(state=state, reply=cart_reply(state))
        item.suggested_quantity = None
        item.status = ItemStatus.MATCHED if item.quantity else ItemStatus.MISSING_QTY
        return self._advance_multiple_quantity_choice(state)

    def _enter_other_quantity(self, state: ConversationState) -> EngineResult:
        """Переводит диалог к ручному вводу количества."""
        item = state.current_item()
        if item is None:
            return EngineResult(state=state, reply=cart_reply(state))
        state.stage = (
            SessionStage.AWAIT_UNIT_QUANTITY
            if item.status == ItemStatus.UNIT_MISMATCH
            else SessionStage.AWAIT_MULTIPLE_QUANTITY
        )
        expected_unit = item.catalog_unit or item.unit
        if item.status == ItemStatus.UNIT_MISMATCH and expected_unit:
            prompt = (
                f"Укажите количество в {expected_unit} одним сообщением. "
                f"Например: 5 {expected_unit}."
            )
        else:
            example = f"5 {expected_unit}".strip()
            prompt = f"Укажите другое количество одним сообщением. Например: {example}."
        return EngineResult(
            state=state,
            reply=BotReply(
                text=prompt,
                rows=[
                    [Button(text="Вернуться к вариантам", callback_data="v2:resolve")],
                    [
                        Button(
                            text="Не добавлять",
                            callback_data=f"v2:skip:{state_item_index(state, item)}",
                        )
                    ],
                ],
            ),
        )

    def _show_multiple_quantity_choice(self, state: ConversationState) -> EngineResult:
        """Открывает варианты количества без автоматического изменения."""
        item = select_multiple_warning(state)
        if item is None:
            state.current_issue_item_id = ""
            return EngineResult(state=state, reply=final_review_reply(state))
        state.stage = SessionStage.AWAIT_SUBMIT_CONFIRM
        state.status = "await_multiple_choice"
        return EngineResult(state=state, reply=multiple_quantity_choice_reply(item))

    def _advance_multiple_quantity_choice(self, state: ConversationState) -> EngineResult:
        """Показывает следующий выбор количества или финальную проверку."""
        state.current_issue_item_id = ""
        next_item = select_multiple_warning(state)
        if next_item is not None:
            state.stage = SessionStage.AWAIT_SUBMIT_CONFIRM
            state.status = "await_multiple_choice"
            return EngineResult(state=state, reply=multiple_quantity_choice_reply(next_item))
        state.stage = SessionStage.AWAIT_SUBMIT_CONFIRM
        state.status = "await_submit_confirm"
        return EngineResult(state=state, reply=final_review_reply(state))

    def _skip_current(self, state: ConversationState) -> EngineResult:
        """Пропускает текущую позицию."""
        skip_current_item(state)
        return self._advance(state)

    def _confirm_current(self, state: ConversationState) -> EngineResult:
        """Подтверждает текущую позицию черновика."""
        confirm_duplicate_item(state)
        return self._advance(state)

    def _remove_item(self, command: ParsedCommand, state: ConversationState) -> EngineResult:
        """Удаляет выбранную позицию из черновика."""
        outcome = remove_draft_item(command, state).outcome
        if outcome is DraftActionOutcome.ADVANCE:
            return self._advance(state)
        if outcome is DraftActionOutcome.REMOVED:
            return EngineResult(state=state, reply=cart_reply(state, title="Позиция удалена"))
        return EngineResult(state=state, reply=BotReply(text="Не нашёл такую позицию в черновике."))

    def _edit_existing_comment(
        self, command: ParsedCommand, state: ConversationState
    ) -> EngineResult:
        """Изменяет комментарий только у однозначно найденного товара черновика."""
        target = clean_command_target(command.comment_target_query)
        comment = " ".join(command.comment_text.split()).strip(" .,;:-—–")
        if command.comment_scope == "order":
            if command.comment_action == "remove":
                had_comments = any(
                    item.status != ItemStatus.SKIPPED and item.comment.strip()
                    for item in state.cart
                )
                clear_all_active_comments(state)
                notice = "Комментарии удалены" if had_comments else "Комментариев для удаления нет"
                return EngineResult(
                    state=state,
                    reply=cart_reply(state, notice=notice),
                )
            if not comment:
                return EngineResult(
                    state=state,
                    reply=cart_reply(state, notice="Не указан текст общего комментария"),
                )
            apply_global_comment(state, comment)
            return EngineResult(state=state, reply=cart_reply(state, notice="Комментарий добавлен"))

        if command.comment_action == "remove":
            if not target:
                return EngineResult(
                    state=state,
                    reply=cart_reply(state, notice="Не указан товар для удаления комментария"),
                )
        elif not target or not comment:
            return EngineResult(
                state=state,
                reply=BotReply(
                    text="⚠️ Укажите товар и комментарий, например: «к батону — желательно крупный»."
                ),
            )
        item = find_cart_item(state, target)
        if item is None:
            return EngineResult(
                state=state,
                reply=cart_reply(
                    state,
                    notice=f"Не нашёл товар «{target}» для комментария",
                ),
            )
        if command.comment_action == "remove":
            clear_item_comment(item)
            notice = "Комментарий удалён"
        else:
            append_item_comment(item, comment)
            notice = "Комментарий добавлен"
        return EngineResult(state=state, reply=cart_reply(state, notice=notice))

    def _edit_quantity(self, command: ParsedCommand, state: ConversationState) -> EngineResult:
        """Изменяет количество выбранной позиции."""
        if command.edit_quantity is None:
            return EngineResult(state=state, reply=BotReply(text="Укажите новое количество."))
        target = normalize_text(command.target_query)
        item = state.current_item()
        if target:
            item = find_cart_item(state, command.target_query)
        if item is None:
            return EngineResult(
                state=state, reply=BotReply(text="Позиция для изменения не найдена.")
            )
        was_multiple_choice = item in multiple_warnings(state) and state.stage in {
            SessionStage.AWAIT_SUBMIT_CONFIRM,
            SessionStage.AWAIT_MULTIPLE_QUANTITY,
        }
        quantity = command.edit_quantity
        if (
            command.edit_unit
            and item.catalog_unit
            and normalize_unit(command.edit_unit) != normalize_unit(item.catalog_unit)
        ):
            item.quantity = quantity
            item.unit = normalize_unit(command.edit_unit)
            item.status = ItemStatus.UNIT_MISMATCH
            return self._advance(state)
        item.quantity = quantity
        item.unit = item.catalog_unit or command.edit_unit or item.unit
        if item.catalog_product_id:
            item.status = ItemStatus.MATCHED
            item.suggested_quantity = suggested_quantity_for_multiple(item)
        if was_multiple_choice:
            return self._advance_multiple_quantity_choice(state)
        return self._advance(state)

    def _prepare_submission(
        self, event: ConversationInteraction, state: ConversationState
    ) -> EngineResult:
        """Фиксирует черновик для надёжной отправки."""
        if (
            state.pending_submission
            and state.pending_submission.failed_stage == "dispatch_uncertain"
        ):
            return EngineResult(
                state=state,
                reply=submission_dispatch_uncertain_reply(
                    state,
                    state.pending_submission.order_no,
                ),
            )
        if (
            state.pending_submission
            and state.pending_submission.order_no
            and state.pending_submission.rows
        ):
            state.pending_submission.last_attempt_at = datetime.now(UTC)
            state.pending_submission.last_error = ""
            state.stage = SessionStage.SUBMITTING
            state.status = "submitting"
            return EngineResult(
                state=state,
                reply=submission_retry_reply(state.pending_submission.order_no),
                enqueue_submission=True,
            )
        matched = [item for item in state.cart if item.status == ItemStatus.MATCHED]
        if not matched:
            return EngineResult(state=state, reply=cart_reply(state))
        now = datetime.now(UTC)
        order_no = f"{now:%Y%m%d-%H%M%S}-{event.conversation_id[-4:]}"
        supplier_totals: dict[str, float] = defaultdict(float)
        for item in matched:
            supplier_totals[item.supplier] += item.amount
        rows = []
        for item in matched:
            for department, quantity in self._submission_department_quantities(item):
                amount = quantity * item.price if item.price is not None else 0.0
                rows.append(
                    {
                        "Время создания заявки": now.isoformat(),
                        "Время изменения": now.isoformat(),
                        "ID заявки": f"{item.supplier}|{state.restaurant}|{order_no}",
                        "№ Заявки": order_no,
                        "Условное название поставщика": item.supplier,
                        "Условное наз-ие заведения": state.restaurant,
                        "Роль": department,
                        "ID товара": item.catalog_product_id,
                        "Наименование у поставщика": item.catalog_name,
                        "Ед.Изм. для заказа": item.catalog_unit,
                        "Минимальная Кратность в заказе": item.minimum_multiple or "",
                        "Полезный V, m Нетто Ед.Изм.для Заказа": item.useful_volume or "",
                        "Цена за Ед.Изм. для заказа": item.price or "",
                        "Кол-во": quantity or "",
                        "Мин сумма Заказа по Поставщику": item.supplier_minimum_amount or "",
                        "Комментарий": item.comment,
                        "Сумма по товару в заказе": amount,
                        "Сумма по заявке к поставщику": supplier_totals[item.supplier],
                        "Стадия": "Новая заявка",
                        "Стадия от Заведения": "",
                        "_department": department,
                    }
                )
        state.pending_submission = PendingSubmission(
            order_no=order_no,
            trace_id=state.order_trace_id,
            telegram_user_id=event.actor_id or event.conversation_id,
            telegram_chat_id=event.conversation_id,
            rows=rows,
            spreadsheet_id=state.spreadsheet_id,
            venue_code=state.venue_code,
        )
        state.stage = SessionStage.SUBMITTING
        progress = (
            "Отправляю заявку"
            if self.settings.google_order_submission_enabled
            else "Подготавливаю заявку"
        )
        return EngineResult(
            state=state,
            reply=BotReply(text=f"{progress} <b>{order_no}</b>..."),
            enqueue_submission=True,
        )

    def _handle_submission_failed_recovery(
        self,
        event: ConversationInteraction,
        command: ParsedCommand,
        state: ConversationState,
        decision: CompatibilityDecision,
    ) -> EngineResult:
        """Применяет recovery lock до любого изменения текущего черновика."""
        action = decision.action
        mode = decision.mode
        pending = state.pending_submission
        if action in {CompatibilityAction.REJECT, CompatibilityAction.AMBIGUOUS}:
            if mode == "broken" or pending is None:
                reply = submission_recovery_unavailable_reply()
            elif mode == "dispatch_uncertain":
                reply = submission_dispatch_uncertain_reply(state, pending.order_no)
            else:
                reply = submission_failure_reply(state, pending.order_no)
            return EngineResult(state=state, reply=reply)

        if command.intent in {
            Intent.HELP,
            Intent.THANKS,
            Intent.SMALL_TALK,
            Intent.GREETING,
        }:
            passive = self.passive_intent_handler.handle(command, state)
            if passive is not None:
                return passive
        if command.intent is Intent.ORDER_STATUS:
            status = self.order_status_handler.handle(event, command, state)
            if status is not None:
                return status
        if command.intent is Intent.PRODUCT_ADD_LIST:
            return EngineResult(state=state, reply=product_add_requests_reply(state))
        if command.intent in {Intent.BACK, Intent.SHOW_CART}:
            if command.intent is Intent.SHOW_CART:
                state.cart_page = self._cart_page(command, state)
            return EngineResult(state=state, reply=cart_reply(state))

        retry_requested = (
            command.retry_requested
            or command.intent in {Intent.SUBMIT_AS_IS, Intent.SUBMIT_REQUEST, Intent.CONFIRM}
            or command.dialogue_response is DialogueResponse.AFFIRM
        )
        if retry_requested and mode == "retryable":
            return self._prepare_submission(event, state)
        if pending is None:
            return EngineResult(state=state, reply=submission_recovery_unavailable_reply())
        reply = (
            submission_dispatch_uncertain_reply(state, pending.order_no)
            if mode == "dispatch_uncertain"
            else submission_failure_reply(state, pending.order_no)
        )
        return EngineResult(state=state, reply=reply)

    @staticmethod
    def _submission_department_quantities(item: CartItem) -> list[tuple[str, float]]:
        """Возвращает количества позиции по отделам для записи в таблицу."""
        department_values = (
            ("Зал", item.department_quantities.hall),
            ("Бар", item.department_quantities.bar),
            ("Кухня", item.department_quantities.kitchen),
        )
        rows = [
            (department, value) for department, value in department_values if value and value > 0
        ]
        if rows:
            return rows
        return [(normalize_department(item.department) or "Кухня", item.quantity or 0)]
