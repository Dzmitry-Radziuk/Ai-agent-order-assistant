"""Проверяет поведение, связанное с модулем «test voice quantity recovery»."""

import pytest

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


def test_multi_item_source_spans_keep_order_quantity_local_to_last_item() -> None:
    """Проверяет локальную принадлежность quantity и отсутствие заказа в названии товара."""
    source = (
        "\u0425\u0440\u0435\u043d \u0441\u0442\u043e\u043b\u043e\u0432\u044b\u0439 \u0434\u043e\u043c\u0430\u0448\u043d\u0438\u0439 \u043a\u0430\u043b\u043e\u0440\u0438\u0439\u043d\u044b\u0439, 160 GRT \u0411, \u0420\u043e\u0441\u0441\u0438\u044f 12 \u043a \u043e\u0434\u043d\u043e\u043c\u0443, 5 \u0448\u0442\u0443\u043a, \u0438 "
        "\u0433\u043e\u0440\u0447\u0438\u0446\u0430 \u0434\u0438\u0436\u043e\u043d\u0441\u043a\u0430\u044f, \u0447\u0430\u0442\u043b, \u0432\u0435\u0434\u0440\u043e, 1 \u043a\u0438\u043b\u043e\u0433\u0440\u0430\u043c\u043c, 6 \u0448\u0442\u0443\u043a \u0432 \u043a\u043e\u0440\u043e\u0431\u043a\u0435, \u0424\u0440\u0430\u043d\u0446\u0438\u044f. "
        "\u041d\u0443\u0436\u043d\u043e 13 \u0448\u0442\u0443\u043a."
    )
    result = recover_omitted_explicit_items(
        {
            "intent": Intent.ADD_ITEMS,
            "global_comment": "\u041d\u0443\u0436\u043d\u043e 13 \u0448\u0442\u0443\u043a",
            "items": [
                {
                    "product_query": "\u0425\u0440\u0435\u043d \u0441\u0442\u043e\u043b\u043e\u0432\u044b\u0439 \u0434\u043e\u043c\u0430\u0448\u043d\u0438\u0439 \u043a\u0430\u043b\u043e\u0440\u0438\u0439\u043d\u044b\u0439",
                    "quantity": 5,
                    "unit": "\u0448\u0442",
                    "source_line": "",
                },
                {
                    "product_query": "\u0433\u043e\u0440\u0447\u0438\u0446\u0430 \u0434\u0438\u0436\u043e\u043d\u0441\u043a\u0430\u044f, \u0447\u0430\u0442\u043b, \u0432\u0435\u0434\u0440\u043e",
                    "quantity": 6,
                    "unit": "\u0448\u0442",
                    "source_line": "",
                },
            ],
        },
        source,
    )

    assert result["global_comment"] == ""
    assert result.get("comment_clarification", "") == ""
    assert [(item["quantity"], item["unit"]) for item in result["items"]] == [
        (5.0, "\u0448\u0442"),
        (13.0, "\u0448\u0442"),
    ]
    assert all(not item["comment"] for item in result["items"])
    assert "13" not in result["items"][1]["product_query"]
    assert result["items"][0]["source_line"] == source
    assert result["items"][0]["source_span"] != result["items"][1]["source_span"]


def test_leading_spoken_quantities_stay_with_the_following_product() -> None:
    """Не переносит начальное словесное количество на соседний товар."""
    source = "Один килограмм пеламиды и два килограмма томатов."
    restored = recover_omitted_explicit_items(
        {
            "intent": Intent.ADD_ITEMS,
            "items": [
                {
                    "product_query": "пеламиды",
                    "quantity": 1,
                    "unit": "кг",
                    "source_line": "",
                },
                {
                    "product_query": "томатов",
                    "quantity": 2,
                    "unit": "кг",
                    "source_line": "",
                },
            ],
        },
        source,
    )

    assert [(item["quantity"], item["unit"]) for item in restored["items"]] == [
        (1.0, "кг"),
        (2.0, "кг"),
    ]


