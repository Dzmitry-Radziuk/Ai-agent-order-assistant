"""Описывает неизменяемые факты и ссылки на позиции исходного сообщения."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SemanticFactKind(StrEnum):
    """Перечисляет роли, которые может иметь фрагмент исходного текста."""

    PRODUCT = "product"
    CATALOG_ATTRIBUTE = "catalog_attribute"
    ORDER_QUANTITY = "order_quantity"
    COMMENT = "comment"
    SEPARATOR = "separator"
    COMMAND_TEXT = "command_text"


@dataclass(frozen=True, slots=True)
class SemanticFact:
    """Хранит роль, границы и происхождение одного текстового факта."""

    kind: SemanticFactKind
    start: int
    end: int
    original_text: str
    normalized_value: str
    confidence: float
    provenance: str


@dataclass(frozen=True, slots=True)
class SemanticItemReference:
    """Связывает извлечённую позицию с её устойчивым фрагментом источника."""

    item_index: int
    source_text: str
    anchor_text: str
    anchor_tokens: frozenset[str]
