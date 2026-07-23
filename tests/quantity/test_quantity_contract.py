from restaurant_bot.services.parser import parse_quantity_unit


def test_short_quantity_answers_support_digits_and_number_words() -> None:
    assert parse_quantity_unit("10 штук") == (10, "шт")
    assert parse_quantity_unit("пять кг") == (5, "кг")
    assert parse_quantity_unit("3") == (3, "")


def test_short_quantity_answer_does_not_invent_unit() -> None:
    assert parse_quantity_unit("десять") == (10, "")
    assert parse_quantity_unit("не знаю") == (None, "")
