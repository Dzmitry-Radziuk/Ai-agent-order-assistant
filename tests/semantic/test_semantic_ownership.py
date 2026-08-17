"""Проверяет единый разбор товарных признаков и их происхождение."""

from __future__ import annotations

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


def test_voice_quantity_phrase_is_shared_with_text_parser() -> None:
    """Одинаково распознаёт количество в текстовом и голосовом транскрипте."""
    source = "Макароны CASA MILO, десять штук"

    text_items = parse_product_lines(source)
    voice_items = parse_product_lines(source)

    assert [(item.quantity, item.unit) for item in text_items] == [(10, "шт")]
    assert [item.model_dump() for item in voice_items] == [item.model_dump() for item in text_items]


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