def test_leading_count_word_keeps_model_piece_quantity() -> None:
    """Сохраняет количество штук без повторно названной единицы."""
    source = "Бутылка водки и две селедки."
    restored = recover_omitted_explicit_items(
        {
            "intent": Intent.ADD_ITEMS,
            "items": [
                {
                    "product_query": "водки",
                    "quantity": None,
                    "unit": "",
                    "source_line": "",
                },
                {
                    "product_query": "селедки",
                    "quantity": 2,
                    "unit": "шт",
                    "source_line": "",
                },
            ],
        },
        source,
    )

    assert restored["items"][0]["quantity"] is None
    assert (restored["items"][1]["quantity"], restored["items"][1]["unit"]) == (2.0, "шт")


def test_spoken_pair_keeps_piece_unit_after_neighbor_bottle_quantity() -> None:
    """Не переносит единицу «бутылка» на товар с количеством «пару»."""
    source = "А если я хочу две бутылки водки и пару селедки?"
    restored = recover_omitted_explicit_items(
        {
            "intent": Intent.ADD_ITEMS,
            "items": [
                {
                    "product_query": "водка",
                    "quantity": 2,
                    "unit": "бутылка",
                    "source_line": source,
                },
                {
                    "product_query": "селедка",
                    "quantity": 2,
                    "unit": "штука",
                    "source_line": source,
                },
            ],
        },
        source,
    )

    assert [(item["quantity"], item["unit"]) for item in restored["items"]] == [
        (2.0, "бут"),
        (2.0, "шт"),
    ]


def test_partial_ai_source_lines_are_bound_to_their_product_anchors() -> None:
    """Восстанавливает хвост количества по границе товара, а не по соседней фасовке."""
    source = (
        "Свинина Окорок Пармский с/к 5 кг,"
        "Хрен столовый Домашний, Кал-й,160грт/Б, Россия (12/1) 10 штук"
    )
    restored = recover_omitted_explicit_items(
        {
            "intent": Intent.ADD_ITEMS,
            "items": [
                {
                    "product_query": "Свинина Окорок Пармский с/к 5 кг",
                    "quantity": 5,
                    "unit": "кг",
                    "source_line": "Свинина Окорок Пармский с/к 5 кг",
                },
                {
                    "product_query": "Хрен столовый Домашний, Кал-й,160грт/Б, Россия",
                    "quantity": 10,
                    "unit": "шт",
                    "source_line": "Хрен столовый Домашний, Кал-й,160грт/Б, Россия (12/1)",
                },
            ],
        },
        source,
    )

    first, second = restored["items"]
    assert (first["quantity"], first["unit"]) == (5.0, "кг")
    assert first.get("packaging_text") in (None, "")
    assert (second["quantity"], second["unit"]) == (10.0, "шт")
    assert second["packaging_text"] == "12/1"
    assert second["packaging_role"] == "catalog_attribute"
    assert "10 штук" in second["source_span"]


def test_partial_ai_source_line_keeps_trailing_local_comment() -> None:
    """Не отрезает комментарий между товаром и следующим товарным якорем."""
    source = (
        "Филе лосося 0,8-1,3 кг, нужно 10 кг, обязательно зачищенное, "
        "лук зелёный 10 кг срез корня от 5 сантиметров"
    )
    restored = recover_omitted_explicit_items(
        {
            "intent": Intent.ADD_ITEMS,
            "items": [
                {
                    "product_query": "Филе лосося 0,8-1,3 кг",
                    "quantity": 10,
                    "unit": "кг",
                    "comment": "обязательно зачищенное",
                    "source_line": "Филе лосося 0,8-1,3 кг",
                },
                {
                    "product_query": "лук зелёный",
                    "quantity": 10,
                    "unit": "кг",
                    "comment": "срез корня от 5 сантиметров",
                    "source_line": "лук зелёный 10 кг срез корня от 5 сантиметров",
                },
            ],
        },
        source,
    )

    assert restored["items"][0]["comment"] == "обязательно зачищенное"
    assert restored["items"][1]["comment"] == "срез корня от 5 сантиметров"
    assert "обязательно зачищенное" in restored["items"][0]["source_span"]


