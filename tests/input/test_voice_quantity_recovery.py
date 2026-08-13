"""Проверяет поведение, связанное с модулем «test voice quantity recovery»."""

from restaurant_bot.domain.models import Intent
from restaurant_bot.integrations.openai_client import (
    recover_omitted_explicit_items,
    restore_explicit_order_terms,
)


def test_compact_catalog_measurement_does_not_become_order_quantity() -> None:
    """Не принимает слитную фасовочную меру из названия за заказ."""
    source = "\u0425\u043b\u043e\u043f\u044c\u044f \u043e\u0432\u0441\u044f\u043d\u044b\u0435 \u0413\u0435\u0440\u043a\u0443\u043b\u0435\u0441 450\u0433"
    restored = recover_omitted_explicit_items(
        {
            "intent": Intent.ADD_ITEMS,
            "items": [
                {
                    "product_query": source,
                    "quantity": None,
                    "unit": "",
                    "source_line": source,
                }
            ],
        },
        source,
    )

    item = restored["items"][0]
    assert item["quantity"] is None
    assert item["unit"] == ""
    assert item["product_query"] == source
    assert item["packaging_text"] == "450г"
    assert item["packaging_role"] == "catalog_attribute"


def test_voice_recovery_restores_quantity_and_unit_from_shared_source_line() -> None:
    """Проверяет, что голос восстановление восстанавливает количество и единица измерения из общая исходный строка."""
    source = "Сироп роза 10 штук, говядина 5 кг"
    items = [
        {"product_query": "Сироп роза", "quantity": None, "unit": "", "source_line": source},
        {"product_query": "говядина", "quantity": None, "unit": "", "source_line": source},
    ]

    restored = restore_explicit_order_terms(items, source)

    assert [(item["quantity"], item["unit"]) for item in restored] == [(10.0, "шт"), (5.0, "кг")]


def test_packaging_and_order_sentence_collapses_ai_shadow_items() -> None:
    """Схлопывает фасовку и заказное количество одной голосовой позиции."""
    source = (
        "Марципановые конфеты на шоколадной подложке, кенигсбергский стиль, "
        "90 грамм. Желательно привезти завтра. Нужно 5 килограмм."
    )
    restored = recover_omitted_explicit_items(
        {
            "intent": Intent.ADD_ITEMS,
            "items": [
                {
                    "product_query": "Марципановые конфеты на шоколадной подложке, кенигсбергский стиль, 90 грамм",
                    "quantity": 90,
                    "unit": "г",
                    "source_line": source,
                },
                {"product_query": "Нужно", "quantity": 5, "unit": "кг", "source_line": source},
            ],
        },
        source,
    )

    assert len(restored["items"]) == 1
    assert (restored["items"][0]["quantity"], restored["items"][0]["unit"]) == (5.0, "кг")


def test_packaging_connector_and_order_collapses_ai_shadow_items() -> None:
    """Схлопывает упаковочную связку и финальное количество одного товара."""
    source = "Тесто для спринг-роллов 550 грамм на 20 штук Екимал 5 штук"
    restored = recover_omitted_explicit_items(
        {
            "intent": Intent.ADD_ITEMS,
            "items": [
                {
                    "product_query": "Тесто для спринг-роллов 550 грамм на 20 штук Екимал",
                    "quantity": 5,
                    "unit": "шт",
                    "source_line": source,
                },
                {
                    "product_query": "тесто для спринг-роллов на 20 штук екимал 5 штук",
                    "quantity": 550,
                    "unit": "г",
                    "source_line": source,
                },
            ],
        },
        source,
    )

    assert len(restored["items"]) == 1
    assert (restored["items"][0]["quantity"], restored["items"][0]["unit"]) == (5.0, "шт")


def test_voice_recovery_never_replaces_the_model_product_name() -> None:
    """Проверяет, что голос восстановление никогда не replaces модель товар название."""
    source = "Сыропроза 10 штук"
    items = [{"product_query": "Сыропроза", "quantity": None, "unit": "", "source_line": source}]

    restored = restore_explicit_order_terms(items, source)

    assert restored[0]["product_query"] == "Сыропроза"
    assert restored[0]["quantity"] == 10.0
    assert restored[0]["unit"] == "шт"


