from restaurant_bot.domain.models import Intent
from restaurant_bot.services.parser import infer_intent, parse_product_lines


def test_parses_quantity_and_unit_after_product_name() -> None:
    item = parse_product_lines("Сироп роза 10 штук")[0]
    assert item.product_query == "Сироп роза"
    assert item.quantity == 10
    assert item.unit == "шт"


def test_parses_multiple_products_in_one_message() -> None:
    items = parse_product_lines("говядина 10 кг, курица 10 шт")
    assert [(item.product_query, item.quantity, item.unit) for item in items] == [
        ("говядина", 10, "кг"),
        ("курица", 10, "шт"),
    ]


def test_submit_phrases_are_not_treated_as_product_search() -> None:
    assert infer_intent("отправить").intent is Intent.SUBMIT_REQUEST
    assert infer_intent("отправить поставщику").intent is Intent.SUBMIT_AS_IS


def test_parses_trailing_supplier_comment_after_quantity() -> None:
    item = parse_product_lines("Сироп роза 5 шт, желательно охлаждённым")[0]
    assert (item.product_query, item.quantity, item.unit) == ("Сироп роза", 5, "шт")
    assert item.comment == "желательно охлаждённым"
    assert item.user_comment_to_supplier == "желательно охлаждённым"


def test_parser_keeps_multiword_comment_after_quantity_without_punctuation() -> None:
    item = parse_product_lines("Сироп роза 5 штук обязательно позвонить перед доставкой")[0]

    assert (item.product_query, item.quantity, item.unit) == ("Сироп роза", 5, "шт")
    assert item.comment == "обязательно позвонить перед доставкой"
    assert item.user_comment_to_supplier == item.comment


def test_parser_recovers_every_product_in_a_conjoined_spoken_list() -> None:
    items = parse_product_lines("Сироп роза 10 штук говядина 5 кг и джем 10 штук")

    assert [(item.product_query, item.quantity, item.unit) for item in items] == [
        ("Сироп роза", 10, "шт"),
        ("говядина", 5, "кг"),
        ("джем", 10, "шт"),
    ]


def test_product_packaging_remains_part_of_name_when_order_quantity_follows() -> None:
    item = parse_product_lines("Сироп Роза 1 л — 12")[0]
    assert (item.product_query, item.quantity, item.unit) == ("Сироп Роза 1 л", 12, "")
