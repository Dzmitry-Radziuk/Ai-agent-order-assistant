"""Проверяет единый разбор товарных признаков и их происхождение."""

from __future__ import annotations

from restaurant_bot.domain.models import Intent
from restaurant_bot.parsing.ai.comment_reconciliation import _apply_semantic_comment_bindings
from restaurant_bot.parsing.ai.reconciliation import recover_omitted_explicit_items
from restaurant_bot.parsing.products import parse_product_lines
from restaurant_bot.parsing.semantic.measurements import extract_semantic_facts
from restaurant_bot.parsing.semantic.models import SemanticFactKind


def test_long_catalog_tail_stays_one_semantic_item() -> None:
    """Сохраняет длинное название каталога одной позицией."""
    source = "Макароны Спагетти с чернилами каракатицы CASA MILO, пакет, 500 гр, 15 шт/упак, Италия"

    payload = {
        "intent": "add_items",
        "items": [
            {
                "product_query": "Макароны Спагетти с чернилами каракатицы CASA MILO, пакет, 500 гр",
                "quantity": None,
                "unit": "",
                "source_line": source,
            },
            {
                "product_query": "в упаковке Италия",
                "quantity": 15,
                "unit": "шт",
                "source_line": source,
            },
        ],
    }

    result = recover_omitted_explicit_items(payload, source)

    assert len(result["items"]) == 1
    assert result["items"][0]["product_query"].endswith("Италия")
    assert result["items"][0]["quantity"] is None


def test_packaging_paraphrases_are_catalog_facts() -> None:
    """Не превращает разные формулировки фасовки в количество заказа."""
    sources = (
        "Соус, 15 шт/упак",
        "Соус, 15 штук в упаковке",
        "Соус, в упаковке 15 штук",
        "Соус, упаковка по 15 штук",
        "Соус, 4 шт/кор",
        "Соус, 4 штуки в коробке",
        "Соус, в коробке 4 штуки",
        "Соус, коробка по 4 штуки",
    )

    for source in sources:
        items = parse_product_lines(source)
        assert len(items) == 1, source
        assert items[0].quantity is None, source
        assert items[0].packaging_role == "catalog_attribute", source


def test_decimal_comma_is_not_a_product_separator() -> None:
    """Сохраняет десятичную запятую внутри измерения товара."""
    items = parse_product_lines("Молоко 3,2%, 1,5 л")

    assert len(items) == 1
    assert len(items[0].product_query.split()) >= 2
    assert "3,2%" in items[0].product_query


def test_product_anchor_evidence_keeps_real_list_boundaries() -> None:
    """Разделяет только самостоятельные товарные названия."""
    assert [item.product_query for item in parse_product_lines("лук, картошка, морковь")] == [
        "лук",
        "картошка",
        "морковь",
    ]
    assert [
        (item.product_query, item.quantity, item.unit)
        for item in parse_product_lines("лук 5 кг, картошка 10 кг")
    ] == [("лук", 5, "кг"), ("картошка", 10, "кг")]


def test_unmarked_qualifier_tail_stays_with_one_product() -> None:
    """Сохраняет уточнения картофеля у одной товарной позиции."""
    source = "Картофель молодой вес, сорт желательно Агретто, и чтобы крупные были картошинки."

    items = parse_product_lines(source)

    assert len(items) == 1
    assert items[0].product_query == "Картофель молодой сорт Агретто"
    assert items[0].comment == "чтобы крупные были картошинки"


def test_unknown_ai_payload_recovers_one_qualifier_product() -> None:
    """Восстанавливает одну товарную позицию, если ИИ не распознал структуру."""
    source = "Картофель молодой вес, сорт желательно Агретто."

    result = recover_omitted_explicit_items({"intent": Intent.UNKNOWN, "items": []}, source)

    assert result["intent"] is Intent.ADD_ITEMS
    assert len(result["items"]) == 1
    assert result["items"][0]["product_query"] == "Картофель молодой сорт Агретто"


