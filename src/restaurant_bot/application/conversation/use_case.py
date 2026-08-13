"""Единая точка запуска channel-neutral обработки диалога."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from restaurant_bot.application.conversation.contracts import (
    ConversationEffectPlan,
    ConversationInput,
    ConversationResult,
    ConversationView,
    SemanticAction,
)
from restaurant_bot.domain.models import CatalogProduct, ConversationState, ParsedCommand


class _ReplyLike(Protocol):
    """Описывает минимальный ответ старого процессора."""

    text: str
    rows: Sequence[Sequence[Any]]


class _ProcessorResultLike(Protocol):
    """Описывает минимальный результат существующего engine."""

    state: ConversationState
    reply: _ReplyLike
    enqueue_submission: bool
    enqueue_order_status: bool
    enqueue_product_add: bool
    enqueue_review_submission: bool
    invalidate_catalog: bool
    order_status_page: int
    order_status_detail_page: int
    order_status_selected_index: int | None
    order_status_order_number: str


class ConversationProcessor(Protocol):
    """Определяет обработчик бизнес-сценария диалога."""

    def handle(
        self,
        interaction: Any,
        command: ParsedCommand,
        state: ConversationState,
        catalog: list[CatalogProduct],
    ) -> _ProcessorResultLike:
        """Обрабатывает нейтральное взаимодействие."""


class ConversationApplication:
    """Координирует общий диалоговый use case для разных каналов."""

    def __init__(self, processor: ConversationProcessor) -> None:
        """Сохраняет внедрённый stateful processor."""
        self._processor = processor

    def process(
        self,
        interaction: ConversationInput,
        command: ParsedCommand,
        state: ConversationState,
        catalog: list[CatalogProduct],
    ) -> ConversationResult:
        """Обрабатывает вход и возвращает нейтральный результат."""
        result = self._processor.handle(interaction, command, state, catalog)
        actions = tuple(
            SemanticAction(
                action_id=str(getattr(button, "callback_data", "")),
                label=str(getattr(button, "text", "")),
                group=row_index,
            )
            for row_index, row in enumerate(result.reply.rows)
            for button in row
        )
        return ConversationResult(
            state=result.state,
            view=ConversationView(text=result.reply.text, actions=actions),
            effects=ConversationEffectPlan(
                enqueue_submission=result.enqueue_submission,
                enqueue_order_status=result.enqueue_order_status,
                enqueue_product_add=result.enqueue_product_add,
                enqueue_review_submission=result.enqueue_review_submission,
                invalidate_catalog=result.invalidate_catalog,
            ),
            order_status_page=result.order_status_page,
            order_status_detail_page=result.order_status_detail_page,
            order_status_selected_index=result.order_status_selected_index,
            order_status_order_number=result.order_status_order_number,
        )
