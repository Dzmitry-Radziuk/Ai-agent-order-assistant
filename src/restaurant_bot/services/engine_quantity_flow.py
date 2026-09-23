"""Владеет модальным потоком уточнения количества товара."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from restaurant_bot.conversation.quantity_resolution import (
    is_multiple_warning,
    multiple_warnings,
    select_multiple_warning,
    suggested_quantity_for_multiple,
)
from restaurant_bot.conversation.selection import find_cart_item_candidates
from restaurant_bot.conversation.state.queries import item_index as state_item_index
from restaurant_bot.domain.models import (
    BotReply,
    Button,
    CartItem,
    ConversationState,
    EngineResult,
    ItemStatus,
    ParsedCommand,
    SessionStage,
)
from restaurant_bot.domain.text import normalize_text
from restaurant_bot.domain.unit_conversion import convert_quantity
from restaurant_bot.domain.units import normalize_unit
from restaurant_bot.presentation.telegram.replies import (
    multiple_quantity_choice_reply,
    review_or_department_reply,
)

if TYPE_CHECKING:
    from restaurant_bot.services.engine import ConversationEngine


class EngineQuantityFlowService:
    """Изолирует изменение количества, не меняя порядок переходов движка."""

    def __init__(
        self,
        owner: ConversationEngine,
        *,
        cart_renderer: Any,
    ) -> None:
        """Подключает поток к каноническому движку переходов."""
        self._owner = owner
        self._cart_renderer = cart_renderer

    def use_catalog_unit(self, state: ConversationState) -> EngineResult:
        """Принимает единицу измерения из каталога."""
        item = state.current_item()
        if item is None:
            return EngineResult(state=state, reply=self._cart_renderer(state))
        if item.catalog_unit:
            converted = convert_quantity(item.quantity or 0, item.unit, item.catalog_unit)
            if converted is not None:
                item.quantity = converted
            item.unit = item.catalog_unit
        item.status = ItemStatus.MATCHED if item.quantity else ItemStatus.MISSING_QTY
        return self._owner._advance(state)

    def accept_suggested_quantity(self, state: ConversationState) -> EngineResult:
        """Принимает рекомендуемое количество товара."""
        item = state.current_item()
        if item is None:
            return EngineResult(state=state, reply=self._cart_renderer(state))
        if item.suggested_quantity is not None:
            previous_quantity = item.quantity
            item.quantity = item.suggested_quantity
            item.quantity_user_edited = True
            state.refresh_department_after_quantity_change(
                item,
                previous_quantity=previous_quantity,
            )
        item.status = ItemStatus.MATCHED if item.quantity else ItemStatus.MISSING_QTY
        return self._owner._advance_multiple_quantity_choice(state)

    def keep_current_quantity(self, state: ConversationState) -> EngineResult:
        """Сохраняет текущее количество позиции."""
        item = state.current_item()
        if item is None:
            return EngineResult(state=state, reply=self._cart_renderer(state))
        item.suggested_quantity = None
        item.status = ItemStatus.MATCHED if item.quantity else ItemStatus.MISSING_QTY
        return self._owner._advance_multiple_quantity_choice(state)

    def enter_other_quantity(self, state: ConversationState) -> EngineResult:
        """Переводит диалог к ручному вводу количества."""
        item = state.current_item()
        if item is None:
            return EngineResult(state=state, reply=self._cart_renderer(state))
        state.stage = (
            SessionStage.AWAIT_UNIT_QUANTITY
            if item.status == ItemStatus.UNIT_MISMATCH
            else SessionStage.AWAIT_MULTIPLE_QUANTITY
        )
        expected_unit = item.catalog_unit or item.unit
        if item.status == ItemStatus.UNIT_MISMATCH and expected_unit:
            prompt = (
                f"Укажите количество в {expected_unit} одним сообщением. "
                f"Например: 5 {expected_unit}."
            )
        else:
            example = f"5 {expected_unit}".strip()
            prompt = f"Укажите другое количество одним сообщением. Например: {example}."
        return EngineResult(
            state=state,
            reply=BotReply(
                text=prompt,
                rows=[
                    [Button(text="Вернуться к вариантам", callback_data="v2:resolve")],
                    [
                        Button(
                            text="Не добавлять",
                            callback_data=f"v2:skip:{state_item_index(state, item)}",
                        )
                    ],
                ],
            ),
        )

    @staticmethod
    def select_multiple_warning_for_callback(
        command: ParsedCommand,
        state: ConversationState,
    ) -> CartItem | None:
        """Привязывает кнопку выбора количества к конкретной позиции черновика."""
        target = command.callback_target
        if target:
            item = next((item for item in state.cart if item.id == target), None)
            if item is not None and is_multiple_warning(item):
                state.current_issue_item_id = item.id
                return item
        return select_multiple_warning(state)

    def show_multiple_quantity_choice(self, state: ConversationState) -> EngineResult:
        """Открывает варианты количества без автоматического изменения."""
        item = select_multiple_warning(state)
        if item is None:
            state.current_issue_item_id = ""
            return EngineResult(state=state, reply=review_or_department_reply(state))
        state.stage = SessionStage.AWAIT_SUBMIT_CONFIRM
        state.status = "await_multiple_choice"
        return EngineResult(state=state, reply=multiple_quantity_choice_reply(item))

    def advance_multiple_quantity_choice(self, state: ConversationState) -> EngineResult:
        """Показывает следующий выбор количества или финальную проверку."""
        state.current_issue_item_id = ""
        next_item = select_multiple_warning(state)
        if next_item is not None:
            state.stage = SessionStage.AWAIT_SUBMIT_CONFIRM
            state.status = "await_multiple_choice"
            return EngineResult(state=state, reply=multiple_quantity_choice_reply(next_item))
        state.stage = SessionStage.AWAIT_SUBMIT_CONFIRM
        state.status = "await_submit_confirm"
        return EngineResult(state=state, reply=review_or_department_reply(state))

    def edit_quantity(self, command: ParsedCommand, state: ConversationState) -> EngineResult:
        """Изменяет количество выбранной позиции."""
        if command.edit_quantity is None:
            return EngineResult(state=state, reply=BotReply(text="Укажите новое количество."))
        target = normalize_text(command.target_query)
        item = state.current_item()
        if target:
            matches = find_cart_item_candidates(state, command.target_query)
            if len(matches) > 1:
                names = tuple(
                    dict.fromkeys(row.catalog_name or row.source_query for row in matches)
                )
                choices = "\n".join(f"• {name}" for name in names[:5])
                if len(names) > 5:
                    choices += f"\n• и ещё {len(names) - 5}"
                return EngineResult(
                    state=state,
                    reply=BotReply(
                        text=(
                            "Нашёл несколько похожих позиций. Уточните, у какого товара "
                            "изменить количество:\n"
                            f"{choices}"
                        )
                    ),
                )
            item = matches[0] if matches else None
        if item is None:
            return EngineResult(
                state=state, reply=BotReply(text="Позиция для изменения не найдена.")
            )
        was_multiple_choice = item in multiple_warnings(state) and state.stage in {
            SessionStage.AWAIT_SUBMIT_CONFIRM,
            SessionStage.AWAIT_MULTIPLE_QUANTITY,
        }
        quantity = command.edit_quantity
        if (
            command.edit_unit
            and item.catalog_unit
            and normalize_unit(command.edit_unit) != normalize_unit(item.catalog_unit)
        ):
            previous_quantity = item.quantity
            item.quantity = quantity
            item.quantity_user_edited = True
            state.refresh_department_after_quantity_change(
                item,
                previous_quantity=previous_quantity,
            )
            item.unit = normalize_unit(command.edit_unit)
            item.status = ItemStatus.UNIT_MISMATCH
            return self._owner._advance(state)
        previous_quantity = item.quantity
        item.quantity = quantity
        item.quantity_user_edited = True
        state.refresh_department_after_quantity_change(
            item,
            previous_quantity=previous_quantity,
        )
        item.unit = item.catalog_unit or command.edit_unit or item.unit
        if item.catalog_product_id:
            item.status = ItemStatus.MATCHED
            item.suggested_quantity = suggested_quantity_for_multiple(item)
        if was_multiple_choice:
            return self._owner._advance_multiple_quantity_choice(state)
        return self._owner._advance(state)
