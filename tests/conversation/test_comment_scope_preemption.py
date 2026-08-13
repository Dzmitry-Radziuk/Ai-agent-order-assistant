"""Проверяет поведение, связанное с модулем «test comment scope preemption»."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from restaurant_bot.conversation.comments import comment_scope_items
from restaurant_bot.conversation.routing.contracts import (
    CompatibilityAction,
    CompatibilityContext,
)
from restaurant_bot.conversation.routing.state_compatibility import (
    StateCompatibilityPolicy,
)
from restaurant_bot.domain.models import (
    CartItem,
    ConversationState,
    ExtractedItem,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    SessionStage,
    TelegramEvent,
)
from restaurant_bot.input.telegram_interpretation import TelegramInputInterpreter
from restaurant_bot.integrations.openai_client import CommentScopeDecision
from restaurant_bot.services.engine import ConversationEngine
from restaurant_bot.services.orchestrator import UpdateOrchestrator


def _interpreter(service: UpdateOrchestrator) -> TelegramInputInterpreter:
    """Создаёт интерпретатор для проверки контекстной маршрутизации комментария."""
    return TelegramInputInterpreter(
        service.openai,
        lambda: Mock(),
        service.engine.state_compatibility_policy,
    )


def _event(text: str, input_type: InputKind = InputKind.TEXT) -> TelegramEvent:
    """Создаёт событие для проверки прерывания области комментария."""
    return TelegramEvent(
        update_id=1,
        chat_id="comment-preemption",
        input_type=input_type,
        text=text,
    )


def _pending_state() -> ConversationState:
    """Создаёт черновик с сохранённым pending comment context."""
    existing = CartItem(
        id="chicken",
        source_query="Курица",
        catalog_name="Курица",
        status=ItemStatus.MATCHED,
        quantity=2,
        unit="кг",
    )
    pending = [
        ExtractedItem(product_query="Помидоры", quantity=5, unit="кг"),
        ExtractedItem(product_query="Огурцы", quantity=4, unit="кг"),
        ExtractedItem(product_query="Перец", quantity=3, unit="кг"),
    ]
    return ConversationState(
        cart=[existing],
        pending_comment_items=pending,
        pending_comment_existing_item_ids=[existing.id],
        pending_comment_text="положить отдельно",
        pending_comment_global_comment="на завтра",
        stage=SessionStage.AWAIT_COMMENT_SCOPE,
        status="await_comment_scope",
    )


def test_comment_scope_policy_classifies_contextual_and_independent_commands() -> None:
    """Центральная policy различает fallback области и независимое намерение."""
    policy = StateCompatibilityPolicy()
    state = _pending_state()

    assert (
        policy.evaluate(
            ParsedCommand(
                intent=Intent.ADD_ITEMS,
                comment_scope_action="items",
                comment_target_indexes=[0],
                confidence=0.99,
            ),
            state,
            CompatibilityContext.COMMENT_SCOPE,
        ).action
        is CompatibilityAction.CONTINUE
    )
    assert (
        policy.evaluate(
            ParsedCommand(
                intent=Intent.ADD_ITEMS,
                text="пармезан 3 кг",
                items=[ExtractedItem(product_query="пармезан", quantity=3, unit="кг")],
            ),
            state,
            CompatibilityContext.COMMENT_SCOPE,
        ).action
        is CompatibilityAction.INTERRUPT
    )
    assert (
        policy.evaluate(
            ParsedCommand(
                intent=Intent.ADD_ITEMS,
                text="пармезан",
                items=[ExtractedItem(product_query="пармезан")],
            ),
            state,
            CompatibilityContext.COMMENT_SCOPE,
        ).action
        is CompatibilityAction.INTERRUPT
    )
    assert (
        policy.evaluate(
            ParsedCommand(intent=Intent.UNKNOWN, text="не знаю"),
            state,
            CompatibilityContext.COMMENT_SCOPE,
        ).action
        is CompatibilityAction.AMBIGUOUS
    )


@pytest.mark.parametrize(
    ("action", "indexes", "expected"),
    [
        ("items", [0], {"Курица"}),
        ("items", [0, 2], {"Курица", "Огурцы"}),
        ("order", [], {"Курица", "Помидоры", "Огурцы", "Перец"}),
    ],
)
def test_comment_scope_answers_continue_and_apply_to_selected_items(
    settings,
    action: str,
    indexes: list[int],
    expected: set[str],
) -> None:  # type: ignore[no-untyped-def]
    """Подтверждённая область комментария применяется без подмены intent."""
    state = _pending_state()
    command = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        items=[item.model_copy(deep=True) for item in state.pending_comment_items],
        comment_scope_action=action,
        comment_target_indexes=indexes,
        confidence=0.99,
    )

    result = ConversationEngine(settings).handle(
        _event("Для первого"),
        command,
        state,
        [],
    )

    commented = {
        item.catalog_name or item.source_query
        for item in result.state.cart
        if "положить отдельно" in item.comment
    }
    assert commented == expected


@pytest.mark.parametrize(
    ("intent", "text"),
    [
        (Intent.SHOW_CART, "покажи черновик"),
        (Intent.THANKS, "спасибо"),
    ],
)
def test_comment_scope_interrupts_keep_pending_context(
    settings,
    intent: Intent,
    text: str,
) -> None:  # type: ignore[no-untyped-def]
    """Навигация и thanks не применяют и не очищают pending comment context."""
    state = _pending_state()
    before = state.model_dump(mode="json")
    result = ConversationEngine(settings).handle(
        _event(text),
        ParsedCommand(intent=intent, text=text),
        state,
        [],
    )

    assert result.state.pending_comment_items
    assert result.state.pending_comment_text == before["pending_comment_text"]
    assert result.state.pending_comment_global_comment == before["pending_comment_global_comment"]
    assert all(not item.comment for item in result.state.pending_comment_items)
    assert result.state.cart[0].comment == ""


def test_new_product_interrupt_does_not_receive_pending_comment(settings) -> None:  # type: ignore[no-untyped-def]
    """Новая позиция не получает комментарий старого pending-контекста."""
    state = _pending_state()
    result = ConversationEngine(settings).handle(
        _event("пармезан 3 кг"),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            text="пармезан 3 кг",
            items=[ExtractedItem(product_query="пармезан", quantity=3, unit="кг")],
        ),
        state,
        [],
    )

    parmesan = next(item for item in result.state.cart if item.source_query == "пармезан")
    assert parmesan.comment == ""
    assert [item.product_query for item in result.state.pending_comment_items] == [
        "Помидоры",
        "Огурцы",
        "Перец",
    ]
    assert result.state.pending_comment_existing_item_ids == ["chicken"]
    assert result.state.pending_comment_text == "положить отдельно"
    assert result.state.pending_comment_global_comment == "на завтра"


def test_remove_item_prunes_only_removed_pending_existing_id(settings) -> None:  # type: ignore[no-untyped-def]
    """Удаление товара не оставляет битый ID в области ожидающего комментария."""
    state = _pending_state()
    result = ConversationEngine(settings).handle(
        _event("убери курицу"),
        ParsedCommand(
            intent=Intent.REMOVE_ITEM,
            text="убери курицу",
            target_query="Курица",
        ),
        state,
        [],
    )

    assert result.state.cart[0].status is ItemStatus.SKIPPED
    assert result.state.pending_comment_existing_item_ids == []
    assert len(comment_scope_items(result.state)) == 3
    assert result.state.pending_comment_text == "положить отдельно"


def test_remove_pending_comment_item_drops_only_that_pending_position(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    """Удаление ожидающего товара не оставляет его в списке области комментария."""
    state = _pending_state()
    result = ConversationEngine(settings).handle(
        _event("убери огурцы"),
        ParsedCommand(
            intent=Intent.REMOVE_ITEM,
            text="убери огурцы",
            target_query="Огурцы",
        ),
        state,
        [],
    )

    assert [item.product_query for item in result.state.pending_comment_items] == [
        "Помидоры",
        "Перец",
    ]
    assert result.state.pending_comment_existing_item_ids == ["chicken"]
    assert result.state.pending_comment_text == "положить отдельно"


def test_ambiguous_comment_scope_does_not_change_draft_or_context(settings) -> None:  # type: ignore[no-untyped-def]
    """Случайная фраза только повторяет уточнение области комментария."""
    state = _pending_state()
    before_cart = state.cart[0].model_copy(deep=True)
    result = ConversationEngine(settings).handle(
        _event("я ещё подумаю"),
        ParsedCommand(
            intent=Intent.CLARIFY_CURRENT,
            text="я ещё подумаю",
            comment_scope_action="ambiguous",
        ),
        state,
        [],
    )

    assert result.state.cart[0] == before_cart
    assert result.state.pending_comment_items
    assert result.state.pending_comment_text == "положить отдельно"
    assert result.state.stage is SessionStage.AWAIT_COMMENT_SCOPE


def test_global_parse_precedes_comment_scope_fallback() -> None:
    """Новый товар не отправляется в contextual comment resolver."""
    service = object.__new__(UpdateOrchestrator)
    service.openai = Mock()
    service.openai.parse_text.return_value = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        text="пармезан 3 кг",
        items=[ExtractedItem(product_query="пармезан", quantity=3, unit="кг")],
    )
    service.openai.resolve_comment_scope = Mock(side_effect=AssertionError("must not be called"))
    service.engine = SimpleNamespace(state_compatibility_policy=StateCompatibilityPolicy())

    command = _interpreter(service).interpret_text("пармезан 3 кг", _pending_state())

    assert command.intent is Intent.ADD_ITEMS
    assert command.items[0].product_query == "пармезан"
    service.openai.resolve_comment_scope.assert_not_called()


@pytest.mark.parametrize(
    "intent",
    [Intent.SHOW_CART, Intent.REMOVE_ITEM, Intent.THANKS],
)
def test_independent_global_intents_skip_comment_scope_fallback(intent: Intent) -> None:
    """Самостоятельная навигация не подменяется contextual comment scope."""
    service = object.__new__(UpdateOrchestrator)
    service.openai = Mock()
    service.openai.parse_text.return_value = ParsedCommand(intent=intent)
    service.openai.resolve_comment_scope = Mock(side_effect=AssertionError("must not be called"))
    service.engine = SimpleNamespace(state_compatibility_policy=StateCompatibilityPolicy())

    command = _interpreter(service).interpret_text(intent.value, _pending_state())

    assert command.intent is intent
    service.openai.resolve_comment_scope.assert_not_called()


def test_unknown_global_parse_uses_comment_scope_fallback() -> None:
    """Ответ области комментария передаётся контекстному resolver после глобального разбора."""
    service = object.__new__(UpdateOrchestrator)
    service.openai = Mock()
    service.openai.parse_text.return_value = ParsedCommand(
        intent=Intent.UNKNOWN,
        text="для первого и третьего",
    )
    service.openai.resolve_comment_scope.return_value = CommentScopeDecision(
        action="items",
        target_item_indexes=[0, 2],
        confidence=0.99,
    )
    service.engine = SimpleNamespace(state_compatibility_policy=StateCompatibilityPolicy())

    command = _interpreter(service).interpret_text("для первого и третьего", _pending_state())

    assert command.comment_scope_action == "items"
    assert command.comment_target_indexes == [0, 2]
    service.openai.resolve_comment_scope.assert_called_once()


@pytest.mark.parametrize(
    "intent,text",
    [
        (Intent.ADD_ITEMS, "пармезан 3 кг"),
        (Intent.SHOW_CART, "покажи черновик"),
        (Intent.REMOVE_ITEM, "убери курицу"),
        (Intent.THANKS, "спасибо"),
    ],
)
def test_voice_interrupts_use_global_command_without_comment_scope(
    settings,
    intent: Intent,
    text: str,
) -> None:  # type: ignore[no-untyped-def]
    """Голосовые команды после транскрибации не получают старый комментарий."""
    state = _pending_state()
    item = ExtractedItem(product_query="пармезан", quantity=3, unit="кг")
    command = ParsedCommand(
        intent=intent,
        text=text,
        items=[item] if intent is Intent.ADD_ITEMS else [],
        target_query="Курица" if intent is Intent.REMOVE_ITEM else "",
    )

    result = ConversationEngine(settings).handle(
        _event(text, InputKind.VOICE),
        command,
        state,
        [],
    )

    assert all(not item.comment for item in result.state.pending_comment_items)
    assert result.state.pending_comment_text == "положить отдельно"
