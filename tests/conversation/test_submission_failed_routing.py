"""Проверяет поведение, связанное с модулем «test submission failed routing»."""

from __future__ import annotations

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
    InputKind,
    ItemStatus,
    PendingSubmission,
    SessionStage,
    TelegramEvent,
)
from restaurant_bot.input.telegram_callbacks import parse_callback
from restaurant_bot.parsing.commands.api import infer_intent
from restaurant_bot.services.engine import ConversationEngine


def _item() -> CartItem:
    """Создаёт неизменяемую позицию для recovery-регрессий."""
    return CartItem(
        id="cheese",
        source_query="сыр",
        catalog_name="Сыр",
        catalog_product_id="product-1",
        status=ItemStatus.MATCHED,
        quantity=2,
        unit="кг",
    )


def _state(*, uncertain: bool = False, pending: bool = True) -> ConversationState:
    """Создаёт состояние с замороженным снимком неудачной отправки."""
    snapshot = (
        PendingSubmission(
            order_no="ORDER-1",
            rows=[{"Количество": 2}],
            failed_stage="dispatch_uncertain" if uncertain else "",
        )
        if pending
        else None
    )
    return ConversationState(
        cart=[_item()],
        stage=SessionStage.SUBMISSION_FAILED,
        status="dispatch_uncertain" if uncertain else "submission_failed",
        pending_submission=snapshot,
    )


def _run(
    settings: Settings,
    state: ConversationState,
    phrase: str,
    kind: InputKind = InputKind.TEXT,
) -> EngineResult:
    """Прогоняет структурированную команду через блокировку восстановления движка."""
    command = infer_intent(phrase)
    return ConversationEngine(settings).handle(
        TelegramEvent(update_id=1, chat_id="failed", input_type=kind, text=phrase),
        command,
        state,
        [],
    )


def test_submission_failed_context_exposes_structured_mode() -> None:
    """Policy различает retryable и dispatch-uncertain без анализа ошибки."""
    policy = StateCompatibilityPolicy()
    retryable = evaluate_modal_routing(policy, infer_intent("повтори"), _state())
    uncertain = evaluate_modal_routing(policy, infer_intent("повтори"), _state(uncertain=True))

    assert retryable.submission_failed.action is CompatibilityAction.CONTINUE
    assert retryable.submission_failed.mode == "retryable"
    assert uncertain.submission_failed.action is CompatibilityAction.REJECT
    assert uncertain.submission_failed.mode == "dispatch_uncertain"
    assert policy.context_for(_state()) is CompatibilityContext.SUBMISSION_FAILED


@pytest.mark.parametrize("kind", [InputKind.TEXT, InputKind.VOICE])
@pytest.mark.parametrize(
    "phrase",
    ["повтори", "повтори отправку", "отправь ещё раз", "попробуй снова", "отправляй", "да"],
)
def test_retryable_failure_retries_existing_snapshot_for_text_and_voice(
    settings: Settings,
    kind: InputKind,
    phrase: str,
) -> None:
    """Самостоятельные retry-фразы повторно используют прежний order snapshot."""
    state = _state()
    result = _run(settings, state, phrase, kind)

    assert result.enqueue_submission is True
    assert result.state.stage is SessionStage.SUBMITTING
    assert result.state.pending_submission is not None
    assert result.state.pending_submission.order_no == "ORDER-1"
    assert result.state.pending_submission.rows == [{"Количество": 2}]
    assert result.state.cart[0].quantity == 2


@pytest.mark.parametrize(
    "phrase", ["пармезан 3 кг", "добавь укроп 2 кг", "убери сыр", "измени количество на 9"]
)
def test_retryable_failure_rejects_cart_mutations(settings: Settings, phrase: str) -> None:
    """Recovery lock не позволяет изменить cart и старый pending snapshot."""
    state = _state()
    before = state.model_dump_json()
    result = _run(settings, state, phrase)

    assert result.enqueue_submission is False
    assert result.state.stage is SessionStage.SUBMISSION_FAILED
    assert result.state.model_dump_json() != before  # last_input_text is diagnostic state.
    assert result.state.cart[0].quantity == 2
    assert result.state.pending_submission is not None
    assert result.state.pending_submission.rows == [{"Количество": 2}]
    assert "Отправка не завершена" in result.reply.text


