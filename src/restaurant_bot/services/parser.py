"""Сохраняет публичный контракт parser и путь совместимости callback."""

from __future__ import annotations

import re

from restaurant_bot.domain.models import Intent, ParsedCommand
from restaurant_bot.parsing.commands.dialogue import (
    _standalone_quantity_hint,
    dialogue_response_for,
    retry_requested_for,
)
from restaurant_bot.parsing.commands.item_commands import (
    clean_command_target,
    has_explicit_add_items,
    is_product_add_request_phrase,
)
from restaurant_bot.parsing.commands.normalization import (
    has_negated_action,
    has_negation,
    is_explicit_item_rejection,
    normalize_command_text,
)
from restaurant_bot.parsing.commands.router import parse_text_command
from restaurant_bot.parsing.comment_scope import has_explicit_global_comment_scope
from restaurant_bot.parsing.products import parse_product_lines
from restaurant_bot.parsing.quantities import parse_quantity_unit

__all__ = [
    "clean_command_target",
    "dialogue_response_for",
    "has_explicit_add_items",
    "has_explicit_global_comment_scope",
    "has_negated_action",
    "has_negation",
    "infer_intent",
    "is_explicit_item_rejection",
    "is_product_add_request_phrase",
    "normalize_command_text",
    "parse_callback",
    "parse_product_lines",
    "parse_quantity_unit",
    "retry_requested_for",
]


def infer_intent(text: str, callback_data: str = "") -> ParsedCommand:
    """Определяет intent через text или callback path и добавляет метаданные."""
    command = parse_callback(callback_data) if callback_data else parse_text_command(text)
    quantity_hint, quantity_hint_unit = _standalone_quantity_hint(text)
    return command.model_copy(
        update={
            "quantity_hint": quantity_hint,
            "quantity_hint_unit": quantity_hint_unit,
            "retry_requested": retry_requested_for(text),
            "dialogue_response": dialogue_response_for(
                text or command.text,
                command.intent,
                command.items,
            ),
        }
    )