def test_ai_qualifier_item_is_not_reparsed_as_extra_positions() -> None:
    """Не добавляет вторые позиции поверх уже распознанного товара."""
    source = "Картофель молодой вес, сорт желательно Агретто, и чтобы крупные были картошинки."
    result = recover_omitted_explicit_items(
        {
            "intent": "add_items",
            "items": [
                {
                    "product_query": "картофель молодой сорт Агретто",
                    "quantity": None,
                    "unit": "",
                    "source_line": source,
                }
            ],
            "comment_bindings": [
                {
                    "text": "чтобы крупные были картошинки",
                    "scope": "item",
                    "target_item_indexes": [0],
                    "confidence": 0.99,
                }
            ],
        },
        source,
    )

    assert len(result["items"]) == 1
    assert result["items"][0]["comment"] == "чтобы крупные были картошинки"


def test_catalog_country_does_not_become_comment() -> None:
    """Не принимает страну из названия каталога за пожелание поставщику."""
    source = "Макароны CASA MILO, пакет, 500 гр, Италия"
    payload = {
        "intent": "add_items",
        "items": [
            {
                "product_query": "Макароны CASA MILO, пакет, 500 гр",
                "quantity": None,
                "unit": "",
                "comment": "Италия",
                "source_line": source,
            }
        ],
    }

    result = recover_omitted_explicit_items(payload, source)

    assert result["items"][0].get("comment", "") == ""


def test_product_name_binding_does_not_become_supplier_comment() -> None:
    """Не записывает полное название товара как комментарий поставщику."""
    source = "горчица дежонская — 1 штук"
    result = recover_omitted_explicit_items(
        {
            "intent": "add_items",
            "items": [
                {
                    "product_query": "горчица дежонская",
                    "quantity": 1,
                    "unit": "шт",
                    "source_line": source,
                }
            ],
            "comment_bindings": [
                {
                    "text": "горчица дежонская",
                    "scope": "item",
                    "target_item_indexes": [0],
                    "confidence": 0.99,
                }
            ],
        },
        source,
    )

    item = result["items"][0]
    assert item["product_query"] == "горчица дежонская"
    assert item["comment"] == ""
    assert item["user_comment_to_supplier"] == ""


def test_product_name_prefix_does_not_become_supplier_comment() -> None:
    """Не сохраняет начальную часть названия товара как комментарий."""
    source = "горчица дежонская — 1 штук"
    result = recover_omitted_explicit_items(
        {
            "intent": "add_items",
            "items": [
                {
                    "product_query": "горчица дежонская",
                    "quantity": 1,
                    "unit": "шт",
                    "source_line": source,
                    "comment": "горчица",
                    "user_comment_to_supplier": "горчица",
                }
            ],
            "comment_bindings": [
                {
                    "text": "горчица",
                    "scope": "item",
                    "target_item_indexes": [0],
                    "confidence": 0.99,
                }
            ],
        },
        source,
    )

    assert result["items"][0]["comment"] == ""


def test_comment_action_words_do_not_reach_supplier_comment() -> None:
    """Не записывает служебную команду добавления комментария в заявку."""
    for action in ("добавь комментарий", "покажи итог", "изменить количество"):
        source = f"Курица — 5 кг. {action}."
        result = recover_omitted_explicit_items(
            {
                "intent": "add_items",
                "items": [
                    {
                        "product_query": "Курица",
                        "quantity": 5,
                        "unit": "кг",
                        "source_line": source,
                        "comment": action,
                        "user_comment_to_supplier": action,
                    }
                ],
                "global_comment": action,
                "comment_bindings": [
                    {
                        "text": action,
                        "scope": "item",
                        "target_item_indexes": [0],
                        "confidence": 0.99,
                    }
                ],
            },
            source,
        )

        item = result["items"][0]
        assert result["global_comment"] == ""
        assert item["comment"] == ""
        assert item["user_comment_to_supplier"] == ""


def test_order_comment_action_words_do_not_reach_global_comment() -> None:
    """Не сохраняет служебную команду в общем комментарии заявки."""
    source = "Всем товарам добавь комментарий"
    result = recover_omitted_explicit_items(
        {
            "intent": "add_items",
            "items": [
                {
                    "product_query": "Курица",
                    "quantity": 5,
                    "unit": "кг",
                    "source_line": source,
                }
            ],
            "global_comment": "добавь комментарий",
            "comment_bindings": [
                {
                    "text": "добавь комментарий",
                    "scope": "order",
                    "target_item_indexes": [],
                    "confidence": 0.99,
                }
            ],
        },
        source,
    )

    assert result["global_comment"] == ""
    assert result["items"][0]["comment"] == ""


