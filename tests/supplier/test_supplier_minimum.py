from restaurant_bot.domain.models import (
    CartItem,
    CatalogProduct,
    ConversationState,
    DepartmentQuantities,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    TelegramEvent,
)
from restaurant_bot.services.engine import ConversationEngine
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


def test_supplier_minimum_uses_existing_order_total_plus_current_draft() -> None:
    """Не предупреждает, если таблица и новый черновик вместе набрали минимум."""
    item = CartItem(
        id="beef-bones",
        source_query="Говядина Кости",
        catalog_name="Говядина Кости ПРОДОЛЬНЫЙ распил",
        supplier="Раджабов",
        quantity=1,
        price=350,
        supplier_minimum_amount=2000,
        supplier_current_sum=2550,
        existing_quantity=1,
        status=ItemStatus.MATCHED,
    )

    reply = supplier_warning_details_reply(ConversationState(cart=[item]))

    assert "Минимальная сумма набрана" in reply.text
    assert "не набрана" not in reply.text


def test_supplier_minimum_gap_is_based_on_combined_order_total() -> None:
    """Показывает остаток от суммы в таблице вместе с текущим черновиком."""
    item = CartItem(
        id="beef-bones",
        source_query="Говядина Кости",
        catalog_name="Говядина Кости ПРОДОЛЬНЫЙ распил",
        supplier="Раджабов",
        quantity=1,
        price=350,
        supplier_minimum_amount=2000,
        supplier_current_sum=1200,
        status=ItemStatus.MATCHED,
    )

    reply = supplier_warning_details_reply(ConversationState(cart=[item]))

    assert "1550 ₽ из 2000 ₽" in reply.text
    assert "Не хватает: 450 ₽" in reply.text


def test_final_review_refreshes_sum_and_quantity_from_exact_product_row(settings) -> None:  # type: ignore[no-untyped-def]
    """Обновляет динамические значения только из строки выбранного товара."""
    item = CartItem(
        id="beef-bones",
        source_query="Говядина Кости",
        catalog_product_id="beef-bones",
        catalog_name="Говядина Кости ПРОДОЛЬНЫЙ распил",
        catalog_unit="кг",
        unit="кг",
        department="Кухня",
        supplier="Раджабов",
        quantity=1,
        price=350,
        minimum_multiple=8,
        supplier_minimum_amount=2000,
        supplier_current_sum=0,
        existing_quantity=0,
        status=ItemStatus.MATCHED,
    )
    catalog = [
        CatalogProduct(
            product_id="other-beef",
            name="Говядина Тонкий край",
            supplier="Раджабов",
            supplier_current_sum=5000,
            department_quantities=DepartmentQuantities(kitchen=99),
        ),
        CatalogProduct(
            product_id="beef-bones",
            name="Говядина Кости ПРОДОЛЬНЫЙ распил",
            supplier="Раджабов",
            supplier_current_sum=1200,
            supplier_minimum_amount=2000,
            minimum_multiple=8,
            department_quantities=DepartmentQuantities(kitchen=7),
        ),
    ]

    result = ConversationEngine(settings).handle(
        TelegramEvent(update_id=1, chat_id="7", input_type=InputKind.TEXT, text="отправить"),
        ParsedCommand(intent=Intent.SUBMIT_REQUEST, text="отправить"),
        ConversationState(cart=[item]),
        catalog,
    )

    refreshed = result.state.cart[0]
    assert refreshed.supplier_current_sum == 1200
    assert refreshed.existing_quantity == 7
    assert refreshed.suggested_quantity is None
    assert "Раджабов: 1550 ₽ из 2000 ₽" in result.reply.text
