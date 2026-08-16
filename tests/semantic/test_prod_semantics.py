"""Проверяет границы семантических команд для комментариев и количества."""

from unittest.mock import Mock

from restaurant_bot.conversation.routing.state_compatibility import StateCompatibilityPolicy
from restaurant_bot.domain.models import (
    CartItem,
    CommentSource,
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


def test_existing_comment_target_never_becomes_catalog_item() -> None:
    """Распознаёт явный комментарий к существующему товару без вызова каталога."""
    command = infer_intent("Добавь к горчице домашней комментарий, желательно свежий")

    assert command.intent is Intent.EDIT_COMMENT
    assert command.items == []
    assert command.comment_target_query == "горчице домашней"
    assert command.comment_text == "желательно свежий"


def test_ai_product_placeholder_cannot_replace_deterministic_batch() -> None:
    """Сохраняет товары и общее количество при неполном ответе ИИ."""
    provider = Mock()
    provider.parse_text.return_value = infer_intent("спасибо")
    interpreter = TelegramInputInterpreter(provider, lambda: Mock(), StateCompatibilityPolicy())

    command = interpreter.interpret_text(
        "горчица и лук всё по пять штук привезти завтра",
        ConversationState(),
    )

    assert command.intent is Intent.ADD_ITEMS
    assert [(item.product_query, item.quantity, item.unit) for item in command.items] == [
        ("горчица", 5, "шт"),
        ("лук", 5, "шт"),
    ]
    assert command.global_comment == ""
    assert [item.comment for item in command.items] == ["привезти завтра", "привезти завтра"]


def test_pending_comment_scope_uses_deterministic_text_before_ai() -> None:
    """Распознаёт области комментария без дополнительного вызова ИИ."""
    provider = Mock()
    provider.parse_text.return_value = ParsedCommand(intent=Intent.UNKNOWN)
    state = ConversationState(
        pending_comment_items=[ExtractedItem(product_query="сыр")],
        pending_comment_text="свежий",
        stage=SessionStage.AWAIT_COMMENT_SCOPE,
    )
    interpreter = TelegramInputInterpreter(provider, lambda: Mock(), StateCompatibilityPolicy())

    command = interpreter.interpret_text("только к последнему", state)

    assert command.comment_scope_action == "items"
    assert command.comment_target_indexes == [0]
    provider.resolve_comment_scope.assert_not_called()


def test_cancel_comment_reprocesses_pending_products(settings) -> None:  # type: ignore[no-untyped-def]
    """Отмена комментария сохраняет ожидающие товары и не отменяет добавление."""
    state = ConversationState(
        cart=[CartItem(id="old", source_query="молоко", status=ItemStatus.MATCHED)],
        pending_comment_items=[ExtractedItem(product_query="сыр", quantity=2, unit="кг")],
        pending_comment_text="свежий",
        stage=SessionStage.AWAIT_COMMENT_SCOPE,
    )
    event = TelegramEvent(
        update_id=1,
        chat_id="comments",
        input_type=InputKind.TEXT,
        text="не добавлять",
    )
    interpreter = TelegramInputInterpreter(Mock(), lambda: Mock(), StateCompatibilityPolicy())
    command = interpreter.interpret_text(event.text, state)
    result = ConversationEngine(settings).handle(
        event,
        command,
        state,
        [],
    )

    assert state.pending_comment_items == []
    assert [item.source_query for item in result.state.cart] == ["молоко", "сыр"]


def test_clear_all_comments_preserves_catalog_fields(settings) -> None:  # type: ignore[no-untyped-def]
    """Очищает пользовательские комментарии, не меняя каталожную позицию."""
    item = CartItem(
        id="cheese",
        source_query="сыр",
        catalog_product_id="catalog-cheese",
        catalog_name="Сыр",
        quantity=3,
        unit="кг",
        catalog_unit="кг",
        comment="свежий",
        comment_source=CommentSource.SEMANTIC,
        catalog_comment="каталожная фасовка",
        catalog_comment_source=CommentSource.CATALOG,
        status=ItemStatus.MATCHED,
    )
    state = ConversationState(cart=[item])
    event = TelegramEvent(
        update_id=2,
        chat_id="comments",
        input_type=InputKind.TEXT,
        text="удали все комментарии",
    )
    result = ConversationEngine(settings).handle(event, infer_intent(event.text), state, [])

    current = result.state.cart[0]
    assert current.comment == ""
    assert current.order_comment_fragments == []
    assert current.catalog_comment == "каталожная фасовка"
    assert current.catalog_product_id == "catalog-cheese"
    assert current.quantity == 3


def test_bare_quantity_is_owned_by_missing_quantity_modal() -> None:
    """Различает короткое количество и выбор кандидата по текущему состоянию."""
    provider = Mock()
    provider.parse_text.return_value = infer_intent("5")
    interpreter = TelegramInputInterpreter(provider, lambda: Mock(), StateCompatibilityPolicy())
    state = ConversationState(
        cart=[CartItem(id="missing", source_query="сыр", status=ItemStatus.MISSING_QTY)],
        current_issue_item_id="missing",
        stage=SessionStage.AWAIT_UNIT_QUANTITY,
    )

    quantity_command = interpreter.interpret_text("5", state)
    candidate_state = state.model_copy(deep=True)
    candidate_state.cart[0].status = ItemStatus.AMBIGUOUS
    candidate_state.cart[0].candidates = []
    candidate_command = interpreter.interpret_text("5", candidate_state)

    assert quantity_command.intent is Intent.EDIT_QUANTITY
    assert quantity_command.edit_quantity == 5
    assert candidate_command.intent is Intent.SELECT_CANDIDATE
    assert candidate_command.selected_index == 5
