"""Содержит схемы структурированного ответа OpenAI."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from restaurant_bot.domain.history import HistoryQuery
from restaurant_bot.domain.models import ExtractedItem, Intent


class CommentBindingSchema(BaseModel):
    """Описывает комментарий и товары, к которым он относится."""

    text: str = ""
    scope: Literal["item", "group", "order", "ambiguous"] = "ambiguous"
    target_item_indexes: list[int] = Field(default_factory=list)
    confidence: float = Field(default=0, ge=0, le=1)


class ParsedInputSchema(BaseModel):
    """Проверяет структурированный результат извлечения товаров."""

    intent: Intent = Intent.UNKNOWN
    text: str = ""
    items: list[ExtractedItem] = Field(default_factory=list)
    target_query: str = ""
    target_queries: list[str] = Field(default_factory=list)
    selected_index: int | None = None
    selection_query: str = ""
    edit_quantity: float | None = None
    edit_unit: str = ""
    global_comment: str = ""
    comment_target_query: str = ""
    comment_text: str = ""
    comment_action: Literal["add", "remove", "clear_all"] = "add"
    comment_scope: Literal["item", "order"] = "item"
    comment_bindings: list[CommentBindingSchema] = Field(default_factory=list)
    document_type: str = ""
    history_query: HistoryQuery | None = None


class PhotoRowObservation(BaseModel):
    """Описывает только наблюдение одной визуальной строки фотографии."""

    row_index: int = Field(default=0, ge=0)
    visual_row_index: int | None = Field(default=None, ge=0)
    sheet_row_number: int | None = Field(default=None, ge=1)
    sheet_row_number_confidence: float = Field(default=0, ge=0, le=1)
    row_text: str = ""
    product_text: str = ""
    supplier_hint: str = ""
    hall_quantity: float | None = None
    bar_quantity: float | None = None
    kitchen_quantity: float | None = None
    explicit_order_quantity: float | None = None
    explicit_order_unit: str = ""
    order_entry_text: str = ""
    order_entry_type: Literal[
        "",
        "typed",
        "typed_order_entry",
        "handwritten",
        "handwritten_correction",
    ] = ""
    printed_reference_text: str = ""
    handwritten_quantity_text: str = ""
    crossed_out_quantity_text: str = ""
    corrected_quantity_text: str = ""
    active_quantity_texts: list[str] = Field(default_factory=list)
    comment_text: str = ""
    comment_source: Literal["", "explicit_marker", "user_note", "ambiguous"] = ""
    product_confidence: float = Field(default=1, ge=0, le=1)
    quantity_confidence: float = Field(default=1, ge=0, le=1)
    row_alignment_confidence: float = Field(default=1, ge=0, le=1)


class PhotoDocumentObservation(BaseModel):
    """Описывает структурированное визуальное наблюдение без мутации домена."""

    document_type_proposal: str = ""
    detected_columns: list[str] = Field(default_factory=list)
    has_table_structure: bool = False
    rows: list[PhotoRowObservation] = Field(default_factory=list)
    visible_product_row_count: int | None = Field(default=None, ge=0)
    order_area_complete: bool = True
    uncertain_order_row_count: int = Field(default=0, ge=0)
    scan_complete: bool = True
    scan_warning: str = ""
    document_comment: str = ""
    document_comment_scope: Literal["", "order", "item", "unknown"] = ""
    extraction_confidence: float = Field(default=1, ge=0, le=1)


class ProductMatchDecision(BaseModel):
    """Проверяет решение ИИ о сопоставлении товара."""

    action: str = Field(pattern="^(select|ambiguous|not_found)$")
    selected_product_id: str = ""
    candidate_product_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0, ge=0, le=1)
    contradictions: list[str] = Field(default_factory=list)
    reason: str = ""


class VisibleActionDecision(BaseModel):
    """Проверяет выбор действия среди кнопок текущего экрана."""

    action_id: str = ""
    confidence: float = Field(default=0, ge=0, le=1)
    reason: str = ""


class CommentScopeDecision(BaseModel):
    """Описывает ответ пользователя на уточнение области комментария."""

    action: Literal["items", "order", "cancel", "ambiguous"] = "ambiguous"
    target_item_indexes: list[int] = Field(default_factory=list)
    confidence: float = Field(default=0, ge=0, le=1)
    reason: str = ""
