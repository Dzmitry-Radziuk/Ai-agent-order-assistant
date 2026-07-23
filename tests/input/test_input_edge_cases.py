from restaurant_bot.services.parser import parse_product_lines
from restaurant_bot.services.text import to_float


def test_semicolon_and_newline_lists_keep_every_product() -> None:
    items = parse_product_lines("Сироп роза 10 шт; говядина 5 кг\nДжем 3 банки")

    assert [(item.product_query, item.quantity, item.unit) for item in items] == [
        ("Сироп роза", 10, "шт"),
        ("говядина", 5, "кг"),
        ("Джем", 3, "бан"),
    ]


def test_packaging_and_trailing_order_quantity_are_not_split() -> None:
    item = parse_product_lines("Сироп Роза 1 л — 12")[0]

    assert (item.product_query, item.quantity, item.unit) == ("Сироп Роза 1 л", 12, "")


def test_russian_google_sheets_currency_is_parsed_like_n8n() -> None:
    assert to_float("р.100,0") == 100
    assert to_float("р.1.200,50") == 1200.5
