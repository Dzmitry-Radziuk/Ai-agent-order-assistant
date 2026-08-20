"""Проверяет поведение, связанное с модулем «test input edge cases»."""

import pytest

from restaurant_bot.parsing.numeric import to_float
from restaurant_bot.parsing.products import parse_product_lines


def test_semicolon_and_newline_lists_keep_every_product() -> None:
    """Проверяет, что точка с запятой и перенос строки сохраняют каждый товар отдельной позицией."""
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


def test_word_quantity_with_comma_stays_with_the_product() -> None:
    """Не превращает словесное количество после запятой во вторую позицию."""
    items = parse_product_lines("Яйцо куриное цветное, три короба.")

    assert len(items) == 1
    assert items[0].product_query == "яйцо куриное цветное"
    assert (items[0].quantity, items[0].unit) == (3, "кор")


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


def test_packaging_sentence_and_order_quantity_stay_one_item() -> None:
    """Сохраняет фасовку товара и отдельное заказанное количество одной позиции."""
    items = parse_product_lines(
        "Марципановые конфеты на шоколадной подложке, кенигсбергский стиль, "
        "90 грамм. Желательно привезти завтра. Нужно 5 килограмм."
    )

    assert len(items) == 1
    assert "90 грамм" in items[0].product_query
    assert (items[0].quantity, items[0].unit) == (5, "кг")
    assert items[0].comment == "Желательно привезти завтра"


def test_packaging_connector_and_order_quantity_stay_one_item() -> None:
    """Не принимает связку фасовки «550 грамм на 20 штук» за два товара."""
    items = parse_product_lines("Тесто для спринг-роллов 550 грамм на 20 штук Екимал 5 штук")

    assert len(items) == 1
    assert "на 20 штук" in items[0].product_query
    assert (items[0].quantity, items[0].unit) == (5, "шт")
    assert "550 грамм на 20 шт" in items[0].packaging_text
    assert items[0].packaging_role == "catalog_attribute"


def test_numeric_range_in_product_name_is_not_order_quantity() -> None:
    """Не принимает диапазон фасовки или размера за количество заказа."""
    items = parse_product_lines("Филе форели свежее 0,8-1,2 килограмма зачищенное 3НС")

    assert len(items) == 1
    assert items[0].product_query == "Филе форели свежее 0,8-1,2 килограмма зачищенное 3НС"
    assert items[0].quantity is None
    assert items[0].unit == ""


def test_numeric_range_with_double_hyphen_is_not_order_quantity() -> None:
    """Распознаёт ASCII-запись диапазона с двумя дефисами как характеристику товара."""
    items = parse_product_lines("Филе форели 0,8 -- 1,2 килограмма зачищенное")

    assert len(items) == 1
    assert items[0].quantity is None
    assert items[0].unit == ""


def test_numeric_range_keeps_explicit_quantity_after_product_name() -> None:
    """После диапазона берёт только явно названное количество заказа."""
    items = parse_product_lines("Филе форели 0,8-1,2 кг зачищенное 10 кг")

    assert len(items) == 1
    assert items[0].product_query == "Филе форели 0,8-1,2 кг зачищенное"
    assert (items[0].quantity, items[0].unit) == (10, "кг")


def test_alternative_packaging_is_not_split_into_fake_products() -> None:
    """Сохраняет варианты фасовки одной позиции и отделяет заказанное число ведер."""
    items = parse_product_lines("Капуста квашеная ведро 5 кг или 4,5 кг, два ведра.")

    assert len(items) == 1
    assert "5 кг или 4,5 кг" in items[0].product_query
    assert (items[0].quantity, items[0].unit) == (2, "ведро")


def test_product_code_range_is_kept_before_explicit_order_quantity() -> None:
    """Не отрезает код товара с дефисом перед количеством заказа."""
    items = parse_product_lines("Филе форели зачищенное 30-08 1,3 килограмма.")

    assert len(items) == 1
    assert items[0].product_query == "Филе форели зачищенное 30-08"
    assert (items[0].quantity, items[0].unit) == (1.3, "кг")


@pytest.mark.parametrize(
    "source",
    [
        "Грудинка говяжья ССВУ, 5,5 килограмм, 16,5 килограмм, Блэк Ангус, МирВаторг, Брянск",
        "грудинка 5,5 кг, 16,5 кг, блэк ангус",
        "грудинка 5,5 и 16,5 кг, блэк ангус",
    ],
)
def test_ambiguous_spoken_weight_pair_stays_one_product(source: str) -> None:
    """Не выбирает один вес из неоднозначной пары одной товарной позиции."""
    items = parse_product_lines(source)

    assert len(items) == 1
    item = items[0]
    assert item.product_query == source
    assert item.quantity is None
    assert item.unit == ""
    assert item.comment == ""
    assert item.packaging_role == "ambiguous"
    assert item.packaging_confidence < 0.85


def test_spoken_from_to_range_is_a_product_attribute_not_order_quantity() -> None:
    """Сохраняет устный диапазон от и до как характеристику товара."""
    source = "грудинка от 5,5 до 16,5 кг, блэк ангус"

    items = parse_product_lines(source)

    assert len(items) == 1
    item = items[0]
    assert item.product_query == source
    assert item.quantity is None
    assert item.unit == ""
    assert item.comment == ""
    assert item.packaging_role == "catalog_attribute"
    assert item.packaging_confidence >= 0.85


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("сироп роза 10 шт и джем вишня 5 шт", [("сироп роза", 10), ("джем вишня", 5)]),
        ("говядина 5 кг, томаты 8 кг", [("говядина", 5), ("томаты", 8)]),
    ],
)
def test_multiple_products_still_split_on_independent_names(
    source: str,
    expected: list[tuple[str, int]],
) -> None:
    """Разделяет количества только при самостоятельном названии каждого товара."""
    items = parse_product_lines(source)

    assert [(item.product_query, item.quantity) for item in items] == expected


def test_russian_google_sheets_currency_is_parsed_like_n8n() -> None:
    """Проверяет, что валюта русских таблиц Google разбирается так же, как в n8n."""
    assert to_float("р.100,0") == 100
    assert to_float("р.1.200,50") == 1200.5