@pytest.mark.parametrize(
    ("source", "quantities", "global_comment", "clarification"),
    [
        (
            "\u041b\u0443\u043a 5 \u043a\u0433 \u0438 \u043a\u0430\u0440\u0442\u043e\u0448\u043a\u0430 10 \u043a\u0433",
            [5.0, 10.0],
            "",
            "",
        ),
        (
            "\u041b\u0443\u043a 5 \u043a\u0433, \u043a\u0430\u0440\u0442\u043e\u0448\u043a\u0430. \u041d\u0443\u0436\u043d\u043e 10 \u043a\u0433",
            [5.0, 10.0],
            "\u041d\u0443\u0436\u043d\u043e 10 \u043a\u0433",
            "",
        ),
        (
            "\u041b\u0443\u043a \u0438 \u043a\u0430\u0440\u0442\u043e\u0448\u043a\u0430, \u0432\u0441\u0435\u0433\u043e \u043d\u0443\u0436\u043d\u043e 15 \u043a\u0433",
            [None, None],
            "\u0412\u0441\u0435\u0433\u043e \u043d\u0443\u0436\u043d\u043e 15 \u043a\u0433",
            "\u0412\u0441\u0435\u0433\u043e \u043d\u0443\u0436\u043d\u043e 15 \u043a\u0433",
        ),
        (
            "\u041b\u0443\u043a 5 \u043a\u0433, \u043a\u0430\u0440\u0442\u043e\u0448\u043a\u0430 10 \u043a\u0433. \u041d\u0443\u0436\u043d\u043e 20 \u043a\u0433",
            [5.0, 10.0],
            "\u041d\u0443\u0436\u043d\u043e 20 \u043a\u0433",
            "20 \u043a\u0433",
        ),
    ],
)
def test_multi_item_quantity_ownership_matrix(
    source: str,
    quantities: list[float | None],
    global_comment: str,
    clarification: str,
) -> None:
    """Проверяет локальные, detached и конфликтующие количества в списке."""
    result = recover_omitted_explicit_items(
        {
            "intent": Intent.ADD_ITEMS,
            "global_comment": global_comment,
            "items": [
                {
                    "product_query": "\u043b\u0443\u043a",
                    "quantity": quantities[0],
                    "unit": "\u043a\u0433",
                    "source_line": "",
                },
                {
                    "product_query": "\u043a\u0430\u0440\u0442\u043e\u0448\u043a\u0430",
                    "quantity": quantities[1],
                    "unit": "\u043a\u0433",
                    "source_line": "",
                },
            ],
        },
        source,
    )

    assert [item["quantity"] for item in result["items"]] == quantities
    assert result.get("comment_clarification", "") == clarification


def test_voice_recovery_restores_quantity_and_unit_from_shared_source_line() -> None:
    """Проверяет, что голос восстановление восстанавливает количество и единица измерения из общая исходный строка."""
    source = "Сироп роза 10 штук, говядина 5 кг"
    items = [
        {"product_query": "Сироп роза", "quantity": None, "unit": "", "source_line": source},
        {"product_query": "говядина", "quantity": None, "unit": "", "source_line": source},
    ]

    restored = restore_explicit_order_terms(items, source)

    assert [(item["quantity"], item["unit"]) for item in restored] == [(10.0, "шт"), (5.0, "кг")]


def test_voice_ai_item_with_word_quantity_is_not_split_by_recovery() -> None:
    """Сохраняет одну AI-позицию, если хвост исходной фразы содержит её количество."""
    source = "Яйцо куриное цветное, три короба."
    restored = recover_omitted_explicit_items(
        {
            "intent": Intent.ADD_ITEMS,
            "items": [
                {
                    "product_query": "Яйцо куриное цветное",
                    "quantity": 3,
                    "unit": "короб",
                    "source_line": source,
                }
            ],
        },
        source,
    )

    assert len(restored["items"]) == 1
    assert restored["items"][0]["product_query"] == "Яйцо куриное цветное"
    assert (restored["items"][0]["quantity"], restored["items"][0]["unit"]) == (3, "кор")


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


