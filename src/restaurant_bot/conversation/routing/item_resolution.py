"""Содержит чистые политики разрешения товарных modal-контекстов."""

from __future__ import annotations

import re

from restaurant_bot.conversation.routing.contracts import CompatibilityAction, CompatibilityDecision
from restaurant_bot.conversation.selection import (
    CandidateReferenceStatus,
    resolve_candidate_reference,
)
from restaurant_bot.domain.models import (
    CartItem,
    ConversationState,
    DialogueResponse,
    Intent,
    ItemStatus,
    ParsedCommand,
    SessionStage,
)
from restaurant_bot.domain.text import normalize_text


def has_product_items(command: ParsedCommand) -> bool:
    """Проверяет наличие реальной товарной позиции в команде."""
    if command.dialogue_response is DialogueResponse.UNCERTAIN:
        return False
    return any(item.product_query.strip() for item in command.items)


def evaluate_manual_details(
    command: ParsedCommand,
    state: ConversationState,
    interrupt_intents: frozenset[Intent],
) -> CompatibilityDecision:
    """Разделяет новое намерение и ответ на запрос нового названия."""
    if (
        state.stage is not SessionStage.AWAIT_MANUAL_DETAILS
        or not state.current_issue_item_id
        or state.current_item() is None
    ):
        return CompatibilityDecision(CompatibilityAction.NOT_APPLICABLE)

    if command.intent is Intent.ADD_ITEMS:
        if has_concrete_new_items(command):
            return CompatibilityDecision(CompatibilityAction.INTERRUPT)
        if len(command.items) == 1 and normalize_text(command.items[0].product_query):
            return CompatibilityDecision(CompatibilityAction.CONTINUE)
        return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)

    if command.intent in {Intent.MANUAL_CURRENT, Intent.SKIP_CURRENT}:
        return CompatibilityDecision(CompatibilityAction.CONTINUE)
    if command.intent is Intent.UNKNOWN:
        return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)
    if command.intent in interrupt_intents:
        return CompatibilityDecision(CompatibilityAction.INTERRUPT)
    return CompatibilityDecision(CompatibilityAction.INTERRUPT)


def evaluate_product_add_details(
    command: ParsedCommand,
    state: ConversationState,
    interrupt_intents: frozenset[Intent],
) -> CompatibilityDecision:
    """Разрешает описание ненайденного товара или прерывает его независимой командой."""
    index = state.pending_product_add_item_index
    if (
        state.stage is not SessionStage.AWAIT_PRODUCT_ADD_DETAILS
        or not state.pending_product_add_request_id
        or index is None
        or not 0 <= index < len(state.cart)
    ):
        return CompatibilityDecision(CompatibilityAction.NOT_APPLICABLE)

    if command.intent in {Intent.CANCEL, Intent.PRODUCT_ADD_SKIP}:
        return CompatibilityDecision(CompatibilityAction.CONTINUE)
    if command.intent is Intent.ADD_ITEMS:
        if command.explicit_add_items:
            return CompatibilityDecision(CompatibilityAction.INTERRUPT)
        if has_product_add_description(command):
            return CompatibilityDecision(CompatibilityAction.CONTINUE)
        return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)
    if command.intent is Intent.UNKNOWN:
        if has_product_add_description(command):
            return CompatibilityDecision(CompatibilityAction.CONTINUE)
        return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)
    if command.intent in interrupt_intents or command.intent in {
        Intent.ADD_MORE,
        Intent.EDIT_QUANTITY,
        Intent.MANUAL_CURRENT,
        Intent.SELECT_CANDIDATE,
        Intent.CONFIRM,
        Intent.PRODUCT_ADD,
        Intent.PRODUCT_ADD_RETRY,
        Intent.SEARCH_ALL_SUPPLIERS,
        Intent.SWITCH_SUPPLIER,
    }:
        return CompatibilityDecision(CompatibilityAction.INTERRUPT)
    return CompatibilityDecision(CompatibilityAction.INTERRUPT)


