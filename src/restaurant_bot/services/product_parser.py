"""Совместимый фасад детерминированного разбора товарных строк."""

from restaurant_bot.parsing.products import (
    _extract_global_comment,
    has_explicit_global_comment_scope,
    parse_product_lines,
    parse_quantity_unit,
)

__all__ = [
    "_extract_global_comment",
    "has_explicit_global_comment_scope",
    "parse_product_lines",
    "parse_quantity_unit",
]
