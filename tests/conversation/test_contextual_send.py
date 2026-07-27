from restaurant_bot.services.parser import is_product_add_request_phrase


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
    """Проверяет, что ordinary товар phrase является не a procurement запрос."""
    assert not is_product_add_request_phrase("креветки королевские 10 кг")