def test_packaged_product_uses_only_quantity_after_full_name() -> None:
    """Не принимает фасовку внутри названия за количество заказа."""
    source = "Сыр Швейцарский Сыробогатов 180гр 500 грам свежего"
    items = [
        {
            "product_query": "Сыр Швейцарский Сыробогатов 180гр",
            "quantity": 500.0,
            "unit": "грам",
            "comment": "свежего",
            "source_line": source,
        }
    ]

    restored = restore_explicit_order_terms(items, source)

    assert restored[0]["product_query"] == "Сыр Швейцарский Сыробогатов 180гр"
    assert restored[0]["quantity"] == 500.0
    assert restored[0]["unit"] == "г"
    assert restored[0]["comment"] == "свежего"


def test_model_quantity_wins_when_source_also_contains_packaging() -> None:
    """Не заменяет количество заказа фасовкой из исходного названия."""
    source = "Сыр Швейцарский Сыробогатов 180гр 10 штук"
    items = [
        {
            "product_query": "Сыр Швейцарский Сыробогатов",
            "quantity": 10.0,
            "unit": "штук",
            "source_line": source,
        }
    ]

    restored = restore_explicit_order_terms(items, source)

    assert restored[0]["quantity"] == 10.0
    assert restored[0]["unit"] == "шт"
    assert restored[0]["source_line"] == source


def test_spoken_range_does_not_replace_explicit_order_quantity() -> None:
    """Сохраняет заказанные 3 кг рядом со словесной фасовкой 350–380 г."""
    source = (
        "Утиные ножки триста пятьдесят-триста восемьдесят грамм, "
        "три килограмма, желательно нежирные."
    )
    restored = restore_explicit_order_terms(
        [
            {
                "product_query": "Утиные ножки триста пятьдесят-триста восемьдесят грамм",
                "quantity": 3,
                "unit": "кг",
                "source_line": source,
            }
        ],
        source,
    )

    assert (restored[0]["quantity"], restored[0]["unit"]) == (3.0, "кг")


def test_last_explicit_order_term_wins_over_multiple_packaging_measurements() -> None:
    """Не заменяет заказ 5 шт первыми справочными объёмами огурцов."""
    source = "Огурцы 40 на 45 Майер, 10 литров, 700 грамм, 500 грамм, Германия, 5 штук."
    restored = restore_explicit_order_terms(
        [
            {
                "product_query": "Огурцы 40 на 45 Майер",
                "quantity": 5,
                "unit": "шт",
                "source_line": source,
            }
        ],
        source,
    )

    assert (restored[0]["quantity"], restored[0]["unit"]) == (5.0, "шт")


def test_voice_range_only_line_drops_model_invented_quantity() -> None:
    """Диапазон фасовки не становится количеством заказа после AI-разбора."""
    source = "Филе форели свежее 0,8-1,2 килограмма зачищенное 3НС"
    items = [
        {
            "product_query": "Филе форели свежее",
            "quantity": 0.8,
            "unit": "кг",
            "source_line": source,
        }
    ]

    restored = restore_explicit_order_terms(items, source)

    assert restored[0]["quantity"] is None
    assert restored[0]["unit"] == ""


def test_voice_range_line_uses_only_explicit_order_quantity() -> None:
    """Явное количество после диапазона имеет приоритет над endpoint диапазона."""
    source = "Филе форели 0,8-1,2 кг зачищенное 10 кг"
    items = [
        {
            "product_query": "Филе форели",
            "quantity": 0.8,
            "unit": "кг",
            "source_line": source,
        }
    ]

    restored = restore_explicit_order_terms(items, source)

    assert (restored[0]["quantity"], restored[0]["unit"]) == (10.0, "кг")


def test_voice_range_line_keeps_explicit_bare_order_quantity() -> None:
    """Сохраняет явно указанное число после диапазона даже без единицы."""
    source = "Филе форели 0,8-1,2 кг зачищенное — 2"
    items = [
        {
            "product_query": "Филе форели",
            "quantity": 0.8,
            "unit": "кг",
            "source_line": source,
        }
    ]

    restored = restore_explicit_order_terms(items, source)

    assert (restored[0]["quantity"], restored[0]["unit"]) == (2.0, "")


