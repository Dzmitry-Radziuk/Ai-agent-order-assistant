"""Определяет нейтральные контракты вопросов и ответов по истории заявок."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class HistoryQuestionType(StrEnum):
    """Перечисляет поддержанные виды вопросов по поставке."""

    DELIVERY_DATE = "delivery_date"
    DELIVERY_ON_DATE = "delivery_on_date"
    CURRENT_STATUS = "current_status"
    ARRIVAL_STATUS = "arrival_status"
    ACTIVE_DELIVERY = "active_delivery"
    PAST_DELIVERY = "past_delivery"
    VENUE_DELIVERIES = "venue_deliveries"


class HistoryTemporalScope(StrEnum):
    """Описывает временную область поиска истории."""

    ACTIVE = "active"
    PAST = "past"
    ALL_RELEVANT = "all_relevant"


class HistoryDateReference(StrEnum):
    """Хранит относительную ссылку на дату без привязки к текущим часам."""

    NONE = "none"
    TODAY = "today"
    TOMORROW = "tomorrow"
    EXPLICIT = "explicit"


class HistoryStatusClass(StrEnum):
    """Классифицирует известное или неизвестное значение стадии истории."""

    ACTIVE = "active"
    DELIVERED = "delivered"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class HistoryDeliveryDateRelation(StrEnum):
    """Описывает отношение даты поставки к бизнес-дате ответа."""

    MISSING = "missing"
    TODAY = "today"
    FUTURE = "future"
    PAST = "past"


class HistoryQuery(BaseModel):
    """Описывает структурированный вопрос пользователя о поставке."""

    product_queries: list[str] = Field(default_factory=list)
    question_type: HistoryQuestionType
    temporal_scope: HistoryTemporalScope = HistoryTemporalScope.ACTIVE
    date_reference: HistoryDateReference = HistoryDateReference.NONE
    explicit_date: date | None = None
    original_text: str = ""
    actor_specific: bool = False

    @model_validator(mode="after")
    def validate_product_scope(self) -> HistoryQuery:
        """Разрешает пустой список товаров только для venue-level вопроса."""
        has_product = any(query.strip() for query in self.product_queries)
        if self.question_type is not HistoryQuestionType.VENUE_DELIVERIES and not has_product:
            raise ValueError("Для товарного вопроса нужен хотя бы один товар")
        return self


class HistoryRow(BaseModel):
    """Хранит доказательства одной строки листа «История»."""

    order_number: str = ""
    venue_name: str = ""
    supplier: str = ""
    stage: str = ""
    delivery_date: date | None = None
    delivery_date_text: str = ""
    created_at: datetime | None = None
    created_at_text: str = ""
    product_list: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)


class HistoryProductEntry(BaseModel):
    """Описывает товар из списка одной строки истории."""

    product_name: str
    order_number: str = ""
    venue_name: str = ""
    supplier: str = ""
    stage: str = ""
    delivery_date: date | None = None
    delivery_date_text: str = ""
    created_at: datetime | None = None
    created_at_text: str = ""
    row: HistoryRow


class HistoryMatch(BaseModel):
    """Связывает запрос с товаром и сохраняет score поиска."""

    entry: HistoryProductEntry
    query: str = ""
    score: float = Field(ge=0)
    matched_tokens: list[str] = Field(default_factory=list)
    delivery_date_relation: HistoryDeliveryDateRelation = HistoryDeliveryDateRelation.MISSING


class HistoryAnswerKind(StrEnum):
    """Перечисляет безопасные исходы history use case."""

    RESULTS = "results"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"
    NO_ACTIVE = "no_active"
    EMPTY = "empty"


class HistoryAnswer(BaseModel):
    """Содержит нейтральный результат проверки истории."""

    kind: HistoryAnswerKind
    query: HistoryQuery
    matches: list[HistoryMatch] = Field(default_factory=list)
    alternatives: list[HistoryMatch] = Field(default_factory=list)
    history_row_count: int = 0
    expanded_product_count: int = 0
    target_date: date | None = None
    active_without_delivery_date_count: int = 0
    additional_match_count: int = 0