def evaluate_candidate_selection(
    command: ParsedCommand,
    state: ConversationState,
) -> CompatibilityDecision:
    """Разрешает выбор кандидата только в открытом контексте AMBIGUOUS."""
    if state.stage in {
        SessionStage.AWAIT_MANUAL_DETAILS,
        SessionStage.AWAIT_PRODUCT_ADD_DETAILS,
    }:
        return CompatibilityDecision(CompatibilityAction.NOT_APPLICABLE)
    item = state.current_item()
    if item is None or item.status is not ItemStatus.AMBIGUOUS:
        return CompatibilityDecision(CompatibilityAction.NOT_APPLICABLE)

    if command.intent is Intent.SELECT_CANDIDATE:
        if not item.candidates:
            return CompatibilityDecision(CompatibilityAction.CONTINUE)
        candidate_index = (command.selected_index or 0) - 1
        if command.selection_query or 0 <= candidate_index < len(item.candidates):
            return CompatibilityDecision(CompatibilityAction.CONTINUE)
        return CompatibilityDecision(CompatibilityAction.REJECT)

    if command.intent is Intent.ADD_ITEMS:
        source_text = command.text or (
            command.items[0].source_line if command.items else item.source_line or item.source_query
        )
        reference = resolve_candidate_reference(item, source_text)
        if reference.status is CandidateReferenceStatus.UNIQUE:
            return CompatibilityDecision(CompatibilityAction.CONTINUE)
        if reference.status is CandidateReferenceStatus.AMBIGUOUS:
            return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)
        if has_concrete_new_items(command):
            return CompatibilityDecision(CompatibilityAction.INTERRUPT)
        return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)

    if command.intent is Intent.UNKNOWN:
        source_text = command.text or item.source_line or item.source_query
        reference = resolve_candidate_reference(item, source_text)
        if reference.status is CandidateReferenceStatus.UNIQUE:
            return CompatibilityDecision(CompatibilityAction.CONTINUE)
        if reference.status is CandidateReferenceStatus.AMBIGUOUS:
            return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)
        return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)

    if command.intent in {Intent.CLARIFY_CURRENT, Intent.CANCEL}:
        return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)
    return CompatibilityDecision(CompatibilityAction.INTERRUPT)


def evaluate_not_found(
    command: ParsedCommand,
    state: ConversationState,
    interrupt_intents: frozenset[Intent],
) -> CompatibilityDecision:
    """Разрешает продолжение только открытого сценария ненайденного товара."""
    item = state.current_item()
    if item is None or item.status is not ItemStatus.NOT_FOUND:
        return CompatibilityDecision(CompatibilityAction.NOT_APPLICABLE)

    if state.stage is SessionStage.AWAIT_PRODUCT_ADD_DETAILS and command.intent is Intent.CANCEL:
        return CompatibilityDecision(CompatibilityAction.CONTINUE)
    if command.intent is Intent.ADD_ITEMS:
        if has_concrete_new_items(command):
            return CompatibilityDecision(CompatibilityAction.INTERRUPT)
        if state.stage in {
            SessionStage.AWAIT_MANUAL_DETAILS,
            SessionStage.AWAIT_PRODUCT_ADD_DETAILS,
        }:
            return CompatibilityDecision(CompatibilityAction.CONTINUE)
        return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)

    if command.intent in interrupt_intents:
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


def evaluate_duplicate_pending(
    command: ParsedCommand,
    state: ConversationState,
    interrupt_intents: frozenset[Intent],
) -> CompatibilityDecision:
    """Разрешает ответ на duplicate prompt или прерывает его новым intent."""
    item = state.current_item()
    if item is None or item.status is not ItemStatus.DUPLICATE_PENDING:
        return CompatibilityDecision(CompatibilityAction.NOT_APPLICABLE)

    if command.intent is Intent.ADD_ITEMS:
        if has_named_product_items(command, command.text):
            return CompatibilityDecision(CompatibilityAction.INTERRUPT)
        if has_concrete_new_items(command):
            # Текущий поток количества принимает формы вроде
            # ``давай 3 штуки`` как добавление к открытому duplicate.
            return CompatibilityDecision(CompatibilityAction.CONTINUE)
        return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)

    if command.intent is Intent.REMOVE_ITEM:
        # Детерминированный parser может классифицировать отрицательный ответ
        # вроде ``не добавляй повторно`` как REMOVE_ITEM без товарной цели.
        # Такой ответ обрабатывает существующее правило; цель корзины
        # остаётся независимым intent.
        if not has_matching_cart_target(command, state):
            return CompatibilityDecision(CompatibilityAction.CONTINUE)
        return CompatibilityDecision(CompatibilityAction.INTERRUPT)

    if command.intent in {
        Intent.MERGE_DUPLICATE,
        Intent.CONFIRM,
        Intent.EDIT_QUANTITY,
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
        # Число обрабатывает quantity flow; кандидат здесь не открыт.
        return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)
    if command.intent is Intent.UNKNOWN:
        # Обработчик количества ещё может доказать, что это короткий числовой
        # ответ; иначе engine вернёт duplicate-карточку без изменений.
        return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)
    if command.intent in interrupt_intents:
        return CompatibilityDecision(CompatibilityAction.INTERRUPT)
    return CompatibilityDecision(CompatibilityAction.INTERRUPT)


