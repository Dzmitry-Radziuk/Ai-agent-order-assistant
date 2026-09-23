"""Владеет подготовкой снимка заявки перед надёжной отправкой."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from math import isclose
from typing import TYPE_CHECKING, Any

from restaurant_bot.application.conversation.contracts import ConversationInteraction
from restaurant_bot.domain.departments import normalize_department
from restaurant_bot.domain.models import (
    BotReply,
    CartItem,
    ConversationState,
    EngineResult,
    ItemStatus,
    PendingSubmission,
    SessionStage,
)
from restaurant_bot.presentation.telegram.replies import submission_retry_reply
from restaurant_bot.presentation.telegram.submission import (
    submission_dispatch_uncertain_reply,
)

if TYPE_CHECKING:
    from restaurant_bot.services.engine import ConversationEngine


class EngineSubmissionPreparationService:
    """Фиксирует только локальный snapshot заявки и её строки."""

    def __init__(
        self,
        owner: ConversationEngine,
        *,
        cart_renderer: Callable[[ConversationState], BotReply],
    ) -> None:
        """Подключает настройки и канонический рендер черновика владельца."""
        self._owner = owner
        self._cart_renderer = cart_renderer

    def prepare(
        self,
        event: ConversationInteraction,
        state: ConversationState,
    ) -> EngineResult:
        """Фиксирует черновик для надёжной отправки."""
        if (
            state.pending_submission
            and state.pending_submission.failed_stage == "dispatch_uncertain"
        ):
            return EngineResult(
                state=state,
                reply=submission_dispatch_uncertain_reply(
                    state,
                    state.pending_submission.order_no,
                ),
            )
        if (
            state.pending_submission
            and state.pending_submission.order_no
            and state.pending_submission.rows
        ):
            state.pending_submission.last_attempt_at = datetime.now(UTC)
            state.pending_submission.last_error = ""
            state.stage = SessionStage.SUBMITTING
            state.status = "submitting"
            return EngineResult(
                state=state,
                reply=submission_retry_reply(state.pending_submission.order_no),
                enqueue_submission=True,
            )
        matched = [item for item in state.cart if item.status == ItemStatus.MATCHED]
        if not matched:
            return EngineResult(state=state, reply=self._cart_renderer(state))
        now = datetime.now(UTC)
        order_no = f"{now:%Y%m%d-%H%M%S}-{event.conversation_id[-4:]}"
        supplier_totals: dict[str, float] = defaultdict(float)
        for item in matched:
            supplier_totals[item.supplier] += item.amount
        rows: list[dict[str, Any]] = []
        for item in matched:
            for department, quantity in self.department_quantities(item):
                amount = quantity * item.price if item.price is not None else 0.0
                rows.append(
                    {
                        "Время создания заявки": now.isoformat(),
                        "Время изменения": now.isoformat(),
                        "ID заявки": f"{item.supplier}|{state.restaurant}|{order_no}",
                        "№ Заявки": order_no,
                        "Условное название поставщика": item.supplier,
                        "Условное наз-ие заведения": state.restaurant,
                        "Роль": department,
                        "ID товара": item.catalog_product_id,
                        "Наименование у поставщика": item.catalog_name,
                        "Ед.Изм. для заказа": item.catalog_unit,
                        "Минимальная Кратность в заказе": item.minimum_multiple or "",
                        "Полезный V, m Нетто Ед.Изм.для Заказа": item.useful_volume or "",
                        "Цена за Ед.Изм. для заказа": item.price or "",
                        "Кол-во": quantity or "",
                        "Мин сумма Заказа по Поставщику": item.supplier_minimum_amount or "",
                        "Комментарий": item.comment,
                        "Сумма по товару в заказе": amount,
                        "Сумма по заявке к поставщику": supplier_totals[item.supplier],
                        "Стадия": "Новая заявка",
                        "Стадия от Заведения": "",
                        "_department": department,
                    }
                )
        state.pending_submission = PendingSubmission(
            order_no=order_no,
            trace_id=state.order_trace_id,
            telegram_user_id=event.actor_id or event.conversation_id,
            telegram_chat_id=event.conversation_id,
            rows=rows,
            spreadsheet_id=state.spreadsheet_id,
            venue_code=state.venue_code,
        )
        state.stage = SessionStage.SUBMITTING
        progress = (
            "Отправляю заявку"
            if self._owner.settings.google_order_submission_enabled
            else "Подготавливаю заявку"
        )
        return EngineResult(
            state=state,
            reply=BotReply(text=f"{progress}..."),
            enqueue_submission=True,
        )

    @staticmethod
    def department_quantities(item: CartItem) -> list[tuple[str, float]]:
        """Возвращает количества позиции по отделам для записи в таблицу."""
        department_values = (
            ("Зал", item.department_quantities.hall),
            ("Бар", item.department_quantities.bar),
            ("Кухня", item.department_quantities.kitchen),
        )
        rows = [
            (department, value) for department, value in department_values if value and value > 0
        ]
        distributed_total = sum(value for _, value in rows)
        if rows and isclose(
            distributed_total,
            item.quantity or 0,
            rel_tol=1e-9,
            abs_tol=1e-9,
        ):
            return rows
        if item.quantity_user_edited:
            return [(normalize_department(item.department) or "Кухня", item.quantity or 0)]
        return [(normalize_department(item.department) or "Кухня", item.quantity or 0)]
