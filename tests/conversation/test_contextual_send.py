"""Проверяет поведение, связанное с модулем «test contextual send»."""

from restaurant_bot.parsing.commands.item_commands import is_product_add_request_phrase


def test_procurement_request_phrases_are_recognized_in_any_word_order() -> None:
    """Проверяет, что procurement запрос phrases являются распознаётся в any слово заказ."""
    for phrase in [
        "отправить",
        "отправить снабженцу",
        "отправить запрос снабженцу",
        "запрос снабженцу",
        "передать товар менеджеру",
    ]:
        assert is_product_add_request_phrase(phrase), phrase


def test_ordinary_product_phrase_is_not_a_procurement_request() -> None:
    """Проверяет, что обычная фраза о товаре не является закупочным запросом."""
    assert not is_product_add_request_phrase("креветки королевские 10 кг")
