import pytest

from restaurant_bot.domain.models import (
    Candidate,
    CartItem,
    CatalogProduct,
    ConversationState,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    TelegramEvent,
)
from restaurant_bot.services.engine import ConversationEngine
from restaurant_bot.services.parser import infer_intent


def test_explicit_candidate_selection_uses_selected_catalog_row(settings) -> None:  # type: ignore[no-untyped-def]
    engine = ConversationEngine(settings)
    item = CartItem(
        id="choice",
        source_query="сироп",
        quantity=5,
        unit="шт",
        status=ItemStatus.AMBIGUOUS,
        candidates=[
            Candidate(product_id="rose", name="Сироп Роза", supplier="Сиропы", unit="шт"),
            Candidate(product_id="feijoa", name="Сироп Фейхоа", supplier="Сиропы", unit="шт"),
        ],
    )
    state = ConversationState(cart=[item], current_issue_item_id="choice")
    result = engine.handle(
        TelegramEvent(update_id=1, chat_id="1", input_type=InputKind.CALLBACK),
        ParsedCommand(intent=Intent.SELECT_CANDIDATE, selected_index=2, callback_target="0"),
        state,
        [
            CatalogProduct(product_id="rose", name="Сироп Роза", supplier="Сиропы", unit="шт"),
            CatalogProduct(product_id="feijoa", name="Сироп Фейхоа", supplier="Сиропы", unit="шт"),
        ],
    )

    assert result.state.cart[0].status is ItemStatus.MATCHED
    assert result.state.cart[0].catalog_product_id == "feijoa"


@pytest.mark.parametrize(
    ("phrase", "selected_index"),
    [
        ("первый вариант", 1),
        ("второй вариант", 2),
        ("третий вариант", 3),
        ("четвёртый", 4),
        ("пятый вариант", 5),
    ],
)
def test_spoken_candidate_ordinals_have_deterministic_selection(
    phrase: str, selected_index: int
) -> None:
    command = infer_intent(phrase)
    assert command.intent is Intent.SELECT_CANDIDATE
    assert command.selected_index == selected_index


def test_unique_partial_candidate_name_selects_only_that_candidate(settings) -> None:  # type: ignore[no-untyped-def]
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(product_id="beef", name="Говядина Толстый край", supplier="Мясо", unit="кг"),
        CatalogProduct(product_id="rose", name="Сироп Роза", supplier="Сиропы", unit="шт"),
    ]
    item = CartItem(
        id="choice",
        source_query="товар",
        quantity=10,
        unit="кг",
        status=ItemStatus.AMBIGUOUS,
        candidates=[
            Candidate(product_id="beef", name="Говядина Толстый край", supplier="Мясо", unit="кг"),
            Candidate(product_id="rose", name="Сироп Роза", supplier="Сиропы", unit="шт"),
        ],
    )
    state = ConversationState(cart=[item], current_issue_item_id="choice")
    command = infer_intent("толстый край")
    result = engine.handle(
        TelegramEvent(update_id=1, chat_id="1", input_type=InputKind.VOICE, text="толстый край"),
        command,
        state,
        catalog,
    )

    assert result.state.cart[0].status is ItemStatus.MATCHED
    assert result.state.cart[0].catalog_product_id == "beef"


def test_common_candidate_word_does_not_silently_choose_a_product(settings) -> None:  # type: ignore[no-untyped-def]
    engine = ConversationEngine(settings)
    item = CartItem(
        id="choice",
        source_query="сироп",
        quantity=10,
        unit="шт",
        status=ItemStatus.AMBIGUOUS,
        candidates=[
            Candidate(product_id="rose", name="Сироп Роза", unit="шт"),
            Candidate(product_id="tarhun", name="Сироп Тархун", unit="шт"),
        ],
    )
    state = ConversationState(cart=[item], current_issue_item_id="choice")
    result = engine.handle(
        TelegramEvent(update_id=1, chat_id="1", input_type=InputKind.TEXT, text="сироп"),
        infer_intent("сироп"),
        state,
        [],
    )

    assert result.state.cart[0].status is ItemStatus.AMBIGUOUS
    assert "<b>Выберите подходящий товар</b>" in result.reply.text
    assert "По запросу: сироп" in result.reply.text


def test_unrecognized_voice_on_candidate_card_cannot_create_a_new_product(settings) -> None:  # type: ignore[no-untyped-def]
    item = CartItem(
        id="choice",
        source_query="сироп",
        quantity=10,
        unit="шт",
        status=ItemStatus.AMBIGUOUS,
        candidates=[
            Candidate(product_id="rose", name="Сироп Роза", unit="шт"),
            Candidate(product_id="tarhun", name="Сироп Тархун", unit="шт"),
        ],
    )
    state = ConversationState(cart=[item], current_issue_item_id="choice")
    result = ConversationEngine(settings).handle(
        TelegramEvent(update_id=1, chat_id="1", input_type=InputKind.VOICE, text="Был вариант"),
        ParsedCommand(intent=Intent.ADD_ITEMS, text="Был вариант"),
        state,
        [],
    )

    assert len(result.state.cart) == 1
    assert result.state.cart[0].status is ItemStatus.AMBIGUOUS
