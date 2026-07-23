from restaurant_bot.domain.models import Intent
from restaurant_bot.integrations.openai_client import (
    recover_omitted_explicit_items,
    restore_explicit_order_terms,
)


def test_voice_recovery_restores_quantity_and_unit_from_shared_source_line() -> None:
    source = "Сироп роза 10 штук, говядина 5 кг"
    items = [
        {"product_query": "Сироп роза", "quantity": None, "unit": "", "source_line": source},
        {"product_query": "говядина", "quantity": None, "unit": "", "source_line": source},
    ]

    restored = restore_explicit_order_terms(items, source)

    assert [(item["quantity"], item["unit"]) for item in restored] == [(10.0, "шт"), (5.0, "кг")]


def test_voice_recovery_never_replaces_the_model_product_name() -> None:
    source = "Сыропроза 10 штук"
    items = [{"product_query": "Сыропроза", "quantity": None, "unit": "", "source_line": source}]

    restored = restore_explicit_order_terms(items, source)

    assert restored[0]["product_query"] == "Сыропроза"
    assert restored[0]["quantity"] == 10.0
    assert restored[0]["unit"] == "шт"


def test_empty_voice_model_result_recovers_every_explicit_product() -> None:
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
    payload = {"intent": Intent.SHOW_CART, "items": []}

    restored = recover_omitted_explicit_items(payload, "Показать товары поставщика.")

    assert restored["intent"] is Intent.SHOW_CART
    assert restored["items"] == []


def test_navigation_intent_discards_model_items_before_cart_mutation() -> None:
    payload = {
        "intent": Intent.CHECK_MIN_SUM,
        "items": [{"product_query": "Тестовый товар", "quantity": None, "unit": ""}],
    }

    restored = recover_omitted_explicit_items(payload, "Давай посмотрим товары")

    assert restored["intent"] is Intent.CHECK_MIN_SUM
    assert restored["items"] == []


def test_partial_voice_model_result_restores_the_omitted_conjoined_item() -> None:
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
