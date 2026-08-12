"""Применяет подтверждённую область ожидающего комментария."""

from __future__ import annotations

from dataclasses import dataclass

from restaurant_bot.conversation.comments import (
    clear_pending_comment,
    comment_scope_existing_items,
    comment_scope_items,
    merge_scope_comments,
)
from restaurant_bot.domain.models import (
    CommentSource,
    ConversationState,
    EngineResult,
    Intent,
    ItemStatus,
    ParsedCommand,
    SessionStage,
)
from restaurant_bot.presentation.telegram.replies import (
    cart_reply,
    comment_scope_clarification_reply,
)


@dataclass(frozen=True, slots=True)
class CommentScopeOutcome:
    """Возвращает ответ либо команду для повторного входа в engine."""

    result: EngineResult | None = None
    reprocess_command: ParsedCommand | None = None
    clear_event_text: bool = False


class CommentScopeHandler:
    """Обрабатывает только состояние ожидающего выбора области комментария."""

    def handle(
        self,
        command: ParsedCommand,
        state: ConversationState,
    ) -> CommentScopeOutcome:
        """Проверяет confidence и применяет комментарий к выбранной области."""
        if command.intent in {Intent.CLEAR_CART, Intent.START_NEW_ORDER}:
            clear_pending_comment(state)
            return CommentScopeOutcome(reprocess_command=command)
        if (
            command.intent in {Intent.BACK, Intent.CANCEL}
            or command.comment_scope_action == "cancel"
        ):
            clear_pending_comment(state)
            state.stage = (
                SessionStage.REVIEW
                if any(item.status != ItemStatus.SKIPPED for item in state.cart)
                else SessionStage.COLLECTING
            )
            state.status = state.stage.value
            return CommentScopeOutcome(
                result=EngineResult(
                    state=state,
                    reply=cart_reply(state, title="Комментарий не добавлен"),
                )
            )

        action = command.comment_scope_action
        scope_items = comment_scope_items(state)
        existing_items = comment_scope_existing_items(state)
        existing_count = len(existing_items)
        valid_indexes = list(
            dict.fromkeys(
                index
                for index in command.comment_target_indexes
                if isinstance(index, int)
                and not isinstance(index, bool)
                and 0 <= index < len(scope_items)
            )
        )
        valid_resolution = bool(
            (command.confidence or 0.0) >= 0.9
            and (
                (
                    action == "items"
                    and valid_indexes
                    and valid_indexes == command.comment_target_indexes
                )
                or (action == "order" and not command.comment_target_indexes)
            )
        )
        if not valid_resolution:
            state.stage = SessionStage.AWAIT_COMMENT_SCOPE
            state.status = "await_comment_scope"
            return CommentScopeOutcome(
                result=EngineResult(
                    state=state,
                    reply=comment_scope_clarification_reply(
                        scope_items,
                        state.pending_comment_text,
                    ),
                )
            )

        pending_items = [item.model_copy(deep=True) for item in state.pending_comment_items]
        comment = state.pending_comment_text
        global_comment = state.pending_comment_global_comment
        if action == "order":
            global_comment = merge_scope_comments(global_comment, comment)
        else:
            for index in valid_indexes:
                if index < existing_count:
                    existing_item = existing_items[index]
                    existing_item.comment = merge_scope_comments(existing_item.comment, comment)
                    existing_item.comment_source = CommentSource.SEMANTIC
                    continue
                pending_index = index - existing_count
                merged = merge_scope_comments(
                    pending_items[pending_index].comment,
                    pending_items[pending_index].user_comment_to_supplier,
                    comment,
                )
                pending_items[pending_index] = pending_items[pending_index].model_copy(
                    update={
                        "comment": merged,
                        "user_comment_to_supplier": merged,
                        "comment_source": CommentSource.SEMANTIC,
                    }
                )
        clear_pending_comment(state)
        state.stage = SessionStage.COLLECTING
        state.status = "collecting"
        return CommentScopeOutcome(
            reprocess_command=ParsedCommand(
                intent=Intent.ADD_ITEMS,
                items=pending_items,
                global_comment=global_comment,
            ),
            clear_event_text=True,
        )
