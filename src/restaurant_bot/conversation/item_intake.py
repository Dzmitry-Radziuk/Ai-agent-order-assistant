"""Создание позиции черновика из результата разбора товара."""

from __future__ import annotations

from uuid import uuid4

from restaurant_bot.conversation.comments import (
    merge_comments,
    normalized_comment_fragments,
    remove_global_comment_overlap,
)
from restaurant_bot.domain.departments import normalize_department
from restaurant_bot.domain.models import CartItem, CommentSource, ExtractedItem
from restaurant_bot.domain.units import normalize_unit
from restaurant_bot.parsing.products import parse_product_lines
from restaurant_bot.parsing.quantities import has_explicit_order_quantity


def build_cart_item(
    extracted: ExtractedItem,
    *,
    default_department: str,
    global_comment: str = "",
) -> CartItem:
    """Создаёт позицию черновика с сохранением доказательств количества и комментария."""
    item_comment = extracted.comment or extracted.user_comment_to_supplier
    item_comment = remove_global_comment_overlap(item_comment, global_comment)
    comment_source = extracted.comment_source if item_comment else CommentSource.NONE
    if global_comment:
        comment_source = CommentSource.SEMANTIC
    quantity = extracted.quantity
    unit = normalize_unit(extracted.unit)
    quantity_source_text = extracted.source_span or extracted.source_line
    if quantity is not None and not extracted.quantity_source and quantity_source_text:
        source_items = parse_product_lines(quantity_source_text)
        if (
            len(source_items) == 1
            and source_items[0].quantity is None
            and not has_explicit_order_quantity(quantity_source_text, quantity)
        ):
            # Защищает от ошибочного количества из диапазона размера или фасовки.
            quantity = None
            unit = ""
    return CartItem(
        id=uuid4().hex[:12],
        source_query=extracted.product_query,
        source_line=extracted.source_line,
        source_span=extracted.source_span,
        quantity_source=extracted.quantity_source,
        order_entry_type=extracted.order_entry_type,
        packaging_text=extracted.packaging_text,
        packaging_role=extracted.packaging_role,
        packaging_confidence=extracted.packaging_confidence,
        catalog_identity_provenance=extracted.catalog_identity_provenance,
        photo_sheet_row_number=extracted.photo_sheet_row_number,
        photo_sheet_row_number_confidence=extracted.photo_sheet_row_number_confidence,
        photo_sheet_row_number_authoritative=extracted.photo_sheet_row_number_authoritative,
        quantity=quantity,
        unit=unit,
        department=normalize_department(extracted.department) or default_department,
        department_quantities=extracted.department_quantities.model_copy(deep=True),
        supplier_hint=extracted.supplier_hint,
        comment=merge_comments(item_comment, global_comment),
        order_comment_fragments=normalized_comment_fragments(global_comment),
        comment_source=comment_source,
    )