def test_voice_ambiguous_weight_pair_clears_model_quantity() -> None:
    """Не доверяет выбранному моделью весу из неоднозначной голосовой пары."""
    source = "Грудинка говяжья ССВУ, 5,5 килограмм, 16,5 килограмм, Блэк Ангус, МирВаторг, Брянск"
    restored = recover_omitted_explicit_items(
        {
            "intent": Intent.ADD_ITEMS,
            "items": [
                {
                    "product_query": "Грудинка говяжья ССВУ",
                    "quantity": 5.5,
                    "unit": "кг",
                    "comment": "Блэк Ангус, МирВаторг, Брянск",
                    "source_line": source,
                }
            ],
        },
        source,
    )

    assert len(restored["items"]) == 1
    item = restored["items"][0]
    assert item["product_query"] == source
    assert item["quantity"] is None
    assert item["unit"] == ""
    assert item["comment"] == ""
    assert item["packaging_role"] == "ambiguous"
    assert item["packaging_confidence"] < 0.85


def test_voice_from_to_range_clears_model_quantity() -> None:
    """Не принимает границу голосового диапазона за количество заказа."""
    source = "грудинка от 5,5 до 16,5 кг, блэк ангус"
    restored = recover_omitted_explicit_items(
        {
            "intent": Intent.ADD_ITEMS,
            "items": [
                {
                    "product_query": "грудинка",
                    "quantity": 16.5,
                    "unit": "кг",
                    "comment": "блэк ангус",
                    "source_line": source,
                }
            ],
        },
        source,
    )

    item = restored["items"][0]
    assert item["product_query"] == source
    assert item["quantity"] is None
    assert item["unit"] == ""
    assert item["comment"] == ""
    assert item["packaging_role"] == "catalog_attribute"
    assert item["packaging_confidence"] >= 0.85


def test_voice_unbound_product_fact_is_restored_to_query() -> None:
    """Не доверяет комментарию ИИ без семантической привязки."""
    source = "грудинка 5 кг Блэк Ангус"
    restored = recover_omitted_explicit_items(
        {
            "intent": Intent.ADD_ITEMS,
            "items": [
                {
                    "product_query": "грудинка",
                    "quantity": 5,
                    "unit": "кг",
                    "comment": "Блэк Ангус",
                    "source_line": source,
                }
            ],
        },
        source,
    )

    item = restored["items"][0]
    assert item["product_query"] == "грудинка Блэк Ангус"
    assert item["comment"] == ""
    assert item["user_comment_to_supplier"] == ""
    assert item["comment_source"] == "none"


def test_voice_semantic_binding_sets_comment_provenance() -> None:
    """Помечает подтверждённую ИИ-привязку как семантический комментарий."""
    source = "сироп роза 5 шт, желательно привезти холодным"
    restored = recover_omitted_explicit_items(
        {
            "intent": Intent.ADD_ITEMS,
            "items": [
                {
                    "product_query": "сироп роза",
                    "quantity": 5,
                    "unit": "шт",
                    "comment": "желательно привезти холодным",
                    "source_line": source,
                }
            ],
            "comment_bindings": [
                {
                    "text": "желательно привезти холодным",
                    "scope": "item",
                    "target_item_indexes": [0],
                    "confidence": 0.98,
                }
            ],
        },
        source,
    )

    item = restored["items"][0]
    assert item["comment"] == "желательно привезти холодным"
    assert item["comment_source"] == "semantic"


def test_voice_range_is_restored_to_product_query_not_comment() -> None:
    """Возвращает размер в поиск, даже если AI оставил его вне product_query."""
    source = "Филе форели 0,9-1,3 килограмма зачищенное, Тринца."
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {
                "product_query": "Филе форели",
                "quantity": 1.3,
                "unit": "кг",
                "comment": "0,9-1,3 килограмма зачищенное",
                "source_line": source,
            }
        ],
    }

    restored = recover_omitted_explicit_items(payload, source)
    item = restored["items"][0]

    assert "0,9-1,3 килограмма" in item["product_query"]
    assert "0,9-1,3" not in item["comment"]
    assert item["quantity"] is None
    assert item["unit"] == ""


