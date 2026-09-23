"""Проверяет границы товара, количества и отдела в свободном списке."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from restaurant_bot.config import Settings
from restaurant_bot.conversation.routing.state_compatibility import StateCompatibilityPolicy
from restaurant_bot.domain.models import (
    CatalogProduct,
    ConversationState,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    TelegramEvent,
)
from restaurant_bot.input.media_recognition import InputRecognitionService
from restaurant_bot.input.telegram_interpretation import TelegramInputInterpreter
from restaurant_bot.parsing.commands.router import parse_text_command
from restaurant_bot.services.engine import ConversationEngine

BRIOCHE = "Булка Бриошь 400гр"
MUSTARD = "Горчица Зернистая CHATEL, ведро, 1 кг, 6 шт/кор, Франция"


@pytest.mark.parametrize(
    "text",
    [
        f"{BRIOCHE} 5 зал, {MUSTARD} 6 бар",
        f"{BRIOCHE} 5шт на зал, {MUSTARD} 6шт в бар.",
        f"На зал {BRIOCHE} 5; для отдела бара {MUSTARD} 6",
        f"{BRIOCHE} на зал 5 шт\n{MUSTARD} на бар 6 шт",
        f"Добавь {BRIOCHE} 5 зал\n{MUSTARD} 6 бар",
        f"{BRIOCHE} 5 зал и {MUSTARD} 6 бар",
    ],
)
def test_catalog_commas_and_packaging_keep_order_fields(text: str) -> None:
    """Сохраняет полные названия из лога и независимые количества с отделами."""
    command = parse_text_command(text)
    assert command.intent is Intent.ADD_ITEMS
    assert [(i.product_query, i.quantity, i.department) for i in command.items] == [
        (BRIOCHE, 5, "Зал"),
        (MUSTARD, 6, "Бар"),
    ]
    assert all(not i.comment and i.source_department == i.department for i in command.items)


@pytest.mark.parametrize("separator", [",", ", ", "\n", "; ", " и "])
def test_mixed_assignments_do_not_share_department(separator: str) -> None:
    """Неразмеченный сосед остаётся без отдела и не пропадает из списка."""
    command = parse_text_command(f"Хлеб2шт на зал{separator}Молоко 3 л")
    assert [(i.product_query, i.quantity, i.department) for i in command.items] == [
        ("Хлеб", 2, "Зал"),
        ("Молоко", 3, ""),
    ]


@pytest.mark.parametrize(
    "text", ["Булка Бриошь 400гр на зал", f"{MUSTARD} на зал", "Рыба 300-500г на кухню"]
)
def test_department_does_not_turn_packaging_into_quantity(text: str) -> None:
    """Само указание отдела не подтверждает число из фасовки или диапазона."""
    command = parse_text_command(text)
    assert len(command.items) == 1
    assert command.items[0].quantity is None


@pytest.mark.parametrize(
    "text",
    ["не добавляй хлеб 2 шт на зал", "удали хлеб 2 шт на зал", "комментарий: хлеб 2 шт на зал"],
)
def test_department_cannot_override_another_action(text: str) -> None:
    """Маркер отдела не превращает удаление, отрицание или комментарий в добавление."""
    assert parse_text_command(text).intent is not Intent.ADD_ITEMS


def test_logged_department_typo_does_not_become_comment() -> None:
    """Одна однозначная опечатка в явном отделе не загрязняет название и комментарий."""
    command = parse_text_command("горчица зернистая 10 шт на зал, хдеб бородинский на кухя")
    assert [(i.product_query, i.quantity, i.department) for i in command.items] == [
        ("горчица зернистая", 10, "Зал"),
        ("хдеб бородинский", None, "Кухня"),
    ]
    assert all(not i.comment for i in command.items)


@pytest.mark.parametrize("text", ["Вино белое для кухни", "Вино белое для кухни 2 л"])
def test_catalog_purpose_is_not_an_assignment(text: str) -> None:
    """Слова назначения до количества остаются признаком каталожного товара."""
    command = parse_text_command(text)
    assert all(not i.department for i in command.items)
    assert "для кухни" in command.items[0].product_query


def test_short_word_is_not_autocorrected_into_department() -> None:
    """Не заменяет способ приготовления «на пару» похожим названием Бара."""
    command = parse_text_command("Хлеб на пару 2 шт")
    assert command.items[0].department == ""
    assert "на пару" in command.items[0].product_query


@pytest.mark.parametrize(
    "text",
    [
        "Хлеб 2 шт на зал.",
        "Хлеб2штназал",
        "На зал 2 шт Хлеб",
        "отдел зала: Хлеб 2 шт",
        "Хлеб 2 шт в подразделение зала, пожалуйста!",
        "Хлеб две штуки на зал",
    ],
)
def test_quantity_and_department_word_order_variants(text: str) -> None:
    """Проверяет порядок слов, падеж, слитные числа и пунктуацию расшифровки."""
    command = parse_text_command(text)
    assert command.intent is Intent.ADD_ITEMS
    assert len(command.items) == 1
    item = command.items[0]
    assert item.product_query.casefold() == "хлеб"
    assert item.quantity == 2 and item.department == "Зал" and item.comment == ""


def test_decimal_comma_is_not_a_product_separator() -> None:
    """Сохраняет дробные количества при запятой между соседними позициями."""
    command = parse_text_command("Рис 2,5 кг зал,Мука 1,5 кг бар")
    assert [(i.product_query, i.quantity, i.department) for i in command.items] == [
        ("Рис", 2.5, "Зал"),
        ("Мука", 1.5, "Бар"),
    ]


def test_department_markers_split_logged_items_without_punctuation() -> None:
    """Сохраняет отдельный заказ после первого отдела без запятой между товарами."""
    text = (
        "Хрен столовый Домашний, Кал-й,160грт/Б, Россия (12/1) 10 шт на бар "
        "Горчица Дижонская CHATEL, ведро, 1 кг, 6 шт/кор, Франция 5 на зал"
    )

    command = parse_text_command(text)

    assert command.intent is Intent.ADD_ITEMS
    assert [(i.product_query, i.quantity, i.department) for i in command.items] == [
        ("Хрен столовый Домашний, Кал-й,160грт/Б, Россия (12/1)", 10, "Бар"),
        ("Горчица Дижонская CHATEL, ведро, 1 кг, 6 шт/кор, Франция", 5, "Зал"),
    ]


def test_department_markers_keep_two_items_without_quantities() -> None:
    """Просит два количества, если отделы разделяют товары без знаков препинания."""
    command = parse_text_command("Курица Бедро Филе Без Шкуры на бар Икра красная (кг) зал")

    assert command.intent is Intent.ADD_ITEMS
    assert [(i.product_query, i.quantity, i.department) for i in command.items] == [
        ("Курица Бедро Филе Без Шкуры", None, "Бар"),
        ("Икра красная (кг)", None, "Зал"),
    ]


@pytest.mark.parametrize("kind", [InputKind.TEXT, InputKind.VOICE])
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Хрен столовый Домашний, Кал-й,160грт/Б, Россия (12/1) 10 шт на бар "
            "Горчица Дижонская CHATEL, ведро, 1 кг, 6 шт/кор, Франция 5 на зал",
            [
                ("Хрен столовый Домашний, Кал-й,160грт/Б, Россия (12/1)", 10, "Бар"),
                ("Горчица Дижонская CHATEL, ведро, 1 кг, 6 шт/кор, Франция", 5, "Зал"),
            ],
        ),
        (
            "Курица Бедро Филе Без Шкуры на бар Икра красная (кг) зал",
            [
                ("Курица Бедро Филе Без Шкуры", None, "Бар"),
                ("Икра красная (кг)", None, "Зал"),
            ],
        ),
    ],
)
def test_unseparated_logged_items_reach_draft_without_ai(
    settings: Settings,
    kind: InputKind,
    text: str,
    expected: list[tuple[str, int | None, str]],
) -> None:
    """Проводит две позиции без разделителя из текста и голоса до черновика."""
    provider = MagicMock()
    provider.transcribe.return_value = text
    provider.settings.openai_transcribe_model = "same"
    provider.settings.openai_transcribe_fallback_model = "same"
    interpreter = TelegramInputInterpreter(
        provider, lambda: MagicMock(), StateCompatibilityPolicy()
    )
    state = ConversationState()
    command = (
        InputRecognitionService(MagicMock(), provider)._recognize_voice(
            Path("voice.ogg"), state, interpreter.interpret_text
        )
        if kind is InputKind.VOICE
        else interpreter.interpret_text(text, state)
    )
    provider.parse_text.assert_not_called()
    catalog = [
        CatalogProduct(product_id=str(index), name=name, unit="шт", supplier="Поставщик")
        for index, (name, _, _) in enumerate(expected)
    ]
    event = TelegramEvent(update_id=887, chat_id="123456", input_type=kind, text=text)
    result = ConversationEngine(settings).handle(event, command, state, catalog)

    assert [
        (item.source_query, item.quantity, item.department) for item in result.state.cart
    ] == expected


@pytest.mark.parametrize(
    ("text", "second_query"),
    [
        (
            "Горчица острая 5 зал, Биттер Люксардо 0,75 л 6 бар",
            "Биттер Люксардо 0,75 л",
        ),
        (
            "Горчица острая 5 зал, Биттер Люксардо 0,75 л6бар",
            "Биттер Люксардо 0,75 л",
        ),
        (
            "Горчица острая 5 зал, Биттер Люксардо 0,75л6бар",
            "Биттер Люксардо 0,75л",
        ),
    ],
)
def test_order_count_after_product_size_keeps_each_department_and_name(
    text: str,
    second_query: str,
) -> None:
    """Не путает фасовку товара с заказанным количеством перед отделом."""
    command = parse_text_command(text)

    assert command.intent is Intent.ADD_ITEMS
    assert [
        (item.product_query, item.quantity, item.unit, item.department, item.comment)
        for item in command.items
    ] == [
        ("Горчица острая", 5, "", "Зал", ""),
        (second_query, 6, "", "Бар", ""),
    ]


@pytest.mark.parametrize("kind", [InputKind.TEXT, InputKind.VOICE])
def test_compact_department_count_reaches_cart_without_ai_comment_pollution(
    settings: Settings,
    kind: InputKind,
) -> None:
    """Проводит расшифрованную команду через маршрут и сохраняет два заказа отдельно."""
    text = "Горчица острая 5 зал, Биттер Люксардо 0,75 л6бар"
    provider = MagicMock()
    provider.transcribe.return_value = text
    provider.settings.openai_transcribe_model = "same"
    provider.settings.openai_transcribe_fallback_model = "same"
    interpreter = TelegramInputInterpreter(
        provider,
        lambda: MagicMock(),
        StateCompatibilityPolicy(),
    )
    state = ConversationState()
    command = (
        InputRecognitionService(MagicMock(), provider)._recognize_voice(
            Path("voice.ogg"), state, interpreter.interpret_text
        )
        if kind is InputKind.VOICE
        else interpreter.interpret_text(text, state)
    )
    provider.parse_text.assert_not_called()

    catalog = [
        CatalogProduct(
            product_id="mustard",
            name="Горчица острая",
            unit="шт",
            supplier="Поставщик",
        ),
        CatalogProduct(
            product_id="bitter",
            name="Биттер Люксардо 0,75 л",
            unit="бут",
            supplier="Поставщик",
        ),
    ]
    event = TelegramEvent(update_id=886, chat_id="123456", input_type=kind, text=text)
    result = ConversationEngine(settings).handle(event, command, state, catalog)

    assert [(item.quantity, item.department, item.comment) for item in result.state.cart] == [
        (5, "Зал", ""),
        (6, "Бар", ""),
    ]


@pytest.mark.parametrize("kind", [InputKind.TEXT, InputKind.VOICE])
@pytest.mark.parametrize("quantities", [True, False])
def test_logged_catalog_items_reach_draft_with_local_evidence(
    settings: Settings, kind: InputKind, quantities: bool
) -> None:
    """Проводит текст и расшифровку через каталог, сохранение и финальную проверку."""
    text = (
        f"{BRIOCHE} 5 зал, {MUSTARD} 6 бар" if quantities else f"{BRIOCHE} на зал; {MUSTARD} на бар"
    )
    provider = MagicMock()
    provider.transcribe.return_value = text
    provider.settings.openai_transcribe_model = "same"
    provider.settings.openai_transcribe_fallback_model = "same"
    interpreter = TelegramInputInterpreter(
        provider, lambda: MagicMock(), StateCompatibilityPolicy()
    )
    state = ConversationState()
    command = (
        InputRecognitionService(MagicMock(), provider)._recognize_voice(
            Path("voice.ogg"), state, interpreter.interpret_text
        )
        if kind is InputKind.VOICE
        else interpreter.interpret_text(text, state)
    )
    provider.parse_text.assert_not_called()
    catalog = [
        CatalogProduct(product_id="brioche", name=BRIOCHE, unit="шт", supplier="Пекарня"),
        CatalogProduct(product_id="mustard", name=MUSTARD, unit="шт", supplier="Поставщик"),
    ]
    event = TelegramEvent(update_id=885, chat_id="123456", input_type=kind, text=text)
    engine = ConversationEngine(settings)
    result = engine.handle(event, command, state, catalog)
    restored = ConversationState.model_validate_json(result.state.model_dump_json())
    assert [i.source_query for i in restored.cart] == [BRIOCHE, MUSTARD]
    assert [i.department for i in restored.cart] == ["Зал", "Бар"]
    assert [i.quantity for i in restored.cart] == ([5, 6] if quantities else [None, None])
    assert all(not i.comment for i in restored.cart)
    if quantities:
        assert all(i.status is ItemStatus.MATCHED for i in restored.cart)
        review = engine.handle(
            event, ParsedCommand(intent=Intent.SHOW_FINAL_REVIEW), restored, catalog
        )
        assert "5 шт · Зал" in review.reply.text
        assert "6 шт · Бар" in review.reply.text
        assert [engine._submission_department_quantities(i) for i in restored.cart] == [
            [("Зал", 5)],
            [("Бар", 6)],
        ]
