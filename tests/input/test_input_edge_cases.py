from restaurant_bot.services.parser import parse_product_lines
from restaurant_bot.services.text import to_float


def test_semicolon_and_newline_lists_keep_every_product() -> None:
    """Проверяет, что точка с запятой и перенос строки списки keep каждый товар."""
    items = parse_product_lines("Сироп роза 10 шт; говядина 5 кг\nДжем 3 банки")

    assert [(item.product_query, item.quantity, item.unit) for item in items] == [
        ("Сироп роза", 10, "шт"),
        ("говядина", 5, "кг"),
        ("Джем", 3, "бан"),
    ]


def test_packaging_and_trailing_order_quantity_are_not_split() -> None:
    """Проверяет, что фасовка и после количества заказ количество являются не split."""
    item = parse_product_lines("Сироп Роза 1 л — 12")[0]

    assert (item.product_query, item.quantity, item.unit) == ("Сироп Роза 1 л", 12, "")


def test_bare_number_is_not_created_as_a_product() -> None:
    """Не превращает случайное число в товарную позицию."""
    assert parse_product_lines("12345") == []


def test_compact_packaging_is_kept_inside_exact_product_name() -> None:
    """Не превращает числа из фасовки в отдельные товары."""
    name = (
        "ME-ФБ-Глазной мускул говяжий с/м В/У ~ 2кг*10(~20кг) Фермерский бычок Мираторг (Брянск) Ро"
    )

    items = parse_product_lines(name)

    assert len(items) == 1
    assert items[0].product_query == name
    assert items[0].quantity is None
    assert items[0].unit == ""


def test_trailing_order_quantity_is_separated_from_compact_packaging() -> None:
    """Берёт заказанное количество после полного названия с фасовкой."""
    name = (
        "ME-ФБ-Глазной мускул говяжий с/м В/У ~ 2кг*10(~20кг) Фермерский бычок Мираторг (Брянск) Ро"
    )

    items = parse_product_lines(f"{name} 100 кг")

    assert len(items) == 1
    assert (items[0].product_query, items[0].quantity, items[0].unit) == (
        name,
        100,
        "кг",
    )


def test_russian_google_sheets_currency_is_parsed_like_n8n() -> None:
    """Проверяет, что русские Google таблицы валюта является parsed like n8n."""
    assert to_float("р.100,0") == 100
    assert to_float("р.1.200,50") == 1200.5
