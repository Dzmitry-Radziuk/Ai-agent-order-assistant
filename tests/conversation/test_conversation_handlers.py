"""Проверяет поведение, связанное с модулем «test conversation handlers»."""

from restaurant_bot.domain.models import (
    Candidate,
    CartItem,
    ConversationState,
    DepartmentQuantities,
    ExtractedItem,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    SessionStage,
    TelegramEvent,
)
from restaurant_bot.parsing.commands.api import infer_intent
from restaurant_bot.services.conversation_handlers.candidate_selection import (
    CandidateSelectionHandler,
)
from restaurant_bot.services.conversation_handlers.comment_scope import CommentScopeHandler
from restaurant_bot.services.conversation_handlers.final_review import FinalReviewHandler
from restaurant_bot.services.conversation_handlers.navigation import (
    OrderStatusHandler,
    PassiveIntentHandler,
)
from restaurant_bot.services.conversation_handlers.pending_quantity import (
    PendingQuantityAction,
    PendingQuantityHandler,
)


def _event(text: str) -> TelegramEvent:
    """Создаёт текстовое событие для изолированного обработчика."""
    return TelegramEvent(update_id=1, chat_id="1", input_type=InputKind.TEXT, text=text)


def test_pending_quantity_changes_only_the_open_item() -> None:
    """Применяет короткое количество к текущей нерешённой позиции."""
    first = CartItem(
        id="first",
        source_query="Сироп Роза",
        status=ItemStatus.MISSING_QTY,
        catalog_product_id="rose",
        catalog_unit="шт",
    )
    second = CartItem(
        id="second",
        source_query="Сироп Тархун",
        quantity=3,
        unit="шт",
        status=ItemStatus.MATCHED,
    )
    state = ConversationState(
        cart=[first, second],
        current_issue_item_id="first",
        stage=SessionStage.REVIEW,
    )

    action = PendingQuantityHandler().handle(
        _event("5 штук"),
        ParsedCommand(intent=Intent.UNKNOWN, text="5 штук"),
        state,
    )

    assert action is PendingQuantityAction.ADVANCE
    assert (first.quantity, first.unit, first.status) == (5, "шт", ItemStatus.MATCHED)
    assert (second.quantity, second.unit) == (3, "шт")


def test_pending_quantity_does_not_consume_a_named_product() -> None:
    """Не принимает новый товар за ответ количеством текущей карточке."""
    current = CartItem(
        id="current",
        source_query="Сироп Роза",
        status=ItemStatus.MISSING_QTY,
        catalog_product_id="rose",
        catalog_unit="шт",
    )
    state = ConversationState(
        cart=[current],
        current_issue_item_id="current",
        stage=SessionStage.COLLECTING,
    )
    command = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        text="говядина 5 кг",
        items=[ExtractedItem(product_query="говядина", quantity=5, unit="кг")],
    )

    action = PendingQuantityHandler().handle(_event(command.text), command, state)

    assert action is PendingQuantityAction.NOT_HANDLED
    assert current.quantity is None


def test_final_review_returns_first_unresolved_item() -> None:
    """Не пропускает нерешённую позицию на финальную проверку."""
    unresolved = CartItem(
        id="missing",
        source_query="Сироп Роза",
        status=ItemStatus.MISSING_QTY,
    )
    state = ConversationState(cart=[unresolved])

    outcome = FinalReviewHandler().handle(
        ParsedCommand(intent=Intent.SHOW_FINAL_REVIEW),
        state,
    )

    assert outcome is not None
    assert outcome.prepare_submission is False
    assert outcome.result is not None
    assert state.current_issue_item_id == "missing"


def test_final_review_requests_submission_only_after_all_guards() -> None:
    """Возвращает engine отдельный сигнал подготовки подтверждённой отправки."""
    matched = CartItem(
        id="matched",
        source_query="Сироп Роза",
        quantity=5,
        unit="шт",
        status=ItemStatus.MATCHED,
        catalog_product_id="rose",
    )
    state = ConversationState(cart=[matched], stage=SessionStage.AWAIT_SUBMIT_CONFIRM)

    outcome = FinalReviewHandler().handle(
        ParsedCommand(intent=Intent.SUBMIT_AS_IS),
        state,
    )

    assert outcome is not None
    assert outcome.prepare_submission is True
    assert outcome.result is None