def test_catalog_packaging_role_is_preserved_when_order_quantity_is_separate() -> None:
    """Сохраняет фасовку в поиске и отделяет её от количества заказа."""
    source = "Форель филе 0,8-1,3 кг, зачищенная — 5 кг"
    restored = recover_omitted_explicit_items(
        {
            "intent": Intent.ADD_ITEMS,
            "items": [
                {
                    "product_query": "Форель филе",
                    "quantity": 5,
                    "unit": "кг",
                    "comment": "зачищенная",
                    "source_line": source,
                }
            ],
        },
        source,
    )

    item = restored["items"][0]
    assert item["packaging_role"] == "catalog_attribute"
    assert item["packaging_confidence"] >= 0.85
    assert "0,8-1,3 кг" in item["product_query"]
    assert "зачищенная" in item["product_query"]
    assert item["comment"] == ""
    assert item["comment_source"] == "none"
    assert (item["quantity"], item["unit"]) == (5.0, "кг")


def test_packaging_preference_stays_in_full_comment() -> None:
    """Не переносит явно сформулированное пожелание о фасовке в название."""
    source = "Форель филе — 5 кг. Нужна фасовка по 0,8-1,3 кг"
    restored = recover_omitted_explicit_items(
        {
            "intent": Intent.ADD_ITEMS,
            "items": [
                {
                    "product_query": "Форель филе",
                    "quantity": 5,
                    "unit": "кг",
                    "comment": "Нужна фасовка по 0,8-1,3 кг",
                    "source_line": source,
                }
            ],
        },
        source,
    )

    item = restored["items"][0]
    assert item["packaging_role"] == "user_preference"
    assert "0,8-1,3 кг" not in item["product_query"]
    assert item["comment"] == "Нужна фасовка по 0,8-1,3 кг"


def test_ambiguous_packaging_role_is_not_moved_between_fields() -> None:
    """Не угадывает роль диапазона в неоднозначной формулировке."""
    source = "Форель филе, фасовка 0,8-1,3 кг, 5 кг"
    restored = recover_omitted_explicit_items(
        {
            "intent": Intent.ADD_ITEMS,
            "items": [
                {
                    "product_query": "Форель филе",
                    "quantity": 5,
                    "unit": "кг",
                    "comment": "фасовка 0,8-1,3 кг",
                    "source_line": source,
                }
            ],
        },
        source,
    )

    item = restored["items"][0]
    assert item["packaging_role"] == "ambiguous"
    assert "0,8-1,3 кг" in item["product_query"]
    assert item["comment"] == ""
    assert item["comment_source"] == "none"


def test_source_order_quantity_wins_over_conflicting_ai_scalar() -> None:
    """Сохраняет количество заказа, доказанное исходной фразой."""
    source = "Курица 5 кг"
    restored = recover_omitted_explicit_items(
        {
            "intent": Intent.ADD_ITEMS,
            "items": [
                {
                    "product_query": "Курица",
                    "quantity": 3,
                    "unit": "кг",
                    "source_line": source,
                }
            ],
        },
        source,
    )

    item = restored["items"][0]
    assert (item["quantity"], item["unit"]) == (5.0, "кг")


def test_packaging_only_measurement_does_not_become_order_quantity() -> None:
    """Не принимает вес упаковки за количество заказа."""
    source = "Сыр в упаковке 500 г"
    restored = recover_omitted_explicit_items(
        {
            "intent": Intent.ADD_ITEMS,
            "items": [
                {
                    "product_query": "Сыр",
                    "quantity": 500,
                    "unit": "г",
                    "source_line": source,
                }
            ],
        },
        source,
    )

    item = restored["items"][0]
    assert item["quantity"] is None
    assert item["unit"] == ""
    assert item["packaging_text"] == "500 г"
    assert item["packaging_role"] == "catalog_attribute"


def test_order_and_packaging_keep_separate_roles() -> None:
    """Сохраняет заказанное количество отдельно от фасовки."""
    source = "Сыр 5 кг, упаковки по 500 г"
    restored = recover_omitted_explicit_items(
        {
            "intent": Intent.ADD_ITEMS,
            "items": [
                {
                    "product_query": "Сыр",
                    "quantity": 500,
                    "unit": "г",
                    "source_line": source,
                }
            ],
        },
        source,
    )

    item = restored["items"][0]
    assert (item["quantity"], item["unit"]) == (5.0, "кг")
    assert item["packaging_text"] == "упаковки по 500 г"
    assert item["packaging_role"] == "catalog_attribute"