def test_voice_quantity_phrase_is_shared_with_text_parser() -> None:
    """Одинаково распознаёт количество в текстовом и голосовом транскрипте."""
    source = "Макароны CASA MILO, десять штук"

    text_items = parse_product_lines(source)
    voice_items = parse_product_lines(source)

    assert [(item.quantity, item.unit) for item in text_items] == [(10, "шт")]
    assert [item.model_dump() for item in voice_items] == [item.model_dump() for item in text_items]


def test_explicit_order_fact_is_separate_from_measurement_and_packaging() -> None:
    """Разделяет 1 кг, 6 штук в коробке и заказанные 22 штуки."""
    source = (
        "\u0413\u043e\u0440\u0447\u0438\u0446\u0430 \u0437\u0435\u0440\u043d\u0438\u0441\u0442\u0430\u044f Chatel \u0432\u0435\u0434\u0440\u043e 1 \u043a\u0433, "
        "6 \u0448\u0442\u0443\u043a \u0432 \u043a\u043e\u0440\u043e\u0431\u043a\u0435, \u0424\u0440\u0430\u043d\u0446\u0438\u044f, \u043c\u043d\u0435 \u043d\u0443\u0436\u043d\u043e 22 \u0448\u0442\u0443\u043a\u0438."
    )
    facts = extract_semantic_facts(source)

    by_text = {fact.original_text: fact for fact in facts}
    assert by_text["1 кг"].kind is SemanticFactKind.MEASUREMENT
    assert by_text["1 кг"].provenance == "source.measurement"
    assert by_text["6 штук"].kind is SemanticFactKind.CATALOG_ATTRIBUTE
    assert by_text["6 штук в коробке"].kind is SemanticFactKind.CATALOG_ATTRIBUTE
    assert by_text["22 штуки"].kind is SemanticFactKind.ORDER_QUANTITY
    assert by_text["22 штуки"].provenance == "source.order_marker"


def test_semantic_facts_keep_measurement_provenance() -> None:
    """Различает каталожную фасовку и возможное количество заказа."""
    facts = extract_semantic_facts("Соус 450 мл, 12 шт/кор, десять штук")

    assert all(fact.original_text for fact in facts)
    assert any(fact.kind is SemanticFactKind.CATALOG_ATTRIBUTE for fact in facts)
    assert any(fact.kind is SemanticFactKind.ORDER_QUANTITY for fact in facts)
    assert all(fact.provenance.startswith("source.") for fact in facts)


def test_orphan_ai_fragment_fails_closed_to_deterministic_item() -> None:
    """Не передаёт в каталог позицию без самостоятельного товарного якоря."""
    source = "Соус CASA MILO, в упаковке 500 мл, Италия"
    payload = {
        "intent": "add_items",
        "items": [
            {
                "product_query": "в упаковке Италия",
                "quantity": 15,
                "unit": "шт",
                "source_line": source,
            }
        ],
    }

    result = recover_omitted_explicit_items(payload, source)

    assert len(result["items"]) == 1
    expected = parse_product_lines(source)[0]
    assert result["items"][0]["product_query"] == expected.product_query
    assert result["items"][0]["quantity"] is None


def test_spoken_packaging_variants_keep_neighboring_weight_as_catalog_fact() -> None:
    """Сохраняет вес макарон каталожным признаком во всех разговорных вариантах."""
    for relation in (
        "15 шт/упак",
        "15 штук в упаковке",
        "в упаковке 15 штук",
        "упаковка по 15 штук",
    ):
        source = f"Макароны CASA MILO, пакет, 500 грамм, {relation}, Италия"
        item = parse_product_lines(source)[0]
        assert item.quantity is None
        assert item.packaging_role == "catalog_attribute"
        assert "500 грамм" in item.product_query
        assert "Италия" in item.product_query


def test_spoken_packaging_with_explicit_order_quantity_keeps_one_item() -> None:
    """Отделяет заказанное количество от веса и фасовки макарон."""
    source = "Макароны CASA MILO, пакет, 500 грамм, 15 штук в упаковке, Италия, нужно 10 штук"
    item = parse_product_lines(source)[0]
    assert (item.quantity, item.unit) == (10, "шт")
    assert item.packaging_text == "15 штук"
    assert "500 грамм" in item.product_query
    assert "Италия" in item.product_query