def test_final_review_handler_owns_review_pagination() -> None:
    """Переключает страницу финальной проверки без внешнего эффекта."""
    state = ConversationState(
        cart=[
            CartItem(
                id=f"matched-{index}",
                source_query=f"Товар {index}",
                quantity=1,
                unit="шт",
                status=ItemStatus.MATCHED,
                catalog_product_id=f"product-{index}",
            )
            for index in range(41)
        ]
    )

    outcome = FinalReviewHandler().handle(
        ParsedCommand(intent=Intent.SHOW_FINAL_REVIEW, callback_target="page:2"),
        state,
    )

    assert outcome is not None
    assert outcome.result is not None
    assert state.final_review_page == 2
    assert state.stage is SessionStage.AWAIT_SUBMIT_CONFIRM


def test_final_review_requires_department_for_new_items() -> None:
    """Не показывает кнопку записи до явного выбора подразделения."""
    state = ConversationState(
        cart=[
            CartItem(
                id="matched",
                source_query="Сироп Роза",
                quantity=5,
                unit="шт",
                status=ItemStatus.MATCHED,
                catalog_product_id="rose",
            )
        ],
        department_confirmation_required=True,
    )

    outcome = FinalReviewHandler().handle(
        ParsedCommand(intent=Intent.SHOW_FINAL_REVIEW),
        state,
    )

    assert outcome is not None and outcome.result is not None
    assert "К какому подразделению" in outcome.result.reply.text
    assert all(
        button.callback_data != "v2:submit" for row in outcome.result.reply.rows for button in row
    )


def test_department_selection_can_preserve_photo_distribution() -> None:
    """Сохраняет одновременно Зал и Бар после явного подтверждения фото."""
    item = CartItem(
        id="matched",
        source_query="Горчица",
        quantity=5,
        unit="шт",
        status=ItemStatus.MATCHED,
        catalog_product_id="mustard",
        department_quantities=DepartmentQuantities(hall=2, bar=3),
    )
    state = ConversationState(
        cart=[item],
        department_confirmation_required=True,
    )

    outcome = FinalReviewHandler().handle(
        ParsedCommand(intent=Intent.SELECT_DEPARTMENT, callback_target="preserve"),
        state,
    )

    assert outcome is not None and outcome.result is not None
    assert state.department_confirmed is True
    assert item.department_quantities.hall == 2
    assert item.department_quantities.bar == 3
    assert "Зал 2 шт, Бар 3 шт" in outcome.result.reply.text


def test_department_selection_page_callback_only_changes_preview_page() -> None:
    """Переключает страницу проверки фото, не подтверждая и не меняя отделы."""
    items = [
        CartItem(
            id=f"photo-{index}",
            source_query=f"Товар {index}",
            quantity=1,
            unit="шт",
            status=ItemStatus.MATCHED,
            catalog_product_id=f"product-{index}",
            department_quantities=DepartmentQuantities(kitchen=1),
        )
        for index in range(1, 22)
    ]
    state = ConversationState(
        cart=items,
        department_confirmation_required=True,
        department_selection_page=0,
    )

    outcome = FinalReviewHandler().handle(
        ParsedCommand(intent=Intent.SELECT_DEPARTMENT, callback_target="page:2"),
        state,
    )

    assert outcome is not None and outcome.result is not None
    assert state.department_selection_page == 2
    assert state.department_confirmed is False
    assert "Позиции 21 из 21" not in outcome.result.reply.text
    assert "Позиции 21–21 из 21" in outcome.result.reply.text
    assert "Товар 21" in outcome.result.reply.text
    assert "v2:dept:preserve" in [
        button.callback_data for row in outcome.result.reply.rows for button in row
    ]


def test_department_selection_reassigns_entire_photo_order() -> None:
    """Заменяет распределение фото одним явно выбранным подразделением."""
    item = CartItem(
        id="matched",
        source_query="Горчица",
        quantity=5,
        unit="шт",
        status=ItemStatus.MATCHED,
        catalog_product_id="mustard",
        department_quantities=DepartmentQuantities(hall=2, bar=3),
    )
    state = ConversationState(
        cart=[item],
        department_confirmation_required=True,
    )

    outcome = FinalReviewHandler().handle(
        ParsedCommand(intent=Intent.SELECT_DEPARTMENT, callback_target="kitchen"),
        state,
    )

    assert outcome is not None and outcome.result is not None
    assert state.department == "Кухня"
    assert item.department == "Кухня"
    assert item.department_quantities == DepartmentQuantities()
    assert "5 шт · Кухня" in outcome.result.reply.text


