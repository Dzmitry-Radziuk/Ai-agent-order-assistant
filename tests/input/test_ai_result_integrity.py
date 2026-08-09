from restaurant_bot.domain.models import Intent
from restaurant_bot.integrations.openai_parsing import recover_omitted_explicit_items


def _item_payload(source: str, product_query: str, comment: str = "") -> dict:
    """Создаёт raw AI payload для проверки сохранения товара и комментария."""
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
    """Не добавляет комментарий, если raw AI output его не содержит."""
    source = "свиная шея 5 кг"
    payload = _item_payload(source, "свиная шея")
    restored = recover_omitted_explicit_items(
        payload, source
    )

    assert restored["items"][0]["comment"] == ""


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
