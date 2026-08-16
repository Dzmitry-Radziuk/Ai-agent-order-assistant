"""Проверяет границы семантических команд для комментариев и количества."""

from unittest.mock import Mock

import pytest

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
from restaurant_bot.parsing.semantic_routing import normalize_comment_proposal
from restaurant_bot.services.engine import ConversationEngine


class TranscriptRecognizer:
    """Передаёт голосовой транскрипт в тот же разбор, что и текст."""

    def recognize_media(self, event, state, parse_text, processing_message_id=None):  # type: ignore[no-untyped-def]
        """Использует переданный общий обработчик транскрипта."""
        return parse_text(event.text, state)


def test_existing_comment_target_never_becomes_catalog_item() -> None:
    """Распознаёт явный комментарий к существующему товару без вызова каталога."""
    command = infer_intent("Добавь к горчице домашней комментарий, желательно свежий")

    assert command.intent is Intent.EDIT_COMMENT
    assert command.items == []
    assert command.comment_target_query == "горчице домашней"
    assert command.comment_text == "желательно свежий"


@pytest.mark.parametrize(
    "phrase",
    [
        "Удаляй комментарий о горчице дижонской",
        "Удали комментарий про зеленый лук",
    ],
)
def test_comment_removal_prepositions_keep_existing_item_semantics(phrase: str) -> None:
    """Распознаёт формы «о» и «про» как удаление комментария позиции."""
    command = infer_intent(phrase)

    assert command.intent is Intent.EDIT_COMMENT
    assert command.comment_action == "remove"
    assert command.comment_scope == "item"
    assert command.items == []
    assert command.comment_target_query


def test_comment_removal_o_form_mutates_only_existing_item(settings) -> None:  # type: ignore[no-untyped-def]
    """Удаляет комментарий названной позиции без обращения к каталогу."""
    item = CartItem(
        id="mustard",
        source_query="Горчица дижонская",
        comment="желательно свежая",
        comment_source=CommentSource.SEMANTIC,
        status=ItemStatus.MATCHED,
    )
    event = TelegramEvent(
        update_id=10,
        chat_id="comments",
        input_type=InputKind.TEXT,
        text="Удаляй комментарий о горчице дижонской",
    )
    result = ConversationEngine(settings).handle(
        event,
        infer_intent(event.text),
        ConversationState(cart=[item]),
        [],
    )

    assert result.state.cart[0].comment == ""
    assert result.state.cart[0].id == "mustard"


def test_ai_global_comment_proposal_becomes_order_edit() -> None:
    """Не оставляет общий комментарий без товаров в маршруте добавления позиций."""
    proposal = ParsedCommand(
        intent=Intent.ADD_MORE,
        global_comment="привезти завтра",
    )

    command = normalize_comment_proposal(
        "Добавь в общий комментарий привезти завтра",
        proposal,
    )

    assert command.intent is Intent.EDIT_COMMENT
    assert command.comment_action == "add"
    assert command.comment_scope == "order"
    assert command.comment_text == "привезти завтра"
    assert command.items == []


@pytest.mark.parametrize(
    "phrase",
    [
        "Добавь в общий комментарий привезти завтра к 20:00",
        "Добавь в комментарии привезти к восьми для всей заявки",
    ],
)
def test_explicit_global_comment_forms_are_order_edits(phrase: str) -> None:
    """Распознаёт явные формы общего комментария без создания товаров."""
    command = infer_intent(phrase)

    assert command.intent is Intent.EDIT_COMMENT
    assert command.comment_action == "add"
    assert command.comment_scope == "order"
    assert command.items == []
    assert command.comment_text


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


def test_comment_scope_resolves_named_item_without_catalog() -> None:
    """Выбирает названную позицию только среди текущих pending товаров."""
    provider = Mock()
    provider.parse_text.return_value = ParsedCommand(intent=Intent.UNKNOWN)
    state = ConversationState(
        pending_comment_items=[
            ExtractedItem(product_query="Горчица домашняя"),
            ExtractedItem(product_query="Зеленый лук"),
        ],
        pending_comment_text="привезти завтра",
        stage=SessionStage.AWAIT_COMMENT_SCOPE,
    )
    interpreter = TelegramInputInterpreter(provider, lambda: Mock(), StateCompatibilityPolicy())

    command = interpreter.interpret_text("только для горчицы", state)

    assert command.intent is Intent.ADD_ITEMS
    assert command.comment_scope_action == "items"
    assert command.comment_target_indexes == [0]
    provider.resolve_comment_scope.assert_not_called()


