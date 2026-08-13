"""Проверяет channel-neutral границу прикладного диалога."""

from restaurant_bot.application.conversation import ConversationApplication, ConversationInput
from restaurant_bot.application.conversation.actions import (
    decode_action_token,
    encode_action_token,
)
from restaurant_bot.domain.models import (
    Candidate,
    CartItem,
    CatalogProduct,
    ConversationState,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
)
from restaurant_bot.parsing.commands.api import infer_intent
from restaurant_bot.services.engine import ConversationEngine


def test_text_order_uses_neutral_input_without_telegram_event(settings) -> None:
    """Проводит текстовый заказ через общий use case без TelegramEvent."""
    interaction = ConversationInput(
        interaction_id=101,
        conversation_id="chat-101",
        actor_id="user-101",
        channel="fake-web",
        kind=InputKind.TEXT,
        text="огурцы 5 кг",
    )

    result = ConversationApplication(ConversationEngine(settings)).process(
        interaction,
        infer_intent(interaction.text),
        ConversationState(),
        [CatalogProduct(product_id="cucumber", name="Огурцы", unit="кг")],
    )

    assert result.state.cart
    assert result.state.cart[0].source_query == "огурцы"
    assert result.state.cart[0].quantity == 5
    assert result.effects.enqueue_submission is False
    assert result.view.text


def test_missing_quantity_modal_accepts_neutral_follow_up(settings) -> None:
    """Проводит ответ на ожидаемое количество через тот же use case."""
    state = ConversationState()
    initial = ConversationApplication(ConversationEngine(settings)).process(
        ConversationInput(
            interaction_id=102,
            conversation_id="chat-102",
            actor_id="user-102",
            channel="fake-max",
            kind=InputKind.TEXT,
            text="курица",
        ),
        infer_intent("курица"),
        state,
        [CatalogProduct(product_id="chicken", name="Курица", unit="кг")],
    )
    follow_up = ConversationApplication(ConversationEngine(settings)).process(
        ConversationInput(
            interaction_id=103,
            conversation_id="chat-102",
            actor_id="user-102",
            channel="fake-max",
            kind=InputKind.TEXT,
            text="5 кг",
        ),
        infer_intent("5 кг"),
        initial.state,
        [CatalogProduct(product_id="chicken", name="Курица", unit="кг")],
    )

    assert follow_up.state.cart[0].quantity == 5
    assert follow_up.state.cart[0].unit == "кг"


def test_telegram_action_codec_preserves_callback_corpus() -> None:
    """Сохраняет точные callback-токены при переходе через semantic action."""
    corpus = (
        "v2:skip:2:r7",
        "v2:sel:0:3:r8",
        "v2:qty:item-1:5:r9",
        "v2:cartpage:2:r10",
        "v2:review_submit:token:r11",
    )
    assert [
        encode_action_token(decode_action_token(token, "Действие", 0)) for token in corpus
    ] == list(corpus)


def test_comment_edit_uses_neutral_input(settings) -> None:
    """Применяет комментарий через общий use case без TelegramEvent."""
    state = ConversationState(
        cart=[
            CartItem(
                id="potato",
                source_query="картофель",
                quantity=5,
                unit="кг",
                status=ItemStatus.MATCHED,
                catalog_product_id="potato",
                catalog_name="Картофель",
            )
        ]
    )
    result = ConversationApplication(ConversationEngine(settings)).process(
        ConversationInput(
            interaction_id=104,
            conversation_id="chat-104",
            actor_id="user-104",
            channel="fake-web",
            kind=InputKind.TEXT,
            text="без кожуры",
        ),
        ParsedCommand(
            intent=Intent.EDIT_COMMENT,
            comment_target_query="картофель",
            comment_text="без кожуры",
        ),
        state,
        [CatalogProduct(product_id="potato", name="Картофель", unit="кг")],
    )

    assert result.state.cart[0].comment == "без кожуры"


def test_candidate_selection_uses_neutral_input(settings) -> None:
    """Выбирает кандидата через общий use case без TelegramEvent."""
    item = CartItem(
        id="cheese",
        source_query="сыр",
        quantity=2,
        unit="кг",
        status=ItemStatus.AMBIGUOUS,
        candidates=[
            Candidate(product_id="parmesan", name="Сыр Пармезан", unit="кг"),
            Candidate(product_id="gouda", name="Сыр Гауда", unit="кг"),
        ],
    )
    result = ConversationApplication(ConversationEngine(settings)).process(
        ConversationInput(
            interaction_id=105,
            conversation_id="chat-105",
            actor_id="user-105",
            channel="fake-max",
            kind=InputKind.TEXT,
            text="второй",
        ),
        ParsedCommand(intent=Intent.SELECT_CANDIDATE, selected_index=2),
        ConversationState(cart=[item], current_issue_item_id="cheese"),
        [
            CatalogProduct(product_id="parmesan", name="Сыр Пармезан", unit="кг"),
            CatalogProduct(product_id="gouda", name="Сыр Гауда", unit="кг"),
        ],
    )

    assert result.state.cart[0].catalog_product_id == "gouda"
    assert result.state.cart[0].status is ItemStatus.MATCHED