@pytest.mark.parametrize("phrase", ["покажи черновик", "назад", "спасибо", "помощь", "ну"])
def test_retryable_failure_read_only_commands_preserve_recovery(
    settings: Settings, phrase: str
) -> None:
    """Read-only и пассивные ответы сохраняют stage и pending snapshot."""
    state = _state()
    result = _run(settings, state, phrase)

    assert result.enqueue_submission is False
    assert result.state.stage is SessionStage.SUBMISSION_FAILED
    assert result.state.pending_submission is not None
    assert result.state.pending_submission.order_no == "ORDER-1"


@pytest.mark.parametrize(
    "phrase",
    [
        "повтори",
        "повтори отправку",
        "отправь ещё раз",
        "попробуй снова",
        "отправляй",
        "да",
        "пармезан 3 кг",
    ],
)
def test_dispatch_uncertain_never_retries_or_mutates(settings: Settings, phrase: str) -> None:
    """После начала внешнего POST ни одна retry-like команда не запускает новый POST."""
    state = _state(uncertain=True)
    result = _run(settings, state, phrase)

    assert result.enqueue_submission is False
    assert result.state.stage is SessionStage.SUBMISSION_FAILED
    assert result.state.status == "dispatch_uncertain"
    assert result.state.cart[0].quantity == 2
    assert result.state.pending_submission is not None
    assert result.state.pending_submission.failed_stage == "dispatch_uncertain"
    assert "Нужно проверить отправку" in result.reply.text


def test_reset_starts_new_order_without_deleting_uncertain_submission(
    settings: Settings,
) -> None:
    """Reset освобождает чат, сохраняя старую заявку в журнале отправки."""
    for phrase in ("новый заказ", "/reset"):
        state = _state(uncertain=True)
        result = _run(settings, state, phrase)

        assert result.enqueue_submission is False
        assert result.state.stage is SessionStage.COLLECTING
        assert result.state.cart == []
        assert result.state.pending_submission is None
        assert result.state.spreadsheet_id == ""
        assert "Предыдущая заявка ещё проверяется" in result.reply.text
        assert "не оформляйте её повторно" in result.reply.text.lower()


def test_reset_keeps_retry_and_dispatch_status_routes_separate(settings: Settings) -> None:
    """Reset не превращает retry или проверку внешней отправки в новый POST."""
    retryable = _run(settings, _state(), "/reset")
    uncertain = _run(settings, _state(uncertain=True), "/reset")

    assert retryable.state.stage is SessionStage.COLLECTING
    assert uncertain.state.stage is SessionStage.COLLECTING
    assert retryable.enqueue_submission is False
    assert uncertain.enqueue_submission is False


def test_missing_pending_submission_has_safe_broken_state_fallback(settings: Settings) -> None:
    """При повреждённом состоянии retry без снимка не создаёт новую заявку."""
    result = _run(settings, _state(pending=False), "повтори")

    assert result.enqueue_submission is False
    assert result.state.stage is SessionStage.SUBMISSION_FAILED
    assert result.state.pending_submission is None
    assert "Снимок заявки не найден" in result.reply.text


def test_dispatch_uncertain_check_reads_status_without_submission_enqueue(
    settings: Settings,
) -> None:
    """Кнопка проверки ставит только read-only чтение истории заявки."""
    state = _state(uncertain=True)
    command = parse_callback("v2:check_submission:r0")
    result = ConversationEngine(settings).handle(
        TelegramEvent(
            update_id=2,
            chat_id="failed",
            input_type=InputKind.CALLBACK,
            callback_data="v2:check_submission:r0",
        ),
        command,
        state,
        [],
    )

    assert result.enqueue_submission is False
    assert result.enqueue_order_status is True
    assert result.order_status_order_number == "ORDER-1"