def test_explicit_order_quantity_is_not_overwritten_by_catalog_packaging() -> None:
    """Сохраняет 22 шт и отбрасывает фасовочный комментарий."""
    source = (
        "\u0413\u043e\u0440\u0447\u0438\u0446\u0430 \u0437\u0435\u0440\u043d\u0438\u0441\u0442\u0430\u044f Chatel \u0432\u0435\u0434\u0440\u043e 1 \u043a\u0433, "
        "6 \u0448\u0442\u0443\u043a \u0432 \u043a\u043e\u0440\u043e\u0431\u043a\u0435, \u0424\u0440\u0430\u043d\u0446\u0438\u044f, \u043c\u043d\u0435 \u043d\u0443\u0436\u043d\u043e 22 \u0448\u0442\u0443\u043a\u0438."
    )
    restored = recover_omitted_explicit_items(
        {
            "intent": Intent.ADD_ITEMS,
            "items": [
                {
                    "product_query": "\u0413\u043e\u0440\u0447\u0438\u0446\u0430 \u0437\u0435\u0440\u043d\u0438\u0441\u0442\u0430\u044f Chatel \u0432\u0435\u0434\u0440\u043e 1 \u043a\u0433",
                    "quantity": 22,
                    "unit": "\u0448\u0442",
                    "comment": "6 \u0448\u0442\u0443\u043a \u0432 \u043a\u043e\u0440\u043e\u0431\u043a\u0435, \u0424\u0440\u0430\u043d\u0446\u0438\u044f",
                    "user_comment_to_supplier": "6 \u0448\u0442\u0443\u043a \u0432 \u043a\u043e\u0440\u043e\u0431\u043a\u0435, \u0424\u0440\u0430\u043d\u0446\u0438\u044f",
                    "source_line": source,
                }
            ],
        },
        source,
    )

    item = restored["items"][0]
    assert (item["quantity"], item["unit"]) == (22.0, "\u0448\u0442")
    assert item["packaging_role"] == "catalog_attribute"
    assert item["comment"] == ""
    assert item["user_comment_to_supplier"] == ""
    assert item["comment_source"] == "none"


def test_order_quantity_is_removed_from_catalog_packaging_metadata() -> None:
    """Не оставляет заказанные коробки как числовую фасовку товара."""
    source = "Яйцо куриное 30 штук в пачке 5 коробок"
    restored = restore_explicit_order_terms(
        [
            {
                "product_query": "Яйцо куриное в пачке",
                "quantity": 5,
                "unit": "кор",
                "packaging_text": "в пачке 5 коробок",
                "packaging_role": "catalog_attribute",
                "packaging_confidence": 0.9,
                "source_line": source,
            }
        ],
        source,
    )

    item = restored[0]
    assert (item["quantity"], item["unit"]) == (5.0, "кор")
    assert item["packaging_text"] == "в пачке"
    assert item["packaging_role"] == "catalog_attribute"


