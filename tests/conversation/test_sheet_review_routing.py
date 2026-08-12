from __future__ import annotations

import pytest

from restaurant_bot.conversation.routing.contracts import (
    CompatibilityAction,
    CompatibilityContext,
)
from restaurant_bot.conversation.routing.state_compatibility import (
    StateCompatibilityPolicy,
)
from restaurant_bot.domain.models import (
    ConversationState,
    DialogueResponse,
    ExtractedItem,
    Intent,
    ParsedCommand,
    SessionStage,
)
from restaurant_bot.services.orchestrator import UpdateOrchestrator


def _state(**kwargs: object) -> ConversationState:
    """Создаёт активную карточку sheet-review для routing-тестов."""
    values: dict[str, object] = {
        "stage": SessionStage.REVIEW,
        "review_mode": "sheet_link",
        "review_token": "token123",
        "review_snapshot_hash": "hash",
        "review_venue_code": "VENUE",
        "ui_message_text": "Проверьте текущую заявку",
        "visible_actions": [
            {"label": "Отправить заявку", "action_id": "v2:review_submit:token123:r4"},
            {"label": "Отменить", "action_id": "v2:review_cancel:token123:r4"},
        ],
    }
    values.update(kwargs)
    return ConversationState(**values)


def _command(intent: Intent, **kwargs: object) -> ParsedCommand:
    """Создаёт структурированную команду без анализа исходной фразы."""
    return ParsedCommand(intent=intent, **kwargs)


def test_sheet_review_is_the_only_review_compatibility_context() -> None:
    """Обычный cart review не получает блокирующую sheet-policy."""
    policy = StateCompatibilityPolicy()

    assert policy.context_for(_state()) is CompatibilityContext.SHEET_REVIEW
    assert (
        policy.context_for(ConversationState(stage=SessionStage.REVIEW, review_mode="cart")) is None
    )
    assert (
        policy.evaluate(
            _command(Intent.SHOW_CART),
            ConversationState(stage=SessionStage.REVIEW, review_mode="cart"),
            CompatibilityContext.SHEET_REVIEW,
        ).action
        is CompatibilityAction.NOT_APPLICABLE
    )


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            _command(
                Intent.ADD_ITEMS,
                items=[ExtractedItem(product_query="пармезан", quantity=None)],
            ),
            CompatibilityAction.INTERRUPT,
        ),
        (_command(Intent.ADD_ITEMS), CompatibilityAction.AMBIGUOUS),
        (_command(Intent.REMOVE_ITEM), CompatibilityAction.INTERRUPT),
        (_command(Intent.EDIT_QUANTITY), CompatibilityAction.INTERRUPT),
        (_command(Intent.SHOW_CART), CompatibilityAction.INTERRUPT),
        (_command(Intent.ORDER_STATUS), CompatibilityAction.INTERRUPT),
        (_command(Intent.HELP), CompatibilityAction.INTERRUPT),
        (_command(Intent.THANKS), CompatibilityAction.INTERRUPT),
        (_command(Intent.START_NEW_ORDER), CompatibilityAction.INTERRUPT),
        (_command(Intent.CLEAR_CART), CompatibilityAction.INTERRUPT),
        (_command(Intent.REVIEW_REFRESH), CompatibilityAction.CONTINUE),
        (_command(Intent.REVIEW_SUBMIT), CompatibilityAction.CONTINUE),
        (_command(Intent.REVIEW_CANCEL), CompatibilityAction.CONTINUE),
        (_command(Intent.SUBMIT_REQUEST), CompatibilityAction.CONTINUE),
        (_command(Intent.SUBMIT_AS_IS), CompatibilityAction.CONTINUE),
        (_command(Intent.CONFIRM), CompatibilityAction.CONTINUE),
        (
            _command(Intent.UNKNOWN, dialogue_response=DialogueResponse.AFFIRM),
            CompatibilityAction.CONTINUE,
        ),
        (
            _command(Intent.UNKNOWN, dialogue_response=DialogueResponse.DECLINE),
            CompatibilityAction.CONTINUE,
        ),
        (_command(Intent.UNKNOWN), CompatibilityAction.AMBIGUOUS),
        (
            _command(Intent.UNKNOWN, dialogue_response=DialogueResponse.UNCERTAIN),
            CompatibilityAction.AMBIGUOUS,
        ),
    ],
)
def test_sheet_review_policy_uses_only_structured_command(
    command: ParsedCommand,
    expected: CompatibilityAction,
) -> None:
    """Проверяет continue/interrupt/ambiguous без raw-language правил."""
    decision = StateCompatibilityPolicy().evaluate(
        command,
        _state(),
        CompatibilityContext.SHEET_REVIEW,
    )

    assert decision.action is expected


