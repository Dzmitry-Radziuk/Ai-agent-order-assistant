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
    has_compatible_numeric_characteristics,
    has_complete_query_evidence,
    has_conflicting_catalog_qualifiers,
    has_unscoped_product_variant_qualifier,
    is_broad_category_query,
    nearest_valid_multiple,
    query_evidence_tokens,
    rank_candidates,
    supplier_matches_hint,
    tokens,
    unverified_product_terms,
)
from restaurant_bot.services.parser import (
    clean_command_target,
    has_negated_action,
    has_negation,
    infer_intent,
    is_explicit_item_rejection,
    is_product_add_request_phrase,
    normalize_command_text,
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
    comment_scope_clarification_reply,
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
from restaurant_bot.services.submission_presenter import (
    submission_dispatch_uncertain_reply,
)
from restaurant_bot.services.text import (
    NUMBER_WORDS,
    UNIT_ALIASES,
    convert_quantity,
    escape,
    normalize_text,
    normalize_unit,
    numeric_range_spans,
    parse_number_words,
    remove_global_comment_overlap,
)

_SUPPLIER_COMMENT_PREFIXES = {
    "без",
    "в",
    "до",
    "если",
    "к",
    "на",
    "не",
    "обязательно",
    "отдельно",
    "по",
    "пожалуйста",
    "после",
    "просьба",
    "раздельно",
    "с",
    "срочно",
    "сегодня",
    "только",
    "утром",
    "вечером",
    "желательно",
    "завтра",
}
_SUPPLIER_COMMENT_MODIFIER_RE = re.compile(
    r"(?:ый|ий|ой|ая|яя|ое|ее|ые|ие|ого|его|ому|ему|ым|им|ую|юю|ых|их|"
    r"енно|ано|но|мелко|крупно)$",
    flags=re.I,
)
_SUPPLIER_COMMENT_HINT_ROOTS = {
    "холод",
    "тепл",
    "охлажд",
    "достав",
    "привез",
    "нарез",
    "зачищ",
    "натер",
    "раздел",
    "мелк",
    "крупн",
    "сроч",
    "завтр",
    "сегодн",
    "утр",
    "вечер",
    "позвон",
}
_EXPLICIT_ORDER_QUANTITY_RE = re.compile(
    r"(?:мне\s+)?(?:нужн(?:о|а|ы)|надо|закаж(?:и|ем|у)|добав(?:ь|ить)|"
    r"постав(?:ь|ить)|возьм(?:и|ем)|количеств(?:о|ом)?|вес)\b",
    flags=re.I,
)
_SINGLE_CONTAINER_UNITS = {
    "банка",
    "бутылка",
    "пачка",
    "упаковка",
    "коробка",
    "ведро",
    "рулон",
    "пакет",
    "штука",
    "штуку",
    "штуке",
}
_QUANTITY_LEADIN_WORDS = {
    "да",
    "давай",
    "одна",
    "один",
    "одно",
    "одну",
    "возьми",
    "возьмем",
    "закажи",
    "заказать",
    "ладно",
    "мне",
    "нужна",
    "нужен",
    "нужно",
    "ок",
    "окей",
    "поставь",
    "поставить",
    "пусть",
    "тогда",
    "хорошо",
}


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
        if state.pending_comment_items:
            return self._resolve_pending_comment_scope(event, command, state, catalog)
        self._remove_navigation_command_items(state)
        self._normalize_existing_catalog_comments(state)
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
        command = self._contextual_cart_pagination_command(command, event, state)
        command = self._contextual_final_review_pagination_command(command, event, state)
        command = self._contextual_order_status_command(command, event, state)
        command = self._contextual_voice_command(command, event, state)
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
                SessionStage.REVIEW,
                SessionStage.AWAIT_MULTIPLE_QUANTITY,
                SessionStage.AWAIT_UNIT_QUANTITY,
            }
            and (event.text.strip() or command.text.strip())
            and not self._has_named_product_items(command, event.text or command.text)
        ):
            quantity, unit = self._spoken_quantity(event.text or command.text)
            if quantity is not None:
                item = state.current_item()
                assert item is not None
                item.quantity = quantity
                item.unit = unit or item.catalog_unit or item.unit
                short_container_quantity, short_container_unit = self._spoken_unit_only_quantity(
                    event.text or command.text
                )
                if (
                    item.status == ItemStatus.MISSING_QTY
                    and short_container_quantity is not None
                    and short_container_unit in {"бут", "бан"}
                    and item.catalog_unit == "шт"
                ):
                    # A short container answer on a quantity card means one
                    # catalog item; an explicit count such as "8 банок" still
                    # follows the strict unit-mismatch path below.
                    item.unit = item.catalog_unit
                if item.status == ItemStatus.DUPLICATE_PENDING:
                    # Повторную позицию можно объединить только после
                    # подтверждения в единице каталога. Нельзя молча считать
                    # «банку» штукой или килограммы граммами.
                    if (
                        item.catalog_unit
                        and item.unit
                        and normalize_unit(item.unit) != normalize_unit(item.catalog_unit)
                    ):
                        return self._advance(state)
                    return self._confirm_current(state)
                if item.catalog_unit and item.unit and item.unit != item.catalog_unit:
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
            page = self._order_status_page(command, state)
            detail_page = self._order_status_detail_page(command, state)
            detail_requested = bool(
                command.selected_index is not None
                or command.selection_query
                or command.callback_target in {"detail_next", "detail_previous"}
                or command.callback_target.startswith("detail:")
            )
            if command.selected_index is not None:
                state.order_status_selected_index = command.selected_index
            if command.selection_query:
                state.order_status_selected_order_number = command.selection_query
            if detail_requested:
                selected_index = (
                    command.selected_index
                    if command.selected_index is not None
                    else state.order_status_selected_index
                )
                order_number = command.selection_query or state.order_status_selected_order_number
            else:
                state.order_status_selected_index = None
                state.order_status_selected_order_number = ""
                selected_index = None
                order_number = ""
            state.order_status_view_active = True
            state.order_status_page = page
            state.order_status_detail_page = detail_page
            state.order_status_detail_active = detail_requested
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
                order_status_page=page,
                order_status_detail_page=detail_page,
                order_status_selected_index=selected_index,
                order_status_order_number=order_number,
            )
        if command.intent == Intent.SHOW_CART:
            if (
                state.stage == SessionStage.AWAIT_SUBMIT_CONFIRM
                and self._is_generic_show_products_command(command.text)
            ):
                return EngineResult(state=state, reply=supplier_warning_details_reply(state))
            state.cart_page = self._cart_page(command, state)
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

        if command.comment_clarification:
            state.pending_comment_items = [item.model_copy(deep=True) for item in command.items]
            state.pending_comment_text = command.comment_clarification
            state.pending_comment_global_comment = command.global_comment
            state.stage = SessionStage.AWAIT_COMMENT_SCOPE
            state.status = "await_comment_scope"
            return EngineResult(
                state=state,
                reply=comment_scope_clarification_reply(
                    state.pending_comment_items,
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
            if command.intent == Intent.SHOW_FINAL_REVIEW:
                state.final_review_page = self._final_review_page(command, state)
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
                        same_missing.suggested_quantity = self._suggested_quantity_for_multiple(
                            same_missing
                        )
                        same_missing.status = ItemStatus.MATCHED
                    state.current_issue_item_id = same_missing.id
                    continue
                duplicate = self._find_duplicate(state, item)
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
            # `v2:minsumadd` scopes exactly the next incoming product message
            # in n8n. Retaining this context would incorrectly lock every
            # later product to the same supplier.
            if selected_supplier:
                state.supplier_hint_context = ""
            return self._advance(state, added_count=len(command.items))

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
        if command.intent in {Intent.CLEAR_CART, Intent.START_NEW_ORDER}:
            self._clear_pending_comment(state)
            return self.handle(event, command, state, catalog)
        if (
            command.intent in {Intent.BACK, Intent.CANCEL}
            or command.comment_scope_action == "cancel"
        ):
            self._clear_pending_comment(state)
            state.stage = (
                SessionStage.REVIEW
                if self._has_active_draft_items(state)
                else SessionStage.COLLECTING
            )
            state.status = state.stage.value
            return EngineResult(
                state=state,
                reply=cart_reply(state, title="Комментарий не добавлен"),
            )

        action = command.comment_scope_action
        confidence = command.confidence or 0.0
        valid_indexes = list(
            dict.fromkeys(
                index
                for index in command.comment_target_indexes
                if isinstance(index, int)
                and not isinstance(index, bool)
                and 0 <= index < len(state.pending_comment_items)
            )
        )
        indexes_are_exact = valid_indexes == command.comment_target_indexes
        valid_resolution = bool(
            confidence >= 0.9
            and (
                (action == "items" and valid_indexes and indexes_are_exact)
                or (action == "order" and not command.comment_target_indexes)
            )
        )
        if not valid_resolution:
            state.stage = SessionStage.AWAIT_COMMENT_SCOPE
            state.status = "await_comment_scope"
            return EngineResult(
                state=state,
                reply=comment_scope_clarification_reply(
                    state.pending_comment_items,
                    state.pending_comment_text,
                ),
            )

        pending_items = [item.model_copy(deep=True) for item in state.pending_comment_items]
        comment = state.pending_comment_text
        global_comment = state.pending_comment_global_comment
        if action == "order":
            global_comment = self._merge_comments(global_comment, comment)
        else:
            for index in valid_indexes:
                merged = self._merge_comments(
                    pending_items[index].comment,
                    pending_items[index].user_comment_to_supplier,
                    comment,
                )
                pending_items[index] = pending_items[index].model_copy(
                    update={
                        "comment": merged,
                        "user_comment_to_supplier": merged,
                    }
                )

        self._clear_pending_comment(state)
        state.stage = SessionStage.COLLECTING
        state.status = "collecting"
        resolved = ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=pending_items,
            global_comment=global_comment,
        )
        safe_event = event.model_copy(update={"text": ""})
        return self.handle(safe_event, resolved, state, catalog)

    @staticmethod
    def _clear_pending_comment(state: ConversationState) -> None:
        """Удаляет временные данные уточнения, не затрагивая черновик."""
        state.pending_comment_items = []
        state.pending_comment_text = ""
        state.pending_comment_global_comment = ""

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
            department=extracted.department or self.settings.default_department,
            department_quantities=extracted.department_quantities.model_copy(deep=True),
            supplier_hint=extracted.supplier_hint,
            comment=self._merge_comments(item_comment, global_comment),
        )

    @staticmethod
    def _has_explicit_order_quantity(source_line: str, quantity: float | None) -> bool:
        """Отличает объём заказа от чисел в размере или фасовке товара."""
        if quantity is None:
            return False
        source = str(source_line or "").strip()
        if not source:
            return False
        range_spans = numeric_range_spans(source)
        masked = list(source)
        for start, end in range_spans:
            masked[start:end] = [" "] * (end - start)
        masked_source = "".join(masked)
        unit_pattern = "|".join(
            sorted((re.escape(unit) for unit in UNIT_ALIASES), key=len, reverse=True)
        )
        number_word_pattern = "|".join(
            sorted((re.escape(word) for word in NUMBER_WORDS), key=len, reverse=True)
        )
        value_pattern = re.compile(
            rf"(?<![\w-])(?P<value>\d+(?:[,.]\d+)?|"
            rf"(?:{number_word_pattern})(?:\s+(?:{number_word_pattern}))*)"
            rf"\s*(?P<unit>{unit_pattern})?\b",
            flags=re.I,
        )
        values: list[float] = []
        for match in value_pattern.finditer(masked_source):
            raw_value = match.group("value").casefold()
            try:
                value = float(raw_value.replace(",", "."))
            except ValueError:
                tokens = raw_value.split()
                parsed = parse_number_words(tokens, 0)
                value = parsed[0] if parsed and parsed[1] == len(tokens) else -1
            if value >= 0:
                values.append(value)
        if not values or not any(abs(value - quantity) <= 1e-9 for value in values):
            return False
        # A numeric range is reference data. Any matching value outside it is
        # an order quantity, even when the user omitted an explicit verb.
        if range_spans:
            return True
        if _EXPLICIT_ORDER_QUANTITY_RE.search(masked_source):
            return True
        # A second number or a terminal dash makes the last value an order
        # quantity, while a lone ``180 грамм`` remains a product characteristic.
        if len(values) > 1:
            return True
        return bool(
            re.search(
                rf"(?:^|[-—–:])\s*\d+(?:[,.]\d+)?\s*(?:{unit_pattern})?\s*$",
                masked_source,
                flags=re.I,
            )
        )

    @staticmethod
    def _spoken_unit_only_quantity(text: str) -> tuple[float | None, str]:
        """Понимает короткое «ладно, бутылка» как одну текущую позицию."""
        tokens = [
            token
            for token in re.findall(r"[a-zа-яё]+", normalize_text(text), flags=re.I)
            if token not in _QUANTITY_LEADIN_WORDS
        ]
        if len(tokens) != 1 or tokens[0] not in _SINGLE_CONTAINER_UNITS:
            return None, ""
        return 1, normalize_unit(tokens[0])

    @staticmethod
    def _validate_supplier_hint(
        extracted: ExtractedItem,
        catalog: list[CatalogProduct],
    ) -> ExtractedItem:
        """Оставляет поставщика только при подтверждении живым каталогом."""
        hint = extracted.supplier_hint.strip()
        if not hint:
            return extracted
        matches = {
            product.supplier.strip()
            for product in catalog
            if product.supplier.strip() and supplier_matches_hint(product.supplier, hint)
        }
        if not matches:
            return extracted.model_copy(update={"supplier_hint": ""})
        if len(matches) == 1:
            return extracted.model_copy(update={"supplier_hint": matches.pop()})
        return extracted

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
        return ConversationEngine._spoken_unit_only_quantity(text)

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

    @staticmethod
    def _has_named_product_items(command: ParsedCommand, text: str) -> bool:
        """Отличает полноценный товарный запрос от короткого ответа количеством."""
        if command.intent != Intent.ADD_ITEMS or not command.items:
            return False
        normalized_text = normalize_text(text or command.text)
        response_word_stems = (
            "давай",
            "добав",
            "закаж",
            "измен",
            "исправ",
            "колич",
            "мне",
            "надо",
            "нуж",
            "постав",
            "пусть",
            "сдел",
            "укаж",
            "вес",
            "возьм",
        )
        for item in command.items:
            query = normalize_text(item.product_query)
            if not query or query == normalized_text:
                continue
            query_words = re.findall(r"[a-zа-яё]+", query, flags=re.I)
            if query_words and any(
                not word.startswith(response_word_stems) for word in query_words
            ):
                return True
        return False

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
    def _order_status_page(command: ParsedCommand, state: ConversationState) -> int:
        """Вычисляет страницу истории для списка или выбранной заявки."""
        target = command.callback_target
        if target.startswith("page:"):
            try:
                return max(0, int(target.partition(":")[2]))
            except ValueError:
                return 0
        if target == "next":
            return state.order_status_page + 1
        if target == "previous":
            return max(0, state.order_status_page - 1)
        if target == "current" or command.selected_index is not None or command.selection_query:
            return max(0, state.order_status_page)
        phrase = normalize_text(command.text)
        if state.order_status_view_active and phrase in {"обнови", "обновить", "обновить статусы"}:
            return max(0, state.order_status_page)
        return 0

    @staticmethod
    def _order_status_detail_page(command: ParsedCommand, state: ConversationState) -> int:
        """Resolve the requested page inside a selected order."""
        target = command.callback_target
        if target.startswith("detail:"):
            try:
                return max(0, int(target.partition(":")[2]))
            except ValueError:
                return 0
        if target == "detail_next":
            return max(0, state.order_status_detail_page + 1)
        if target == "detail_previous":
            return max(0, state.order_status_detail_page - 1)
        if command.selected_index is not None or command.selection_query:
            return 0
        return max(0, state.order_status_detail_page)

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

    @staticmethod
    def _final_review_page(command: ParsedCommand, state: ConversationState) -> int:
        """Вычисляет страницу финальной проверки заявки."""
        target = command.callback_target
        if target.startswith("page:"):
            try:
                return max(0, int(target.partition(":")[2]))
            except ValueError:
                return 0
        return max(0, state.final_review_page)

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
        ConversationEngine._clear_pending_comment(state)
        clear_product_add_pending(state)

    @staticmethod
    def _item_index(state: ConversationState, item: CartItem) -> int:
        """Возвращает номер позиции по её идентификатору."""
        return next(
            (index for index, candidate in enumerate(state.cart) if candidate.id == item.id), 0
        )

    @staticmethod
    def _catalog_packaging_measurement(
        item: CartItem,
        candidates: list[Candidate],
    ) -> tuple[float, str] | None:
        """Находит фасовку, ошибочно распознанную как количество заказа.

        Голосовой разбор не знает каталог и может превратить «Марципан 65
        грамм» в количество 65 г. Если единственное число строки совпадает с
        фасовкой кандидата, а единица заказа каталога другая (например, кг),
        безопаснее сохранить число как атрибут товара и запросить реальное
        количество в единице каталога.
        """
        if item.quantity is None or not item.unit or item.quantity_source:
            return None
        source = normalize_text(item.source_line)
        if not source or _EXPLICIT_ORDER_QUANTITY_RE.search(source) or numeric_range_spans(source):
            return None

        unit_pattern = "|".join(
            sorted((re.escape(unit) for unit in UNIT_ALIASES), key=len, reverse=True)
        )
        measurements = re.findall(
            rf"(?<![\w-])(?P<value>\d+(?:[,.]\d+)?)\s*"
            rf"(?P<unit>{unit_pattern})\b",
            source,
            flags=re.IGNORECASE,
        )
        if len(measurements) != 1:
            return None
        value, raw_unit = measurements[0]
        spoken_unit = normalize_unit(raw_unit)
        try:
            spoken_value = float(value.replace(",", "."))
        except ValueError:
            return None
        if abs(spoken_value - item.quantity) > 1e-9 or spoken_unit != item.unit:
            return None

        packaging_candidates = [
            candidate
            for candidate in candidates
            if has_compatible_numeric_characteristics(
                f"{spoken_value:g} {spoken_unit}", candidate.name
            )
            and normalize_unit(candidate.unit) != spoken_unit
        ]
        if not packaging_candidates:
            return None
        return spoken_value, spoken_unit

    def _match_item(
        self,
        item: CartItem,
        catalog: list[CatalogProduct],
        search_scope: SearchScope | None = None,
    ) -> None:
        """Сопоставляет позицию с товаром каталога."""
        self._remove_unanchored_supplier_hint(item)
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
        packaging_measurement = self._catalog_packaging_measurement(item, candidates)
        if packaging_measurement is not None:
            packaging_value, packaging_unit = packaging_measurement
            item.packaging_text = f"{packaging_value:g} {packaging_unit}"
            item.packaging_role = "catalog_attribute"
            item.packaging_confidence = max(item.packaging_confidence, 0.86)
            item.quantity = None
            item.unit = ""
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
        self._sanitize_catalog_facts_before_resolution(item, candidates[0])
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
                comment_start = self._supplier_comment_start(comment_words)
                if comment_start is not None:
                    supplier_comment_words = comment_words[comment_start:]
                    product_extra_words = comment_words[:comment_start]
                    product_words = [
                        word
                        for word in query_words
                        if word in first_evidence or word in product_extra_words
                    ]
                    item.comment = self._merge_comments(
                        item.comment, " ".join(supplier_comment_words)
                    )
                    item.source_query = " ".join(product_words)
                    candidates = rank_candidates(item.source_query, search_catalog, supplier_hint)
                    item.candidates = candidates
        item.supplier_search_locked = False
        broad_query = is_broad_category_query(item.source_query, candidates)
        unverified_terms = self._unverified_query_terms(item.source_query, candidates[0].name)
        if (
            broad_query
            or not can_auto_select(candidates)
            or not has_compatible_numeric_characteristics(item.source_query, candidates[0].name)
            or (
                item.packaging_role == "catalog_attribute"
                and not has_compatible_numeric_characteristics(
                    item.packaging_text, candidates[0].name
                )
            )
            or has_conflicting_catalog_qualifiers(item.source_query, candidates[0].name)
            or has_unscoped_product_variant_qualifier(item.comment)
            or item.packaging_role == "ambiguous"
            or (
                unverified_terms
                and not self._looks_like_supplier_comment_fragment(unverified_terms)
            )
        ):
            item.status = ItemStatus.AMBIGUOUS
            return
        self._apply_catalog(item, candidates[0], catalog)

    @staticmethod
    def _looks_like_supplier_comment_fragment(words: list[str]) -> bool:
        """Отличает инструкцию поставщику от неизвестной части названия товара."""
        return ConversationEngine._supplier_comment_start(words) is not None

    @staticmethod
    def _remove_unanchored_supplier_hint(item: CartItem) -> None:
        """Не считает хвост названия товара поставщиком без явной команды."""
        hint = normalize_text(item.supplier_hint)
        source = normalize_text(item.source_line)
        if not hint or not source:
            return
        explicit_supplier = re.search(
            r"\b(?:поставщик\w*|у\s+поставщик\w*|от\s+поставщик\w*|купить\s+у)\b",
            source,
            flags=re.I,
        )
        if explicit_supplier:
            return
        query = normalize_text(item.source_query)
        if query and query in source and source.find(query) < source.rfind(hint):
            item.supplier_hint = ""

    def _sanitize_catalog_facts_before_resolution(
        self,
        item: CartItem,
        candidate: Candidate,
    ) -> None:
        """Удаляет характеристики каталога из количества и комментария до ИИ-уточнения."""
        product_name = candidate.name
        if item.quantity is not None and item.unit and not item.quantity_source:
            quantity_text = f"{item.quantity:g} {item.unit}"
            explicit_order = self._has_explicit_order_quantity(item.source_line, item.quantity)
            if has_compatible_numeric_characteristics(quantity_text, product_name) and (
                item.packaging_role == "catalog_attribute" or not explicit_order
            ):
                item.quantity = None
                item.unit = ""
        item.comment = self._remove_catalog_fact_comments(
            item.comment,
            item.source_query,
            product_name,
            source_line=item.source_line,
            include_source_query=True,
        )

    @staticmethod
    def _supplier_comment_start(words: list[str]) -> int | None:
        """Находит начало явного комментария внутри неизвестного фрагмента."""
        normalized = [normalize_text(word) for word in words if normalize_text(word)]
        if not normalized:
            return None
        for index, word in enumerate(normalized):
            if word in _SUPPLIER_COMMENT_PREFIXES:
                return index
            if word.endswith(("ть", "ться", "йте")):
                return index
        for index, word in enumerate(normalized):
            if any(word.startswith(root) for root in _SUPPLIER_COMMENT_HINT_ROOTS) and all(
                _SUPPLIER_COMMENT_MODIFIER_RE.search(suffix) for suffix in normalized[index:]
            ):
                # Слова до явного пожелания остаются частью названия. Например,
                # «сироп рза холодным» сохраняет «рза» в поиске.
                return index
        return None

    @staticmethod
    def _unverified_query_terms(query: str, product_name: str) -> list[str]:
        """Возвращает значимые слова запроса, которых нет в строке каталога.

        Единицы измерения и числа проверяются отдельными правилами. Остальные
        слова должны быть подтверждены названием каталога либо явно признаны
        пользовательским пожеланием; иначе единственный похожий кандидат не
        может быть выбран автоматически.
        """
        return unverified_product_terms(query, product_name)

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
        explicit_comment = self._remove_catalog_fact_comments(
            item.comment,
            item.source_query,
            product.name,
            source_line=item.source_line,
            include_source_query=True,
        )
        derived_comment = self._comment_left_after_catalog_match(item.source_query, product.name)
        if derived_comment and not self._looks_like_supplier_comment_fragment(
            re.findall(r"[a-zа-яё0-9]+", normalize_text(derived_comment), flags=re.I)
        ):
            # Unmatched words without a supplier-instruction marker are still
            # part of the user's product name (or an ASR variant). Never write
            # them into the supplier comment after a candidate is selected.
            derived_comment = ""
        user_comment = self._merge_comments(explicit_comment, derived_comment)
        item.comment = self._merge_comments(product.comment, user_comment)

        if item.quantity is None:
            item.status = ItemStatus.MISSING_QTY
            return
        if (
            item.unit
            and item.catalog_unit
            and normalize_unit(item.unit) != normalize_unit(item.catalog_unit)
        ):
            # A spoken unit is user intent. Even convertible pairs are
            # confirmed before changing the order silently.
            item.status = ItemStatus.UNIT_MISMATCH
            return
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
        if numeric_range_spans(item.source_line):
            parsed_source = parse_product_lines(item.source_line)
            if len(parsed_source) == 1:
                parsed_item = parsed_source[0]
                item.source_query = product_name
                if not ConversationEngine._has_explicit_order_quantity(
                    item.source_line,
                    item.quantity,
                ):
                    item.quantity = parsed_item.quantity
                    item.unit = parsed_item.unit if parsed_item.quantity is not None else ""
                if item.comment and normalize_text(item.comment) in normalize_text(product_name):
                    item.comment = ""
                return
            # A shared multi-product source line is reconciled by the parser
            # before it reaches the catalog. Do not parse its normalized token
            # stream: ``0,9-1,3`` would otherwise start with a false zero.
            item.source_query = product_name
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
        return remove_global_comment_overlap(item_comment, global_comment)

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
    def _normalize_existing_catalog_comments(state: ConversationState) -> None:
        """Очищает ошибки нормализации в уже сохранённом черновике.

        Старый черновик мог быть создан до обновления парсера и содержать
        фасовку, страну или остаток названия в поле комментария. Повторная
        обработка удаляет только подтверждённые факты каталога и фразы,
        состоящие исключительно из количества и единицы измерения; реальные
        инструкции поставщику сохраняются.
        """
        for item in state.cart:
            if not item.catalog_name or not item.comment:
                continue
            item.comment = ConversationEngine._remove_catalog_fact_comments(
                item.comment,
                item.source_query,
                item.catalog_name,
                include_source_query=True,
            )

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

    @staticmethod
    def _remove_catalog_fact_comments(
        comment: str,
        source_query: str,
        product_name: str,
        *,
        source_line: str = "",
        include_source_query: bool = False,
    ) -> str:
        """Убирает из комментария характеристики, уже подтверждённые каталогом.

        ИИ иногда оставляет часть названия отдельным комментарием: например,
        «белое» у вина или «номер один» у позиции «№1». Такие слова не являются
        пожеланием поставщику и не должны записываться в колонку комментария.
        Явные пожелания («желательно завтра», «без кожи») сохраняются целиком.
        """

        def canonical_words(value: str) -> list[str]:
            """Канонизирует числа и подпись «номер» для сравнения с каталогом."""
            words = re.findall(r"[a-zа-яё0-9]+", normalize_text(value), flags=re.I)
            result: list[str] = []
            index = 0
            while index < len(words):
                word = words[index]
                if word == "номер" and index + 1 < len(words):
                    number = NUMBER_WORDS.get(words[index + 1])
                    if number is not None and float(number).is_integer():
                        result.append(str(int(number)))
                        index += 2
                        continue
                if word in NUMBER_WORDS and float(NUMBER_WORDS[word]).is_integer():
                    result.append(str(int(NUMBER_WORDS[word])))
                else:
                    result.append(word)
                index += 1
            return result

        def compact_words(value: str) -> set[str]:
            """Возвращает компактные формы для «д/п», «дп» и похожих записей."""
            compact = {
                re.sub(r"[^a-zа-яё0-9]", "", word)
                for word in canonical_words(value)
                if re.sub(r"[^a-zа-яё0-9]", "", word)
            }
            compact.update(
                re.sub(r"[^a-zа-яё0-9]", "", group)
                for group in re.findall(
                    r"[a-zа-яё0-9]+(?:[/.-][a-zа-яё0-9]+)+",
                    normalize_text(value),
                    flags=re.I,
                )
            )
            return compact

        catalog_facts = set(canonical_words(product_name))
        query_facts = set(canonical_words(source_query)) if include_source_query else set()
        compact_catalog_facts = compact_words(product_name)
        compact_query_facts = compact_words(source_query) if include_source_query else set()
        packaging_aliases = {
            "коробка": "кор",
            "коробке": "кор",
            "коробки": "кор",
            "короб": "кор",
        }
        normalized_unit_words = {normalize_text(alias) for alias in UNIT_ALIASES} | {
            normalize_text(unit) for unit in UNIT_ALIASES.values()
        }
        normalized_unit_values = {normalize_text(unit) for unit in UNIT_ALIASES.values()}

        def is_quantity_only_part(words: list[str]) -> bool:
            """Определяет остаток количества, который не является пожеланием."""
            has_number = False
            has_unit = False
            for word in words:
                if word == "и":
                    continue
                if re.fullmatch(r"\d+(?:[.,]\d+)?", word) or word in NUMBER_WORDS:
                    has_number = True
                    continue
                if (
                    word in normalized_unit_words
                    or normalize_text(normalize_unit(word)) in normalized_unit_values
                ):
                    has_unit = True
                    continue
                return False
            return bool(words) and has_number and has_unit

        if not catalog_facts and not query_facts:
            return comment
        kept: list[str] = []
        for part in re.split(r"[;,]", str(comment or "")):
            cleaned = " ".join(part.split()).strip(" .,;")
            if not cleaned:
                continue
            part_words = canonical_words(cleaned)
            normalized_part_words = [packaging_aliases.get(word, word) for word in part_words]
            part_compact = compact_words(cleaned)
            if is_quantity_only_part(part_words):
                continue
            all_catalog_facts = all(
                word in catalog_facts
                or word in query_facts
                or word in {"в", "на", "из", "по"}
                or packaging_aliases.get(word, word) in catalog_facts
                for word in normalized_part_words
            )
            compact_catalog_match = bool(part_compact) and all(
                word in compact_catalog_facts or word in compact_query_facts
                for word in part_compact
            )
            if part_words and (all_catalog_facts or compact_catalog_match):
                continue
            kept.append(cleaned)
        return "; ".join(kept)

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
            return self._advance(state)
        item = self._find_cart_item(state, target_query)
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
        active_rows = [row for row in state.cart if row.status != ItemStatus.SKIPPED]
        by_id = next((row for row in active_rows if row.id == target_query), None)
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
                for row in active_rows
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
        return [(item.department or "Кухня", item.quantity or 0)]