def test_text_department_command_uses_existing_selection_flow() -> None:
    """Применяет текст «добавить на зал» как выбор подразделения заявки."""
    item = CartItem(
        id="matched",
        source_query="Горчица",
        quantity=5,
        unit="шт",
        status=ItemStatus.MATCHED,
        catalog_product_id="mustard",
    )
    state = ConversationState(
        cart=[item],
        department_confirmation_required=True,
    )

    command = infer_intent("добавить на зал")
    outcome = FinalReviewHandler().handle(command, state)

    assert command.intent is Intent.SELECT_DEPARTMENT
    assert command.callback_target == "hall"
    assert outcome is not None and outcome.result is not None
    assert state.department_confirmed is True
    assert item.department == "Зал"
    assert "5 шт · Зал" in outcome.result.reply.text


def test_passive_intent_handler_uses_explicit_dispatch_table() -> None:
    """Возвращает справку и не меняет товарный черновик."""
    state = ConversationState(cart=[CartItem(id="item", source_query="Сироп Роза", quantity=1)])

    result = PassiveIntentHandler().handle(ParsedCommand(intent=Intent.HELP), state)

    assert result is not None
    assert result.state.cart[0].source_query == "Сироп Роза"
    assert result.reply.text


def test_order_status_handler_prepares_background_read_without_calling_it() -> None:
    """Формирует параметры чтения выбранной заявки без внешнего эффекта."""
    state = ConversationState(
        order_status_page=1,
        order_status_selected_index=2,
        order_status_selected_order_number="ORDER-2",
    )
    command = ParsedCommand(
        intent=Intent.ORDER_STATUS,
        callback_target="detail_next",
    )

    result = OrderStatusHandler().handle(
        TelegramEvent(update_id=2, chat_id="1", input_type=InputKind.CALLBACK),
        command,
        state,
    )

    assert result is not None
    assert result.enqueue_order_status is True
    assert result.order_status_page == 0
    assert result.order_status_detail_page == 1
    assert result.order_status_selected_index == 2
    assert result.order_status_order_number == "ORDER-2"


def test_candidate_handler_resolves_spoken_candidate_name() -> None:
    """Находит выбранный вариант по произнесённому названию."""
    item = CartItem(
        id="item",
        source_query="сироп",
        status=ItemStatus.AMBIGUOUS,
        candidates=[
            Candidate(product_id="rose", name="Сироп Роза"),
            Candidate(product_id="tarhun", name="Сироп Тархун"),
        ],
    )
    state = ConversationState(cart=[item], current_issue_item_id="item")

    outcome = CandidateSelectionHandler().resolve(
        ParsedCommand(
            intent=Intent.SELECT_CANDIDATE,
            selection_query="сироп тархуна",
        ),
        state,
    )

    assert outcome.result is None
    assert outcome.item is item
    assert outcome.candidate is not None
    assert outcome.candidate.product_id == "tarhun"
    assert item.status is ItemStatus.AMBIGUOUS


def test_comment_scope_handler_rejects_low_confidence_without_mutation() -> None:
    """Повторяет уточнение, если область комментария не подтверждена."""
    pending = ExtractedItem(product_query="Сироп Роза", quantity=5, unit="шт")
    state = ConversationState(
        pending_comment_items=[pending],
        pending_comment_text="привезти утром",
        stage=SessionStage.AWAIT_COMMENT_SCOPE,
    )

    outcome = CommentScopeHandler().handle(
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            comment_scope_action="items",
            comment_target_indexes=[0],
            confidence=0.7,
        ),
        state,
    )

    assert outcome.result is not None
    assert outcome.reprocess_command is None
    assert state.pending_comment_items == [pending]
    assert state.stage is SessionStage.AWAIT_COMMENT_SCOPE


def test_comment_scope_handler_builds_safe_reprocessing_command() -> None:
    """Добавляет подтверждённый комментарий только выбранной позиции."""
    state = ConversationState(
        pending_comment_items=[
            ExtractedItem(product_query="Сироп Роза", quantity=5, unit="шт"),
            ExtractedItem(product_query="Сироп Тархун", quantity=3, unit="шт"),
        ],
        pending_comment_text="привезти утром",
        pending_comment_global_comment="на завтра",
        stage=SessionStage.AWAIT_COMMENT_SCOPE,
    )

    outcome = CommentScopeHandler().handle(
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            comment_scope_action="items",
            comment_target_indexes=[1],
            confidence=1,
        ),
        state,
    )

    assert outcome.result is None
    assert outcome.clear_event_text is True
    assert outcome.reprocess_command is not None
    assert outcome.reprocess_command.items[0].comment == ""
    assert outcome.reprocess_command.items[1].comment == "привезти утром"
    assert outcome.reprocess_command.global_comment == "на завтра"
    assert state.pending_comment_items == []