class _GlobalParser:
    """Возвращает заданную глобальную команду и запрещает visible-action fallback."""

    def __init__(self, command: ParsedCommand) -> None:
        """Сохраняет результат global parse."""
        self.command = command

    def parse_text(self, text: str) -> ParsedCommand:
        """Возвращает command без подмены контекстной кнопкой."""
        return self.command.model_copy(update={"text": text})

    def choose_visible_action(
        self,
        text: str,
        screen_text: str,
        actions: list[dict[str, str]],
    ) -> str:
        """Падает, если sheet-review передал свободный текст в action AI."""
        raise AssertionError("sheet-review must not call visible-action AI")


def _parse(command: ParsedCommand, text: str) -> ParsedCommand:
    """Прогоняет текст через global parse и sheet-review compatibility boundary."""
    orchestrator = UpdateOrchestrator.__new__(UpdateOrchestrator)
    orchestrator.openai = _GlobalParser(command)
    return orchestrator._parse_text_in_context(text, _state())


@pytest.mark.parametrize(
    ("command", "text"),
    [
        (
            _command(
                Intent.ADD_ITEMS,
                items=[ExtractedItem(product_query="пармезан", quantity=3, unit="кг")],
            ),
            "пармезан 3 кг",
        ),
        (
            _command(
                Intent.ADD_ITEMS,
                items=[ExtractedItem(product_query="пармезан", quantity=None)],
            ),
            "пармезан",
        ),
    ],
)
def test_independent_add_items_is_not_rewritten_by_sheet_review(
    command: ParsedCommand,
    text: str,
) -> None:
    """Новый товар с quantity и без quantity идёт в обычный product flow."""
    parsed = _parse(command, text)

    assert parsed.intent is Intent.ADD_ITEMS
    assert parsed.items[0].product_query == "пармезан"


@pytest.mark.parametrize(
    ("intent", "expected"),
    [
        (Intent.SHOW_CART, Intent.SHOW_CART),
        (Intent.SHOW_FINAL_REVIEW, Intent.SHOW_FINAL_REVIEW),
        (Intent.REMOVE_ITEM, Intent.REMOVE_ITEM),
        (Intent.ORDER_STATUS, Intent.ORDER_STATUS),
        (Intent.HELP, Intent.HELP),
        (Intent.THANKS, Intent.THANKS),
        (Intent.START_NEW_ORDER, Intent.START_NEW_ORDER),
        (Intent.CLEAR_CART, Intent.CLEAR_CART),
    ],
)
def test_independent_intents_are_preserved(
    intent: Intent,
    expected: Intent,
) -> None:
    """Обычные действия не превращаются в refresh/cancel sheet-card."""
    assert _parse(_command(intent), intent.value).intent is expected


@pytest.mark.parametrize(
    ("intent", "expected"),
    [
        (Intent.SUBMIT_REQUEST, Intent.REVIEW_SUBMIT),
        (Intent.SUBMIT_AS_IS, Intent.REVIEW_SUBMIT),
        (Intent.CONFIRM, Intent.REVIEW_SUBMIT),
        (Intent.CANCEL, Intent.REVIEW_CANCEL),
        (Intent.BACK, Intent.REVIEW_CANCEL),
        (Intent.REVIEW_SUBMIT, Intent.REVIEW_SUBMIT),
        (Intent.REVIEW_CANCEL, Intent.REVIEW_CANCEL),
    ],
)
def test_textual_sheet_actions_use_current_review_token(
    intent: Intent,
    expected: Intent,
) -> None:
    """Контекстные submit/cancel используют token из state, а не пустой callback."""
    parsed = _parse(_command(intent), intent.value)

    assert parsed.intent is expected
    assert parsed.callback_target == "token123"


def test_uncertain_sheet_text_preserves_context_and_controls() -> None:
    """Неопределённая фраза не вызывает action AI и оставляет review-контекст."""
    state = _state()
    orchestrator = UpdateOrchestrator.__new__(UpdateOrchestrator)
    orchestrator.openai = _GlobalParser(_command(Intent.UNKNOWN))

    parsed = orchestrator._parse_text_in_context("ну посмотрим", state)
    reply = orchestrator._sheet_review_ambiguous_reply(state)

    assert parsed.intent is Intent.UNKNOWN
    assert state.review_mode == "sheet_link"
    assert state.review_token == "token123"
    assert [button.callback_data for row in reply.rows for button in row] == [
        "v2:review_submit:token123",
        "v2:review_cancel:token123",
        "v2:review:VENUE",
    ]
    assert "Не удалось понять" in reply.text
