from restaurant_bot.parsing.quantities import parse_quantity_unit


def test_short_quantity_answers_support_digits_and_number_words() -> None:
    """Проверяет, что краткий количество ответы поддерживает цифры и число слова."""
    assert parse_quantity_unit("10 штук") == (10, "шт")
    assert parse_quantity_unit("пять кг") == (5, "кг")
    assert parse_quantity_unit("3") == (3, "")
    assert parse_quantity_unit("три.") == (3, "")
    assert parse_quantity_unit(".5 кг") == (0.5, "кг")


def test_short_quantity_answer_does_not_invent_unit() -> None:
    """Проверяет, что краткий количество ответ выполняет не выдумывает единица измерения."""
    assert parse_quantity_unit("десять") == (10, "")
    assert parse_quantity_unit("не знаю") == (None, "")
