from __future__ import annotations

import re
from collections import defaultdict
from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

from restaurant_bot.config import Settings
from restaurant_bot.domain.models import (
    BotReply,
    Button,
    Candidate,
    CartItem,
    CatalogProduct,
    ConversationState,
    EngineResult,
    ExtractedItem,
    InputKind,
    Intent,
    IssueKind,
    ItemStatus,
    ParsedCommand,
    PendingSubmission,
    SearchScope,
    SessionStage,
    TelegramEvent,
)
from restaurant_bot.services.matching import (
    can_auto_select,
    has_complete_query_evidence,
    is_broad_category_query,
    nearest_valid_multiple,
    query_evidence_tokens,
    rank_candidates,
    supplier_matches_hint,
    tokens,
)
from restaurant_bot.services.parser import (
    has_negated_action,
    has_negation,
    infer_intent,
    is_explicit_item_rejection,
    is_product_add_request_phrase,
    parse_product_lines,
    parse_quantity_unit,
)
from restaurant_bot.services.product_add_flow import (
    clear_product_add_pending,
    new_product_add_request_id,
    product_add_prompt,
)
from restaurant_bot.services.replies import (
    added_items_question_reply,
    cart_reply,
    empty_draft_reply,
    final_review_reply,
    help_reply,
    issue_reply,
    multiple_quantity_choice_reply,
    new_order_confirmation_reply,
    new_order_started_reply,
    no_current_manual_reply,
    photo_without_quantities_reply,
    product_add_requests_reply,
    product_add_sending_reply,
    small_talk_reply,
    start_adding_supplier_reply,
    submission_retry_reply,
    supplier_warning_choose_reply,
    supplier_warning_details_reply,
    thanks_reply,
    unknown_intent_reply,
    unrecognized_voice_reply,
    welcome_reply,
)
from restaurant_bot.services.submission_presenter import submission_dispatch_uncertain_reply
from restaurant_bot.services.text import (
    UNIT_ALIASES,
    convert_quantity,
    escape,
    normalize_text,
    normalize_unit,
    parse_number_words,
)


