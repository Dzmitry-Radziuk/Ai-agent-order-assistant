"""Проверяет жизненный цикл комментария для уже добавленных позиций."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from restaurant_bot.conversation.comments import has_pending_comment_scope
from restaurant_bot.conversation.routing.state_compatibility import StateCompatibilityPolicy
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
from restaurant_bot.parsing.commands.api import infer_intent
from restaurant_bot.services.engine import ConversationEngine


class _TranscriptRecognizer:
    """Передаёт голосовой транскрипт в общий текстовый интерпретатор."""

    def recognize_media(self, event, state, parse_text, processing_message_id=None):  # type: ignore[no-untyped-def]
        """Использует тот же callback разбора, что и текстовый канал."""
        return parse_text(event.text, state)


def _event(text: str, input_type: InputKind = InputKind.TEXT, update_id: int = 1) -> TelegramEvent:
    """Создаёт событие комментария для текста или голосовой транскрипции."""
    return TelegramEvent(
        update_id=update_id,
        chat_id="existing-comment-scope",
        input_type=input_type,
        text=text,
    )


def _state() -> ConversationState:
    """Создаёт черновик с горчицей и луком."""
    return ConversationState(
        stage=SessionStage.REVIEW,
        status="review",
        cart=[
            CartItem(
                id="mustard",
                source_query="Горчица домашняя",
                catalog_name="Горчица домашняя",
                status=ItemStatus.MATCHED,
                quantity=2,
                unit="кг",
            ),
            CartItem(
                id="onion",
                source_query="Лук зелёный",
                catalog_name="Лук зелёный",
                status=ItemStatus.MATCHED,
                quantity=1,
                unit="кг",
            ),
        ],
    )


def _interpreter(provider: Mock, voice: bool = False) -> TelegramInputInterpreter:
    """Создаёт интерпретатор с контролируемым текстовым или голосовым входом."""
    return TelegramInputInterpreter(
        provider,
        (lambda: _TranscriptRecognizer()) if voice else (lambda: Mock()),
        StateCompatibilityPolicy(),
    )


def _open_scope(
    settings, *, voice: bool = False
) -> tuple[ConversationEngine, TelegramInputInterpreter, ConversationState]:  # type: ignore[no-untyped-def]
    """Открывает область комментария реальным маршрутом пожелания о доставке."""
    provider = Mock()
    provider.parse_text.return_value = ParsedCommand(intent=Intent.ADD_ITEMS)
    interpreter = _interpreter(provider, voice=voice)
    engine = ConversationEngine(settings)
    state = _state()
    event = _event("Привезти завтра", InputKind.VOICE if voice else InputKind.TEXT)
    command = interpreter.interpret(event, state)
    opened = engine.handle(event, command, state, [])
    assert opened.state.stage is SessionStage.AWAIT_COMMENT_SCOPE
    assert opened.state.pending_comment_items == []
    assert opened.state.pending_comment_existing_item_ids == ["mustard", "onion"]
    assert has_pending_comment_scope(opened.state)
    return engine, interpreter, opened.state


@pytest.mark.parametrize(
    ("answer", "expected_ids"),
    [
        ("для всех товаров", {"mustard", "onion"}),
        ("только для горчицы", {"mustard"}),
        ("только для последнего", {"onion"}),
    ],
)
def test_existing_only_group_scope_finishes_without_new_item(
    settings,
    answer: str,
    expected_ids: set[str],
) -> None:  # type: ignore[no-untyped-def]
    """Применяет групповой комментарий к выбранным существующим позициям."""
    engine, interpreter, state = _open_scope(settings)
    command = interpreter.interpret(_event(answer, update_id=2), state)

    result = engine.handle(_event(answer, update_id=2), command, state, [])

    assert result.state.stage is SessionStage.REVIEW
    assert result.state.pending_comment_items == []
    assert result.state.pending_comment_existing_item_ids == []
    assert {item.id for item in result.state.cart if item.comment} == expected_ids
    assert all(not item.order_comment_fragments for item in result.state.cart)


def test_existing_only_order_scope_records_order_provenance(settings) -> None:  # type: ignore[no-untyped-def]
    """Сохраняет происхождение комментария всей заявки отдельно от группы."""
    engine, interpreter, state = _open_scope(settings)
    answer = "для всей заявки"
    command = interpreter.interpret(_event(answer, update_id=2), state)

    result = engine.handle(_event(answer, update_id=2), command, state, [])

    assert result.state.stage is SessionStage.REVIEW
    assert all(item.comment == "Привезти завтра" for item in result.state.cart)
    assert all(item.order_comment_fragments == ["Привезти завтра"] for item in result.state.cart)


def test_existing_only_cancel_keeps_cart_and_clears_scope(settings) -> None:  # type: ignore[no-untyped-def]
    """Отменяет только ожидающий комментарий существующих позиций."""
    engine, interpreter, state = _open_scope(settings)
    before = [item.model_copy(deep=True) for item in state.cart]
    answer = "не добавлять"
    command = interpreter.interpret(_event(answer, update_id=2), state)

    result = engine.handle(_event(answer, update_id=2), command, state, [])

    assert result.state.stage is SessionStage.REVIEW
    assert result.state.cart == before
    assert result.state.pending_comment_text == ""
    assert result.state.pending_comment_existing_item_ids == []


def test_existing_only_voice_scope_uses_text_path_without_scope_ai(settings) -> None:  # type: ignore[no-untyped-def]
    """Проводит голосовой ответ области через общий детерминированный путь."""
    provider = Mock()
    provider.parse_text.return_value = ParsedCommand(intent=Intent.ADD_ITEMS)
    interpreter = _interpreter(provider, voice=True)
    engine = ConversationEngine(settings)
    state = _state()
    opening_event = _event("Привезти завтра", InputKind.VOICE)
    opening_command = interpreter.interpret(opening_event, state)
    opened = engine.handle(opening_event, opening_command, state, [])

    answer_event = _event("для всех товаров", InputKind.VOICE, 2)
    answer = interpreter.interpret(answer_event, opened.state)
    result = engine.handle(answer_event, answer, opened.state, [])

    assert result.state.stage is SessionStage.REVIEW
    assert [item.comment for item in result.state.cart] == [
        "Привезти завтра",
        "Привезти завтра",
    ]
    assert all(not item.order_comment_fragments for item in result.state.cart)
    provider.resolve_comment_scope.assert_not_called()


def test_comment_scope_predicate_rejects_stale_targets() -> None:
    """Не считает устаревшие идентификаторы действующим уточнением."""
    state = _state()
    state.stage = SessionStage.AWAIT_COMMENT_SCOPE
    state.status = "await_comment_scope"
    state.pending_comment_text = "Привезти завтра"
    state.pending_comment_existing_item_ids = ["removed"]

    assert not has_pending_comment_scope(state)


def test_mixed_group_scope_comments_existing_and_new_without_order_provenance(settings) -> None:  # type: ignore[no-untyped-def]
    """Смешанная область комментирует старые и новые позиции без общего provenance."""
    state = _state()
    pending = ExtractedItem(product_query="Сыр", quantity=2, unit="кг")
    state.pending_comment_items = [pending]
    state.pending_comment_existing_item_ids = ["mustard", "onion"]
    state.pending_comment_text = "Привезти завтра"
    state.stage = SessionStage.AWAIT_COMMENT_SCOPE
    state.status = "await_comment_scope"
    command = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        items=[pending],
        comment_scope_action="items",
        comment_target_indexes=[0, 1, 2],
        confidence=0.99,
    )

    result = ConversationEngine(settings).handle(_event("для всех товаров"), command, state, [])

    assert [item.source_query for item in result.state.cart] == [
        "Горчица домашняя",
        "Лук зелёный",
        "Сыр",
    ]
    assert all(item.comment == "Привезти завтра" for item in result.state.cart)
    assert all(not item.order_comment_fragments for item in result.state.cart)


@pytest.mark.parametrize(
    ("phrase", "scope", "scope_action", "comment"),
    [
        (
            "Добавь в комментарии привезти к восьми для всех товаров",
            "item",
            "items",
            "привезти к восьми",
        ),
        (
            "Добавь в комментарии привезти к восьми для всей заявки",
            "order",
            "order",
            "привезти к восьми",
        ),
    ],
)
def test_direct_comment_commands_keep_group_and_order_semantics(
    phrase: str,
    scope: str,
    scope_action: str,
    comment: str,
) -> None:
    """Разделяет групповую и заявочную область в прямой команде."""
    command = infer_intent(phrase)

    assert command.intent is Intent.EDIT_COMMENT
    assert command.comment_scope == scope
    assert command.comment_scope_action == scope_action
    assert command.comment_text == comment
    assert command.comment_target_query == ""


def test_direct_group_comment_updates_existing_items_without_order_provenance(settings) -> None:  # type: ignore[no-untyped-def]
    """Применяет прямую групповую команду только к активным позициям."""
    state = _state()
    phrase = "Добавь в комментарии привезти к восьми для всех товаров"
    result = ConversationEngine(settings).handle(
        _event(phrase),
        infer_intent(phrase),
        state,
        [],
    )

    assert [item.comment for item in result.state.cart] == [
        "привезти к восьми",
        "привезти к восьми",
    ]
    assert all(not item.order_comment_fragments for item in result.state.cart)


def test_direct_order_comment_updates_existing_items_with_order_provenance(settings) -> None:  # type: ignore[no-untyped-def]
    """Применяет прямую заявочную команду с общим provenance."""
    state = _state()
    phrase = "Добавь в комментарии привезти к восьми для всей заявки"
    result = ConversationEngine(settings).handle(
        _event(phrase),
        infer_intent(phrase),
        state,
        [],
    )

    assert [item.comment for item in result.state.cart] == [
        "привезти к восьми",
        "привезти к восьми",
    ]
    assert [item.order_comment_fragments for item in result.state.cart] == [
        ["привезти к восьми"],
        ["привезти к восьми"],
    ]