def test_long_voice_list_removes_only_each_item_order_quantity_from_packaging() -> None:
    """Не смешивает заказанное количество с фасовкой соседних позиций."""
    source = (
        "Яйцо куриное 30 штук в пачке 5 коробок, "
        "вишня без косточки 10 килограмм 3 коробки, "
        "облепиха замороженная очищенная первый сорт РБ 10 килограмм "
        "в коробке 3 штуки, яйцо куриное цветное 360 на 30, 8 коробок"
    )
    restored = restore_explicit_order_terms(
        [
            {
                "product_query": "Яйцо куриное в пачке",
                "quantity": 5,
                "unit": "кор",
                "packaging_text": "в пачке 5 коробок",
                "packaging_role": "catalog_attribute",
                "source_line": source,
                "source_span": "Яйцо куриное 30 штук в пачке 5 коробок",
            },
            {
                "product_query": "вишня без косточки",
                "quantity": 3,
                "unit": "кор",
                "packaging_text": "10 килограмм 3 коробки",
                "packaging_role": "catalog_attribute",
                "source_line": source,
                "source_span": "вишня без косточки 10 килограмм 3 коробки",
            },
            {
                "product_query": "облепиха замороженная",
                "quantity": 3,
                "unit": "шт",
                "packaging_text": "в коробке 3 штуки",
                "packaging_role": "catalog_attribute",
                "source_line": source,
                "source_span": "облепиха замороженная очищенная первый сорт РБ 10 килограмм в коробке 3 штуки",
            },
            {
                "product_query": "яйцо куриное цветное",
                "quantity": 8,
                "unit": "кор",
                "packaging_text": "360 на 30",
                "packaging_role": "catalog_attribute",
                "source_line": source,
                "source_span": "яйцо куриное цветное 360 на 30, 8 коробок",
            },
        ],
        source,
    )

    assert [item["packaging_text"] for item in restored] == [
        "в пачке",
        "10 килограмм",
        "в коробке",
        "360 на 30",
    ]


@pytest.mark.parametrize(
    ("source", "expected_quantity", "expected_unit"),
    [
        (
            "\u0421\u043e\u0443\u0441 500 \u0433, 12 \u0448\u0442\u0443\u043a \u0432 \u043a\u043e\u0440\u043e\u0431\u043a\u0435, \u043d\u0443\u0436\u043d\u043e 7 \u0448\u0442\u0443\u043a",
            7.0,
            "\u0448\u0442",
        ),
        (
            "\u041c\u0430\u043a\u0430\u0440\u043e\u043d\u044b 500 \u0433, 15 \u0448\u0442/\u0443\u043f\u0430\u043a, \u043c\u043d\u0435 \u043d\u0443\u0436\u043d\u043e 10 \u0448\u0442\u0443\u043a",
            10.0,
            "\u0448\u0442",
        ),
        (
            "\u041c\u0430\u0441\u043b\u043e 450 \u043c\u043b, 12 \u0448\u0442/\u043a\u043e\u0440, \u0437\u0430\u043a\u0430\u0437\u0430\u0442\u044c 24 \u0448\u0442\u0443\u043a",
            24.0,
            "\u0448\u0442",
        ),
        (
            "\u0422\u043e\u0432\u0430\u0440 1 \u043a\u0433, \u043a\u043e\u0440\u043e\u0431\u043a\u0430 \u043f\u043e 6 \u0448\u0442\u0443\u043a, \u0434\u043e\u0431\u0430\u0432\u0438\u0442\u044c 18 \u0448\u0442\u0443\u043a",
            18.0,
            "\u0448\u0442",
        ),
        (
            "\u0422\u043e\u0432\u0430\u0440 1 \u043a\u0433, 6 \u0448\u0442\u0443\u043a \u0432 \u043a\u043e\u0440\u043e\u0431\u043a\u0435",
            None,
            "",
        ),
    ],
)
def test_quantity_provenance_table_keeps_only_order_quantity(
    source: str,
    expected_quantity: float | None,
    expected_unit: str,
) -> None:
    """Проверяет таблицу фасовок и явных количеств заказа."""
    restored = recover_omitted_explicit_items(
        {
            "intent": Intent.ADD_ITEMS,
            "items": [
                {
                    "product_query": source.split()[0],
                    "quantity": 6,
                    "unit": "\u0448\u0442",
                    "source_line": source,
                }
            ],
        },
        source,
    )

    item = restored["items"][0]
    assert (item["quantity"], item["unit"]) == (expected_quantity, expected_unit)


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


def test_single_quantity_before_comment_is_carried_to_catalog_reconciliation() -> None:
    """Сохраняет предложенное количество до проверки фасовки каталогом."""
    source = "Утка Жир Кусок 10 кг, желательно на завтра к 8 вечера"
    restored = restore_explicit_order_terms(
        [
            {
                "product_query": "Утка Жир Кусок",
                "quantity": 10,
                "unit": "кг",
                "source_line": source,
            }
        ],
        source,
    )

    assert (restored[0]["quantity"], restored[0]["unit"]) == (10.0, "кг")


