from restaurant_bot.domain.models import CartItem, ItemStatus
from restaurant_bot.presentation.telegram.replies import issue_reply


def test_missing_quantity_card_has_skip_action() -> None:
    """Проверяет, что отсутствующий количество карточка имеет пропуск действие."""
    reply = issue_reply(
        CartItem(
            id="rose",
            source_query="Сироп Роза",
            catalog_name="Сироп Роза",
            catalog_unit="шт",
            status=ItemStatus.MISSING_QTY,
        ),
        0,
    )

    assert "Укажите количество" in reply.text
    assert reply.rows[0][0].text == "Не добавлять"
    assert reply.rows[0][0].callback_data == "v2:skip:0"


def test_unit_mismatch_card_requests_quantity_in_catalog_unit() -> None:
    """Проверяет, что единица измерения mismatch карточка запросы количество в каталог единица измерения."""
    reply = issue_reply(
        CartItem(
            id="rose",
            source_query="Сироп Роза",
            catalog_name="Сироп Роза",
            quantity=10,
            unit="шт",
            catalog_unit="л",
            status=ItemStatus.UNIT_MISMATCH,
        ),
        0,
    )

    assert [row[0].callback_data for row in reply.rows] == [
        "v2:unitedit:0",
        "v2:skip:0",
    ]
    assert "Добавить 10 л" not in reply.text


def test_unit_mismatch_card_suggests_package_count_without_auto_applying() -> None:
    """Предлагает округлённое число упаковок отдельной кнопкой."""
    reply = issue_reply(
        CartItem(
            id="cheese",
            source_query="Сыр Швейцарский Сыробогатов 180гр",
            catalog_name="Сыр Швейцарский Сыробогатов 180гр",
            quantity=500,
            unit="г",
            catalog_unit="шт",
            status=ItemStatus.UNIT_MISMATCH,
        ),
        0,
    )

    assert reply.rows[0][0].text == "Заказать 3 шт (≈ 540 г)"
    assert reply.rows[0][0].callback_data == "v2:qty:cheese:3"
    assert reply.rows[1][0].text == "Ввести количество в шт"
    assert reply.rows[2][0].text == "Не добавлять"
