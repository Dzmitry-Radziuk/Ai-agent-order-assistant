"""Проверяет голосовое сохранение текущего количества при предупреждении о кратности."""

from __future__ import annotations

import pytest

from restaurant_bot.domain.models import (
    ConversationState,
    ExtractedItem,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    SessionStage,
    TelegramEvent,
)
from restaurant_bot.parsing.commands.api import infer_intent
from restaurant_bot.services.engine import ConversationEngine


def _voice(text: str) -> TelegramEvent:
    """Создаёт тестовое голосовое событие Telegram."""
    return TelegramEvent(update_id=1, chat_id="voice-keep", input_type=InputKind.VOICE, text=text)


def _multiple_state(engine: ConversationEngine) -> ConversationState:
    """Создаёт состояние с открытым предупреждением о кратности."""
    item = engine._build_item(ExtractedItem(product_query="Горчица", quantity=1, unit="шт"))
    item.id = "multiple"
    item.status = ItemStatus.MATCHED
    item.catalog_product_id = "mustard"
    item.catalog_name = "Горчица"
    item.catalog_unit = "шт"
    item.minimum_multiple = 3
    item.suggested_quantity = 3
    return ConversationState(
        stage=SessionStage.AWAIT_SUBMIT_CONFIRM,
        current_issue_item_id=item.id,
        cart=[item],
    )


@pytest.mark.parametrize("phrase", ["Оставить одну штуку", "Оставь 1 шт"])
def test_keep_current_quantity_phrase_is_not_a_product(phrase: str) -> None:
    """Маршрутизирует варианты кнопки сохранения без создания товара «оставить»."""
    command = infer_intent(phrase)

    assert command.intent is Intent.KEEP_CURRENT_QUANTITY
    assert command.items == []


def test_voice_keep_current_quantity_clears_multiple_warning(settings) -> None:  # type: ignore[no-untyped-def]
    """Сохраняет введённое количество и переходит к следующей проверке заявки."""
    engine = ConversationEngine(settings)
    state = _multiple_state(engine)
    phrase = "Оставить одну штуку"

    result = engine.handle(_voice(phrase), infer_intent(phrase), state, [])

    assert result.state.cart[0].quantity == 1
    assert result.state.cart[0].suggested_quantity is None
    assert result.state.current_issue_item_id == ""
    assert "Выберите количество" not in result.reply.text


def test_voice_keep_current_quantity_overrides_wrong_ai_skip(settings) -> None:  # type: ignore[no-untyped-def]
    """Не даёт ошибочному AI-intent skip превратить сохранение количества в пропуск товара."""
    engine = ConversationEngine(settings)
    state = _multiple_state(engine)
    phrase = "Оставить одну штуку"

    result = engine.handle(
        _voice(phrase),
        ParsedCommand(intent=Intent.SKIP_CURRENT, text=phrase),
        state,
        [],
    )

    assert result.state.cart[0].status is ItemStatus.MATCHED
    assert result.state.cart[0].suggested_quantity is None


def test_voice_keep_marker_with_different_quantity_still_edits(settings) -> None:  # type: ignore[no-untyped-def]
    """Не считает отличающееся число просьбой сохранить старое количество."""
    engine = ConversationEngine(settings)
    state = _multiple_state(engine)
    phrase = "Оставить 2 штуки"

    result = engine.handle(_voice(phrase), infer_intent(phrase), state, [])

    assert result.state.cart[0].quantity == 2
    assert result.state.cart[0].suggested_quantity == 3
