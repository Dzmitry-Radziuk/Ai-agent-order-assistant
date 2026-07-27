from restaurant_bot.domain.models import CartItem, ConversationState, ItemStatus
from restaurant_bot.services.replies import supplier_warning_details_reply


def test_supplier_minimum_warning_shows_gap_and_recovery_actions() -> None:
    """Проверяет, что поставщик минимум предупреждение показывает gap и восстановление действия."""
    item = CartItem(
        id="rose",
        source_query="Сироп Роза",
        catalog_name="Сироп Роза",
        supplier="Сиропы",
        quantity=5,
        price=100,
        supplier_minimum_amount=1000,
        status=ItemStatus.MATCHED,
    )

    reply = supplier_warning_details_reply(ConversationState(cart=[item]))

    assert "Минимальная сумма не набрана" in reply.text
    assert "Сиропы" in reply.text
    callbacks = [button.callback_data for row in reply.rows for button in row]
    assert callbacks[0] == "v2:minsumadd:0"
    assert callbacks[-1] == "v2:cart"