def test_voice_comment_scope_uses_text_resolver_for_all_named_and_order_cases() -> None:
    """Голосовой транскрипт использует общий resolver области комментария."""
    provider = Mock()
    provider.parse_text.return_value = ParsedCommand(intent=Intent.UNKNOWN)
    provider.resolve_comment_scope.side_effect = AssertionError("AI fallback is not expected")
    interpreter = TelegramInputInterpreter(
        provider,
        lambda: TranscriptRecognizer(),
        StateCompatibilityPolicy(),
    )
    state = ConversationState(
        pending_comment_items=[
            ExtractedItem(product_query="Горчица домашняя"),
            ExtractedItem(product_query="Зеленый лук"),
        ],
        pending_comment_text="привезти завтра",
        stage=SessionStage.AWAIT_COMMENT_SCOPE,
    )

    commands = [
        interpreter.interpret(
            TelegramEvent(
                update_id=index,
                chat_id="voice-scope",
                input_type=InputKind.VOICE,
                text=phrase,
            ),
            state,
        )
        for index, phrase in enumerate(
            ("только для горчицы", "для всех товаров", "для всей заявки", "не добавлять"),
            start=1,
        )
    ]

    assert commands[0].comment_target_indexes == [0]
    assert commands[1].comment_scope_action == "items"
    assert commands[1].comment_target_indexes == [0, 1]
    assert commands[2].comment_scope_action == "order"
    assert commands[3].intent is Intent.CANCEL


def test_ambiguous_named_comment_scope_is_safe() -> None:
    """Не выбирает товар, если названная область не доказана."""
    provider = Mock()
    provider.parse_text.return_value = ParsedCommand(intent=Intent.UNKNOWN)
    provider.resolve_comment_scope.side_effect = AssertionError("ambiguous scope must stay local")
    state = ConversationState(
        pending_comment_items=[
            ExtractedItem(product_query="Горчица домашняя"),
            ExtractedItem(product_query="Горчица дижонская"),
        ],
        pending_comment_text="свежий",
        stage=SessionStage.AWAIT_COMMENT_SCOPE,
    )
    interpreter = TelegramInputInterpreter(provider, lambda: Mock(), StateCompatibilityPolicy())

    command = interpreter.interpret_text("для горчицы", state)

    assert command.intent is Intent.CLARIFY_CURRENT
    assert command.comment_scope_action == "ambiguous"
    assert command.comment_target_indexes == []


def test_unit_mismatch_manual_quantity_uses_catalog_unit(settings) -> None:  # type: ignore[no-untyped-def]
    """Ручной ответ количества после unit mismatch принимает ожидаемую единицу."""
    item = CartItem(
        id="mustard",
        source_query="Горчица",
        catalog_product_id="mustard-product",
        catalog_name="Горчица",
        quantity=3,
        unit="шт",
        catalog_unit="кг",
        status=ItemStatus.UNIT_MISMATCH,
    )
    state = ConversationState(
        cart=[item],
        current_issue_item_id=item.id,
        stage=SessionStage.REVIEW,
    )
    engine = ConversationEngine(settings)
    entered = engine.handle(
        TelegramEvent(update_id=40, chat_id="qty", input_type=InputKind.TEXT, text="ввести другое"),
        ParsedCommand(intent=Intent.ENTER_OTHER_QUANTITY, text="ввести другое"),
        state,
        [],
    )
    assert entered.state.stage is SessionStage.AWAIT_UNIT_QUANTITY

    provider = Mock()
    interpreter = TelegramInputInterpreter(provider, lambda: Mock(), StateCompatibilityPolicy())
    reply = interpreter.interpret_text("5", entered.state)
    result = engine.handle(
        TelegramEvent(update_id=41, chat_id="qty", input_type=InputKind.TEXT, text="5"),
        reply,
        entered.state,
        [],
    )

    assert result.state.cart[0].quantity == 5
    assert result.state.cart[0].unit == "кг"
    assert result.state.cart[0].status is ItemStatus.MATCHED


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
