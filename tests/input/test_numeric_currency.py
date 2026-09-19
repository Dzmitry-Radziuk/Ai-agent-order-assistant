"""Проверяет разбор валютных значений из Google Sheets."""

from restaurant_bot.parsing.numeric import to_float


def test_currency_thousands_separator_is_not_read_as_decimal() -> None:
    """Проверяет, что формат «р.5 000» означает пять тысяч рублей."""
    assert to_float("р.5 000") == 5000
    assert to_float("р.5\u00a0000") == 5000


def test_currency_decimal_and_grouped_values_keep_existing_contract() -> None:
    """Проверяет десятичные и составные денежные значения."""
    assert to_float("р.67,4") == 67.4
    assert to_float("р.1.200,50") == 1200.5
