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
    """Проверяет, что голос восстановление supports количество до товар."""
    payload = {"intent": Intent.UNKNOWN, "items": []}

    restored = recover_omitted_explicit_items(payload, "10 штук сироп роза")

    assert restored["intent"] is Intent.ADD_ITEMS
    assert [
        (item["product_query"], item["quantity"], item["unit"]) for item in restored["items"]
    ] == [
        ("сироп роза", 10.0, "шт"),
    ]


def test_voice_recovery_does_not_invent_quantity_that_was_not_spoken() -> None:
    """Проверяет, что голос восстановление выполняет не выдумывает количество что was не произнесённый."""
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


def test_shared_source_line_does_not_copy_last_quantity_to_every_product() -> None:
    """Берёт количества по строкам исходного сообщения, а не из копии ИИ."""
    source = "Сироп Снгря - 2 шт\nКордиал Апельсин - 3 шт"
    items = [
        {
            "product_query": "Сироп Снгря",
            "quantity": 2,
            "unit": "шт",
            "source_line": source,
        },
        {
            "product_query": "Кордиал Апельсин",
            "quantity": 3,
            "unit": "шт",
            "source_line": source,
        },
    ]

    restored = restore_explicit_order_terms(items, source)

    assert [(item["quantity"], item["unit"]) for item in restored] == [
        (2, "шт"),
        (3, "шт"),
    ]


def test_empty_voice_add_items_uses_the_source_recovery_card(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что пустой результат голос добавление позиции использует исходный восстановление карточка."""
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
    """Проверяет, что unknown пустой результат голос использует тот же исходный восстановление карточка."""
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
    """Проверяет, что голос комментарий ложная позиция является не created как a отдельно товар."""
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
    """Проверяет, что реальный аудио перекрёстная позиция ложная позиция является removed."""
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
    """Проверяет, что общий комментарий является не duplicated как a товар."""
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


def test_local_comments_are_recovered_when_ai_leaves_them_only_in_source_lines() -> None:
    """Восстанавливает разные локальные комментарии и отделяет их от общего."""
    source = (
        "срп трхн в бутылках 10 штук "
        "срп роза 1 штука в банках и всё желательно на завтра"
    )
    payload = {
        "intent": Intent.ADD_ITEMS,
        "global_comment": "всё желательно на завтра",
        "items": [
            {
                "product_query": "срп трхн",
                "quantity": 10,
                "unit": "штук",
                "comment": "",
                "source_line": "срп трхн в бутылках 10 штук",
            },
            {
                "product_query": "срп роза",
                "quantity": 1,
                "unit": "штука",
                "comment": "",
                "source_line": "срп роза 1 штука в банках",
            },
        ],
    }

    restored = recover_omitted_explicit_items(payload, source)

    assert restored["global_comment"] == "желательно на завтра"
    assert [item["comment"] for item in restored["items"]] == [
        "в бутылках",
        "в банках",
    ]


def test_shared_source_line_is_not_guessed_as_an_item_comment() -> None:
    """Не приписывает одному товару остаток общей строки с несколькими товарами."""
    source = "сироп тархун 10 штук и сироп роза 1 штука"
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {
                "product_query": "сироп тархун",
                "quantity": 10,
                "unit": "шт",
                "comment": "",
                "source_line": source,
            },
            {
                "product_query": "сироп роза",
                "quantity": 1,
                "unit": "шт",
                "comment": "",
                "source_line": source,
            },
        ],
    }

    restored = recover_omitted_explicit_items(payload, source)

    assert [item.get("comment", "") for item in restored["items"]] == ["", ""]


def test_voice_beef_cannot_gain_an_unspoken_qualifier_or_auto_select(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что голос говядина не может получает an unspoken qualifier или auto select."""
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
