"""Проверяет загрузку денежных ограничений каталога."""

from unittest.mock import MagicMock

from restaurant_bot.integrations.google_sheets import GoogleSheetsGateway


def test_catalog_minimum_amount_keeps_thousands_separator(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что формат Google Sheets «р.5 000» становится 5000."""
    gateway = GoogleSheetsGateway(settings)
    gateway.read_rows = MagicMock(  # type: ignore[method-assign]
        return_value=[
            {
                "ID товара": "mustard",
                "Наименование у Поставщика": "Горчица острая",
                "Основной поставщик (Условное наз-ие)": "Тестовый поставщик",
                "Мин сумма для заказа поставщику": "р.5 000",
                "Цена за Ед,Изм, для заказа": "р.67,4",
                "__row_number": 7,
            }
        ]
    )

    product = gateway.load_catalog("venue-sheet")[0]

    assert product.supplier == "Тестовый поставщик"
    assert product.price == 67.4
    assert product.supplier_minimum_amount == 5000
