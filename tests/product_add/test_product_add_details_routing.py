from restaurant_bot.conversation.routing.contracts import (
    CompatibilityAction,
    CompatibilityContext,
)
from restaurant_bot.conversation.routing.state_compatibility import (
    StateCompatibilityPolicy,
)
from restaurant_bot.domain.models import (
    Candidate,
    CartItem,
    ConversationState,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    SessionStage,
    TelegramEvent,
)
from restaurant_bot.parsing.commands.api import infer_intent
from restaurant_bot.services.engine import ConversationEngine


def _event(text: str, update_id: int = 1) -> TelegramEvent:
    """Создаёт текстовое событие для проверки modal routing."""
    return TelegramEvent(
        update_id=update_id,
        chat_id="chat",
        input_type=InputKind.TEXT,
        text=text,
    )


def _voice_event(text: str, update_id: int = 1) -> TelegramEvent:
    """Создаёт голосовое событие после транскрибации."""
    return TelegramEvent(
        update_id=update_id,
        chat_id="chat",
        input_type=InputKind.VOICE,
        text=text,
    )


def _pending_state() -> ConversationState:
    """Создаёт состояние ожидания описания ненайденного товара."""
    item = CartItem(id="mango", source_query="манго", status=ItemStatus.NOT_FOUND)
    return ConversationState(
        stage=SessionStage.AWAIT_PRODUCT_ADD_DETAILS,
        status="await_product_add_details",
        cart=[item],
        current_issue_item_id=item.id,
        pending_product_add_item_index=0,
        pending_product_add_request_id="add-mango",
    )


def test_parser_marks_only_explicit_product_add_command() -> None:
    """Проверяет семантический флаг явного добавления товара."""
    assert infer_intent("Мисо-паста Genzo, 1 кг").explicit_add_items is False
    assert infer_intent("Краб камчатский М/Л, 6 кг").explicit_add_items is False
    assert infer_intent("пармезан 3 кг").explicit_add_items is False
    assert infer_intent("добавь пармезан 3 кг").explicit_add_items is True
    assert infer_intent("добавь укроп 2 кг").explicit_add_items is True


def test_explicit_add_interrupts_product_add_details_without_leaking_context(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что новая позиция не получает контекст старого product-add запроса."""
    state = _pending_state()
    command = infer_intent("добавь пармезан 3 кг")

    result = ConversationEngine(settings).handle(_event(command.text), command, state, [])

    assert command.explicit_add_items is True
    assert result.state.cart[0].status is ItemStatus.NOT_FOUND
    assert result.state.pending_product_add_request_id == "add-mango"
    assert result.state.pending_product_add_item_index == 0
    assert result.state.product_add_requests == []
    parmesan = result.state.cart[1]
    assert parmesan.source_query == "пармезан"
    assert parmesan.quantity == 3
    assert parmesan.unit == "кг"


def test_product_add_details_independent_navigation_does_not_mutate_pending_state(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что независимая навигация прерывает описание без очистки pending context."""
    state = _pending_state()
    result = ConversationEngine(settings).handle(
        _event("покажи черновик"),
        infer_intent("покажи черновик"),
        state,
        [],
    )

    assert result.state.pending_product_add_request_id == "add-mango"
    assert result.state.pending_product_add_item_index == 0
    assert result.state.product_add_requests == []
    assert result.state.cart[0].status is ItemStatus.NOT_FOUND


def test_product_add_details_policy_prioritizes_explicit_intent() -> None:
    """Проверяет приоритет независимой команды над modal context."""
    policy = StateCompatibilityPolicy()
    state = _pending_state()
    assert (
        policy.evaluate(
            ParsedCommand(intent=Intent.UNKNOWN, text="Мисо-паста Genzo, 1 кг"),
            state,
            CompatibilityContext.PRODUCT_ADD_DETAILS,
        ).action
        is CompatibilityAction.CONTINUE
    )
    assert (
        policy.evaluate(
            ParsedCommand(intent=Intent.ADD_ITEMS, text="добавь пармезан", explicit_add_items=True),
            state,
            CompatibilityContext.PRODUCT_ADD_DETAILS,
        ).action
        is CompatibilityAction.INTERRUPT
    )
    assert (
        policy.evaluate(
            ParsedCommand(intent=Intent.SHOW_CART, text="покажи черновик"),
            state,
            CompatibilityContext.PRODUCT_ADD_DETAILS,
        ).action
        is CompatibilityAction.INTERRUPT
    )
    assert (
        policy.evaluate(
            ParsedCommand(intent=Intent.UNKNOWN),
            state,
            CompatibilityContext.PRODUCT_ADD_DETAILS,
        ).action
        is CompatibilityAction.AMBIGUOUS
    )


def test_product_add_details_disables_candidate_contextual_fallback() -> None:
    """Проверяет, что product-add prompt имеет приоритет над выбором кандидата."""
    policy = StateCompatibilityPolicy()
    state = _pending_state()
    state.cart[0].status = ItemStatus.AMBIGUOUS
    state.cart[0].candidates = [
        Candidate(product_id="miso", name="Мисо-паста Genzo", supplier="Тестовый")
    ]

    decision = policy.evaluate(
        infer_intent("Мисо-паста Genzo, 1 кг"),
        state,
        CompatibilityContext.CANDIDATE_SELECTION,
    )

    assert decision.action is CompatibilityAction.NOT_APPLICABLE


def test_product_add_details_description_is_not_rewritten_to_candidate(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет обработку описания product-add поверх старого candidate context."""
    state = _pending_state()
    state.cart[0].status = ItemStatus.AMBIGUOUS
    state.cart[0].candidates = [
        Candidate(product_id="miso", name="Мисо-паста Genzo", supplier="Тестовый")
    ]
    command = infer_intent("Мисо-паста Genzo, 1 кг")

    result = ConversationEngine(settings).handle(_event(command.text), command, state, [])

    assert result.enqueue_product_add is True
    assert result.state.product_add_requests[0]["description"] == command.text
    assert result.state.cart[0].status is ItemStatus.SKIPPED


def test_explicit_add_product_add_interrupt_does_not_create_legacy_request(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что явное добавление не переписывается в legacy product-add."""
    state = _pending_state()
    state.cart[0].status = ItemStatus.AMBIGUOUS
    state.cart[0].candidates = [
        Candidate(product_id="parmesan", name="Сыр Пармезан", supplier="Тестовый")
    ]
    command = infer_intent("добавь товар пармезан 3 кг")

    result = ConversationEngine(settings).handle(_event(command.text), command, state, [])

    assert command.explicit_add_items is True
    assert result.state.product_add_requests == []
    assert result.state.cart[0].status is ItemStatus.AMBIGUOUS
    assert result.state.cart[1].source_query == "товар пармезан"
    assert result.state.cart[1].quantity == 3
    assert result.state.cart[1].unit == "кг"


def test_voice_explicit_add_product_add_interrupts_pending_details(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что голосовая явная команда использует тот же interrupt path."""
    state = _pending_state()
    command = infer_intent("добавь пармезан три килограмма")

    result = ConversationEngine(settings).handle(
        _voice_event(command.text),
        command,
        state,
        [],
    )

    assert command.explicit_add_items is True
    assert result.state.product_add_requests == []
    assert result.state.pending_product_add_request_id == "add-mango"
    assert result.state.cart[1].source_query == "пармезан"
    assert result.state.cart[1].quantity == 3
    assert result.state.cart[1].unit == "кг"
