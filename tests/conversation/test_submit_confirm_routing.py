from __future__ import annotations

from typing import Any

import pytest

from restaurant_bot.config import Settings
from restaurant_bot.conversation.routing.contracts import (
    CompatibilityAction,
    CompatibilityContext,
)
from restaurant_bot.conversation.routing.modal_routing import evaluate_modal_routing
from restaurant_bot.conversation.routing.state_compatibility import (
    StateCompatibilityPolicy,
)
from restaurant_bot.domain.models import (
    CartItem,
    ConversationState,
    EngineResult,
    ExtractedItem,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    SessionStage,
    TelegramEvent,
)
from restaurant_bot.input.telegram_callbacks import parse_callback
from restaurant_bot.parsing.commands.api import infer_intent
from restaurant_bot.services.engine import ConversationEngine


def _event(text: str, input_type: InputKind = InputKind.TEXT) -> TelegramEvent:
    """Создаёт событие для submit-confirm regression tests."""
    return TelegramEvent(update_id=1, chat_id="submit-confirm", input_type=input_type, text=text)


def _matched_item() -> CartItem:
    """Создаёт готовую позицию для финальной проверки."""
    return CartItem(
        id="ready",
        source_query="сыр",
        catalog_product_id="cheese",
        catalog_name="Сыр",
        supplier="Поставщик",
        catalog_unit="кг",
        quantity=2,
        unit="кг",
        price=10,
        status=ItemStatus.MATCHED,
    )


def _state(**kwargs: object) -> ConversationState:
    """Создаёт валидный state финального review."""
    values: dict[str, Any] = {
        "cart": [_matched_item()],
        "stage": SessionStage.AWAIT_SUBMIT_CONFIRM,
    }
    values.update(kwargs)
    return ConversationState(**values)


def _run_text_or_voice(
    settings: Settings,
    phrase: str,
    input_type: InputKind,
) -> tuple[ParsedCommand, EngineResult]:
    """Прогоняет глобально распознанную текстовую или голосовую команду."""
    command = infer_intent(phrase)
    return command, ConversationEngine(settings).handle(
        _event(phrase, input_type), command, _state(), []
    )


def test_submit_confirm_policy_requires_valid_cart_review() -> None:
    """Не активирует submit-confirm поверх нерешённой позиции."""
    policy = StateCompatibilityPolicy()
    valid = _state()
    unresolved = _state(
        cart=[_matched_item().model_copy(update={"status": ItemStatus.MISSING_QTY})]
    )

    assert (
        policy.evaluate(
            ParsedCommand(intent=Intent.CONFIRM), valid, CompatibilityContext.SUBMIT_CONFIRM
        ).action
        is CompatibilityAction.CONTINUE
    )
    assert (
        policy.evaluate(
            ParsedCommand(intent=Intent.CONFIRM),
            unresolved,
            CompatibilityContext.SUBMIT_CONFIRM,
        ).action
        is CompatibilityAction.NOT_APPLICABLE
    )


@pytest.mark.parametrize("input_type", [InputKind.TEXT, InputKind.VOICE])
@pytest.mark.parametrize(
    "phrase",
    ["да", "подтверждаю", "всё верно", "отправляй", "да, отправляй"],
)
def test_affirmative_submit_confirmation_is_identical_for_text_and_voice(
    settings: Settings,
    phrase: str,
    input_type: InputKind,
) -> None:
    """Канонизирует affirmative confirmation в существующий submit flow."""
    _, result = _run_text_or_voice(settings, phrase, input_type)

    assert result.state.stage is SessionStage.SUBMITTING
    assert result.enqueue_submission is True
    assert result.state.pending_submission is not None


@pytest.mark.parametrize("phrase", ["нет", "не отправляй", "передумал", "назад"])
@pytest.mark.parametrize("input_type", [InputKind.TEXT, InputKind.VOICE])
def test_decline_and_back_leave_review_without_submission(
    settings: Settings,
    phrase: str,
    input_type: InputKind,
) -> None:
    """Закрывает confirmation modal возвратом к сохранённому черновику."""
    _, result = _run_text_or_voice(settings, phrase, input_type)

    assert result.state.stage is SessionStage.REVIEW
    assert result.enqueue_submission is False
    assert result.state.pending_submission is None
    assert result.state.cart[0].status is ItemStatus.MATCHED


@pytest.mark.parametrize("input_type", [InputKind.TEXT, InputKind.VOICE])
def test_uncertain_confirmation_repeats_review_without_cart_mutation(
    settings: Settings,
    input_type: InputKind,
) -> None:
    """Не превращает неуверенный ответ в новую товарную позицию."""
    _, result = _run_text_or_voice(settings, "ну", input_type)

    assert result.state.stage is SessionStage.AWAIT_SUBMIT_CONFIRM
    assert result.enqueue_submission is False
    assert len(result.state.cart) == 1
    assert "Финальная проверка" in result.reply.text


