import pytest

from restaurant_bot.domain.models import (
    Candidate,
    CartItem,
    CatalogProduct,
    ConversationState,
    ExtractedItem,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    SessionStage,
    TelegramEvent,
)
from restaurant_bot.services.engine import ConversationEngine
from restaurant_bot.services.parser import infer_intent, parse_callback


def test_explicit_candidate_selection_uses_selected_catalog_row(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что явный кандидат выбор использует выбранный каталог строка."""
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
    """Проверяет, что произнесённый кандидат порядковые номера имеют детерминированный выбор."""
    command = infer_intent(phrase)
    assert command.intent is Intent.SELECT_CANDIDATE
    assert command.selected_index == selected_index


def test_unique_partial_candidate_name_selects_only_that_candidate(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что unique partial кандидат название выбирает только что кандидат."""
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
    """Проверяет, что common кандидат слово выполняет не без подтверждения choose a товар."""
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
    assert "Нашлось несколько похожих товаров" not in result.reply.text
    assert "По запросу «<b>сироп</b>» найдено несколько вариантов." in result.reply.text


def test_unrecognized_voice_on_candidate_card_cannot_create_a_new_product(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что нераспознанный голос on кандидат карточка не может create a новый товар."""
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


def test_selecting_typo_candidate_finishes_two_item_list_and_drops_numeric_garbage(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    """После выбора продолжает список и не возвращает старый цифровой мусор."""
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(product_id="rose", name="Сироп Роза, 1л", unit="шт"),
        CatalogProduct(product_id="tarhun", name="Сироп Тархун, 1л", unit="шт"),
        CatalogProduct(product_id="feijoa", name="Сироп Фейхоа, 1л", unit="шт"),
        CatalogProduct(product_id="hazelnut", name="Сироп Фундук, 1л", unit="шт"),
        CatalogProduct(product_id="sangria", name="Сироп Сангрия, 1л", unit="шт"),
        CatalogProduct(product_id="cordial", name="Кордиал Апельсин", unit="шт"),
    ]
    stale = CartItem(
        id="numeric",
        source_query="12345",
        status=ItemStatus.NOT_FOUND,
    )
    state = ConversationState(cart=[stale], current_issue_item_id=stale.id)
    text = "Сироп Снгря - 2 шт\nКордиал Апельсин - 3 шт"
    added = engine.handle(
        TelegramEvent(update_id=10, chat_id="10", input_type=InputKind.TEXT, text=text),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            text=text,
            items=[
                ExtractedItem(product_query="Сироп Снгря", quantity=2, unit="шт"),
                ExtractedItem(product_query="Кордиал Апельсин", quantity=3, unit="шт"),
            ],
        ),
        state,
        catalog,
    )

    assert [item.source_query for item in added.state.cart] == [
        "Сироп Снгря",
        "Кордиал Апельсин",
    ]
    assert added.state.current_item() is not None
    assert added.state.current_item().candidates[0].product_id == "sangria"

    selected = engine.handle(
        TelegramEvent(update_id=11, chat_id="10", input_type=InputKind.CALLBACK),
        parse_callback("v2:sel:0:0"),
        added.state,
        catalog,
    )

    assert selected.state.stage is SessionStage.AWAIT_ADD_MORE_CONFIRM
    assert [
        (item.catalog_product_id, item.quantity, item.status) for item in selected.state.cart
    ] == [
        ("sangria", 2, ItemStatus.MATCHED),
        ("cordial", 3, ItemStatus.MATCHED),
    ]
    assert "Добавить ещё товары?" in selected.reply.text


@pytest.mark.parametrize("input_type", [InputKind.TEXT, InputKind.VOICE])
def test_ambiguous_product_uses_human_copy_for_text_and_voice(
    settings, input_type: InputKind
) -> None:  # type: ignore[no-untyped-def]
    """Показывает одинаковое понятное уточнение для текста и голоса."""
    result = ConversationEngine(settings).handle(
        TelegramEvent(update_id=20, chat_id="20", input_type=input_type, text="сироп 2 шт"),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            text="сироп 2 шт",
            items=[ExtractedItem(product_query="сироп", quantity=2, unit="шт")],
        ),
        ConversationState(),
        [
            CatalogProduct(product_id="rose", name="Сироп Роза, 1л", unit="шт"),
            CatalogProduct(product_id="tarhun", name="Сироп Тархун, 1л", unit="шт"),
        ],
    )

    assert "Нашлось несколько похожих товаров" not in result.reply.text
    assert "По запросу «<b>сироп</b>» найдено несколько вариантов." in result.reply.text
    assert "Уточните, какой товар вы имели в виду:" in result.reply.text


@pytest.mark.parametrize("input_type", [InputKind.TEXT, InputKind.VOICE])
def test_not_found_product_uses_human_copy_for_text_and_voice(
    settings, input_type: InputKind
) -> None:  # type: ignore[no-untyped-def]
    """Показывает одинаковое понятное отсутствие товара для текста и голоса."""
    result = ConversationEngine(settings).handle(
        TelegramEvent(
            update_id=21,
            chat_id="21",
            input_type=input_type,
            text="креветки королевские 2 шт",
        ),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            text="креветки королевские 2 шт",
            items=[
                ExtractedItem(
                    product_query="креветки королевские",
                    quantity=2,
                    unit="шт",
                )
            ],
        ),
        ConversationState(),
        [],
    )

    assert result.reply.text == (
        "⚠️ <b>Товар не найден</b>\n\n"
        "По вашему запросу «<b>креветки королевские</b>» ничего не найдено.\n\n"
        "Вы можете изменить название, отправить запрос менеджеру по снабжению "
        "или не добавлять товар."
    )
