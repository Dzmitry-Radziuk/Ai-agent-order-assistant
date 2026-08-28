"""Проверяет поведение, связанное с модулем «test ai result integrity»."""

from restaurant_bot.domain.models import Intent
from restaurant_bot.integrations.openai_parsing import recover_omitted_explicit_items


def _item_payload(source: str, product_query: str, comment: str = "") -> dict:
    """Создаёт исходные данные ИИ для проверки сохранения товара и комментария."""
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {
                "product_query": product_query,
                "quantity": 5,
                "unit": "кг",
                "comment": comment,
                "user_comment_to_supplier": comment,
                "comment_source": "semantic" if comment else "none",
                "source_line": source,
            }
        ],
    }
    if comment:
        payload["comment_bindings"] = [
            {
                "text": comment,
                "scope": "item",
                "target_item_indexes": [0],
                "confidence": 0.99,
            }
        ]
    return payload


def test_product_facts_in_query_are_not_duplicated_as_supplier_comment() -> None:
    """Не дублирует подтверждённый признак названия в комментарии поставщику."""
    source = "свиная шея 5 кг без костей без кожи без хрящиков"
    comment = "без костей без кожи без хрящиков"

    restored = recover_omitted_explicit_items(
        _item_payload(source, "свиная шея без костей без кожи без хрящиков", comment),
        source,
    )

    item = restored["items"][0]
    assert "без костей без кожи без хрящиков" in item["product_query"]
    assert item["comment"] == ""


def test_structured_comments_are_preserved_for_common_product_constraints() -> None:
    """Сохраняет распознанные требования для разных товарных формулировок."""
    cases = (
        ("картофель 10 кг не червивый", "картофель", "не червивый"),
        ("лук зеленый 1 кг не усохший", "лук зеленый", "не усохший"),
        ("лук порей 2 кг срез корня 5 см", "лук порей", "срез корня 5 см"),
    )
    for source, product_query, comment in cases:
        item = recover_omitted_explicit_items(
            _item_payload(source, product_query, comment), source
        )["items"][0]
        assert item["comment"] == comment


def test_absent_comment_is_not_invented_by_postprocessing() -> None:
    """Не добавляет комментарий, если исходный результат ИИ его не содержит."""
    source = "свиная шея 5 кг"
    payload = _item_payload(source, "свиная шея")
    restored = recover_omitted_explicit_items(payload, source)

    assert restored["items"][0]["comment"] == ""


def test_clean_product_query_is_not_replaced_by_source_punctuation() -> None:
    """Не заменяет чистое название товара только из-за точки в источнике."""
    source = "\u041c\u043d\u0435 \u043d\u0443\u0436\u0435\u043d \u043b\u0443\u043a."
    payload = _item_payload(source, "\u043b\u0443\u043a")

    restored = recover_omitted_explicit_items(payload, source)

    assert restored["items"][0]["product_query"] == "\u043b\u0443\u043a"


def test_clean_product_query_is_not_replaced_by_conversational_wrapper() -> None:
    """Не восстанавливает разговорную вводную поверх названия товара."""
    source = "\u041c\u043d\u0435 \u043d\u0443\u0436\u0435\u043d \u043b\u0443\u043a, \u043f\u043e\u0436\u0430\u043b\u0443\u0439\u0441\u0442\u0430."
    payload = _item_payload(source, "\u043b\u0443\u043a")

    restored = recover_omitted_explicit_items(payload, source)

    assert restored["items"][0]["product_query"] == "\u043b\u0443\u043a"


def test_spoken_pair_quantity_does_not_replace_ai_product_query() -> None:
    """Не возвращает словесное количество в уже корректное название от ИИ."""
    source = "Мне нужно пару килограмм лука свежего."
    payload = _item_payload(source, "лука свежего")
    payload["items"][0]["quantity"] = 2
    payload["items"][0]["unit"] = "кг"

    restored = recover_omitted_explicit_items(payload, source)

    assert [
        (item["product_query"], item["quantity"], item["unit"]) for item in restored["items"]
    ] == [("лука свежего", 2, "кг")]


def test_standalone_spoken_pair_leadin_preserves_ai_product_query() -> None:
    """Не заменяет результат AI исходной фразой «нужно пара килограмма»."""
    source = "Нужно пара килограмма лука зеленого."
    payload = _item_payload(source, "лук зеленый")
    payload["items"][0]["quantity"] = 2
    payload["items"][0]["unit"] = "кг"

    restored = recover_omitted_explicit_items(payload, source)

    assert [
        (item["product_query"], item["quantity"], item["unit"]) for item in restored["items"]
    ] == [("лук зеленый", 2, "кг")]