def test_single_quantity_before_comment_survives_ai_recovery() -> None:
    """Не теряет количество заказа при восстановлении ответа ИИ."""
    source = "Утка Жир Кусок 10 кг, желательно на завтра к 8 вечера"
    restored = recover_omitted_explicit_items(
        {
            "intent": Intent.ADD_ITEMS,
            "items": [
                {
                    "product_query": "Утка Жир Кусок",
                    "quantity": 10,
                    "unit": "кг",
                    "comment": "желательно на завтра к 8 вечера",
                    "source_line": source,
                }
            ],
        },
        source,
    )

    item = restored["items"][0]
    assert (item["quantity"], item["unit"]) == (10.0, "кг")
    assert item["comment"] == "желательно на завтра к 8 вечера"


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


def test_long_voice_items_keep_local_quantities_and_comments() -> None:
    """Сохраняет количества и комментарии у своих позиций длинного транскрипта."""
    source = (
        "Мне нужно филе лосося 0,8-1,2 килограмма, свежее, 5 килограмм мне нужно, "
        "обязательно зачищенное ТрНЦ, также мне нужен лук зелёный 10 килограмм "
        "срез корня от 5 сантиметров и вино на кухню."
    )
    restored = recover_omitted_explicit_items(
        {
            "intent": Intent.ADD_ITEMS,
            "items": [
                {
                    "product_query": "филе лосося 0,8-1,2 килограмма",
                    "quantity": 5,
                    "unit": "кг",
                    "source_line": source,
                    "source_span": (
                        "филе лосося 0,8-1,2 килограмма, свежее, 5 килограмм мне "
                        "нужно, обязательно зачищенное ТрНЦ"
                    ),
                },
                {
                    "product_query": "лук зелёный",
                    "quantity": 10,
                    "unit": "кг",
                    "source_line": source,
                    "source_span": "лук зелёный 10 килограмм срез корня от 5 сантиметров",
                },
                {
                    "product_query": "вино на кухню",
                    "quantity": None,
                    "unit": "",
                    "source_line": source,
                    "source_span": "вино на кухню",
                },
            ],
            "comment_bindings": [
                {
                    "text": "обязательно зачищенное ТрНЦ",
                    "scope": "item",
                    "target_item_indexes": [0],
                    "confidence": 0.99,
                },
                {
                    "text": "срез корня от 5 сантиметров",
                    "scope": "item",
                    "target_item_indexes": [1],
                    "confidence": 0.99,
                },
            ],
        },
        source,
    )

    assert [(item["quantity"], item["unit"]) for item in restored["items"]] == [
        (5.0, "кг"),
        (10.0, "кг"),
        (None, ""),
    ]
    assert [item["comment"] for item in restored["items"]] == [
        "обязательно зачищенное ТрНЦ",
        "срез корня от 5 сантиметров",
        "",
    ]


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


def test_recognized_voice_items_do_not_restore_nested_comment_fragments() -> None:
    """Не добавляет товар из частей уже подтверждённых source-span позиций."""
    source = (
        "Мне нужно филе лосося 0,8-1,3 кг, 20-22 кг коробки. "
        "Обязательно свежее и зачищенное 3NC. "
        "И лук зеленый срез корня от 5 сантиметров, 5 кг"
    )
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {
                "product_query": "филе лосося 0,8-1,3 кг зачищенное 3NC",
                "quantity": None,
                "unit": "",
                "comment": "обязательно свежее",
                "source_line": "филе лосося 0,8-1,3 кг",
            },
            {
                "product_query": "лук зеленый",
                "quantity": 5,
                "unit": "кг",
                "comment": "срез корня от 5 сантиметров",
                "source_line": "лук зеленый срез корня от 5 сантиметров, 5 кг",
            },
        ],
    }

    restored = recover_omitted_explicit_items(payload, source)

    assert [item["product_query"] for item in restored["items"]] == [
        "филе лосося 0,8-1,3 кг зачищенное 3NC",
        "лук зеленый",
    ]
    assert [item["comment"] for item in restored["items"]] == [
        "обязательно свежее",
        "срез корня от 5 сантиметров",
    ]


