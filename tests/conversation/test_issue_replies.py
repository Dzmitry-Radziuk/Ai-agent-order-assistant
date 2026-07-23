from restaurant_bot.domain.models import CartItem, ItemStatus
from restaurant_bot.services.replies import issue_reply


def test_missing_quantity_card_has_skip_action() -> None:
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


def test_unit_mismatch_card_offers_catalog_unit_or_manual_quantity() -> None:
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
        "v2:unitok:0",
        "v2:unitedit:0",
        "v2:skip:0",
    ]