def test_add_items_interrupts_submit_confirmation_without_metadata_leak(settings) -> None:  # type: ignore[no-untyped-def]
    """Новый товар закрывает submit modal и идёт обычным add-items маршрутом."""
    command = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        text="пармезан 3 кг",
        items=[ExtractedItem(product_query="пармезан", quantity=3, unit="кг")],
    )

    result = ConversationEngine(settings).handle(_event(command.text), command, _state(), [])

    assert result.state.stage is SessionStage.REVIEW
    assert result.enqueue_submission is False
    assert len(result.state.cart) == 2
    assert result.state.cart[0].source_query == "сыр"
    assert result.state.cart[1].source_query == "пармезан"
    assert result.state.cart[1].quantity == 3


def test_add_more_interrupts_submit_confirmation_even_with_affirmative_dialogue(
    settings: Settings,
) -> None:
    """Не принимает просьбу добавить товары за подтверждение отправки."""
    phrase = "давай добавим ещё товары"
    command = infer_intent(phrase)

    result = ConversationEngine(settings).handle(_event(phrase), command, _state(), [])

    assert command.intent is Intent.ADD_MORE
    assert result.state.stage is SessionStage.COLLECTING
    assert result.enqueue_submission is False
    assert len(result.state.cart) == 1


@pytest.mark.parametrize("phrase", ["спасибо", "помощь", "покажи черновик"])
def test_passive_and_cart_commands_close_submit_modal(settings, phrase: str) -> None:  # type: ignore[no-untyped-def]
    """Независимые passive/cart intents не оставляют старый submit stage."""
    command = infer_intent(phrase)
    result = ConversationEngine(settings).handle(_event(phrase), command, _state(), [])

    assert result.state.stage is SessionStage.REVIEW
    assert result.enqueue_submission is False
    assert len(result.state.cart) == 1


def test_multiple_warning_blocks_affirmative_submission(settings) -> None:  # type: ignore[no-untyped-def]
    """Multiple warning остаётся сильнее affirmative confirmation."""
    item = _matched_item().model_copy(update={"suggested_quantity": 4})
    state = ConversationState(cart=[item], stage=SessionStage.AWAIT_SUBMIT_CONFIRM)
    command = infer_intent("да")

    result = ConversationEngine(settings).handle(_event("да"), command, state, [])

    assert result.state.stage is SessionStage.AWAIT_SUBMIT_CONFIRM
    assert result.enqueue_submission is False
    assert result.state.pending_submission is None
    assert "Проверьте количество" in result.reply.text


def test_fresh_submit_callback_reaches_existing_submission_boundary(settings) -> None:  # type: ignore[no-untyped-def]
    """Свежий submit callback использует существующий prepare-submission flow."""
    state = _state(ui_revision=3)
    command = parse_callback("v2:submit:r3")

    result = ConversationEngine(settings).handle(
        TelegramEvent(
            update_id=2,
            chat_id="submit-confirm",
            input_type=InputKind.CALLBACK,
            callback_data="v2:submit:r3",
        ),
        command,
        state,
        [],
    )

    assert result.state.stage is SessionStage.SUBMITTING
    assert result.enqueue_submission is True


@pytest.mark.parametrize("callback", ["v2:submit:r2", "v2:back:r2", "v2:finalpage:1:r2"])
def test_stale_submit_review_callbacks_do_not_mutate_state(settings, callback: str) -> None:  # type: ignore[no-untyped-def]
    """Устаревший callback отклоняется до modal mutation и enqueue."""
    state = _state(ui_revision=3)
    command = parse_callback(callback)
    before = state.model_dump_json()

    result = ConversationEngine(settings).handle(
        TelegramEvent(
            update_id=3,
            chat_id="submit-confirm",
            input_type=InputKind.CALLBACK,
            callback_data=callback,
        ),
        command,
        state,
        [],
    )

    assert result.state.model_dump_json() == before
    assert result.enqueue_submission is False


def test_photo_items_interrupt_submit_confirmation_without_submission(settings) -> None:  # type: ignore[no-untyped-def]
    """Распознанное фото проходит обычный ADD_ITEMS flow после review."""
    command = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        items=[ExtractedItem(product_query="пармезан", quantity=3, unit="кг")],
    )

    result = ConversationEngine(settings).handle(
        TelegramEvent(
            update_id=4,
            chat_id="submit-confirm",
            input_type=InputKind.PHOTO,
            file_id="photo",
            mime_type="image/jpeg",
        ),
        command,
        _state(),
        [],
    )

    assert result.state.stage is SessionStage.REVIEW
    assert result.enqueue_submission is False
    assert len(result.state.cart) == 2


def test_modal_routing_exposes_submit_confirm_decision() -> None:
    """Возвращает submit-confirm решение через единый ModalRoutingDecision."""
    decision = evaluate_modal_routing(
        StateCompatibilityPolicy(),
        ParsedCommand(intent=Intent.CONFIRM),
        _state(),
    )

    assert decision.submit_confirm.action is CompatibilityAction.CONTINUE
    assert decision.submit_confirm_active is True