def test_multi_item_recovery_does_not_copy_one_product_packaging() -> None:
    """Не переносит фасовку первой позиции на соседние товары списка."""
    source = (
        "Мне нужно филе лосося 0.8-1.3 кг, 22 кг в коробке. "
        "Мне нужно 5 кг обязательно зачищенные тримсе и лук зеленый 4 кг "
        "срез корня от 5 см"
    )
    items = [
        {
            "product_query": "филе лосося 0.8-1.3 кг",
            "quantity": 22,
            "unit": "кг",
            "source_span": "филе лосося 0.8-1.3 кг, 22 кг в коробке",
            "packaging_text": "22 кг в коробке",
            "packaging_role": "catalog_attribute",
        },
        {
            "product_query": "тримсе",
            "quantity": 5,
            "unit": "кг",
            "source_span": "Мне нужно 5 кг обязательно зачищенные тримсе",
            "packaging_text": "",
            "packaging_role": "none",
        },
        {
            "product_query": "лук зеленый",
            "quantity": 4,
            "unit": "кг",
            "source_span": "лук зеленый 4 кг срез корня от 5 см",
            "packaging_text": "",
            "packaging_role": "none",
        },
    ]

    restored = restore_explicit_order_terms(items, source)

    assert [(item["packaging_text"], item["packaging_role"]) for item in restored[1:]] == [
        ("", "none"),
        ("", "none"),
    ]


def test_reconciliation_removes_trailing_list_connector_from_product_query() -> None:
    """Не оставляет союз перед следующей позицией в названии товара."""
    source = "Мне нужно филе форели 0,8-1,3 кг, а также лук свежий и хлеб."
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {
                "product_query": "филе форели 0,8-1,3 кг, а",
                "quantity": None,
                "unit": "",
                "source_line": "филе форели 0,8-1,3 кг, а",
            },
            {
                "product_query": "лук свежий",
                "quantity": None,
                "unit": "",
                "source_line": "лук свежий",
            },
            {
                "product_query": "хлеб",
                "quantity": None,
                "unit": "",
                "source_line": "хлеб",
            },
        ],
    }

    restored = recover_omitted_explicit_items(payload, source)

    assert restored["items"][0]["product_query"] == "филе форели 0,8-1,3 кг"


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


@pytest.mark.parametrize("source", ["филе", "фарш", "тушка", "мякоть"])
def test_unknown_ai_response_recovers_standalone_product_form(source: str) -> None:
    """Передаёт самостоятельную форму товара в каталог после пустого ответа ИИ."""
    restored = recover_omitted_explicit_items(
        {"intent": Intent.UNKNOWN, "items": []},
        source,
    )

    assert restored["intent"] is Intent.ADD_ITEMS
    assert [
        (item["product_query"], item["quantity"], item["unit"]) for item in restored["items"]
    ] == [(source, None, "")]


def test_unknown_ai_response_does_not_recover_lone_quality_qualifier() -> None:
    """Не превращает одиночный признак качества в товар без основания."""
    restored = recover_omitted_explicit_items(
        {"intent": Intent.UNKNOWN, "items": []},
        "свежий",
    )

    assert restored["intent"] is Intent.UNKNOWN
    assert restored["items"] == []


def test_unknown_ai_response_does_not_recover_deictic_voice_phrase() -> None:
    """Не создаёт новый товар из голосовой ссылки на неуказанный контекст."""
    restored = recover_omitted_explicit_items(
        {"intent": Intent.UNKNOWN, "items": []},
        "Все эти лежитки оранжевые",
    )

    assert restored["intent"] is Intent.UNKNOWN
    assert restored["items"] == []
