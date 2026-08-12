"""Разбирает callback-данные Telegram в ParsedCommand."""

from __future__ import annotations

import re

from restaurant_bot.domain.models import Intent, ParsedCommand


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