class ConversationEngine:
    """Применяет бизнес-правила к состоянию диалога."""

    def __init__(self, settings: Settings):
        """Инициализирует компонент."""
        self.settings = settings

    def handle(
        self,
        event: TelegramEvent,
        command: ParsedCommand,
        state: ConversationState,
        catalog: list[CatalogProduct],
    ) -> EngineResult:
        """Обрабатывает входные данные текущего компонента."""
        state.last_input_text = event.text or command.text
        self._remove_navigation_command_items(state)
        self._remove_cart_comment_shadows(state)
        self._remove_exact_cart_duplicates(state)

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
        command = self._contextual_negative_command(command, event, state)
        command = self._contextual_quantity_command(command, event.text, state)
        command = self._contextual_voice_command(command, event, state)
        if command.intent in {
            Intent.SUBMIT_REQUEST,
            Intent.SHOW_FINAL_REVIEW,
            Intent.CHECK_MIN_SUM,
        }:
            self._refresh_cart_order_values(state, catalog)
        if (
            state.stage == SessionStage.AWAIT_ADD_MORE_CONFIRM
            and command.intent == Intent.ADD_ITEMS
        ):
            # A product sent directly is an implicit confirmation to continue.
            # Any following issue card must use the normal collecting context.
            state.stage = SessionStage.COLLECTING
            state.status = "collecting"

        if state.stage == SessionStage.AWAIT_ADD_MORE_CONFIRM and command.intent in {
            Intent.BACK,
            Intent.CANCEL,
            Intent.SHOW_CART,
        }:
            state.pending_added_items_count = 0
            state.stage = SessionStage.REVIEW
            state.status = "review"
            return EngineResult(state=state, reply=cart_reply(state))

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
        if (
            current
            and current.status == ItemStatus.AMBIGUOUS
            and command.intent in {Intent.UNKNOWN, Intent.ADD_ITEMS, Intent.MANUAL_CURRENT}
        ):
            selection_query = normalize_text(event.text or command.text)
            if selection_query:
                scores = [
                    self._contains_score(selection_query, normalize_text(candidate.name))
                    for candidate in current.candidates
                ]
                best_score = max(scores, default=0)
                if best_score >= 2 and scores.count(best_score) == 1:
                    command = ParsedCommand(
                        intent=Intent.SELECT_CANDIDATE,
                        text=event.text or command.text,
                        selection_query=event.text or command.text,
                        callback_target=str(self._item_index(state, current)),
                    )
                elif event.input_type == InputKind.VOICE:
                    # On an open candidate card every vague voice utterance
                    # is a possible choice, never a new product.  Keeping the
                    # same card is safer than polluting the draft with a bad
                    # transcription such as "Был вариант".
                    command = ParsedCommand(
                        intent=Intent.CONTINUE_CURRENT, text=event.text or command.text
                    )

        # n8n appends the current UI revision to callback_data. A stale button
        # may still be delivered by Telegram after a newer card was rendered;
        # it must only reopen the current view and never mutate the cart.
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

        # A delivery retry of product-add details must not be interpreted as a
        # fresh product line after the first attempt has already created the
        # stable request. The orchestration inbox also deduplicates updates,
        # but this preserves the n8n contract at the state-machine boundary.
        if any(
            str(request.get("description_event_key") or "") == str(event.update_id)
            for request in state.product_add_requests
        ):
            return EngineResult(state=state, reply=cart_reply(state))

        if state.pending_new_order_confirmation:
            phrase = normalize_text(event.text or command.text)
            if command.intent in {
                Intent.CLEAR_CART,
                Intent.START_NEW_ORDER,
                Intent.CONFIRM,
            } or self._is_explicit_yes(phrase):
                return self._start_new_order(state)
            if (
                command.intent in {Intent.BACK, Intent.CANCEL, Intent.SHOW_CART}
                or has_negation(phrase)
                or self._has_any_prefix(phrase, "нет", "остав", "сохран", "передум")
            ):
                state.pending_new_order_confirmation = False
                return EngineResult(state=state, reply=cart_reply(state))
            return EngineResult(state=state, reply=new_order_confirmation_reply(state))

        # n8n's missing_qty branch treats a short answer like "10" or
        # "10 штук" as the quantity of the currently opened item.
        current_item = state.current_item()
        if (
            event.input_type != InputKind.CALLBACK
            and current_item
            and current_item.status
            in {ItemStatus.MISSING_QTY, ItemStatus.UNIT_MISMATCH, ItemStatus.DUPLICATE_PENDING}
            and state.stage
            in {
                SessionStage.COLLECTING,
                SessionStage.AWAIT_MULTIPLE_QUANTITY,
                SessionStage.AWAIT_UNIT_QUANTITY,
            }
            and (event.text.strip() or command.text.strip())
        ):
            quantity, unit = self._spoken_quantity(event.text or command.text)
            if quantity is not None:
                item = state.current_item()
                assert item is not None
                item.quantity = quantity
                item.unit = unit or item.catalog_unit or item.unit
                if item.status == ItemStatus.DUPLICATE_PENDING:
                    return self._confirm_current(state)
                if item.catalog_unit and item.unit and item.unit != item.catalog_unit:
                    converted = convert_quantity(item.quantity, item.unit, item.catalog_unit)
                    if converted is not None:
                        item.quantity = converted
                        item.unit = item.catalog_unit
                    else:
                        item.status = ItemStatus.UNIT_MISMATCH
                        return self._advance(state)
                item.status = (
                    ItemStatus.MATCHED
                    if item.catalog_product_id or item.catalog_name or item.catalog_unit
                    else item.status
                )
                return self._advance(state)

        if command.intent == Intent.GREETING:
            return EngineResult(state=state, reply=welcome_reply(state))
        if command.intent == Intent.HELP:
            return EngineResult(state=state, reply=help_reply(state))
        if command.intent == Intent.THANKS:
            return EngineResult(state=state, reply=thanks_reply(state))
        if command.intent == Intent.SMALL_TALK:
            return EngineResult(state=state, reply=small_talk_reply(state))
        if command.intent == Intent.ORDER_STATUS:
            return EngineResult(
                state=state,
                reply=BotReply(
                    text=(
                        "Обновляю статус заявки..."
                        if event.input_type == InputKind.CALLBACK
                        else "Проверяю статусы ваших заявок..."
                    )
                ),
                enqueue_order_status=True,
            )
        if command.intent == Intent.SHOW_CART:
            if (
                state.stage == SessionStage.AWAIT_SUBMIT_CONFIRM
                and self._is_generic_show_products_command(command.text)
            ):
                return EngineResult(state=state, reply=supplier_warning_details_reply(state))
            return EngineResult(state=state, reply=cart_reply(state))
        if command.intent == Intent.START_NEW_ORDER:
            if state.stage == SessionStage.SUBMITTED or not self._has_active_draft_items(state):
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
            return EngineResult(state=state, reply=start_adding_supplier_reply(state, index))

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
            self._select_multiple_warning(state)
            return self._accept_suggested_quantity(state)
        if command.intent in {Intent.KEEP_CURRENT_QUANTITY, Intent.KEEP_MULTIPLE}:
            self._select_multiple_warning(state)
            return self._keep_current_quantity(state)
        if command.intent in {Intent.ENTER_OTHER_QUANTITY, Intent.EDIT_MULTIPLE}:
            self._select_multiple_warning(state)
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
        if command.intent in {
            Intent.SUBMIT_REQUEST,
            Intent.SHOW_FINAL_REVIEW,
            Intent.CHECK_MIN_SUM,
        }:
            unresolved = self._first_unresolved(state)
            if unresolved:
                state.current_issue_item_id = unresolved.id
                return EngineResult(
                    state=state, reply=issue_reply(unresolved, self._item_index(state, unresolved))
                )
            if not any(item.status == ItemStatus.MATCHED for item in state.cart):
                return EngineResult(state=state, reply=empty_draft_reply())
            state.current_issue_item_id = ""
            state.current_issue_kind = None
            state.stage = SessionStage.AWAIT_SUBMIT_CONFIRM
            return EngineResult(state=state, reply=final_review_reply(state))
        if command.intent == Intent.SUBMIT_AS_IS:
            unresolved = self._first_unresolved(state)
            if unresolved:
                state.current_issue_item_id = unresolved.id
                return EngineResult(
                    state=state, reply=issue_reply(unresolved, self._item_index(state, unresolved))
                )
            has_multiple_warning = any(
                item.status == ItemStatus.MATCHED
                and item.suggested_quantity is not None
                and item.suggested_quantity != item.quantity
                for item in state.cart
            )
            if has_multiple_warning:
                state.stage = SessionStage.AWAIT_SUBMIT_CONFIRM
                return EngineResult(state=state, reply=final_review_reply(state))
            return self._prepare_submission(event, state)
        if command.intent == Intent.CANCEL:
            self._clear_transient_dialog_state(state)
            state.stage = SessionStage.COLLECTING
            return EngineResult(state=state, reply=cart_reply(state, title="Отправка отменена"))

        if state.stage == SessionStage.AWAIT_PRODUCT_ADD_DETAILS:
            description = (command.text or event.text).strip()
            if description:
                request_id = state.pending_product_add_request_id or new_product_add_request_id(
                    None
                )
                existing = next(
                    (
                        row
                        for row in state.product_add_requests
                        if row.get("request_id") == request_id
                    ),
                    None,
                )
                if existing is None:
                    now = datetime.now(UTC).isoformat()
                    index = state.pending_product_add_item_index
                    item = (
                        state.cart[index] if index is not None and index < len(state.cart) else None
                    )
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
                # n8n removes the source unresolved line from the purchase
                # draft once its separate procurement request is created.
                # Keeping it as NOT_FOUND made one product appear twice:
                # in "Запросы снабженцу" and again in "Нужно уточнить".
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

        if command.intent == Intent.ADD_ITEMS:
            if command.global_comment:
                self._apply_global_comment(state, command.global_comment)
            if command.global_comment and not command.items:
                active_count = sum(item.status != ItemStatus.SKIPPED for item in state.cart)
                return EngineResult(
                    state=state,
                    reply=BotReply(
                        text=(
                            "✅ <b>Общий комментарий добавлен</b>\n\n"
                            f"{escape(command.global_comment)}\n\n"
                            f"Применён ко всем товарам: {active_count}."
                        )
                    ),
                )
            if event.input_type == InputKind.VOICE and not command.items:
                return EngineResult(state=state, reply=unrecognized_voice_reply(state))
            if event.input_type == InputKind.PHOTO and not command.items:
                return EngineResult(state=state, reply=photo_without_quantities_reply(state))
            if state.stage == SessionStage.AWAIT_PRODUCT_ADD_DETAILS:
                description = command.text.strip() or ", ".join(
                    item.source_line or item.product_query for item in command.items
                )
                if description:
                    request_id = state.pending_product_add_request_id or new_product_add_request_id(
                        None
                    )
                    index = state.pending_product_add_item_index
                    item = (
                        state.cart[index] if index is not None and index < len(state.cart) else None
                    )
                    existing = next(
                        (
                            row
                            for row in state.product_add_requests
                            if row.get("request_id") == request_id
                        ),
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
            if state.stage == SessionStage.AWAIT_MANUAL_DETAILS and state.current_issue_item_id:
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
            for extracted in command.items:
                if selected_supplier and not extracted.supplier_hint:
                    extracted = extracted.model_copy(update={"supplier_hint": selected_supplier})
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
                        same_missing.suggested_quantity = self._suggested_quantity_for_multiple(
                            same_missing
                        )
                        same_missing.status = ItemStatus.MATCHED
                    state.current_issue_item_id = same_missing.id
                    continue
                duplicate = self._find_duplicate(state, item)
                if duplicate and item.status in {ItemStatus.MATCHED, ItemStatus.MISSING_QTY}:
                    item.status = ItemStatus.DUPLICATE_PENDING
                    item.issue_message = duplicate.id
                    item.duplicate_existing_quantity = duplicate.quantity or 0
                    item.duplicate_existing_unit = duplicate.unit or duplicate.catalog_unit
                state.cart.append(item)
            # `v2:minsumadd` scopes exactly the next incoming product message
            # in n8n. Retaining this context would incorrectly lock every
            # later product to the same supplier.
            if selected_supplier:
                state.supplier_hint_context = ""
            return self._advance(state, added_count=len(command.items))

        if event.input_type == InputKind.VOICE:
            return EngineResult(state=state, reply=unrecognized_voice_reply(state))
        return EngineResult(state=state, reply=unknown_intent_reply(state))

    @staticmethod
    def _remove_navigation_command_items(state: ConversationState) -> None:
        """Удаляет ранее ошибочно добавленные голосовые команды из черновика."""
        navigation_intents = {
            Intent.ADD_MORE,
            Intent.BACK,
            Intent.CHECK_MIN_SUM,
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
        item_comment = self._remove_global_comment_overlap(item_comment, global_comment)
        return CartItem(
            id=uuid4().hex[:12],
            source_query=extracted.product_query,
            source_line=extracted.source_line,
            quantity_source=extracted.quantity_source,
            order_entry_type=extracted.order_entry_type,
            quantity=extracted.quantity,
            unit=normalize_unit(extracted.unit),
            department=extracted.department or self.settings.default_department,
            supplier_hint=extracted.supplier_hint,
            comment=self._merge_comments(item_comment, global_comment),
        )

    @staticmethod
    def _spoken_quantity(text: str) -> tuple[float | None, str]:
        """Извлекает явно произнесённое количество."""
        quantity, unit = parse_quantity_unit(text)
        if quantity is not None:
            return quantity, unit
        words = [
            cleaned
            for word in normalize_text(text).replace(",", ".").split()
            if (cleaned := re.sub(r"\.+$", "", word))
        ]
        for index in range(len(words)):
            parsed = parse_number_words(words, index)
            if parsed is None:
                continue
            quantity, end = parsed
            unit = (
                normalize_unit(words[end])
                if end < len(words) and words[end] in UNIT_ALIASES
                else ""
            )
            return quantity, unit
        for item in parse_product_lines(text):
            if item.quantity is not None:
                return item.quantity, item.unit
        return None, ""

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

        has_multiple_warning = (
            current.status == ItemStatus.MATCHED
            and current.suggested_quantity is not None
            and current.suggested_quantity != current.quantity
        )
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

        if current.status == ItemStatus.MISSING_QTY and quantity is not None:
            return command.model_copy(
                update={
                    "intent": Intent.EDIT_QUANTITY,
                    "edit_quantity": quantity,
                    "edit_unit": unit,
                    "target_query": "",
                }
            )
        return command

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

        if (
            current
            and current.status == ItemStatus.DUPLICATE_PENDING
            and command.intent == Intent.ADD_MORE
            and self._is_duplicate_merge_confirmation(phrase)
        ):
            return command.model_copy(update={"intent": Intent.MERGE_DUPLICATE})

        if state.stage == SessionStage.AWAIT_ADD_MORE_CONFIRM:
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

        warnings = [
            item
            for item in state.cart
            if item.status == ItemStatus.MATCHED
            and item.suggested_quantity is not None
            and item.suggested_quantity != item.quantity
        ]
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

        if state.stage == SessionStage.AWAIT_SUBMIT_CONFIRM:
            if self._is_explicit_yes(phrase) or self._has_any_prefix(phrase, "подтверж", "отправ"):
                return command.model_copy(update={"intent": Intent.SUBMIT_AS_IS})
            if self._has_any_prefix(phrase, "нет", "отмен", "назад", "черновик"):
                return command.model_copy(update={"intent": Intent.BACK})

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
    def _has_active_draft_items(state: ConversationState) -> bool:
        """Проверяет наличие позиций, которые пользователь ещё может потерять."""
        return any(item.status != ItemStatus.SKIPPED for item in state.cart)

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
        if (
            current.status == ItemStatus.MATCHED
            and current.suggested_quantity is not None
            and current.suggested_quantity != current.quantity
            and (
                command.intent == Intent.KEEP_CURRENT_QUANTITY
                or has_negated_action(
                    phrase,
                    "исправ",
                    "измен",
                    "округл",
                    "рекоменд",
                    "замен",
                )
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
        groups: dict[str, tuple[float, float, float]] = {}
        for item in state.cart:
            if item.status != ItemStatus.MATCHED or not item.supplier:
                continue
            current, added, minimum = groups.get(item.supplier, (0.0, 0.0, 0.0))
            groups[item.supplier] = (
                max(current, float(item.supplier_current_sum or 0)),
                added + item.amount,
                max(minimum, float(item.supplier_minimum_amount or 0)),
            )
        options = [
            supplier
            for supplier, (current, added, minimum) in groups.items()
            if minimum > 0 and current + added < minimum
        ]
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
        groups: dict[str, tuple[float, float, float]] = {}
        for item in state.cart:
            if item.status != ItemStatus.MATCHED or not item.supplier:
                continue
            current, added, minimum = groups.get(item.supplier, (0.0, 0.0, 0.0))
            groups[item.supplier] = (
                max(current, float(item.supplier_current_sum or 0)),
                added + item.amount,
                max(minimum, float(item.supplier_minimum_amount or 0)),
            )
        return sum(
            1
            for current, added, minimum in groups.values()
            if minimum > 0 and current + added < minimum
        )

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
        clear_product_add_pending(state)

    @staticmethod
    def _item_index(state: ConversationState, item: CartItem) -> int:
        """Возвращает номер позиции по её идентификатору."""
        return next(
            (index for index, candidate in enumerate(state.cart) if candidate.id == item.id), 0
        )

    def _match_item(
        self,
        item: CartItem,
        catalog: list[CatalogProduct],
        search_scope: SearchScope | None = None,
    ) -> None:
        """Сопоставляет позицию с товаром каталога."""
        search_catalog = catalog
        outside_supplier_candidates: list[Candidate] = []
        if item.supplier_hint:
            scoped = [
                product
                for product in catalog
                if supplier_matches_hint(product.supplier, item.supplier_hint)
            ]
            if search_scope != SearchScope.ANY_SUPPLIER:
                # A selected supplier is a strict filter.  If that supplier
                # has no catalog rows, we still retain matches from the full
                # catalog only as safe suggestions, never as an auto-match.
                search_catalog = scoped
        supplier_hint = item.supplier_hint if search_scope != SearchScope.ANY_SUPPLIER else ""
        candidates = rank_candidates(item.source_query, search_catalog, supplier_hint)
        if item.supplier_hint and search_scope != SearchScope.ANY_SUPPLIER:
            candidates = [
                candidate
                for candidate in candidates
                if has_complete_query_evidence(item.source_query, candidate.name)
            ]
        item.candidates = candidates
        if not candidates:
            if item.supplier_hint and search_scope != SearchScope.ANY_SUPPLIER:
                outside_supplier_candidates = rank_candidates(item.source_query, catalog)
                outside_supplier_candidates = [
                    candidate
                    for candidate in outside_supplier_candidates
                    if has_complete_query_evidence(item.source_query, candidate.name)
                ]
                item.candidates = outside_supplier_candidates
                item.supplier_search_locked = True
            item.status = ItemStatus.NOT_FOUND
            return
        if not item.comment and len(candidates) >= 2:
            query_words = re.findall(r"[a-zа-я0-9]+", normalize_text(item.source_query), flags=re.I)
            query_tokens = tokens(item.source_query)
            first_evidence = query_evidence_tokens(item.source_query, candidates[0].name)
            second_evidence = query_evidence_tokens(item.source_query, candidates[1].name)
            shared_category_evidence = first_evidence == second_evidence
            uniquely_supported_variant = len(first_evidence) >= 2 and len(first_evidence) > len(
                second_evidence
            )
            if (
                first_evidence
                and (shared_category_evidence or uniquely_supported_variant)
                and len(first_evidence) < len(query_tokens)
                and not any(
                    has_complete_query_evidence(item.source_query, candidate.name)
                    for candidate in candidates
                )
            ):
                comment_words = [word for word in query_words if word not in first_evidence]
                product_words = [word for word in query_words if word in first_evidence]
                item.comment = self._merge_comments(item.comment, " ".join(comment_words))
                item.source_query = " ".join(product_words)
                candidates = rank_candidates(item.source_query, search_catalog, supplier_hint)
                item.candidates = candidates
        item.supplier_search_locked = False
        broad_query = is_broad_category_query(item.source_query, candidates)
        if broad_query or not can_auto_select(candidates):
            item.status = ItemStatus.AMBIGUOUS
            return
        self._apply_catalog(item, candidates[0], catalog)

    def _apply_catalog(
        self,
        item: CartItem,
        candidate: Candidate,
        catalog: list[CatalogProduct],
    ) -> None:
        """Применяет каталог к распознанным позициям."""
        product = next((p for p in catalog if p.product_id == candidate.product_id), None)
        if product is None:
            item.status = ItemStatus.NOT_FOUND
            return
        item.catalog_product_id = product.product_id
        item.catalog_name = product.name
        item.supplier = product.supplier
        item.catalog_unit = normalize_unit(product.unit)
        item.price = product.price
        item.minimum_multiple = product.minimum_multiple
        item.useful_volume = product.useful_volume
        item.supplier_minimum_amount = product.supplier_minimum_amount
        item.supplier_current_sum = product.supplier_current_sum
        item.existing_quantity = product.department_quantities.for_department(item.department) or 0
        self._reconcile_quantity_with_catalog_name(item, product.name)
        user_comment = item.comment or self._comment_left_after_catalog_match(
            item.source_query, product.name
        )
        item.comment = self._merge_comments(product.comment, user_comment)

        if item.quantity is None:
            item.status = ItemStatus.MISSING_QTY
            return
        if item.unit and item.catalog_unit and item.unit != item.catalog_unit:
            converted = convert_quantity(item.quantity, item.unit, item.catalog_unit)
            if converted is None:
                item.status = ItemStatus.UNIT_MISMATCH
                return
            item.quantity = converted
            item.unit = item.catalog_unit
        elif not item.unit:
            item.unit = item.catalog_unit

        suggested = self._suggested_quantity_for_multiple(item)
        if suggested:
            item.suggested_quantity = suggested
        else:
            item.suggested_quantity = None
        item.status = ItemStatus.MATCHED

    def _refresh_cart_order_values(
        self,
        state: ConversationState,
        catalog: list[CatalogProduct],
    ) -> None:
        """Обновляет сумму и количество из строки каждого выбранного товара."""
        products_by_id = {product.product_id: product for product in catalog}
        for item in state.cart:
            if item.status != ItemStatus.MATCHED or not item.catalog_product_id:
                continue
            product = products_by_id.get(item.catalog_product_id)
            if product is None:
                continue
            item.supplier_current_sum = product.supplier_current_sum
            item.existing_quantity = (
                product.department_quantities.for_department(item.department) or 0
            )
            item.supplier_minimum_amount = product.supplier_minimum_amount
            item.minimum_multiple = product.minimum_multiple
            item.suggested_quantity = self._suggested_quantity_for_multiple(item)

    @staticmethod
    def _reconcile_quantity_with_catalog_name(item: CartItem, product_name: str) -> None:
        """Отделяет количество заказа от фасовки в полном названии."""
        if item.quantity_source:
            return
        source_tokens = re.findall(r"[a-zа-я0-9%]+", normalize_text(item.source_line), flags=re.I)
        product_tokens = re.findall(r"[a-zа-я0-9%]+", normalize_text(product_name), flags=re.I)
        if not source_tokens or not product_tokens or len(source_tokens) < len(product_tokens):
            return

        product_start = next(
            (
                index
                for index in range(len(source_tokens) - len(product_tokens) + 1)
                if source_tokens[index : index + len(product_tokens)] == product_tokens
            ),
            None,
        )
        if product_start is None:
            return

        before = " ".join(source_tokens[:product_start])
        after = " ".join(source_tokens[product_start + len(product_tokens) :])
        explicit_quantity: tuple[float | None, str] = (None, "")
        for outside_name in (after, before):
            quantity, unit = parse_quantity_unit(outside_name)
            if quantity is not None:
                explicit_quantity = (quantity, unit)
                break

        item.source_query = product_name
        item.quantity, item.unit = explicit_quantity
        if item.comment and normalize_text(item.comment) in normalize_text(product_name):
            item.comment = ""

    @staticmethod
    def _suggested_quantity_for_multiple(item: CartItem) -> float | None:
        """Рассчитывает количество с учётом кратности."""
        if item.quantity is None:
            return None
        valid_total = nearest_valid_multiple(
            item.existing_quantity + item.quantity, item.minimum_multiple
        )
        if valid_total is None:
            return None
        return round(valid_total - item.existing_quantity, 6)

    @staticmethod
    def _merge_comments(*values: str) -> str:
        """Объединяет комментарии каталога и пользователя."""
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            for part in str(value or "").split(";"):
                comment = " ".join(part.split()).strip(" .,;")
                key = comment.casefold()
                if comment and key not in seen:
                    result.append(comment)
                seen.add(key)
        return "; ".join(result)

    @staticmethod
    def _remove_global_comment_overlap(item_comment: str, global_comment: str) -> str:
        """Удаляет общую часть, которую ИИ также включил в комментарий позиции."""
        item_text = " ".join(str(item_comment or "").split()).strip(" .,;")
        global_text = " ".join(str(global_comment or "").split()).strip(" .,;")
        if not item_text or not global_text:
            return item_text
        match = re.search(re.escape(global_text), item_text, flags=re.I)
        if match is None:
            return item_text
        remaining = f"{item_text[: match.start()]} {item_text[match.end() :]}"
        remaining = re.sub(
            r"(?:\b(?:и|а)\s+)?(?:все|всё|всем|для всех)\s*(?=$|[,;])",
            " ",
            remaining,
            flags=re.I,
        )
        remaining = re.sub(r"\s+", " ", remaining)
        return remaining.strip(" .,;:-—–")

    @staticmethod
    def _apply_global_comment(state: ConversationState, global_comment: str) -> None:
        """Добавляет общий комментарий один раз ко всем активным товарам заявки."""
        for item in state.cart:
            if item.status != ItemStatus.SKIPPED:
                item.comment = ConversationEngine._merge_comments(
                    item.comment,
                    global_comment,
                )

    @staticmethod
    def _remove_cart_comment_shadows(state: ConversationState) -> None:
        """Удаляет ложные позиции, созданные из комментариев."""
        shadow_ids: set[str] = set()
        for owner in state.cart:
            comments = {
                normalize_text(part).strip(" .,;")
                for part in owner.comment.split(";")
                if normalize_text(part).strip(" .,;")
            }
            if not comments:
                continue
            for shadow in state.cart:
                if shadow.id == owner.id or shadow.catalog_product_id:
                    continue
                if normalize_text(shadow.source_query).strip(" .,;") not in comments:
                    continue
                if owner.quantity is None and shadow.quantity is not None:
                    owner.quantity = shadow.quantity
                    owner.unit = shadow.unit or owner.unit
                shadow_ids.add(shadow.id)
        if not shadow_ids:
            return
        state.cart = [item for item in state.cart if item.id not in shadow_ids]
        if state.current_issue_item_id in shadow_ids:
            state.current_issue_item_id = ""

    @staticmethod
    def _remove_exact_cart_duplicates(state: ConversationState) -> None:
        """Объединяет подтверждённые дубликаты позиций черновика."""
        owners: dict[tuple[str, str, str], CartItem] = {}
        duplicate_ids: set[str] = set()
        for item in state.cart:
            # A pending duplicate is a confirmation card, not extraction noise.
            # Only duplicate rows that were already accepted may be collapsed.
            if not item.catalog_product_id or item.status != ItemStatus.MATCHED:
                continue
            key = (
                item.catalog_product_id,
                normalize_unit(item.unit or item.catalog_unit),
                item.department,
            )
            owner = owners.get(key)
            if owner is None:
                owners[key] = item
                continue
            # Одинаковые количества обычно означают повтор одной операции
            # доставки или дубль распознавания, поэтому не удваиваем их.
            # Разные количества — это уже две добавленные партии одного товара;
            # пользователь должен видеть их одной суммарной строкой.
            if owner.quantity != item.quantity:
                owner.quantity = (owner.quantity or 0) + (item.quantity or 0)
            owner.comment = ConversationEngine._merge_comments(owner.comment, item.comment)
            duplicate_ids.add(item.id)

        if not duplicate_ids:
            return
        state.cart = [item for item in state.cart if item.id not in duplicate_ids]
        if state.current_issue_item_id in duplicate_ids:
            state.current_issue_item_id = ""

    @staticmethod
    def _comment_left_after_catalog_match(query: str, product_name: str) -> str:
        """Извлекает комментарий, оставшийся после поиска товара."""
        query_words = re.findall(r"[a-zа-я0-9]+", normalize_text(query), flags=re.I)
        product_words = re.findall(r"[a-zа-я0-9]+", normalize_text(product_name), flags=re.I)
        if not query_words or not product_words:
            return ""

        matched_query_indexes: set[int] = set()
        next_query_index = 0
        for product_word in product_words:
            match_index = next(
                (
                    index
                    for index in range(next_query_index, len(query_words))
                    if query_words[index] == product_word
                ),
                None,
            )
            if match_index is None:
                continue
            matched_query_indexes.add(match_index)
            next_query_index = match_index + 1

        # Two matching words are strong evidence for a multiword product.  A
        # one-word catalog name is allowed only when that sole word matched.
        required_matches = min(2, len(product_words))
        if len(matched_query_indexes) < required_matches:
            return ""

        remainder = [
            word for index, word in enumerate(query_words) if index not in matched_query_indexes
        ]
        return " ".join(remainder).strip(" .,;")

    def _find_duplicate(self, state: ConversationState, item: CartItem) -> CartItem | None:
        """Находит дубликат товарной позиции."""
        if not item.catalog_product_id:
            return None
        return next(
            (
                existing
                for existing in state.cart
                if existing.status != ItemStatus.SKIPPED
                and existing.catalog_product_id == item.catalog_product_id
            ),
            None,
        )

    def _first_unresolved(self, state: ConversationState) -> CartItem | None:
        """Возвращает приоритетную нерешённую позицию."""
        priority = {
            ItemStatus.DUPLICATE_PENDING: 0,
            ItemStatus.UNIT_MISMATCH: 1,
            ItemStatus.MISSING_QTY: 2,
            ItemStatus.AMBIGUOUS: 3,
            ItemStatus.NOT_FOUND: 4,
            ItemStatus.NEW: 5,
            ItemStatus.AI_PENDING: 6,
        }
        unresolved = [item for item in state.cart if item.status in priority]
        unresolved.sort(key=lambda item: priority[item.status])
        return unresolved[0] if unresolved else None

    def _advance(self, state: ConversationState, added_count: int = 0) -> EngineResult:
        """Переходит к следующей нерешённой позиции."""
        if added_count:
            state.pending_added_items_count = added_count
        unresolved = self._first_unresolved(state)
        if unresolved:
            state.current_issue_item_id = unresolved.id
            state.current_issue_kind = {
                ItemStatus.AMBIGUOUS: IssueKind.CANDIDATE,
                ItemStatus.NOT_FOUND: IssueKind.NOT_FOUND,
                ItemStatus.MISSING_QTY: IssueKind.QUANTITY,
                ItemStatus.UNIT_MISMATCH: IssueKind.UNIT,
                ItemStatus.DUPLICATE_PENDING: IssueKind.DUPLICATE,
            }.get(unresolved.status)
            return EngineResult(
                state=state, reply=issue_reply(unresolved, self._item_index(state, unresolved))
            )
        state.current_issue_item_id = ""
        state.current_issue_kind = None
        if state.pending_added_items_count:
            prompt_count = state.pending_added_items_count
            state.pending_added_items_count = 0
            state.stage = SessionStage.AWAIT_ADD_MORE_CONFIRM
            state.status = "await_add_more_confirm"
            return EngineResult(
                state=state,
                reply=added_items_question_reply(state, prompt_count),
            )
        state.stage = SessionStage.REVIEW if state.cart else SessionStage.COLLECTING
        title = f"Добавлено позиций: {added_count}" if added_count else "Черновик заявки"
        return EngineResult(state=state, reply=cart_reply(state, title=title))

    def _select_candidate(
        self,
        command: ParsedCommand,
        state: ConversationState,
        catalog: list[CatalogProduct],
    ) -> EngineResult:
        """Выбирает указанный товар из списка кандидатов."""
        item = state.current_item()
        if command.callback_target.isdigit():
            item_index = int(command.callback_target)
            item = state.cart[item_index] if 0 <= item_index < len(state.cart) else None
        if item is None or item.status != ItemStatus.AMBIGUOUS:
            return EngineResult(state=state, reply=cart_reply(state))
        index = (command.selected_index or 0) - 1
        if command.selection_query:
            query = normalize_text(command.selection_query)
            index = max(
                range(len(item.candidates)),
                key=lambda candidate_index: self._contains_score(
                    query, normalize_text(item.candidates[candidate_index].name)
                ),
                default=-1,
            )
        if index < 0 or index >= len(item.candidates):
            return EngineResult(state=state, reply=issue_reply(item, self._item_index(state, item)))
        self._apply_catalog(item, item.candidates[index], catalog)
        duplicate = self._find_duplicate(
            ConversationState(cart=[current for current in state.cart if current.id != item.id]),
            item,
        )
        if duplicate and item.status in {ItemStatus.MATCHED, ItemStatus.MISSING_QTY}:
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

    @staticmethod
    def _multiple_warnings(state: ConversationState) -> list[CartItem]:
        """Возвращает товары, для которых нужно выбрать количество."""
        return [
            item
            for item in state.cart
            if item.status == ItemStatus.MATCHED
            and item.suggested_quantity is not None
            and item.suggested_quantity != item.quantity
        ]

    def _select_multiple_warning(self, state: ConversationState) -> CartItem | None:
        """Выбирает текущий товар с предупреждением или первый доступный."""
        current = state.current_item()
        warnings = self._multiple_warnings(state)
        if current in warnings:
            return current
        if warnings:
            state.current_issue_item_id = warnings[0].id
            return warnings[0]
        return None

    def _show_multiple_quantity_choice(self, state: ConversationState) -> EngineResult:
        """Открывает варианты количества без автоматического изменения."""
        item = self._select_multiple_warning(state)
        if item is None:
            state.current_issue_item_id = ""
            return EngineResult(state=state, reply=final_review_reply(state))
        state.stage = SessionStage.AWAIT_SUBMIT_CONFIRM
        state.status = "await_multiple_choice"
        return EngineResult(state=state, reply=multiple_quantity_choice_reply(item))

    def _advance_multiple_quantity_choice(self, state: ConversationState) -> EngineResult:
        """Показывает следующий выбор количества или финальную проверку."""
        state.current_issue_item_id = ""
        next_item = self._select_multiple_warning(state)
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
        state.current_issue_item_id = ""
        return self._advance(state)

    def _confirm_current(self, state: ConversationState) -> EngineResult:
        """Подтверждает текущую позицию черновика."""
        item = state.current_item()
        if item and item.status == ItemStatus.DUPLICATE_PENDING:
            existing = next((row for row in state.cart if row.id == item.issue_message), None)
            if existing:
                existing.quantity = (existing.quantity or 0) + (item.quantity or 0)
                item.status = ItemStatus.SKIPPED
            state.current_issue_item_id = ""
        return self._advance(state)

    def _remove_item(self, command: ParsedCommand, state: ConversationState) -> EngineResult:
        """Удаляет выбранную позицию из черновика."""
        target = normalize_text(command.target_query)
        if not target and state.current_item():
            state.current_item().status = ItemStatus.SKIPPED  # type: ignore[union-attr]
            return self._advance(state)
        item = self._find_cart_item(state, command.target_query)
        if item is not None:
            item.status = ItemStatus.SKIPPED
            return EngineResult(state=state, reply=cart_reply(state, title="Позиция удалена"))
        return EngineResult(state=state, reply=BotReply(text="Не нашёл такую позицию в черновике."))

    @staticmethod
    def _contains_score(target: str, candidate: str) -> int:
        """Оценивает совпадение названий с учётом пунктуации и окончаний."""
        if not target or not candidate:
            return 0
        if target == candidate:
            return 100
        if target in candidate or candidate in target:
            return 80
        target_tokens = re.findall(r"[a-zа-яё0-9%]+", target, flags=re.I)
        candidate_tokens = re.findall(r"[a-zа-яё0-9%]+", candidate, flags=re.I)
        if not target_tokens or not candidate_tokens:
            return 0

        exact_matches = 0
        inflected_matches = 0
        for target_token in target_tokens:
            if target_token in candidate_tokens:
                exact_matches += 1
                continue
            if any(
                ConversationEngine._tokens_share_stem(target_token, candidate_token)
                for candidate_token in candidate_tokens
            ):
                inflected_matches += 1
        return exact_matches * 12 + inflected_matches * 10

    @staticmethod
    def _tokens_share_stem(left: str, right: str) -> bool:
        """Сравнивает формы одного слова без агрессивного морфологического угадывания."""
        shorter_length = min(len(left), len(right))
        if shorter_length < 3:
            return False
        common_length = 0
        for left_char, right_char in zip(left, right, strict=False):
            if left_char != right_char:
                break
            common_length += 1
        if shorter_length <= 4:
            return common_length >= 3 and abs(len(left) - len(right)) <= 2
        return common_length >= 4

    def _find_cart_item(
        self,
        state: ConversationState,
        target_query: str,
    ) -> CartItem | None:
        """Находит только однозначно названную позицию черновика."""
        if not target_query:
            return None
        by_id = next((row for row in state.cart if row.id == target_query), None)
        if by_id is not None:
            return by_id

        target = normalize_text(target_query)
        scored = sorted(
            (
                (
                    max(
                        self._contains_score(target, normalize_text(row.source_query)),
                        self._contains_score(target, normalize_text(row.catalog_name)),
                    ),
                    row,
                )
                for row in state.cart
            ),
            key=lambda pair: pair[0],
            reverse=True,
        )
        if not scored or scored[0][0] <= 0:
            return None
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            return None
        return scored[0][1]

    def _edit_quantity(self, command: ParsedCommand, state: ConversationState) -> EngineResult:
        """Изменяет количество выбранной позиции."""
        if command.edit_quantity is None:
            return EngineResult(state=state, reply=BotReply(text="Укажите новое количество."))
        target = normalize_text(command.target_query)
        item = state.current_item()
        if target:
            item = self._find_cart_item(state, command.target_query)
        if item is None:
            return EngineResult(
                state=state, reply=BotReply(text="Позиция для изменения не найдена.")
            )
        was_multiple_choice = item in self._multiple_warnings(state) and state.stage in {
            SessionStage.AWAIT_SUBMIT_CONFIRM,
            SessionStage.AWAIT_MULTIPLE_QUANTITY,
        }
        quantity = command.edit_quantity
        if command.edit_unit and item.catalog_unit:
            converted = convert_quantity(quantity, command.edit_unit, item.catalog_unit)
            if converted is None:
                return EngineResult(
                    state=state, reply=issue_reply(item, self._item_index(state, item))
                )
            quantity = converted
        item.quantity = quantity
        item.unit = item.catalog_unit or command.edit_unit or item.unit
        if item.catalog_product_id:
            item.status = ItemStatus.MATCHED
            item.suggested_quantity = self._suggested_quantity_for_multiple(item)
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
            rows.append(
                {
                    "Время создания заявки": now.isoformat(),
                    "Время изменения": now.isoformat(),
                    "ID заявки": f"{item.supplier}|{state.restaurant}|{order_no}",
                    "№ Заявки": order_no,
                    "Условное название поставщика": item.supplier,
                    "Условное наз-ие заведения": state.restaurant,
                    "Роль": item.department or self.settings.default_department,
                    "ID товара": item.catalog_product_id,
                    "Наименование у поставщика": item.catalog_name,
                    "Ед.Изм. для заказа": item.catalog_unit,
                    "Минимальная Кратность в заказе": item.minimum_multiple or "",
                    "Полезный V, m Нетто Ед.Изм.для Заказа": item.useful_volume or "",
                    "Цена за Ед.Изм. для заказа": item.price or "",
                    "Кол-во": item.quantity or "",
                    "Мин сумма Заказа по Поставщику": item.supplier_minimum_amount or "",
                    "Комментарий": item.comment,
                    "Сумма по товару в заказе": item.amount,
                    "Сумма по заявке к поставщику": supplier_totals[item.supplier],
                    "Стадия": "Новая заявка",
                    "Стадия от Заведения": "",
                    "_department": item.department,
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
        return EngineResult(
            state=state,
            reply=BotReply(text=f"Отправляю заявку <b>{order_no}</b>..."),
            enqueue_submission=True,
        )
