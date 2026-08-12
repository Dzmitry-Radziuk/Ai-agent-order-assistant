from __future__ import annotations

import re
from collections import defaultdict
from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

from restaurant_bot.catalog.evidence import (
    query_evidence_tokens,
)
from restaurant_bot.catalog.resolver import CatalogResolver
from restaurant_bot.config import Settings
from restaurant_bot.conversation.comments import (
    apply_global_comment,
    clear_pending_comment,
    comment_scope_items,
    merge_comments,
    prune_pending_comment_item_ids,
    remove_cart_comment_shadows,
    remove_global_comment_overlap,
)
from restaurant_bot.conversation.draft import (
    find_duplicate,
    has_active_draft_items,
    remove_exact_cart_duplicates,
)
from restaurant_bot.conversation.progression import ProgressionKind
from restaurant_bot.conversation.progression import advance as advance_progression
from restaurant_bot.conversation.quantity_resolution import (
    is_multiple_warning,
    multiple_warnings,
    select_multiple_warning,
    suggested_quantity_for_multiple,
)
from restaurant_bot.conversation.routing.contracts import (
    CompatibilityAction,
    CompatibilityContext,
    CompatibilityDecision,
)
from restaurant_bot.conversation.routing.item_resolution import has_named_product_items
from restaurant_bot.conversation.routing.modal_routing import (
    evaluate_modal_routing,
)
from restaurant_bot.conversation.routing.state_compatibility import (
    StateCompatibilityPolicy,
)
from restaurant_bot.conversation.selection import contains_score, find_cart_item
from restaurant_bot.conversation.state.queries import (
    first_unresolved as first_unresolved_item,
)
from restaurant_bot.conversation.state.queries import item_index as state_item_index
from restaurant_bot.domain.models import (
    BotReply,
    Button,
    Candidate,
    CartItem,
    CatalogProduct,
    CommentSource,
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
    TelegramEvent,
)
from restaurant_bot.orders.catalog_resolution import CatalogResolutionService
from restaurant_bot.orders.supplier_minimums import supplier_minimum_warnings
from restaurant_bot.parsing.quantities import has_explicit_order_quantity
from restaurant_bot.services.comment_policy import supplier_comment_start
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
from restaurant_bot.services.parser import (
    clean_command_target,
    dialogue_response_for,
    has_negated_action,
    has_negation,
    infer_intent,
    is_explicit_item_rejection,
    is_product_add_request_phrase,
    normalize_command_text,
    parse_product_lines,
)
from restaurant_bot.services.product_add_flow import (
    clear_product_add_pending,
    new_product_add_request_id,
    product_add_prompt,
)
from restaurant_bot.services.replies import (
    added_items_question_reply,
    cart_reply,
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
from restaurant_bot.services.submission_presenter import (
    submission_dispatch_uncertain_reply,
    submission_failure_reply,
    submission_in_progress_reply,
    submission_recovery_unavailable_reply,
)
from restaurant_bot.services.text import (
    NUMBER_WORDS,
    UNIT_ALIASES,
    convert_quantity,
    escape,
    normalize_department,
    normalize_text,
    normalize_unit,
)


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

    def handle(
        self,
        event: TelegramEvent,
        command: ParsedCommand,
        state: ConversationState,
        catalog: list[CatalogProduct],
    ) -> EngineResult:
        """Обрабатывает входные данные текущего компонента."""
        if (
            event.input_type in {InputKind.TEXT, InputKind.VOICE}
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
        # Keep the existing voice normalizer as a pre-policy normalization
        # step for legacy/LLM commands that mislabel a submit phrase as add-more.
        if event.input_type is InputKind.VOICE and (
            command.intent in {Intent.ADD_MORE, Intent.CONFIRM}
            or state.stage is SessionStage.AWAIT_SUBMIT_CONFIRM
        ):
            command = self._contextual_voice_command(command, event, state)
        modal_decision = evaluate_modal_routing(
            self.state_compatibility_policy,
            command,
            state,
        )
        # A stale callback must be rejected before any modal transition or
        # cleanup can mutate the current draft.
        if (
            event.input_type == InputKind.CALLBACK
            and command.callback_revision is not None
            and command.callback_revision != state.ui_revision
        ):
            unresolved = self._first_unresolved(state)
            reply = (
                issue_reply(unresolved, self._item_index(state, unresolved))
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
                if self._is_generic_show_products_command(command.text):
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
                    reply=issue_reply(current, self._item_index(state, current)),
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

        # The source n8n workflow accepts voice phrases such as "отправь
        # запрос снабженцу" in any word order while a not-found/candidate card
        # is open.  Do this before generic intent handling: the parser can
        # otherwise interpret the phrase as a product line, and bare
        # "отправить" remains the normal submit command in every other state.
        current = state.current_item()

        # Voice transcription and an LLM intent are advisory only.  The
        # n8n workflow resolves short spoken corrections against the card that
        # is currently open: a phrase such as "исправить" must never become a
        # new product search, and "поставь 40" must affect only that item.
        # Keep that state-aware rule local and deterministic for text and
        # voice alike.
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
            command = self._contextual_negative_command(command, event, state)
            command = self._contextual_quantity_command(command, event.text, state)
            command = self._contextual_cart_pagination_command(command, event, state)
            command = self._contextual_final_review_pagination_command(command, event, state)
            command = self._contextual_order_status_command(command, event, state)
            command = self._contextual_voice_command(command, event, state)

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
                    reply=issue_reply(current, self._item_index(state, current)),
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
                    reply=issue_reply(current, self._item_index(state, current)),
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
            self._refresh_cart_order_values(state, catalog)
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
                callback_target=str(self._item_index(state, current)),
            )

        # n8n treats an unambiguous spoken/textual candidate name as a
        # contextual selection while the candidate card is open.  Do not turn
        # a weak category match (for example, just "сироп") into a silent
        # selection: only one strictly best candidate may be selected.
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
                        callback_target=str(self._item_index(state, current)),
                    )
                elif event.input_type == InputKind.VOICE and not command.items:
                    # On an open candidate card every vague voice utterance
                    # is a possible choice, never a new product.  Keeping the
                    # same card is safer than polluting the draft with a bad
                    # transcription such as "Был вариант".
                    command = ParsedCommand(
                        intent=Intent.CONTINUE_CURRENT, text=event.text or command.text
                    )
                elif candidate_decision.action is CompatibilityAction.AMBIGUOUS:
                    # A tied category-like phrase is contextual clarification,
                    # not a second product line and not an implicit candidate.
                    command = ParsedCommand(
                        intent=Intent.CONTINUE_CURRENT, text=event.text or command.text
                    )

        # A delivery retry of product-add details must not be interpreted as a
        # fresh product line after the first attempt has already created the
        # stable request. The orchestration inbox also deduplicates updates,
        # but this preserves the n8n contract at the state-machine boundary.
        if any(
            str(request.get("description_event_key") or "") == str(event.update_id)
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
                    reply=issue_reply(current, self._item_index(state, current)),
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
                and self._is_generic_show_products_command(command.text)
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
            self._match_item(item, catalog, state.search_scope)
            return self._advance(state)
        if command.intent == Intent.SWITCH_SUPPLIER:
            index = self._callback_item_index(command, state)
            if index is not None and 0 <= index < len(state.cart):
                state.cart[index].status = ItemStatus.SKIPPED
            state.supplier_hint_context = ""
            state.supplier_search_locked = False
            # n8n skips the locked item, then returns to the supplier-warning
            # chooser; it does not accept a free-form supplier name here.
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

        # `v2:back` is n8n's return-to-draft button. It must render the
        # existing draft (including unresolved positions), not clear context
        # and ask for another product.
        if command.intent == Intent.BACK:
            if event.input_type == InputKind.CALLBACK or command.text.startswith("v2:back"):
                return EngineResult(state=state, reply=cart_reply(state))
            self._clear_transient_dialog_state(state)
            state.stage = SessionStage.COLLECTING
            return EngineResult(state=state, reply=cart_reply(state))
        if command.intent == Intent.ADD_MORE:
            self._clear_transient_dialog_state(state)
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
                                callback_data=f"v2:skip:{self._item_index(state, current)}",
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
            self._clear_transient_dialog_state(state)
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
            and not (event.input_type is InputKind.PHOTO and command.intent is Intent.ADD_ITEMS)
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
            if event.input_type == InputKind.VOICE and not command.items:
                return EngineResult(state=state, reply=unrecognized_voice_reply(state))
            if event.input_type == InputKind.PHOTO and not command.items:
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
                    self._match_item(current, catalog)
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
                self._match_item(item, catalog)
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
                    # n8n treats a repeated product without a quantity as the
                    # same open question, not as another line in the draft.
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
            # `v2:minsumadd` scopes exactly the next incoming product message
            # in n8n. Retaining this context would incorrectly lock every
            # later product to the same supplier.
            if selected_supplier:
                state.supplier_hint_context = ""
            preferred_issue_item_id = next(iter(newly_unresolved_ids), "")
            return self._advance(
                state,
                added_count=len(command.items),
                preferred_issue_item_id=preferred_issue_item_id,
            )

        if event.input_type == InputKind.VOICE:
            return EngineResult(state=state, reply=unrecognized_voice_reply(state))
        return EngineResult(state=state, reply=unknown_intent_reply(state))

    def _resolve_pending_comment_scope(
        self,
        event: TelegramEvent,
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
        item_comment = extracted.comment or extracted.user_comment_to_supplier
        item_comment = remove_global_comment_overlap(item_comment, global_comment)
        comment_source = extracted.comment_source if item_comment else CommentSource.NONE
        if global_comment:
            comment_source = CommentSource.SEMANTIC
        quantity = extracted.quantity
        unit = normalize_unit(extracted.unit)
        if quantity is not None and not extracted.quantity_source and extracted.source_line:
            source_items = parse_product_lines(extracted.source_line)
            if (
                len(source_items) == 1
                and source_items[0].quantity is None
                and not self._has_explicit_order_quantity(
                    extracted.source_line,
                    quantity,
                )
            ):
                # Последняя защита от ошибочного количества, которое модель
                # взяла из диапазона размера или фасовки в исходной строке.
                quantity = None
                unit = ""
        return CartItem(
            id=uuid4().hex[:12],
            source_query=extracted.product_query,
            source_line=extracted.source_line,
            quantity_source=extracted.quantity_source,
            order_entry_type=extracted.order_entry_type,
            packaging_text=extracted.packaging_text,
            packaging_role=extracted.packaging_role,
            packaging_confidence=extracted.packaging_confidence,
            quantity=quantity,
            unit=unit,
            department=normalize_department(extracted.department)
            or self.settings.default_department,
            department_quantities=extracted.department_quantities.model_copy(deep=True),
            supplier_hint=extracted.supplier_hint,
            comment=merge_comments(item_comment, global_comment),
            comment_source=comment_source,
        )

    @staticmethod
    def _has_explicit_order_quantity(source_line: str, quantity: float | None) -> bool:
        """Отличает объём заказа от чисел в размере или фасовке товара."""
        return has_explicit_order_quantity(source_line, quantity)

    @staticmethod
    def _spoken_unit_only_quantity(text: str) -> tuple[float | None, str]:
        """Понимает короткое «ладно, бутылка» как одну текущую позицию."""
        return PendingQuantityHandler.spoken_unit_only_quantity(text)

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
        """Извлекает явно произнесённое количество."""
        return PendingQuantityHandler.spoken_quantity(text)

    @staticmethod
    def _mentions_expected_unit(text: str, expected_unit: str) -> bool:
        """Проверяет, упомянул ли пользователь единицу открытой позиции."""
        normalized_expected = normalize_unit(expected_unit)
        if not normalized_expected:
            return False
        words = re.findall(r"[a-zа-яё]+", normalize_text(text), flags=re.I)
        return any(
            word in UNIT_ALIASES and normalize_unit(word) == normalized_expected for word in words
        )

    def _has_named_product_items(self, command: ParsedCommand, text: str) -> bool:
        """Отличает полноценный товарный запрос от короткого ответа количеством."""
        return has_named_product_items(command, text)

    def _contextual_quantity_command(
        self,
        command: ParsedCommand,
        text: str,
        state: ConversationState,
    ) -> ParsedCommand:
        """Обрабатывает команду количества с учётом текущего экрана."""
        if command.text.startswith("v2:"):
            return command
        current = state.current_item()
        if current is None:
            return command

        phrase = normalize_text(text or command.text)
        if not phrase:
            return command
        words = set(phrase.split())
        has_product_word = any(
            word.startswith(("товар", "позици", "продукт", "покуп")) for word in words
        )
        quantity, unit = self._spoken_quantity(phrase)
        correction = any(word.startswith(("исправ", "поправ", "измен", "поменя")) for word in words)
        recommended = (
            "как нужно" in phrase
            or "рекоменд" in phrase
            or "округл" in phrase
            or "ближайш" in phrase
        )
        contextual_add = bool(
            re.fullmatch(
                r"(?:(?:добавь|добавить)(?: еще)?(?: недостающее)?|докинь|дополни)",
                phrase,
            )
        )
        add_to_current = (
            command.intent == Intent.ADD_MORE or contextual_add
        ) and not has_product_word
        increment_current = quantity is not None and bool(
            re.search(
                r"(?:^|\s)(?:добавь|добавить|докинь|докинуть|прибавь|прибавить|дополни)"
                r"(?:\s|$)",
                phrase,
            )
        )

        has_multiple_warning = is_multiple_warning(current)
        if has_multiple_warning:
            if quantity is not None:
                return command.model_copy(
                    update={
                        "intent": Intent.EDIT_QUANTITY,
                        "edit_quantity": (current.quantity or 0) + quantity
                        if increment_current
                        else quantity,
                        "edit_unit": unit,
                        "target_query": "",
                    }
                )
            if recommended or add_to_current:
                return command.model_copy(update={"intent": Intent.ACCEPT_SUGGESTED_QUANTITY})
            if correction:
                wants_manual_quantity = any(
                    word.startswith(("друг", "нов", "сво")) for word in words
                )
                return command.model_copy(
                    update={
                        "intent": (
                            Intent.ENTER_OTHER_QUANTITY
                            if wants_manual_quantity
                            else Intent.FIX_MULTIPLE
                        )
                    }
                )

        if current.status == ItemStatus.UNIT_MISMATCH:
            rejects_quantity_change = has_negated_action(
                phrase,
                "добав",
                "введ",
                "укаж",
                "постав",
                "закаж",
                "возьм",
                "измен",
                "поменя",
                "перевед",
                "конверт",
            )
            if rejects_quantity_change:
                return command
            if quantity is not None:
                return command.model_copy(
                    update={
                        "intent": Intent.EDIT_QUANTITY,
                        "edit_quantity": quantity,
                        "edit_unit": unit,
                        "target_query": "",
                    }
                )
            if add_to_current or "перевед" in phrase or "конверт" in phrase:
                return command.model_copy(update={"intent": Intent.USE_CATALOG_UNIT})
            if correction or self._mentions_expected_unit(phrase, current.catalog_unit):
                return command.model_copy(update={"intent": Intent.ENTER_OTHER_QUANTITY})

        if (
            current.status == ItemStatus.MISSING_QTY
            and quantity is not None
            and (
                not self._has_named_product_items(command, phrase)
                # A parser/ASR pass may classify a bare spoken number such as
                # «пять» or «один» as candidate #1.  Once the selected item
                # is already known to be missing a quantity, the context is
                # stronger than that generic intent and the number is always
                # the answer to the quantity question.
                or (
                    command.intent == Intent.SELECT_CANDIDATE
                    and self._is_quantity_only_phrase(phrase)
                )
            )
        ):
            return command.model_copy(
                update={
                    "intent": Intent.EDIT_QUANTITY,
                    "edit_quantity": quantity,
                    "edit_unit": unit,
                    "target_query": "",
                }
            )
        return command

    @staticmethod
    def _is_quantity_only_phrase(phrase: str) -> bool:
        """Проверяет, что короткая фраза содержит только число и единицу.

        Это контекстная проверка для карточки «Укажите количество». Она не
        меняет обычный выбор кандидата: фразы «первый вариант» и «вариант
        один» не считаются количеством.
        """
        normalized = normalize_text(phrase)
        if not normalized:
            return False
        words = normalized.replace(",", ".").split()
        if words and words[0] in {
            "первый",
            "первая",
            "первое",
            "второй",
            "вторая",
            "третья",
            "вариант",
        }:
            return False
        allowed = set(UNIT_ALIASES) | set(NUMBER_WORDS)
        for word in words:
            if re.fullmatch(r"\d+(?:\.\d+)?", word):
                continue
            if word not in allowed:
                return False
        return any(re.fullmatch(r"\d+(?:\.\d+)?", word) or word in NUMBER_WORDS for word in words)

    def _contextual_cart_pagination_command(
        self,
        command: ParsedCommand,
        event: TelegramEvent,
        state: ConversationState,
    ) -> ParsedCommand:
        """Оставляет голосовую навигацию в черновике, если он разбит на страницы."""
        page_callbacks: set[int] = set()
        for action in state.visible_actions:
            callback_data = action.get("action_id", "")
            match = re.fullmatch(r"v2:cartpage:(\d+)(?::r\d+)?", callback_data)
            if match:
                page_callbacks.add(int(match.group(1)))
        if not page_callbacks:
            return command

        raw = event.text or command.text
        phrase = normalize_command_text(raw)
        navigation_target = self._status_navigation_target(phrase)
        if navigation_target not in {"next", "previous"}:
            return command

        target_page = state.cart_page + (1 if navigation_target == "next" else -1)
        if target_page not in page_callbacks:
            target_page = state.cart_page
        return ParsedCommand(
            intent=Intent.SHOW_CART,
            text=raw,
            callback_target=f"page:{max(0, target_page)}",
        )

    def _contextual_order_status_command(
        self,
        command: ParsedCommand,
        event: TelegramEvent,
        state: ConversationState,
    ) -> ParsedCommand:
        """Связывает короткую фразу с последним показанным списком заявок."""
        if not state.order_status_view_active:
            return command
        raw = event.text or command.text
        phrase = normalize_command_text(raw)
        if not phrase:
            return command
        navigation_target = self._status_navigation_target(phrase)
        if navigation_target:
            if state.order_status_detail_active:
                navigation_target = f"detail_{navigation_target}"
            selected_index = (
                state.order_status_selected_index if state.order_status_detail_active else None
            )
            order_number = (
                state.order_status_selected_order_number if state.order_status_detail_active else ""
            )
            return ParsedCommand(
                intent=Intent.ORDER_STATUS,
                text=raw,
                selected_index=selected_index,
                selection_query=order_number,
                callback_target=navigation_target,
            )
        if command.intent == Intent.ORDER_STATUS and (
            command.selected_index is not None or command.selection_query or command.callback_target
        ):
            return command
        if command.intent == Intent.SELECT_CANDIDATE and command.selected_index is not None:
            return ParsedCommand(
                intent=Intent.ORDER_STATUS,
                text=raw,
                selected_index=command.selected_index,
            )
        selection_phrase = re.sub(
            r"^(?:(?:покаж\w*|открой\w*|выбер\w*|посмотр\w*|давай)\s+)+",
            "",
            phrase,
        )
        selection_phrase = re.sub(
            r"^(?:(?:заявк\w*|заказ\w*)\s+)?(?:номер\s+)?",
            "",
            selection_phrase,
        )
        spoken_index = self._spoken_choice_index(selection_phrase)
        if spoken_index is not None and spoken_index <= len(state.order_status_order_numbers):
            return ParsedCommand(
                intent=Intent.ORDER_STATUS,
                text=raw,
                selected_index=spoken_index,
            )
        if re.fullmatch(
            r"(?:назад(?:\s+к списку)?|к списку|вернись назад|вернись к списку)"
            r"(?:\s+(?:заявок|заказов))?",
            phrase,
        ):
            return ParsedCommand(
                intent=Intent.ORDER_STATUS,
                text=raw,
                callback_target="current",
            )
        return command

    def _contextual_final_review_pagination_command(
        self,
        command: ParsedCommand,
        event: TelegramEvent,
        state: ConversationState,
    ) -> ParsedCommand:
        """Оставляет голосовую навигацию в постраничной финальной проверке."""
        page_callbacks: set[int] = set()
        for action in state.visible_actions:
            callback_data = action.get("action_id", "")
            match = re.fullmatch(r"v2:finalpage:(\d+)(?::r\d+)?", callback_data)
            if match:
                page_callbacks.add(int(match.group(1)))
        if not page_callbacks:
            return command

        raw = event.text or command.text
        phrase = normalize_command_text(raw)
        navigation_target = self._status_navigation_target(phrase)
        if navigation_target not in {"next", "previous"}:
            return command

        target_page = state.final_review_page + (1 if navigation_target == "next" else -1)
        if target_page not in page_callbacks:
            target_page = state.final_review_page
        return ParsedCommand(
            intent=Intent.SHOW_FINAL_REVIEW,
            text=raw,
            callback_target=f"page:{max(0, target_page)}",
        )

    @staticmethod
    def _status_navigation_target(phrase: str) -> str:
        """Распознаёт разговорную навигацию по страницам истории."""
        cleaned = re.sub(
            r"^(?:(?:ну|покаж\w*|открой\w*|перей\w*|верн\w*|листай\w*|"
            r"перелист\w*|пролист\w*|поехал\w*|давай\w*|мне|можешь|можно|пожалуйста)\s+)+",
            "",
            phrase,
        ).strip()
        cleaned = re.sub(r"^(?:на|в|к)\s+", "", cleaned)
        if "список" in cleaned or "списку" in cleaned:
            return ""
        if re.fullmatch(
            r"(?:следующ\w*|дальше|ещ[её]|(?:более\s+)?стар\w*|впер[её]д)"
            r"(?:\s+(?:страниц\w*|заявк\w*|заказ\w*))?",
            cleaned,
        ):
            return "next"
        if re.fullmatch(
            r"(?:предыдущ\w*|новее|назад|более\s+нов\w*)"
            r"(?:\s+(?:страниц\w*|заявк\w*|заказ\w*))?",
            cleaned,
        ):
            return "previous"
        return ""

    @staticmethod
    def _cart_page(command: ParsedCommand, state: ConversationState) -> int:
        """Resolve the requested page of the current draft."""
        target = command.callback_target
        if target.startswith("page:"):
            try:
                return max(0, int(target.partition(":")[2]))
            except ValueError:
                return 0
        return max(0, state.cart_page)

    def _contextual_voice_command(
        self,
        command: ParsedCommand,
        event: TelegramEvent,
        state: ConversationState,
    ) -> ParsedCommand:
        """Обрабатывает голосовую команду с учётом текущего экрана."""
        if event.input_type != InputKind.VOICE:
            return command
        if command.intent == Intent.EDIT_QUANTITY and command.edit_quantity is not None:
            return command

        raw = event.text or command.text
        phrase = normalize_text(raw)
        if not phrase:
            return command
        current = state.current_item()

        # Speech recognition may keep only the adjective from a short command
        # such as «Новая заявка» and return «Новая».  Treat a lone new-order
        # adjective as navigation only for voice input and only without an
        # open issue card; a real product line with a name or quantity remains
        # ordinary product input.
        if (
            current is None
            and command.intent == Intent.ADD_ITEMS
            and len(command.items) == 1
            and command.items[0].quantity is None
            and normalize_text(command.items[0].product_query)
            in {"новая", "новую", "новый", "новое"}
        ):
            return command.model_copy(update={"intent": Intent.START_NEW_ORDER, "items": []})

        if (
            current
            and current.status == ItemStatus.DUPLICATE_PENDING
            and command.intent == Intent.ADD_MORE
            and self._is_duplicate_merge_confirmation(phrase)
        ):
            return command.model_copy(update={"intent": Intent.MERGE_DUPLICATE})

        if state.stage == SessionStage.AWAIT_ADD_MORE_CONFIRM:
            if not has_negated_action(
                phrase,
                "отправ",
                "оформ",
                "запиш",
                "переда",
            ) and self._has_any_prefix(
                phrase,
                "отправ",
                "оформ",
                "запиш",
                "переда",
            ):
                return command.model_copy(update={"intent": Intent.SUBMIT_REQUEST})
            if command.intent == Intent.ADD_MORE or self._is_explicit_yes(phrase):
                return command.model_copy(update={"intent": Intent.ADD_MORE})
            if command.intent in {Intent.BACK, Intent.CANCEL, Intent.SHOW_CART}:
                return command.model_copy(update={"intent": Intent.BACK})
            if self._has_any_prefix(
                phrase, "нет", "хват", "достат", "готов", "больше не", "не надо"
            ):
                return command.model_copy(update={"intent": Intent.BACK})

        # Navigation is global in n8n.  It must win on every card: saying
        # "добавить товары" on a duplicate or unit-warning card opens product
        # collection and must not be mistaken for "merge" or "use catalog
        # unit" merely because both local actions contain the stem "добав".
        if command.intent in {
            Intent.ADD_MORE,
            Intent.BACK,
            Intent.CHECK_MIN_SUM,
            Intent.EDIT_COMMENT,
            Intent.CLARIFY_CURRENT,
            Intent.CLEAR_CART,
            Intent.START_NEW_ORDER,
            Intent.GREETING,
            Intent.HELP,
            Intent.ORDER_STATUS,
            Intent.PRODUCT_ADD_LIST,
            Intent.SHOW_CART,
            Intent.SHOW_FINAL_REVIEW,
            Intent.SMALL_TALK,
            Intent.THANKS,
        }:
            return command

        if current and current.status == ItemStatus.AMBIGUOUS:
            spoken_index = self._spoken_choice_index(phrase)
            if spoken_index is not None:
                return ParsedCommand(
                    intent=Intent.SELECT_CANDIDATE,
                    text=raw,
                    selected_index=spoken_index,
                    callback_target=str(self._item_index(state, current)),
                )
            if self._has_any_prefix(phrase, "не добав", "пропуст", "не нужен", "откаж"):
                return command.model_copy(update={"intent": Intent.SKIP_CURRENT})
            if self._has_any_prefix(phrase, "измен", "другое назв", "название товар"):
                return command.model_copy(
                    update={
                        "intent": Intent.MANUAL_CURRENT,
                        "callback_target": str(self._item_index(state, current)),
                    }
                )

        if current and current.status == ItemStatus.NOT_FOUND:
            if current.rename_attempted and (
                self._is_explicit_yes(phrase) or self._has_any_prefix(phrase, "отправ")
            ):
                return command.model_copy(
                    update={
                        "intent": Intent.PRODUCT_ADD,
                        "callback_target": str(self._item_index(state, current)),
                    }
                )
            if current.rename_attempted and self._has_any_prefix(
                phrase, "нет", "не добав", "отмен"
            ):
                return command.model_copy(
                    update={
                        "intent": Intent.PRODUCT_ADD_SKIP,
                        "callback_target": str(self._item_index(state, current)),
                    }
                )
            if self._has_any_prefix(phrase, "не добав", "пропуст", "не нужен"):
                return command.model_copy(update={"intent": Intent.SKIP_CURRENT})
            if self._has_any_prefix(phrase, "измен", "другое назв", "название товар"):
                return command.model_copy(
                    update={
                        "intent": Intent.MANUAL_CURRENT,
                        "callback_target": str(self._item_index(state, current)),
                    }
                )
            if "всех поставщик" in phrase or "у других поставщик" in phrase:
                return command.model_copy(
                    update={
                        "intent": Intent.SEARCH_ALL_SUPPLIERS,
                        "callback_target": str(self._item_index(state, current)),
                    }
                )
            if "другого поставщик" in phrase:
                return command.model_copy(
                    update={
                        "intent": Intent.SWITCH_SUPPLIER,
                        "callback_target": str(self._item_index(state, current)),
                    }
                )

        if current and current.status == ItemStatus.DUPLICATE_PENDING:
            if self._is_duplicate_skip_confirmation(phrase):
                return command.model_copy(update={"intent": Intent.SKIP_CURRENT})
            if self._is_explicit_yes(phrase) or self._is_duplicate_merge_confirmation(phrase):
                return command.model_copy(update={"intent": Intent.MERGE_DUPLICATE})

        if current and current.status == ItemStatus.UNIT_MISMATCH:
            if command.intent == Intent.SKIP_CURRENT:
                return command
            if not has_negation(phrase) and (
                self._is_explicit_yes(phrase) or self._has_any_prefix(phrase, "остав", "добав")
            ):
                return command.model_copy(update={"intent": Intent.USE_CATALOG_UNIT})
            if self._has_any_prefix(phrase, "нет", "измен", "другое колич"):
                return command.model_copy(update={"intent": Intent.ENTER_OTHER_QUANTITY})

        warnings = multiple_warnings(state)
        if warnings:
            if self._has_any_prefix(phrase, "остав", "как указ", "не исправ"):
                return command.model_copy(update={"intent": Intent.KEEP_MULTIPLE})
            if self._has_any_prefix(phrase, "рекоменд", "ближайш", "округл"):
                return command.model_copy(update={"intent": Intent.ACCEPT_SUGGESTED_QUANTITY})

            current_warning = state.current_item()
            if current_warning in warnings:
                spoken_index = self._spoken_choice_index(phrase)
                if spoken_index is None and command.intent == Intent.SELECT_CANDIDATE:
                    spoken_index = command.selected_index
                if spoken_index == 1:
                    return command.model_copy(update={"intent": Intent.ACCEPT_SUGGESTED_QUANTITY})
                if spoken_index == 2:
                    return command.model_copy(update={"intent": Intent.ENTER_OTHER_QUANTITY})
                if spoken_index == 3:
                    return command.model_copy(update={"intent": Intent.KEEP_CURRENT_QUANTITY})
            if self._spoken_quantity(phrase)[0] is None and self._has_any_prefix(
                phrase,
                "исправ",
                "поправ",
                "измен",
                "поменя",
                "выб",
                "подоб",
            ):
                return command.model_copy(update={"intent": Intent.FIX_MULTIPLE})

        supplier_index = self._spoken_supplier_warning_index(phrase, state)
        if supplier_index is not None:
            return command.model_copy(
                update={"intent": Intent.ADD_SUPPLIER_ITEMS, "callback_target": str(supplier_index)}
            )

        warning_count = self._supplier_warning_count(state)
        if (
            warning_count > 1
            and self._has_any_prefix(phrase, "выбер", "выбрат", "выбор")
            and "поставщик" in phrase
        ):
            return command.model_copy(update={"intent": Intent.CHOOSE_SUPPLIER_WARNING})

        if re.fullmatch(
            r"(?:(?:давай )?(?:добавим|добавить|доберем|добрать)(?: еще)? "
            r"(?:товары|позиции|сумму)(?: (?:этого|у этого|по этому) поставщик\w*)?|"
            r"(?:давай )?(?:добрать|доберем) до (?:минималки|минимальной суммы))",
            phrase,
        ):
            if warning_count == 1:
                return command.model_copy(
                    update={"intent": Intent.ADD_SUPPLIER_ITEMS, "callback_target": "0"}
                )
            if warning_count > 1:
                return command.model_copy(update={"intent": Intent.CHOOSE_SUPPLIER_WARNING})

        requests = [
            request
            for request in state.product_add_requests
            if request.get("description") and request.get("status") != "cancelled"
        ]
        if (
            not has_negation(phrase)
            and "запрос" in phrase
            and any(
                stem in phrase
                for stem in (
                    "снабжен",
                    "покаж",
                    "показ",
                    "посмотр",
                    "откр",
                    "вывед",
                    "спис",
                    "пров",
                    "повтор",
                    "еще раз",
                )
            )
        ):
            if self._has_any_prefix(phrase, "повтор", "еще раз"):
                index = self._spoken_choice_index(phrase) or 1
                retryable = [row for row in requests if row.get("status") == "write_failed"]
                if 0 < index <= len(retryable):
                    return command.model_copy(
                        update={
                            "intent": Intent.PRODUCT_ADD_RETRY,
                            "callback_target": str(retryable[index - 1].get("request_id") or ""),
                        }
                    )
            return command.model_copy(update={"intent": Intent.PRODUCT_ADD_LIST})
        return command

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
        event: TelegramEvent,
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
                    "description_event_key": str(event.update_id),
                    "telegram_user_id": event.telegram_user_id or event.chat_id,
                    "telegram_username": event.telegram_username,
                    "telegram_first_name": event.telegram_first_name,
                    "telegram_last_name": event.telegram_last_name,
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

    def _contextual_negative_command(
        self,
        command: ParsedCommand,
        event: TelegramEvent,
        state: ConversationState,
    ) -> ParsedCommand:
        """Применяет отрицание до любых подтверждающих и изменяющих маршрутов."""
        if event.input_type == InputKind.CALLBACK:
            return command
        raw = event.text or command.text
        phrase = normalize_text(raw)
        if not phrase:
            return command

        current = state.current_item()
        rejects_item = is_explicit_item_rejection(phrase)

        if state.stage == SessionStage.AWAIT_PRODUCT_ADD_DETAILS and (
            rejects_item
            or command.intent == Intent.CANCEL
            or has_negated_action(
                phrase,
                "отправ",
                "созда",
                "оформ",
                "добав",
                "подтверж",
            )
        ):
            return command.model_copy(
                update={
                    "intent": Intent.PRODUCT_ADD_SKIP,
                    "callback_target": str(state.pending_product_add_item_index or 0),
                }
            )

        if state.stage == SessionStage.AWAIT_ADD_MORE_CONFIRM and (
            rejects_item
            or command.intent == Intent.CANCEL
            or has_negated_action(phrase, "добав", "продолж", "внес", "докин")
        ):
            return command.model_copy(update={"intent": Intent.BACK})

        if state.stage == SessionStage.AWAIT_SUBMIT_CONFIRM and (
            command.intent == Intent.CANCEL
            or has_negated_action(phrase, "отправ", "подтверж", "оформ", "переда", "запиш")
            or self._has_any_prefix(phrase, "передум", "отмен")
        ):
            return command.model_copy(update={"intent": Intent.BACK})

        if current is None:
            return command
        if rejects_item:
            return command.model_copy(update={"intent": Intent.SKIP_CURRENT})
        if current.status == ItemStatus.DUPLICATE_PENDING and (
            command.intent == Intent.CANCEL
            or has_negated_action(
                phrase,
                "объедин",
                "добав",
                "суммир",
                "прибав",
                "слож",
                "увелич",
            )
        ):
            return command.model_copy(update={"intent": Intent.SKIP_CURRENT})
        if current.status == ItemStatus.UNIT_MISMATCH and (
            command.intent == Intent.CANCEL
            or has_negated_action(
                phrase,
                "использ",
                "остав",
                "добав",
                "перевед",
                "конверт",
                "подтверж",
            )
        ):
            return command.model_copy(update={"intent": Intent.ENTER_OTHER_QUANTITY})
        if current.status == ItemStatus.AMBIGUOUS and (
            command.intent == Intent.CANCEL
            or has_negated_action(phrase, "выбер", "выбир", "возьм", "бери")
            or (has_negation(phrase) and self._spoken_choice_index(phrase) is not None)
        ):
            return command.model_copy(update={"intent": Intent.CONTINUE_CURRENT})
        if is_multiple_warning(current) and (
            command.intent == Intent.KEEP_CURRENT_QUANTITY
            or has_negated_action(
                phrase,
                "исправ",
                "измен",
                "округл",
                "рекоменд",
                "замен",
            )
        ):
            return command.model_copy(update={"intent": Intent.KEEP_CURRENT_QUANTITY})
        return command

    @staticmethod
    def _has_any_prefix(phrase: str, *stems: str) -> bool:
        """Проверяет наличие одного из допустимых префиксов."""
        return any(re.search(rf"(?:^|\s){re.escape(stem)}", phrase) for stem in stems)

    @staticmethod
    def _is_explicit_yes(phrase: str) -> bool:
        """Отличает подтверждение «да» от разговорного слова «давай»."""
        if has_negation(phrase):
            return False
        words = re.findall(r"[a-zа-яё0-9-]+", phrase, flags=re.I)
        return any(
            word
            in {
                "да",
                "ага",
                "ок",
                "окей",
                "конечно",
                "верно",
                "правильно",
                "согласен",
                "согласна",
                "подтверждаю",
            }
            for word in words
        )

    @staticmethod
    def _is_duplicate_merge_confirmation(phrase: str) -> bool:
        """Распознаёт разговорное подтверждение объединения повторного товара."""
        words = re.findall(r"[a-zа-яё0-9-]+", phrase, flags=re.I)
        has_negation = any(
            word in {"не", "нет", "отмена", "отменить", "пропусти", "пропустить"} for word in words
        )
        has_merge_action = any(
            word.startswith(
                (
                    "добав",
                    "объедин",
                    "суммир",
                    "прибав",
                    "слож",
                    "плюс",
                    "увелич",
                    "вмест",
                )
            )
            for word in words
        )
        has_new_items = any(
            word.startswith(("товар", "позиц", "продукт", "ещ", "нов", "друг")) for word in words
        )
        return has_merge_action and not has_new_items and not has_negation

    @staticmethod
    def _is_duplicate_skip_confirmation(phrase: str) -> bool:
        """Распознаёт отказ от повторного добавления товара."""
        return bool(
            re.search(
                r"(?:^|\s)(?:нет|не\s+(?:надо|добав\w*)|пропуст\w*|"
                r"отмен\w*|остав\w*\s+как\s+есть)",
                phrase,
            )
        )

    @staticmethod
    def _is_generic_show_products_command(text: str) -> bool:
        """Определяет просьбу показать товары без упоминания черновика."""
        words = normalize_text(text).split()
        has_show_action = any(
            word.startswith(("покаж", "показ", "посмотр", "откр", "вывед")) for word in words
        )
        has_product_target = any(
            word.startswith(("товар", "позиц", "продукт", "спис")) for word in words
        )
        has_draft_target = any(
            word.startswith(("черновик", "корзин", "заявк", "заказ")) for word in words
        )
        return has_show_action and has_product_target and not has_draft_target

    @staticmethod
    def _spoken_choice_index(phrase: str) -> int | None:
        """Определяет номер варианта из живой речи."""
        if has_negation(phrase):
            return None
        words = {
            1: r"(?:1|один|перв\w*)",
            2: r"(?:2|два|втор\w*)",
            3: r"(?:3|три|трет\w*)",
            4: r"(?:4|четыр\w*)",
            5: r"(?:5|пят\w*)",
        }
        for index, token in words.items():
            if re.search(
                rf"(?:^|\s)(?:(?:давай|выбираю|беру|нужен|хочу)\s+)?(?:вариант\s+)?{token}(?:\s|$)",
                phrase,
            ):
                return index
        return None

    @staticmethod
    def _spoken_supplier_warning_index(phrase: str, state: ConversationState) -> int | None:
        """Определяет поставщика из голосовой команды."""
        options = [warning.supplier for warning in supplier_minimum_warnings(state)]
        compact_phrase = ConversationEngine._spoken_acronym(phrase)
        for index, supplier in enumerate(options):
            normalized_supplier = normalize_text(supplier)
            compact_supplier = normalized_supplier.replace(" ", "")
            if normalized_supplier and (
                normalized_supplier in phrase or compact_supplier == compact_phrase.replace(" ", "")
            ):
                return index
        return None

    @staticmethod
    def _supplier_warning_count(state: ConversationState) -> int:
        """Считает предупреждения по поставщикам."""
        return len(supplier_minimum_warnings(state))

    @staticmethod
    def _spoken_acronym(value: str) -> str:
        """Нормализует произнесённую аббревиатуру поставщика."""
        letters = {
            "а": "а",
            "бэ": "б",
            "вэ": "в",
            "гэ": "г",
            "дэ": "д",
            "е": "е",
            "ё": "ё",
            "жэ": "ж",
            "зэ": "з",
            "и": "и",
            "ка": "к",
            "эл": "л",
            "эм": "м",
            "эн": "н",
            "о": "о",
            "пэ": "п",
            "эр": "р",
            "эс": "с",
            "тэ": "т",
            "у": "у",
            "эф": "ф",
            "ха": "х",
            "це": "ц",
            "че": "ч",
            "ша": "ш",
            "ща": "щ",
            "ы": "ы",
            "э": "э",
            "ю": "ю",
            "я": "я",
        }
        return "".join(letters.get(word, word) for word in value.split())

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
    def _clear_transient_dialog_state(state: ConversationState) -> None:
        """Очищает временный диалог без удаления черновика."""
        state.current_issue_item_id = ""
        state.current_issue_kind = None
        state.search_scope = SearchScope.SUPPLIER_ONLY
        state.supplier_search_locked = False
        state.supplier_hint_context = ""
        state.manual_item_index = None
        state.unit_item_index = None
        state.edit_multiple_index = None
        state.pending_added_items_count = 0
        clear_pending_comment(state)
        clear_product_add_pending(state)

    @staticmethod
    def _repeat_add_more_prompt(state: ConversationState) -> BotReply:
        """Повторяет существующий вопрос о добавлении товаров без мутации state."""
        fallback = added_items_question_reply(state, 1)
        return BotReply(
            text=state.ui_message_text or fallback.text,
            rows=fallback.rows,
        )

    @staticmethod
    def _item_index(state: ConversationState, item: CartItem) -> int:
        """Возвращает номер позиции по её идентификатору."""
        return state_item_index(state, item)

    @staticmethod
    def _catalog_packaging_measurement(
        item: CartItem,
        candidates: list[Candidate],
    ) -> tuple[float, str] | None:
        """Совместимо вызывает владельца разрешения каталога."""
        return CatalogResolutionService._catalog_packaging_measurement(item, candidates)

    def _match_item(
        self,
        item: CartItem,
        catalog: list[CatalogProduct],
        search_scope: SearchScope | None = None,
    ) -> None:
        """Совместимо вызывает сопоставление позиции в новом владельце."""
        self.catalog_resolution.match_item(item, catalog, search_scope)

    @staticmethod
    def _looks_like_supplier_comment_fragment(words: list[str]) -> bool:
        """Отличает инструкцию поставщику от неизвестной части названия товара."""
        return ConversationEngine._supplier_comment_start(words) is not None

    @staticmethod
    def _remove_unanchored_supplier_hint(item: CartItem) -> None:
        """Совместимо вызывает очистку подсказки поставщика."""
        CatalogResolutionService._remove_unanchored_supplier_hint(item)

    def _sanitize_catalog_facts_before_resolution(
        self,
        item: CartItem,
        candidate: Candidate | None,
    ) -> None:
        """Совместимо вызывает очистку фактов каталога перед решением."""
        self.catalog_resolution._sanitize_catalog_facts_before_resolution(item, candidate)

    @staticmethod
    def _supplier_comment_start(words: list[str]) -> int | None:
        """Находит начало явного комментария внутри неизвестного фрагмента."""
        return supplier_comment_start(words)

    def _apply_catalog(
        self,
        item: CartItem,
        candidate: Candidate,
        catalog: list[CatalogProduct],
    ) -> None:
        """Совместимо вызывает применение результата каталога к позиции."""
        self.catalog_resolution.apply_catalog(item, candidate, catalog)

    def _refresh_cart_order_values(
        self,
        state: ConversationState,
        catalog: list[CatalogProduct],
    ) -> None:
        """Совместимо вызывает обновление каталожных значений черновика."""
        self.catalog_resolution.refresh_cart_order_values(state, catalog)

    @staticmethod
    def _reconcile_quantity_with_catalog_name(item: CartItem, product_name: str) -> None:
        """Совместимо вызывает отделение заказа от фасовки каталога."""
        CatalogResolutionService._reconcile_quantity_with_catalog_name(item, product_name)

    def _first_unresolved(self, state: ConversationState) -> CartItem | None:
        """Возвращает приоритетную нерешённую позицию."""
        return first_unresolved_item(state)

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
        if progression.kind is ProgressionKind.ISSUE:
            assert progression.item is not None
            return EngineResult(
                state=state,
                reply=issue_reply(progression.item, self._item_index(state, progression.item)),
            )
        if progression.kind is ProgressionKind.ADD_MORE_CONFIRM:
            return EngineResult(
                state=state,
                reply=added_items_question_reply(state, progression.prompt_count),
            )
        title = (
            f"Добавлено позиций: {progression.added_count}"
            if progression.added_count
            else "Черновик заявки"
        )
        return EngineResult(state=state, reply=cart_reply(state, title=title))

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
        self._apply_catalog(item, outcome.candidate, catalog)
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
                            callback_data=f"v2:skip:{self._item_index(state, item)}",
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
        item = state.current_item()
        if item:
            item.status = ItemStatus.SKIPPED
            if state.pending_added_items_count:
                state.pending_added_items_count -= 1
        state.current_issue_item_id = ""
        return self._advance(state)

    def _confirm_current(self, state: ConversationState) -> EngineResult:
        """Подтверждает текущую позицию черновика."""
        item = state.current_item()
        if item and item.status == ItemStatus.DUPLICATE_PENDING:
            existing = next((row for row in state.cart if row.id == item.issue_message), None)
            if (
                existing
                and item.catalog_unit
                and item.unit
                and normalize_unit(item.unit) != normalize_unit(item.catalog_unit)
            ):
                return self._advance(state)
            if existing:
                existing.quantity = (existing.quantity or 0) + (item.quantity or 0)
                item.status = ItemStatus.SKIPPED
            state.current_issue_item_id = ""
        return self._advance(state)

    def _remove_item(self, command: ParsedCommand, state: ConversationState) -> EngineResult:
        """Удаляет выбранную позицию из черновика."""
        target_query = clean_command_target(command.target_query)
        target = normalize_text(target_query)
        if not target and state.current_item():
            state.current_item().status = ItemStatus.SKIPPED  # type: ignore[union-attr]
            prune_pending_comment_item_ids(state)
            return self._advance(state)
        item = find_cart_item(state, target_query)
        if item is not None:
            was_current = item.id == state.current_issue_item_id
            item.status = ItemStatus.SKIPPED
            if was_current:
                state.current_issue_item_id = ""
                state.current_issue_kind = None
            prune_pending_comment_item_ids(state)
            return EngineResult(state=state, reply=cart_reply(state, title="Позиция удалена"))
        pending_matches = sorted(
            [
                (
                    contains_score(target, normalize_text(pending.product_query)),
                    pending,
                )
                for pending in state.pending_comment_items
            ],
            key=lambda pair: pair[0],
        )
        pending_item = None
        if pending_matches:
            best_score = pending_matches[-1][0]
            if best_score > 0 and sum(score == best_score for score, _ in pending_matches) == 1:
                pending_item = pending_matches[-1][1]
        if pending_item is not None:
            state.pending_comment_items.remove(pending_item)
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
                for row in state.cart:
                    if row.status != ItemStatus.SKIPPED:
                        row.comment = ""
                        row.comment_source = CommentSource.NONE
                return EngineResult(
                    state=state,
                    reply=cart_reply(state, notice="Общий комментарий удалён"),
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
            item.comment = ""
            item.comment_source = CommentSource.NONE
            notice = "Комментарий удалён"
        else:
            item.comment = merge_comments(item.comment, comment)
            item.comment_source = CommentSource.SEMANTIC
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

    def _prepare_submission(self, event: TelegramEvent, state: ConversationState) -> EngineResult:
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
        order_no = f"{now:%Y%m%d-%H%M%S}-{event.chat_id[-4:]}"
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
            telegram_user_id=event.telegram_user_id or event.chat_id,
            telegram_chat_id=event.chat_id,
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
        event: TelegramEvent,
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