def parse_callback(data: str) -> ParsedCommand:
    """Разбирает данные нажатой кнопки."""
    parts = data.split(":")
    if parts[0] == "v2":
        parts = parts[1:]
    revision = None
    if parts and re.fullmatch(r"r\d+", parts[-1], re.I):
        revision = int(parts.pop()[1:])
    action = parts[0] if parts else ""
    rest = parts[1:]
    mapping = {
        # Метки намеренно не выводятся из их названий. Эти
        # значения являются callback-контрактом Engine v2.1 Prepare в n8n.
        # `cart` открывает финальную проверку; `back` показывает черновик.
        "cart": Intent.SHOW_FINAL_REVIEW,
        "new": Intent.START_NEW_ORDER,
        "submit": Intent.SUBMIT_AS_IS,
        "clear": Intent.CLEAR_CART,
        "cancel": Intent.CANCEL,
        "back": Intent.BACK,
        "skip": Intent.SKIP_CURRENT,
        "rename": Intent.MANUAL_CURRENT,
        "manual": Intent.MANUAL_CURRENT,
        "orders": Intent.ORDER_STATUS,
        "help": Intent.HELP,
        "check_min": Intent.CHECK_MIN_SUM,
        "resolve": Intent.CONTINUE_CURRENT,
        "final_review": Intent.SHOW_FINAL_REVIEW,
        "review": Intent.REVIEW_REFRESH,
        "review_submit": Intent.REVIEW_SUBMIT,
        "review_cancel": Intent.REVIEW_CANCEL,
        "add": Intent.ADD_MORE,
        "addreq": Intent.PRODUCT_ADD,
        "addreqlist": Intent.PRODUCT_ADD_LIST,
        "addreqretry": Intent.PRODUCT_ADD_RETRY,
        "addreqskip": Intent.PRODUCT_ADD_SKIP,
        "searchall": Intent.SEARCH_ALL_SUPPLIERS,
        "switchsupplier": Intent.SWITCH_SUPPLIER,
        "keepmul": Intent.KEEP_MULTIPLE,
        "keepwarn": Intent.KEEP_MULTIPLE,
        "mulone": Intent.FIX_MULTIPLE,
        "minsum": Intent.CHECK_MIN_SUM,
        "minsumadd": Intent.ADD_SUPPLIER_ITEMS,
        "minsumchoose": Intent.CHOOSE_SUPPLIER_WARNING,
        "fixmul": Intent.FIX_MULTIPLE,
        "editmul": Intent.EDIT_MULTIPLE,
        "unitedit": Intent.UNIT_EDIT,
        "unitok": Intent.UNIT_OK,
        "use_catalog_unit": Intent.USE_CATALOG_UNIT,
        "confirm": Intent.CONFIRM,
        "keep_current": Intent.KEEP_CURRENT_QUANTITY,
        "accept_multiple": Intent.ACCEPT_SUGGESTED_QUANTITY,
        "enter_quantity": Intent.ENTER_OTHER_QUANTITY,
        "dupmerge": Intent.MERGE_DUPLICATE,
    }
    if action in {"select", "sel"} and rest:
        try:
            selected = int(rest[-1])
            if action == "sel":
                selected += 1
            return ParsedCommand(
                intent=Intent.SELECT_CANDIDATE,
                selected_index=selected,
                callback_revision=revision,
                callback_target=rest[0],
            )
        except ValueError:
            return ParsedCommand(
                intent=Intent.SELECT_CANDIDATE,
                selection_query=rest[-1],
                callback_revision=revision,
                callback_target=rest[0],
            )
    if action == "remove" and rest:
        return ParsedCommand(
            intent=Intent.REMOVE_ITEM,
            target_query=rest[-1],
            callback_revision=revision,
            callback_target=rest[0],
        )
    if action == "qty" and len(rest) >= 2:
        try:
            quantity = float(rest[-1])
        except ValueError:
            quantity = None
        return ParsedCommand(
            intent=Intent.EDIT_QUANTITY,
            target_query=rest[0],
            edit_quantity=quantity,
            callback_revision=revision,
        )
    if action == "order" and rest:
        try:
            selected_index = int(rest[-1])
        except ValueError:
            selected_index = None
        return ParsedCommand(
            intent=Intent.ORDER_STATUS,
            text=data,
            selected_index=selected_index,
            selection_query="" if selected_index is not None else rest[-1],
            callback_revision=revision,
        )
    if action == "orderitems" and len(rest) >= 2:
        try:
            selected_index = int(rest[0])
        except ValueError:
            selected_index = None
        try:
            detail_page = max(0, int(rest[1]))
        except ValueError:
            detail_page = 0
        return ParsedCommand(
            intent=Intent.ORDER_STATUS,
            text=data,
            selected_index=selected_index,
            selection_query="" if selected_index is not None else rest[0],
            callback_target=f"detail:{detail_page}",
            order_status_detail_page=detail_page,
            callback_revision=revision,
        )
    if action == "orderspage" and rest:
        try:
            page = max(0, int(rest[-1]))
        except ValueError:
            page = 0
        return ParsedCommand(
            intent=Intent.ORDER_STATUS,
            text=data,
            callback_target=f"page:{page}",
            callback_revision=revision,
        )
    if action == "cartpage" and rest:
        try:
            page = max(0, int(rest[0]))
        except ValueError:
            page = 0
        return ParsedCommand(
            intent=Intent.SHOW_CART,
            text=data,
            callback_target=f"page:{page}",
            callback_revision=revision,
        )
    if action == "finalpage" and rest:
        try:
            page = max(0, int(rest[0]))
        except ValueError:
            page = 0
        return ParsedCommand(
            intent=Intent.SHOW_FINAL_REVIEW,
            text=data,
            callback_target=f"page:{page}",
            callback_revision=revision,
        )
    return ParsedCommand(
        intent=mapping.get(action, Intent.UNKNOWN),
        text=data,
        callback_revision=revision,
        callback_target=rest[0] if rest else "",
    )
