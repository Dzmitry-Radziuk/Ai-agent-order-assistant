"""Проверяет, что AI не создаёт несколько позиций из одного фрагмента речи."""

from restaurant_bot.domain.models import Intent
from restaurant_bot.parsing.ai.reconciliation import recover_omitted_explicit_items


def test_reconciliation_drops_nested_ai_projection_from_single_source_occurrence() -> None:
    """Не добавляет отдельный товар из фасовки той же голосовой позиции."""
    source = "семена мака 1 килограмм 3 штуки"
    restored = recover_omitted_explicit_items(
        {
            "intent": Intent.ADD_ITEMS,
            "items": [
                {
                    "product_query": "семена мака 1 килограмм",
                    "quantity": 3,
                    "unit": "шт",
                    "source_line": source,
                },
                {
                    "product_query": "семена мака",
                    "quantity": 1,
                    "unit": "кг",
                    "source_line": "семена мака 1 килограмм",
                },
            ],
        },
        source,
    )

    assert [
        (item["product_query"], item["quantity"], item["unit"]) for item in restored["items"]
    ] == [("семена мака 1 килограмм", 3.0, "шт")]


def test_reconciliation_keeps_repeated_product_when_second_occurrence_is_independent() -> None:
    """Не склеивает два отдельно произнесённых одинаковых товара."""
    source = "семена мака 1 килограмм 3 штуки и семена мака 1 килограмм"
    restored = recover_omitted_explicit_items(
        {
            "intent": Intent.ADD_ITEMS,
            "items": [
                {
                    "product_query": "семена мака 1 килограмм",
                    "quantity": 3,
                    "unit": "шт",
                    "source_line": "семена мака 1 килограмм 3 штуки",
                },
                {
                    "product_query": "семена мака",
                    "quantity": 1,
                    "unit": "кг",
                    "source_line": "семена мака 1 килограмм",
                },
            ],
        },
        source,
    )

    assert len(restored["items"]) == 2


def test_reconciliation_strips_additive_leadin_from_following_product() -> None:
    """Не отправляет в каталог разговорную связку перед следующим товаром."""
    source = "И также хлеб."
    restored = recover_omitted_explicit_items(
        {
            "intent": Intent.ADD_ITEMS,
            "items": [
                {
                    "product_query": "хлеб",
                    "source_line": source,
                }
            ],
        },
        source,
    )

    assert restored["items"][0]["product_query"] == "хлеб"


def test_reconciliation_deduplicates_reordered_comment_binding() -> None:
    """Не повторяет один комментарий, если AI поменял порядок одинаковых слов."""
    source = "молоко кокосовое 5 штук в пакетах желательно"
    restored = recover_omitted_explicit_items(
        {
            "intent": Intent.ADD_ITEMS,
            "items": [
                {
                    "product_query": "молоко кокосовое",
                    "quantity": 5,
                    "unit": "шт",
                    "comment": "желательно в пакетах",
                    "source_line": source,
                }
            ],
            "comment_bindings": [
                {
                    "text": "в пакетах желательно",
                    "scope": "item",
                    "target_item_indexes": [0],
                    "confidence": 0.99,
                }
            ],
        },
        source,
    )

    assert restored["items"][0]["comment"] == "желательно в пакетах"
