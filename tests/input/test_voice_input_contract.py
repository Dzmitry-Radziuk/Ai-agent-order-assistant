from restaurant_bot.domain.models import (
    CatalogProduct,
    ConversationState,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    TelegramEvent,
)
from restaurant_bot.integrations.openai_client import (
    recover_omitted_explicit_items,
    restore_explicit_order_terms,
)
from restaurant_bot.services.engine import ConversationEngine


def test_voice_recovery_supports_quantity_before_product() -> None:
    payload = {"intent": Intent.UNKNOWN, "items": []}

    restored = recover_omitted_explicit_items(payload, "10 штук сироп роза")

    assert restored["intent"] is Intent.ADD_ITEMS
    assert [
        (item["product_query"], item["quantity"], item["unit"]) for item in restored["items"]
    ] == [
        ("сироп роза", 10.0, "шт"),
    ]


def test_voice_recovery_does_not_invent_quantity_that_was_not_spoken() -> None:
    items = [
        {
            "product_query": "бутылка воды",
            "quantity": None,
            "unit": "",
            "source_line": "бутылка воды",
        }
    ]

    restored = restore_explicit_order_terms(items, "бутылка воды")

    assert restored[0]["quantity"] is None
    assert restored[0]["unit"] == ""


def test_empty_voice_add_items_uses_the_source_recovery_card(settings) -> None:  # type: ignore[no-untyped-def]
    result = ConversationEngine(settings).handle(
        TelegramEvent(update_id=7, chat_id="7", input_type=InputKind.VOICE),
        ParsedCommand(intent=Intent.ADD_ITEMS),
        ConversationState(),
        [],
    )

    assert result.reply.text == (
        "⚠️ <b>Не удалось распознать голосовое сообщение</b>\n\n"
        "Повторите короче или отправьте текстом.\n\n"
        "Пример: <code>сироп роза 3 штуки</code>"
    )
    assert [[button.text, button.callback_data] for row in result.reply.rows for button in row] == [
        ["Обновить статусы", "v2:orders"],
        ["Добавить товары", "v2:add"],
    ]


def test_unknown_empty_voice_uses_the_same_source_recovery_card(settings) -> None:  # type: ignore[no-untyped-def]
    result = ConversationEngine(settings).handle(
        TelegramEvent(update_id=8, chat_id="8", input_type=InputKind.VOICE),
        ParsedCommand(intent=Intent.UNKNOWN),
        ConversationState(),
        [],
    )

    assert "Не удалось распознать голосовое сообщение" in result.reply.text
    assert [[button.callback_data for button in row] for row in result.reply.rows] == [
        ["v2:orders"],
        ["v2:add"],
    ]


def test_voice_comment_shadow_is_not_created_as_a_separate_product() -> None:
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {
                "product_query": "сироп роза",
                "quantity": None,
                "unit": "",
                "comment": "холодным",
                "source_line": "сироп роза холодным 3 штуки",
            },
            {
                "product_query": "холодным",
                "quantity": 3,
                "unit": "шт",
                "comment": ".",
                "source_line": "сироп роза холодным 3 штуки",
            },
        ],
    }

    restored = recover_omitted_explicit_items(payload, "сироп роза холодным 3 штуки")

    assert len(restored["items"]) == 1
    assert restored["items"][0]["product_query"] == "сироп роза"
    assert restored["items"][0]["quantity"] == 3
    assert restored["items"][0]["unit"] == "шт"
    assert restored["items"][0]["comment"] == "холодным"


def test_real_audio_cross_item_shadow_is_removed() -> None:
    source = "Сыровроза 5 штук, желательно холодным. И говядина 10 килограмм мраморная."
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {
                "product_query": "Сыровроза",
                "quantity": 5,
                "unit": "шт",
                "comment": "желательно холодным",
                "source_line": source,
            },
            {
                "product_query": "говядина мраморная",
                "quantity": 10,
                "unit": "кг",
                "comment": "",
                "source_line": source,
            },
            {
                "product_query": "желательно холодным. И говядина",
                "quantity": 10,
                "unit": "кг",
                "comment": "",
                "source_line": source,
            },
        ],
    }

    restored = recover_omitted_explicit_items(payload, source)

    assert len(restored["items"]) == 2
    assert [item["product_query"] for item in restored["items"]] == [
        "Сыровроза",
        "говядина мраморная",
    ]


def test_global_comment_is_not_duplicated_as_a_product() -> None:
    source = (
        "Сироп роза 5 штук только охлаждённый и говядина 10 килограмм без кожи. "
        "Всё привезти после девяти утра без звонка."
    )
    global_comment = "привезти после девяти утра без звонка"
    payload = {
        "intent": Intent.ADD_ITEMS,
        "global_comment": global_comment,
        "items": [
            {
                "product_query": "сироп роза",
                "quantity": 5,
                "unit": "шт",
                "comment": "только охлаждённый",
                "source_line": source,
            },
            {
                "product_query": "говядина",
                "quantity": 10,
                "unit": "кг",
                "comment": "без кожи",
                "source_line": source,
            },
            {
                "product_query": global_comment,
                "quantity": None,
                "unit": "",
                "comment": "",
                "source_line": source,
            },
        ],
    }

    restored = recover_omitted_explicit_items(payload, source)

    assert restored["global_comment"] == global_comment
    assert [item["product_query"] for item in restored["items"]] == [
        "сироп роза",
        "говядина",
    ]
    assert [item["comment"] for item in restored["items"]] == [
        "только охлаждённый",
        "без кожи",
    ]


def test_voice_beef_cannot_gain_an_unspoken_qualifier_or_auto_select(settings) -> None:  # type: ignore[no-untyped-def]
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {
                "product_query": "Говядина мраморная",
                "quantity": 10,
                "unit": "кг",
                "source_line": "Говядина 10 килограмм",
            }
        ],
    }
    restored = recover_omitted_explicit_items(payload, "Говядина 10 килограмм")
    command = ParsedCommand.model_validate(restored)
    catalog = [
        CatalogProduct(product_id="thin", name="Говядина Тонкий край", unit="кг"),
        CatalogProduct(product_id="bones", name="Говядина Кости ПРОДОЛЬНЫЙ распил", unit="кг"),
    ]

    result = ConversationEngine(settings).handle(
        TelegramEvent(update_id=9, chat_id="9", input_type=InputKind.VOICE),
        command,
        ConversationState(),
        catalog,
    )

    assert command.items[0].product_query == "Говядина"
    assert result.state.cart[0].source_query == "Говядина"
    assert result.state.cart[0].status is ItemStatus.AMBIGUOUS
    assert [candidate.name for candidate in result.state.cart[0].candidates] == [
        "Говядина Тонкий край",
        "Говядина Кости ПРОДОЛЬНЫЙ распил",
    ]