def test_source_order_quantity_is_not_replaced_by_packaging_fallback() -> None:
    """Не заменяет количество заказа из исходной строки найденной фасовкой."""
    source = "Соус 500 мл, 12 штук в коробке — 2 шт"
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {
                "product_query": "Соус 500 мл",
                "quantity": 2,
                "unit": "шт",
                "packaging_text": "12 штук в коробке",
                "packaging_role": "catalog_attribute",
                "source_line": source,
            }
        ],
    }

    restored = recover_omitted_explicit_items(payload, source)

    item = restored["items"][0]
    assert item["product_query"] == "Соус 500 мл, 12 штук в коробке"
    assert (item["quantity"], item["unit"]) == (2.0, "шт")


def test_spoken_pair_quantity_does_not_replace_each_item_in_voice_list() -> None:
    """Не возвращает словесное количество в названия всех позиций голосового списка."""
    source = "Нужно пару килограмм филе форели и пару килограмм лука свежего, а также пару килограмм икры"
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {
                "product_query": "филе форели",
                "quantity": 2,
                "unit": "кг",
                "source_line": "пару килограмм филе форели",
            },
            {
                "product_query": "лук свежий",
                "quantity": 2,
                "unit": "кг",
                "source_line": "пару килограмм лука свежего",
            },
            {
                "product_query": "икра",
                "quantity": 2,
                "unit": "кг",
                "source_line": "пару килограмм икры",
            },
        ],
    }

    restored = recover_omitted_explicit_items(payload, source)

    assert [item["product_query"] for item in restored["items"]] == [
        "филе форели",
        "лук свежий",
        "икра",
    ]
    assert [(item["quantity"], item["unit"]) for item in restored["items"]] == [
        (2, "кг"),
        (2, "кг"),
        (2, "кг"),
    ]


def test_trailing_spoken_weight_pair_keeps_ai_items_clean_in_voice_list() -> None:
    """Сохраняет названия и вес для разговорного порядка «товар пару килограмм»."""
    source = "Форели пару килограмм, лук зелёный пару килограмм."
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {
                "product_query": "форель",
                "quantity": 2,
                "unit": "кг",
                "source_line": "Форели пару килограмм",
            },
            {
                "product_query": "лук зелёный",
                "quantity": 2,
                "unit": "кг",
                "source_line": "лук зелёный пару килограмм",
            },
        ],
    }

    restored = recover_omitted_explicit_items(payload, source)

    assert [
        (item["product_query"], item["quantity"], item["unit"]) for item in restored["items"]
    ] == [
        ("форель", 2.0, "кг"),
        ("лук зелёный", 2.0, "кг"),
    ]


def test_source_evidence_reconciliation_is_idempotent_without_title_comment() -> None:
    """Повторная сверка не создаёт комментарий из признака названия."""
    source = "свиная шея 5 кг без костей без кожи"
    payload = _item_payload(
        source,
        "свиная шея без костей без кожи",
        "без костей без кожи",
    )

    once = recover_omitted_explicit_items(payload, source)
    twice = recover_omitted_explicit_items(once, source)

    assert twice["items"] == once["items"]
    assert twice["items"][0]["quantity"] == 5
    assert twice["items"][0]["comment"] == ""


def test_source_evidence_idempotency_covers_title_comments_and_quantity_contracts() -> None:
    """Проверяет идемпотентность признака названия, выдуманного комментария и quantity."""
    valid_comment = _item_payload("курица охлаждённая 5 кг", "курица охлаждённая", "охлаждённая")
    invented_comment = _item_payload("курица 5 кг", "курица", "охлаждённая")
    spoken_quantity = _item_payload("пармезан пять штук", "пармезан")
    spoken_quantity["items"][0]["quantity"] = None
    spoken_quantity["items"][0]["unit"] = ""

    for payload, expected_comment in (
        (valid_comment, ""),
        (invented_comment, ""),
        (spoken_quantity, ""),
    ):
        once = recover_omitted_explicit_items(payload, payload["items"][0]["source_line"])
        twice = recover_omitted_explicit_items(once, once["items"][0]["source_line"])

        assert twice == once
        assert twice["items"][0]["comment"] == expected_comment

    assert spoken_quantity["items"][0]["quantity"] == 5
    assert spoken_quantity["items"][0]["unit"] == "шт"