def evaluate_unit_mismatch(
    command: ParsedCommand,
    state: ConversationState,
    interrupt_intents: frozenset[Intent],
) -> CompatibilityDecision:
    """Разрешает только подтверждённые ответы на unit mismatch."""
    item = state.current_item()
    if item is None or item.status is not ItemStatus.UNIT_MISMATCH:
        return CompatibilityDecision(CompatibilityAction.NOT_APPLICABLE)

    if command.intent is Intent.ADD_ITEMS:
        if has_named_product_items(command, command.text):
            return CompatibilityDecision(CompatibilityAction.INTERRUPT)
        return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)

    if command.intent is Intent.REMOVE_ITEM:
        if has_matching_cart_target(command, state):
            return CompatibilityDecision(CompatibilityAction.INTERRUPT)
        return CompatibilityDecision(CompatibilityAction.CONTINUE)

    if command.intent is Intent.EDIT_QUANTITY:
        target = normalize_text(command.target_query)
        if not target or target_matches_item(target, item):
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
        # Контекстное восстановление ещё может доказать, что это количество
        # или ответ единицей каталога до повторной оценки policy.
        return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)
    if command.intent is Intent.SELECT_CANDIDATE:
        # Число не должно открывать выбор кандидата в карточке единицы.
        return CompatibilityDecision(CompatibilityAction.AMBIGUOUS)
    if command.intent in interrupt_intents:
        return CompatibilityDecision(CompatibilityAction.INTERRUPT)
    return CompatibilityDecision(CompatibilityAction.INTERRUPT)


def target_matches_item(target: str, item: CartItem) -> bool:
    """Проверяет, что структурированный target указывает на текущий item."""
    return any(
        candidate and (target in candidate or candidate in target)
        for candidate in (
            normalize_text(item.source_query),
            normalize_text(item.catalog_name),
        )
    )


def has_matching_cart_target(command: ParsedCommand, state: ConversationState) -> bool:
    """Проверяет, указывает ли REMOVE_ITEM на реальную активную строку корзины."""
    target = normalize_text(command.target_query)
    if not target:
        return False
    return any(
        target in candidate or candidate in target
        for item in state.cart
        if item.status is not ItemStatus.SKIPPED
        for candidate in (
            normalize_text(item.source_query),
            normalize_text(item.catalog_name),
        )
        if candidate
    )


def has_product_add_description(command: ParsedCommand) -> bool:
    """Проверяет наличие структурированного или свободного описания товара."""
    return bool(
        command.text.strip()
        or any(item.product_query.strip() or item.source_line.strip() for item in command.items)
    )


def has_concrete_new_items(command: ParsedCommand) -> bool:
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


def has_named_product_items(command: ParsedCommand, text: str) -> bool:
    """Отличает товарный запрос от короткого ответа количеством."""
    if command.intent != Intent.ADD_ITEMS or not command.items:
        return False
    normalized_text = normalize_text(text or command.text)
    response_word_stems = (
        "давай",
        "дава",
        "добав",
        "добавля",
        "закаж",
        "объедин",
        "измен",
        "исправ",
        "колич",
        "введ",
        "хоч",
        "лучш",
        "мне",
        "надо",
        "нуж",
        "ну",
        "ладн",
        "потом",
        "покаж",
        "помен",
        "прибав",
        "плюс",
        "постав",
        "привез",
        "пусть",
        "сдел",
        "счит",
        "слож",
        "сумм",
        "укаж",
        "уточн",
        "вес",
        "возьм",
        "конечн",
        "окей",
        "единиц",
        "кил",
        "грам",
        "литр",
        "миллил",
    )
    for item in command.items:
        query = normalize_text(item.product_query)
        if not query:
            continue
        query_words = re.findall(r"[a-zа-яё]+", query, flags=re.I)
        if not query_words:
            continue
        if query == normalized_text and any(
            word.startswith(response_word_stems)
            or word in {"товар", "товары", "заявка", "заказ", "вариант"}
            for word in query_words
        ):
            continue
        if any(not word.startswith(response_word_stems) for word in query_words):
            return True
    return False
