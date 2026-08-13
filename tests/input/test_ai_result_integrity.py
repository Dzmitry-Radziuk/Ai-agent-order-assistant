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


def test_product_facts_are_intentionally_preserved_in_query_and_comment() -> None:
    """Сохраняет дублирующиеся требования товара в обоих полях позиции."""
    source = "свиная шея 5 кг без костей без кожи без хрящиков"
    comment = "без костей без кожи без хрящиков"

    restored = recover_omitted_explicit_items(
        _item_payload(source, "свиная шея без костей без кожи без хрящиков", comment),
        source,
    )

    item = restored["items"][0]
    assert "без костей без кожи без хрящиков" in item["product_query"]
    assert item["comment"] == comment


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


def test_source_evidence_reconciliation_is_idempotent() -> None:
    """Повторная сверка не дублирует комментарий или количество."""
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
    assert twice["items"][0]["comment"] == "без костей без кожи"


def test_source_evidence_idempotency_covers_comment_and_quantity_contracts() -> None:
    """Проверяет идемпотентность валидного, выдуманного комментария и quantity."""
    valid_comment = _item_payload("курица охлаждённая 5 кг", "курица охлаждённая", "охлаждённая")
    invented_comment = _item_payload("курица 5 кг", "курица", "охлаждённая")
    spoken_quantity = _item_payload("пармезан пять штук", "пармезан")
    spoken_quantity["items"][0]["quantity"] = None
    spoken_quantity["items"][0]["unit"] = ""

    for payload, expected_comment in (
        (valid_comment, "охлаждённая"),
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
