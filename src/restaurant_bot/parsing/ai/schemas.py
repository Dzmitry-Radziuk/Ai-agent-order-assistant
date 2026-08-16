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
