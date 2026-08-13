"""Проверяет channel-neutral границу прикладного диалога."""

from restaurant_bot.application.conversation import ConversationApplication, ConversationInput
from restaurant_bot.domain.models import CatalogProduct, ConversationState, InputKind
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
        [CatalogProduct(product_id="chicken", name="Курица", unit="кг")],
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