def test_ai_item_and_comment_binding_are_not_reparsed() -> None:
    """Проверяет, что комментарий ИИ не превращается в отдельные товары."""
    source = (
        "Марципан в темном шоколаде 70 грамм, мне нужно 5 килограмм, желательно крупными брикетами."
    )
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {
                "product_query": "Марципан в темном шоколаде",
                "quantity": 5,
                "unit": "кг",
                "comment": "",
                "user_comment_to_supplier": "",
                "source_line": source,
            }
        ],
        "comment_bindings": [
            {
                "text": "желательно крупными брикетами",
                "scope": "item",
                "target_item_indexes": [0],
                "confidence": 0.98,
            }
        ],
    }

    restored = recover_omitted_explicit_items(payload, source)

    assert len(restored["items"]) == 1
    assert restored["items"][0]["product_query"] == "Марципан в темном шоколаде"
    assert restored["items"][0]["quantity"] == 5
    assert restored["items"][0]["comment"] == "желательно крупными брикетами"


def test_non_empty_ai_item_list_is_kept_without_deterministic_append() -> None:
    """Проверяет, что заполненный ответ ИИ не дополняется повторным парсингом."""
    source = "Сироп роза 10 штук, говядина 5 кг, всё на завтра"
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {
                "product_query": "Сироп роза",
                "quantity": 10,
                "unit": "шт",
                "comment": "",
                "source_line": source,
            },
            {
                "product_query": "говядина",
                "quantity": 5,
                "unit": "кг",
                "comment": "",
                "source_line": source,
            },
        ],
        "global_comment": "всё на завтра",
    }

    restored = recover_omitted_explicit_items(payload, source)

    assert [item["product_query"] for item in restored["items"]] == [
        "Сироп роза",
        "говядина",
    ]
    assert restored["global_comment"] == "на завтра"


def test_shadow_collapse_keeps_two_real_products_and_one_word_items() -> None:
    """Не удаляет однословный товар и две независимые позиции."""
    source = "курица 5 кг и сыр 2 кг"
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {"product_query": "курица", "quantity": 5, "unit": "кг", "source_line": source},
            {"product_query": "сыр", "quantity": 2, "unit": "кг", "source_line": source},
        ],
    }

    restored = recover_omitted_explicit_items(payload, source)

    assert [(item["product_query"], item["quantity"]) for item in restored["items"]] == [
        ("курица", 5.0),
        ("сыр", 2.0),
    ]


def test_shadow_collapse_keeps_intentional_duplicate_occurrences() -> None:
    """Не схлопывает два явно заказанных количества одного товара."""
    source = "курица 5 кг, курица 3 кг"
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {"product_query": "курица", "quantity": 5, "unit": "кг", "source_line": source},
            {"product_query": "курица", "quantity": 3, "unit": "кг", "source_line": source},
        ],
    }

    restored = recover_omitted_explicit_items(payload, source)

    assert [item["quantity"] for item in restored["items"]] == [5.0, 3.0]


def test_shadow_collapse_removes_contained_query_from_one_source_occurrence() -> None:
    """Удаляет только query-фрагмент той же товарной occurrence."""
    source = "сыр пармезан 1 кг"
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {
                "product_query": "сыр пармезан",
                "quantity": 1,
                "unit": "кг",
                "source_line": source,
            },
            {"product_query": "пармезан", "quantity": 1, "unit": "кг", "source_line": source},
        ],
    }

    restored = recover_omitted_explicit_items(payload, source)

    assert [item["product_query"] for item in restored["items"]] == ["сыр пармезан"]


def test_shadow_collapse_is_idempotent_for_comment_projection() -> None:
    """Повторный shadow pass не меняет уже очищенный список."""
    source = "сироп роза холодным 3 штуки"
    payload = {
        "intent": Intent.ADD_ITEMS,
        "items": [
            {
                "product_query": "сироп роза",
                "quantity": None,
                "unit": "",
                "comment": "холодным",
                "source_line": source,
            },
            {
                "product_query": "холодным",
                "quantity": 3,
                "unit": "шт",
                "comment": ".",
                "source_line": source,
            },
        ],
    }

    once = recover_omitted_explicit_items(payload, source)
    twice = recover_omitted_explicit_items(once, source)

    assert twice == once
    assert len(twice["items"]) == 1
