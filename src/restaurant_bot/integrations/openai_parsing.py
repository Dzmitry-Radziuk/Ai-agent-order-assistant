"""Сохраняет обратную совместимость для AI-схем и reconciliation."""

from restaurant_bot.parsing.ai.comment_reconciliation import (
    _CONVERSATIONAL_PRODUCT_LEADIN_RE,
    _comment_scope_has_explicit_anchor,
)
from restaurant_bot.parsing.ai.item_reconciliation import (
    _repair_command_mixed_script_queries,
    remove_unsupported_query_qualifiers,
)
from restaurant_bot.parsing.ai.quantity_reconciliation import (
    _quantities_with_units,
    restore_explicit_order_terms,
)
from restaurant_bot.parsing.ai.reconciliation import recover_omitted_explicit_items
from restaurant_bot.parsing.ai.schemas import (
    CommentBindingSchema,
    CommentScopeDecision,
    ParsedInputSchema,
    ProductMatchDecision,
    VisibleActionDecision,
)
from restaurant_bot.parsing.ai.shadow_items import collapse_comment_shadow_items

__all__ = [
    "_CONVERSATIONAL_PRODUCT_LEADIN_RE",
    "CommentBindingSchema",
    "CommentScopeDecision",
    "ParsedInputSchema",
    "ProductMatchDecision",
    "VisibleActionDecision",
    "_comment_scope_has_explicit_anchor",
    "_quantities_with_units",
    "_repair_command_mixed_script_queries",
    "collapse_comment_shadow_items",
    "recover_omitted_explicit_items",
    "remove_unsupported_query_qualifiers",
    "restore_explicit_order_terms",
]