def test_dash_range_never_becomes_endpoint_quantity() -> None:
    """Очищает AI-число, если источник содержит только диапазон."""
    source = "Говядина 500–700 г"
    restored = recover_omitted_explicit_items(
        {
            "intent": Intent.ADD_ITEMS,
            "items": [
                {
                    "product_query": "Говядина",
                    "quantity": 500,
                    "unit": "г",
                    "source_line": source,
                }
            ],
        },
        source,
    )

    item = restored["items"][0]
    assert item["quantity"] is None
    assert item["unit"] == ""
    assert "500–700 г" in item["product_query"]


def test_percentage_and_order_quantity_keep_distinct_roles() -> None:
    """Не превращает процент товара в количество заказа."""
    source = "Сыр 45% 5 кг"
    restored = recover_omitted_explicit_items(
        {
            "intent": Intent.ADD_ITEMS,
            "items": [
                {
                    "product_query": "Сыр 45%",
                    "quantity": 45,
                    "unit": "%",
                    "source_line": source,
                }
            ],
        },
        source,
    )

    item = restored["items"][0]
    assert "45%" in item["product_query"]
    assert (item["quantity"], item["unit"]) == (5.0, "кг")


def test_quantity_packaging_reconciliation_is_idempotent() -> None:
    """Повторная provenance-сверка не меняет результат."""
    source = "Сыр 5 кг, упаковки по 500 г"
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {
                "product_query": "Сыр",
                "quantity": 500,
                "unit": "г",
                "source_line": source,
            }
        ],
    }

    once = recover_omitted_explicit_items(payload, source)
    twice = recover_omitted_explicit_items(once, source)

    assert twice == once


def test_text_and_voice_transcripts_share_quantity_packaging_result() -> None:
    """Одинаковый текст и транскрипт голоса дают одинаковую семантику."""
    source = "Сыр в упаковке 500 г"
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {
                "product_query": "Сыр",
                "quantity": 500,
                "unit": "г",
                "source_line": source,
            }
        ],
    }

    text_result = recover_omitted_explicit_items(payload.copy(), source)
    voice_result = recover_omitted_explicit_items(
        {"intent": Intent.ADD_ITEMS, "items": [dict(payload["items"][0])]}, source
    )

    assert text_result["items"] == voice_result["items"]


def test_ai_duplicate_items_from_one_voice_line_are_collapsed() -> None:
    """Объединяет AI-дубликаты с раздельно распознанными комментарием и количеством."""
    source = "Форель свежая 0.8-1.3 кг, зачищенная тринце, 10 кг."
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {
                "product_query": "Форель свежая",
                "quantity": None,
                "unit": "кг",
                "comment": "зачищенная тринце",
                "source_line": source,
            },
            {
                "product_query": "Форель свежая",
                "quantity": 10,
                "unit": "кг",
                "comment": "",
                "source_line": source,
            },
        ],
    }

    restored = recover_omitted_explicit_items(payload, source)

    assert len(restored["items"]) == 1
    item = restored["items"][0]
    assert "0.8-1.3 кг" in item["product_query"]
    assert "зачищенная тринце" in item["product_query"]
    assert (item["quantity"], item["unit"]) == (10.0, "кг")
    assert item["comment"] == ""


def test_ai_packaging_alternative_drops_connector_fragment() -> None:
    """Не создаёт отдельный товар из слова «или» в вариантах фасовки."""
    source = "Капуста квашеная ведро 5 кг или 4,5 кг, два ведра."
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {
                "product_query": "Капуста квашеная",
                "quantity": 2,
                "unit": "ведро",
                "comment": "5 кг или 4,5 кг",
                "source_line": source,
            },
            {
                "product_query": "или",
                "quantity": 4.5,
                "unit": "кг",
                "comment": "два ведра",
                "source_line": source,
            },
        ],
    }

    restored = recover_omitted_explicit_items(payload, source)

    assert len(restored["items"]) == 1
    assert restored["items"][0]["product_query"] == ("Капуста квашеная ведро 5 кг или 4,5 кг")
    assert restored["items"][0]["comment"] == ""
    assert (restored["items"][0]["quantity"], restored["items"][0]["unit"]) == (2.0, "ведро")


