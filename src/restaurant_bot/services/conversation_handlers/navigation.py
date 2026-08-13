"""Обрабатывает пассивную навигацию без изменения товарного черновика."""

from __future__ import annotations

from collections.abc import Callable

from restaurant_bot.application.conversation.contracts import ConversationInteraction
from restaurant_bot.domain.models import (
    BotReply,
    ConversationState,
    EngineResult,
    InputKind,
    Intent,
    ParsedCommand,
)
from restaurant_bot.presentation.telegram.replies import (
    help_reply,
    small_talk_reply,
    thanks_reply,
    welcome_reply,
)
from restaurant_bot.text_normalization import normalize_text

ReplyBuilder = Callable[[ConversationState], BotReply]

_PASSIVE_REPLIES: dict[Intent, ReplyBuilder] = {
    Intent.GREETING: welcome_reply,
    Intent.HELP: help_reply,
    Intent.THANKS: thanks_reply,
    Intent.SMALL_TALK: small_talk_reply,
}


class PassiveIntentHandler:
    """Маршрутизирует простые intent по таблице ответов."""

    def handle(
        self,
        command: ParsedCommand,
        state: ConversationState,
    ) -> EngineResult | None:
        """Возвращает ответ для пассивного intent или передаёт управление дальше."""
        builder = _PASSIVE_REPLIES.get(command.intent)
        if builder is None:
            return None
        if command.intent is Intent.GREETING:
            first_contact = not bool(state.metadata.get("onboarding_shown"))
            state.metadata["onboarding_shown"] = True
            return EngineResult(
                state=state,
                reply=welcome_reply(state, first_contact=first_contact),
            )
        return EngineResult(state=state, reply=builder(state))


class OrderStatusHandler:
    """Подготавливает запрос чтения истории без прямого внешнего вызова."""

    def handle(
        self,
        event: ConversationInteraction,
        command: ParsedCommand,
        state: ConversationState,
    ) -> EngineResult | None:
        """Сохраняет выбранную страницу и возвращает флаг фонового чтения."""
        if command.intent != Intent.ORDER_STATUS:
            return None
        page = self._page(command, state)
        detail_page = self._detail_page(command, state)
        detail_requested = bool(
            command.selected_index is not None
            or command.selection_query
            or command.callback_target in {"detail_next", "detail_previous"}
            or command.callback_target.startswith("detail:")
        )
        if command.selected_index is not None:
            state.order_status_selected_index = command.selected_index
        if command.selection_query:
            state.order_status_selected_order_number = command.selection_query
        if detail_requested:
            selected_index = (
                command.selected_index
                if command.selected_index is not None
                else state.order_status_selected_index
            )
            order_number = command.selection_query or state.order_status_selected_order_number
        else:
            state.order_status_selected_index = None
            state.order_status_selected_order_number = ""
            selected_index = None
            order_number = ""
        state.order_status_view_active = True
        state.order_status_page = page
        state.order_status_detail_page = detail_page
        state.order_status_detail_active = detail_requested
        return EngineResult(
            state=state,
            reply=BotReply(
                text=(
                    "Обновляю статус заявки..."
                    if event.kind == InputKind.CALLBACK
                    else "Проверяю статусы ваших заявок..."
                )
            ),
            enqueue_order_status=True,
            order_status_page=page,
            order_status_detail_page=detail_page,
            order_status_selected_index=selected_index,
            order_status_order_number=order_number,
        )

    @staticmethod
    def _page(command: ParsedCommand, state: ConversationState) -> int:
        """Вычисляет страницу истории для списка или выбранной заявки."""
        target = command.callback_target
        if target.startswith("page:"):
            try:
                return max(0, int(target.partition(":")[2]))
            except ValueError:
                return 0
        if target == "next":
            return state.order_status_page + 1
        if target == "previous":
            return max(0, state.order_status_page - 1)
        if target == "current" or command.selected_index is not None or command.selection_query:
            return max(0, state.order_status_page)
        phrase = normalize_text(command.text)
        if state.order_status_view_active and phrase in {"обнови", "обновить", "обновить статусы"}:
            return max(0, state.order_status_page)
        return 0

    @staticmethod
    def _detail_page(command: ParsedCommand, state: ConversationState) -> int:
        """Вычисляет страницу внутри выбранной заявки."""
        target = command.callback_target
        if target.startswith("detail:"):
            try:
                return max(0, int(target.partition(":")[2]))
            except ValueError:
                return 0
        if target == "detail_next":
            return max(0, state.order_status_detail_page + 1)
        if target == "detail_previous":
            return max(0, state.order_status_detail_page - 1)
        if command.selected_index is not None or command.selection_query:
            return 0
        return max(0, state.order_status_detail_page)
