"""Проверяет поведение, связанное с модулем «test voice input contract»."""

from restaurant_bot.domain.models import (
    CatalogProduct,
    ConversationState,
    ExtractedItem,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    TelegramEvent,
)
from restaurant_bot.integrations.openai_client import (
    CommentBindingSchema,
    ParsedInputSchema,
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
        "🔸 <b><u>К сожалению, мне не удалось распознать голосовое сообщение</u></b>\n\n"
        "Повторите короче или отправьте текстом.\n\n"
        "Пример: <code>сироп роза 3 штуки</code>"
    )
    assert [[button.text, button.callback_data] for row in result.reply.rows for button in row] == [
        ["Обновить статусы", "v2:orders"],
        ["Добавить товары", "v2:add"],
    ]
    assert result.state.cart == []


def test_unknown_empty_voice_uses_the_same_source_recovery_card(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что unknown пустой результат голос использует тот же исходный восстановление карточка."""
    result = ConversationEngine(settings).handle(
        TelegramEvent(update_id=8, chat_id="8", input_type=InputKind.VOICE),
        ParsedCommand(intent=Intent.UNKNOWN),
        ConversationState(),
        [],
    )

    assert "К сожалению, мне не удалось распознать голосовое сообщение" in result.reply.text
    assert [[button.callback_data for button in row] for row in result.reply.rows] == [
        ["v2:orders"],
        ["v2:add"],
    ]
    assert result.state.cart == []


def test_voice_comment_shadow_is_not_created_as_a_separate_product() -> None:
    """Проверяет, что голосовой комментарий ошибочной позиции не создаётся отдельным товаром."""
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
    assert restored["items"][0]["product_query"] == "сироп роза холодным"
    assert restored["items"][0]["quantity"] == 3
    assert restored["items"][0]["unit"] == "шт"
    assert restored["items"][0]["comment"] == ""


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
    """Проверяет, что общий комментарий не дублируется как товар."""
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
        "говядина без кожи",
    ]
    assert [item["comment"] for item in restored["items"]] == [
        "только охлаждённый",
        "",
    ]


def test_local_comments_are_recovered_when_ai_leaves_them_only_in_source_lines() -> None:
    """Восстанавливает разные локальные комментарии и отделяет их от общего."""
    source = "срп трхн в бутылках 10 штук срп роза 1 штука в банках и всё желательно на завтра"
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
    assert [item["product_query"] for item in restored["items"]] == [
        "срп трхн в бутылках",
        "срп роза в банках",
    ]
    assert [item["comment"] for item in restored["items"]] == ["", ""]


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


def test_packaging_connector_is_not_created_as_a_product() -> None:
    """Не создаёт ложный товар из связки «200 мл на 170 г»."""
    source = "Горчица зернистая 200 мл на 170 г СТБ Россия"
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {
                "product_query": "Горчица зернистая",
                "quantity": 200,
                "unit": "мл",
                "comment": "на 170 г",
                "source_line": source,
            },
            {
                "product_query": "на",
                "quantity": 170,
                "unit": "г",
                "comment": "СТБ Россия",
                "source_line": source,
            },
        ],
    }

    restored = recover_omitted_explicit_items(payload, source)

    assert [item["product_query"] for item in restored["items"]] == [
        "Горчица зернистая на 170 г стб россия"
    ]
    assert restored["items"][0]["comment"] == ""


def test_root_cut_requirement_is_applied_to_the_products_instead_of_becoming_one() -> None:
    """Переносит требование к срезу корней в комментарии перечисленных товаров."""
    source = "Укроп 2 килограмма, петрушка 3 килограмма, срез корня 5 сантиметров, не больше."
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {
                "product_query": "укроп",
                "quantity": 2,
                "unit": "кг",
                "comment": "",
                "source_line": source,
            },
            {
                "product_query": "петрушка",
                "quantity": 3,
                "unit": "кг",
                "comment": "",
                "source_line": source,
            },
            {
                "product_query": "срез корня",
                "quantity": 5,
                "unit": "см",
                "comment": "не больше",
                "source_line": source,
            },
        ],
    }

    restored = recover_omitted_explicit_items(payload, source)

    assert [item["product_query"] for item in restored["items"]] == ["укроп", "петрушка"]
    assert [(item["quantity"], item["unit"]) for item in restored["items"]] == [
        (2.0, "кг"),
        (3.0, "кг"),
    ]
    assert [item["comment"] for item in restored["items"]] == [
        "срез корня 5 сантиметров, не больше",
        "срез корня 5 сантиметров, не больше",
    ]


def test_real_root_product_is_not_converted_to_a_processing_comment() -> None:
    """Сохраняет явно названный корень сельдерея отдельным товаром."""
    source = "Укроп 2 килограмма и корень сельдерея 5 килограммов."
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {
                "product_query": "укроп",
                "quantity": 2,
                "unit": "кг",
                "comment": "",
                "source_line": source,
            },
            {
                "product_query": "корень сельдерея",
                "quantity": 5,
                "unit": "кг",
                "comment": "",
                "source_line": source,
            },
        ],
    }

    restored = recover_omitted_explicit_items(payload, source)

    assert [item["product_query"] for item in restored["items"]] == [
        "укроп",
        "корень сельдерея",
    ]
    assert [item.get("comment", "") for item in restored["items"]] == ["", ""]


def test_semantic_group_scope_does_not_leak_to_an_unrelated_preceding_product() -> None:
    """Ограничивает групповое требование указанными ИИ позициями."""
    source = (
        "Молоко 2 литра, укроп 2 килограмма, петрушка 3 килограмма, "
        "срез корня 5 сантиметров, не больше."
    )
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {"product_query": "молоко", "quantity": 2, "unit": "л", "source_line": source},
            {"product_query": "укроп", "quantity": 2, "unit": "кг", "source_line": source},
            {
                "product_query": "петрушка",
                "quantity": 3,
                "unit": "кг",
                "source_line": source,
            },
            {
                "product_query": "срез корня",
                "quantity": 5,
                "unit": "см",
                "source_line": source,
            },
        ],
        "comment_bindings": [
            {
                "text": "срез корня 5 сантиметров, не больше",
                "scope": "group",
                "target_item_indexes": [1, 2],
                "confidence": 0.97,
            }
        ],
    }

    restored = recover_omitted_explicit_items(payload, source)

    assert [item["product_query"] for item in restored["items"]] == [
        "молоко",
        "укроп",
        "петрушка",
    ]
    assert [item.get("comment", "") for item in restored["items"]] == [
        "",
        "срез корня 5 сантиметров, не больше",
        "срез корня 5 сантиметров, не больше",
    ]


def test_semantic_group_comment_removes_a_generic_shadow_product() -> None:
    """Применяет групповое пожелание без словаря конкретных товаров и комментариев."""
    source = "Томаты 5 кг, огурцы 4 кг, всё разложить по отдельным коробкам."
    parsed = ParsedInputSchema(
        intent=Intent.ADD_ITEMS,
        items=[
            ExtractedItem(product_query="томаты", quantity=5, unit="кг", source_line=source),
            ExtractedItem(product_query="огурцы", quantity=4, unit="кг", source_line=source),
            ExtractedItem(product_query="разложить по отдельным коробкам", source_line=source),
        ],
        comment_bindings=[
            CommentBindingSchema(
                text="разложить по отдельным коробкам",
                scope="group",
                target_item_indexes=[0, 1],
                confidence=0.97,
            )
        ],
    )

    restored = recover_omitted_explicit_items(parsed.model_dump(), source)

    assert [item["product_query"] for item in restored["items"]] == ["томаты", "огурцы"]
    assert [item["comment"] for item in restored["items"]] == [
        "разложить по отдельным коробкам",
        "разложить по отдельным коробкам",
    ]


def test_ai_cannot_expand_trailing_quality_comment_to_all_items() -> None:
    """Оставляет неявное пожелание только у последнего названного товара."""
    source = "Молоко 10 литров, сливки 5 литров, обязательно холодные."
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {
                "product_query": "молоко",
                "quantity": 10,
                "unit": "л",
                "comment": "обязательно холодные",
                "source_line": source,
            },
            {
                "product_query": "сливки",
                "quantity": 5,
                "unit": "л",
                "comment": "обязательно холодные",
                "source_line": source,
            },
        ],
        "comment_bindings": [
            {
                "text": "обязательно холодные",
                "scope": "group",
                "target_item_indexes": [0, 1],
                "confidence": 0.98,
            }
        ],
    }

    restored = recover_omitted_explicit_items(payload, source)

    assert [item.get("comment", "") for item in restored["items"]] == [
        "",
        "обязательно холодные",
    ]


def test_ai_cannot_guess_detached_relational_comment_scope() -> None:
    """Требует уточнение, даже если ИИ уверенно назначил двусмысленную фразу группе."""
    source = "Томаты 5 кг, огурцы 4 кг, положить отдельно."
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {"product_query": "томаты", "quantity": 5, "unit": "кг", "source_line": source},
            {"product_query": "огурцы", "quantity": 4, "unit": "кг", "source_line": source},
        ],
        "comment_bindings": [
            {
                "text": "положить отдельно",
                "scope": "group",
                "target_item_indexes": [0, 1],
                "confidence": 0.99,
            }
        ],
    }

    restored = recover_omitted_explicit_items(payload, source)

    assert restored["comment_clarification"] == "положить отдельно"
    assert [item.get("comment", "") for item in restored["items"]] == ["", ""]


def test_relational_group_comment_is_ambiguous_even_without_transcribed_comma() -> None:
    """Не полагается на пунктуацию, которую распознавание голоса может потерять."""
    source = "Помидоры 5 килограммов, огурцы 4 килограмма положить отдельно."
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {"product_query": "помидоры", "quantity": 5, "unit": "кг", "source_line": source},
            {"product_query": "огурцы", "quantity": 4, "unit": "кг", "source_line": source},
        ],
        "comment_bindings": [
            {
                "text": "положить отдельно",
                "scope": "group",
                "target_item_indexes": [0, 1],
                "confidence": 0.99,
            }
        ],
    }

    restored = recover_omitted_explicit_items(payload, source)

    assert restored["comment_clarification"] == "положить отдельно"
    assert [item.get("comment", "") for item in restored["items"]] == ["", ""]


def test_equal_comments_can_be_bound_to_two_items_separately() -> None:
    """Не теряет одинаковый комментарий при двух независимых точных привязках."""
    source = "Кола 2 бутылки без льда, лимонад 3 бутылки без льда."
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {
                "product_query": "кола",
                "quantity": 2,
                "unit": "бут",
                "comment": "без льда",
                "source_line": source,
            },
            {
                "product_query": "лимонад",
                "quantity": 3,
                "unit": "бут",
                "comment": "без льда",
                "source_line": source,
            },
        ],
        "comment_bindings": [
            {
                "text": "без льда",
                "scope": "item",
                "target_item_indexes": [0],
                "confidence": 0.99,
            },
            {
                "text": "без льда",
                "scope": "item",
                "target_item_indexes": [1],
                "confidence": 0.99,
            },
        ],
    }

    restored = recover_omitted_explicit_items(payload, source)

    assert [item.get("comment", "") for item in restored["items"]] == [
        "без льда",
        "без льда",
    ]


def test_group_scope_from_previous_sentence_cannot_leak_to_next_items() -> None:
    """Не использует слова общего охвата из предыдущего независимого пожелания."""
    source = (
        "Оба вида яблок оставить в пакетах. "
        "Молоко 10 литров, сливки 5 литров, обязательно холодные."
    )
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {"product_query": "молоко", "quantity": 10, "unit": "л", "source_line": source},
            {"product_query": "сливки", "quantity": 5, "unit": "л", "source_line": source},
        ],
        "comment_bindings": [
            {
                "text": "обязательно холодные",
                "scope": "group",
                "target_item_indexes": [0, 1],
                "confidence": 0.99,
            }
        ],
    }

    restored = recover_omitted_explicit_items(payload, source)

    assert [item.get("comment", "") for item in restored["items"]] == [
        "",
        "обязательно холодные",
    ]


def test_detached_group_comment_without_scope_requests_clarification() -> None:
    """Не назначает отдельное пожелание последнему товару без уточнения охвата."""
    source = "Сироп роза 5 штук и сироп тархун 10 штук. Доставить утром."
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {
                "product_query": "Сироп роза",
                "quantity": 5,
                "unit": "шт",
                "source_line": source,
            },
            {
                "product_query": "Сироп тархун",
                "quantity": 10,
                "unit": "шт",
                "source_line": source,
            },
        ],
        "comment_bindings": [
            {
                "text": "Доставить утром",
                "scope": "group",
                "target_item_indexes": [0, 1],
                "confidence": 0.95,
            }
        ],
    }

    restored = recover_omitted_explicit_items(payload, source)

    assert restored["comment_clarification"] == "Доставить утром"
    assert [item.get("comment", "") for item in restored["items"]] == ["", ""]


def test_detached_comment_cannot_be_guessed_as_one_item_comment() -> None:
    """Не разрешает ИИ назначить отдельное пожелание одному случайному товару."""
    source = "Сироп Роза 5 штук и Сироп Тархун 10 штук. Доставить утром."
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {
                "product_query": "Сироп Роза",
                "quantity": 5,
                "unit": "шт",
                "source_line": source,
            },
            {
                "product_query": "Сироп Тархун",
                "quantity": 10,
                "unit": "шт",
                "source_line": source,
            },
        ],
        "comment_bindings": [
            {
                "text": "Доставить утром",
                "scope": "item",
                "target_item_indexes": [0],
                "confidence": 0.98,
            }
        ],
    }

    restored = recover_omitted_explicit_items(payload, source)

    assert restored["comment_clarification"] == "Доставить утром"
    assert [item.get("comment", "") for item in restored["items"]] == ["", ""]


def test_complete_product_lines_are_not_saved_as_item_comments() -> None:
    """Отбрасывает ошибочные ИИ-привязки, дублирующие товар и количество."""
    source = "Сироп Роза 5 штук, Сироп Вунди 10 штук."
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {
                "product_query": "Сироп Роза",
                "quantity": 5,
                "unit": "штук",
                "source_line": "Сироп Роза 5 штук",
            },
            {
                "product_query": "Сироп Вунди",
                "quantity": 10,
                "unit": "штук",
                "source_line": "Сироп Вунди 10 штук",
            },
        ],
        "comment_bindings": [
            {
                "text": "Сироп Роза 5 штук",
                "scope": "item",
                "target_item_indexes": [0],
                "confidence": 0.98,
            },
            {
                "text": "Сироп Вунди 10 штук",
                "scope": "item",
                "target_item_indexes": [1],
                "confidence": 0.98,
            },
        ],
    }

    restored = recover_omitted_explicit_items(payload, source)

    assert [item.get("comment", "") for item in restored["items"]] == ["", ""]


def test_conversational_product_line_is_not_saved_as_item_comment() -> None:
    """Не сохраняет разговорную просьбу с товаром как комментарий поставщику."""
    source = "Добавьте, пожалуйста, Сироп Роза пять штук."
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {
                "product_query": "Сироп Роза",
                "quantity": 5,
                "unit": "штук",
                "source_line": source,
            }
        ],
        "comment_bindings": [
            {
                "text": source,
                "scope": "item",
                "target_item_indexes": [0],
                "confidence": 0.95,
            }
        ],
    }

    restored = recover_omitted_explicit_items(payload, source)

    assert restored["items"][0].get("comment", "") == ""


def test_bot_facing_request_wrappers_are_not_item_comments() -> None:
    """Отбрасывает разные разговорные обращения к боту без закрытого списка фраз."""
    wrappers = [
        "Добавьте, пожалуйста",
        "Пожалуйста, закажите",
        "Мне нужно заказать",
        "Нам надо добавить",
        "Я хочу заказать",
        "Мы хотим внести",
        "Можно мне добавить",
        "Будьте добры, добавьте",
        "Давайте добавим",
        "Внесите в заявку",
        "Запишите в заказ",
        "Добавьте в черновик",
        "Оформите, пожалуйста",
    ]

    for wrapper in wrappers:
        source = f"{wrapper} Сироп Роза 5 штук."
        payload = {
            "intent": Intent.ADD_ITEMS,
            "items": [
                {
                    "product_query": "Сироп Роза",
                    "quantity": 5,
                    "unit": "шт",
                    "source_line": source,
                }
            ],
            "comment_bindings": [
                {
                    "text": wrapper,
                    "scope": "item",
                    "target_item_indexes": [0],
                    "confidence": 0.99,
                }
            ],
        }

        restored = recover_omitted_explicit_items(payload, source)

        assert restored["items"][0].get("comment", "") == "", wrapper


def test_bot_facing_request_wrapper_preserves_real_supplier_comment() -> None:
    """Удаляет обращение к боту, но сохраняет следующую полезную инструкцию."""
    source = "Добавьте, пожалуйста, Сироп Роза 5 штук без льда."
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {
                "product_query": "Сироп Роза",
                "quantity": 5,
                "unit": "шт",
                "source_line": source,
            }
        ],
        "comment_bindings": [
            {
                "text": "Добавьте, пожалуйста, без льда",
                "scope": "item",
                "target_item_indexes": [0],
                "confidence": 0.99,
            }
        ],
    }

    restored = recover_omitted_explicit_items(payload, source)

    assert restored["items"][0]["comment"] == "без льда"


def test_semantic_item_comment_is_applied_only_to_its_target() -> None:
    """Не переносит локальное пожелание на соседний товар."""
    source = "Томаты 5 кг только спелые, огурцы 4 кг."
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {
                "product_query": "томаты",
                "quantity": 5,
                "unit": "кг",
                "comment": "",
                "source_line": source,
            },
            {
                "product_query": "огурцы",
                "quantity": 4,
                "unit": "кг",
                "comment": "",
                "source_line": source,
            },
        ],
        "comment_bindings": [
            {
                "text": "только спелые",
                "scope": "item",
                "target_item_indexes": [0],
                "confidence": 0.96,
            }
        ],
    }

    restored = recover_omitted_explicit_items(payload, source)

    assert [item.get("comment", "") for item in restored["items"]] == [
        "только спелые",
        "",
    ]


def test_semantic_order_comment_preserves_explicit_global_scope() -> None:
    """Сохраняет явно общий комментарий для всей заявки."""
    source = "Томаты 5 кг, огурцы 4 кг. Всё привезти к семи утра."
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {"product_query": "томаты", "quantity": 5, "unit": "кг", "source_line": source},
            {"product_query": "огурцы", "quantity": 4, "unit": "кг", "source_line": source},
        ],
        "comment_bindings": [
            {
                "text": "привезти к семи утра",
                "scope": "order",
                "target_item_indexes": [],
                "confidence": 0.99,
            }
        ],
    }

    restored = recover_omitted_explicit_items(payload, source)

    assert restored["global_comment"] == "привезти к семи утра"
    assert [item.get("comment", "") for item in restored["items"]] == ["", ""]


def test_uncertain_comment_scope_requests_clarification_without_changing_draft(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    """Просит уточнить неуверенную область комментария и не добавляет товары."""
    source = "Томаты 5 кг, огурцы 4 кг, положить отдельно."
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {
                "product_query": "томаты",
                "quantity": 5,
                "unit": "кг",
                "source_line": source,
            },
            {
                "product_query": "огурцы",
                "quantity": 4,
                "unit": "кг",
                "source_line": source,
            },
        ],
        "comment_bindings": [
            {
                "text": "положить отдельно",
                "scope": "ambiguous",
                "target_item_indexes": [],
                "confidence": 0.55,
            }
        ],
    }
    restored = recover_omitted_explicit_items(payload, source)
    command = ParsedCommand.model_validate(restored)
    state = ConversationState()

    result = ConversationEngine(settings).handle(
        TelegramEvent(update_id=10, chat_id="10", input_type=InputKind.VOICE, text=source),
        command,
        state,
        [],
    )

    assert command.comment_clarification == "положить отдельно"
    assert result.state.cart == []
    assert "Уточните комментарий" in result.reply.text
    assert "положить отдельно" in result.reply.text


def test_voice_beef_cannot_gain_an_unspoken_qualifier_or_auto_select(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что голосовой запрос говядины не получает невысказанный признак и не выбирается автоматически."""
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