def test_two_ai_projections_of_one_anchor_collapse_to_one_operation() -> None:
    """Не превращает две AI-проекции одного стейка в две операции заказа."""
    source = "Стейк скерт Блэк Ангус Мираторг"
    result = recover_omitted_explicit_items(
        {
            "intent": "add_items",
            "items": [
                {"product_query": "Стейк скерт Блэк Ангус", "source_line": source},
                {"product_query": "Стейк Блэк Ангус Мираторг", "source_line": source},
            ],
        },
        source,
    )
    assert len(result["items"]) == 1


def test_explicit_order_comment_scope_strips_control_words() -> None:
    """Сохраняет общий охват и оставляет только текст пожелания."""
    source = "Всем товарам нужно привезти завтра до восьми вечера"
    payload = {"global_comment": source}
    items = [{"product_query": "Говядина", "source_line": source}]
    _apply_semantic_comment_bindings(
        payload,
        items,
        [{"text": source, "scope": "item", "target_item_indexes": [0], "confidence": 0.99}],
        source,
    )
    assert payload["global_comment"] == "привезти завтра до восьми вечера"
    assert items[0]["comment"] == ""


def test_ai_order_comment_command_wrapper_is_not_saved() -> None:
    """Удаляет оболочку команды при сверке общего комментария ИИ."""
    source = "Добавь общий комментарий привезти завтра к восьми вечера"
    payload = {"global_comment": source}
    items = [{"product_query": "Лук зелёный", "source_line": source}]

    _apply_semantic_comment_bindings(
        payload,
        items,
        [{"text": source, "scope": "item", "target_item_indexes": [0], "confidence": 0.99}],
        source,
    )

    assert payload["global_comment"] == "привезти завтра к восьми вечера"
    assert items[0]["comment"] == ""


def test_supplier_name_inside_product_does_not_lock_scope() -> None:
    """Не принимает название поставщика внутри товара за область поиска."""
    source = "Стейк скерт Блэк Ангус Мираторг"
    result = recover_omitted_explicit_items(
        {
            "intent": "add_items",
            "items": [
                {
                    "product_query": source,
                    "supplier_hint": "Мираторг",
                    "source_line": source,
                }
            ],
        },
        source,
    )
    assert result["items"][0]["supplier_hint"] == ""


def test_explicit_supplier_relation_is_preserved() -> None:
    """Сохраняет поставщика при явно названной связи с ним."""
    source = "Стейк скерт Блэк Ангус у поставщика Мираторг"
    result = recover_omitted_explicit_items(
        {
            "intent": "add_items",
            "items": [
                {
                    "product_query": source,
                    "supplier_hint": "Мираторг",
                    "source_line": source,
                }
            ],
        },
        source,
    )
    assert result["items"][0]["supplier_hint"] == "Мираторг"


def test_numeric_source_fragment_is_not_comment() -> None:
    """Не сохраняет приблизительное числовое описание как пожелание поставщику."""
    source = "Говядина приблизительно 8"
    result = recover_omitted_explicit_items(
        {
            "intent": "add_items",
            "items": [
                {
                    "product_query": "Говядина",
                    "comment": "приблизительно 8",
                    "source_line": source,
                }
            ],
        },
        source,
    )
    assert result["items"][0]["comment"] == ""


def test_comment_target_survives_shadow_item_collapse() -> None:
    """Сохраняет комментарий у товара после удаления теневой AI-позиции."""
    source = "Стейк скерт Блэк Ангус 5 кг, желательно без костей"
    result = recover_omitted_explicit_items(
        {
            "intent": "add_items",
            "items": [
                {
                    "product_query": "Стейк без костей",
                    "source_line": source,
                },
                {
                    "product_query": "Стейк скерт Блэк Ангус",
                    "quantity": 5,
                    "unit": "кг",
                    "source_line": source,
                },
            ],
            "comment_bindings": [
                {
                    "text": "без костей",
                    "scope": "item",
                    "target_item_indexes": [0],
                    "confidence": 0.99,
                }
            ],
        },
        source,
    )
    owner = next(
        item for item in result["items"] if item["product_query"] == "Стейк скерт Блэк Ангус"
    )
    assert "без костей" in owner["comment"]
    assert all(
        item["product_query"] == "Стейк скерт Блэк Ангус" or not item["comment"]
        for item in result["items"]
    )