def test_empty_voice_model_result_recovers_every_explicit_product() -> None:
    """Проверяет, что пустой результат голос модель результат восстанавливает каждый явный товар."""
    payload = {"intent": Intent.UNKNOWN, "items": []}

    restored = recover_omitted_explicit_items(
        payload, "Сироп роза 10 штук и говядина 5 кг и джем 3 банки"
    )

    assert restored["intent"] is Intent.ADD_ITEMS
    assert [
        (item["product_query"], item["quantity"], item["unit"]) for item in restored["items"]
    ] == [
        ("Сироп роза", 10.0, "шт"),
        ("говядина", 5.0, "кг"),
        ("джем", 3.0, "бан"),
    ]


def test_navigation_intent_can_never_be_recovered_as_a_product() -> None:
    """Проверяет, что навигационное намерение никогда не восстанавливается как товар."""
    payload = {"intent": Intent.SHOW_CART, "items": []}

    restored = recover_omitted_explicit_items(payload, "Показать товары поставщика.")

    assert restored["intent"] is Intent.SHOW_CART
    assert restored["items"] == []


def test_navigation_intent_discards_model_items_before_cart_mutation() -> None:
    """Проверяет, что навигация намерение отбрасывает модель позиции до черновик mutation."""
    payload = {
        "intent": Intent.CHECK_MIN_SUM,
        "items": [{"product_query": "Тестовый товар", "quantity": None, "unit": ""}],
    }

    restored = recover_omitted_explicit_items(payload, "Давай посмотрим товары")

    assert restored["intent"] is Intent.CHECK_MIN_SUM
    assert restored["items"] == []


def test_partial_voice_model_result_restores_the_omitted_conjoined_item() -> None:
    """Проверяет, что partial голос модель результат восстанавливает пропущенная соединённый союзом позиция."""
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {
                "product_query": "Сироп роза",
                "quantity": 10,
                "unit": "шт",
                "source_line": "Сироп роза 10 штук",
            }
        ],
    }

    restored = recover_omitted_explicit_items(payload, "Сироп роза 10 штук и говядина 5 кг")

    assert [
        (item["product_query"], item["quantity"], item["unit"]) for item in restored["items"]
    ] == [
        ("Сироп роза", 10, "шт"),
        ("говядина", 5.0, "кг"),
    ]


def test_pair_of_apples_uses_its_own_spoken_quantity() -> None:
    """Проверяет, что пара для яблок использует its own произнесённый количество."""
    source = "Сироп роза пять штук, говядина десять килограмм и пару яблок."
    items = [
        {
            "product_query": "сироп роза",
            "quantity": 5,
            "unit": "шт",
            "source_line": "сироп роза 5 шт",
        },
        {
            "product_query": "говядина",
            "quantity": 10,
            "unit": "кг",
            "source_line": "говядина 10 кг",
        },
        {"product_query": "яблоки", "quantity": None, "unit": "", "source_line": "яблоки"},
    ]

    restored = restore_explicit_order_terms(items, source)

    assert [(item["quantity"], item["unit"]) for item in restored] == [
        (5.0, "шт"),
        (10.0, "кг"),
        (2.0, "шт"),
    ]


def test_voice_recovery_removes_a_model_invented_quantity() -> None:
    """Проверяет, что восстановление голоса удаляет выдуманное моделью количество."""
    source = "Сироп роза пять штук и бутылка воды."
    items = [
        {
            "product_query": "сироп роза",
            "quantity": 5,
            "unit": "шт",
            "source_line": "сироп роза 5 шт",
        },
        {
            "product_query": "бутылка воды",
            "quantity": 1,
            "unit": "бут",
            "source_line": "бутылка воды",
        },
    ]

    restored = restore_explicit_order_terms(items, source)

    assert restored[1]["quantity"] is None
    assert restored[1]["unit"] == ""
