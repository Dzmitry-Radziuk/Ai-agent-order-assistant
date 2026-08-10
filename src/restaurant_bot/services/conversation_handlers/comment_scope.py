"""Применяет подтверждённую область ожидающего комментария."""

from __future__ import annotations

from dataclasses import dataclass

from restaurant_bot.domain.models import (
    CartItem,
    CommentSource,
    ConversationState,
    EngineResult,
    ExtractedItem,
    Intent,
    ItemStatus,
    ParsedCommand,
    SessionStage,
)
from restaurant_bot.services.comment_policy import comment_semantic_key
from restaurant_bot.services.replies import cart_reply, comment_scope_clarification_reply


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
            self.clear_pending(state)
            return CommentScopeOutcome(reprocess_command=command)
        if (
            command.intent in {Intent.BACK, Intent.CANCEL}
            or command.comment_scope_action == "cancel"
        ):
            self.clear_pending(state)
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
            global_comment = self.merge_comments(global_comment, comment)
        else:
            for index in valid_indexes:
                if index < existing_count:
                    existing_item = existing_items[index]
                    existing_item.comment = self.merge_comments(existing_item.comment, comment)
                    existing_item.comment_source = CommentSource.SEMANTIC
                    continue
                pending_index = index - existing_count
                merged = self.merge_comments(
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
        self.clear_pending(state)
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

    @staticmethod
    def clear_pending(state: ConversationState) -> None:
        """Удаляет временные данные выбора области комментария."""
        state.pending_comment_items = []
        state.pending_comment_existing_item_ids = []
        state.pending_comment_text = ""
        state.pending_comment_global_comment = ""

    @staticmethod
    def merge_comments(*values: str) -> str:
        """Объединяет части комментария без повторов."""
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            for part in str(value or "").split(";"):
                cleaned = part.strip(" .,;")
                key = comment_semantic_key(cleaned)
                if cleaned and key not in seen:
                    result.append(cleaned)
                    seen.add(key)
        return "; ".join(result)


def comment_scope_existing_items(state: ConversationState) -> list[CartItem]:
    """Возвращает сохранённые позиции черновика в порядке показа уточнения."""
    by_id = {item.id: item for item in state.cart if item.status is not ItemStatus.SKIPPED}
    return [
        by_id[item_id]
        for item_id in state.pending_comment_existing_item_ids
        if item_id in by_id
    ]


def comment_scope_items(state: ConversationState) -> list[ExtractedItem]:
    """Объединяет позиции черновика и новые позиции для выбора области комментария."""
    existing = [
        ExtractedItem(
            product_query=item.catalog_name or item.source_query,
            quantity=item.quantity,
            unit=item.unit or item.catalog_unit,
            comment=item.comment,
            user_comment_to_supplier=item.comment,
            comment_source=item.comment_source,
            source_line=item.source_line,
        )
        for item in comment_scope_existing_items(state)
    ]
    return existing + [item.model_copy(deep=True) for item in state.pending_comment_items]
