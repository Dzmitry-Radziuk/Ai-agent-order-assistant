from restaurant_bot.domain.models import CartItem, ConversationState, DepartmentQuantities


def test_conversation_state_uses_kitchen_as_default_department() -> None:
    assert ConversationState().department == "Кухня"


def test_department_defaults_and_quantity_lookup_use_normal_russian_names() -> None:
    quantities = DepartmentQuantities(hall=1, bar=2, kitchen=3)

    assert CartItem(id="item", source_query="Товар").department == "Кухня"
    assert quantities.for_department("Зал") == 1
    assert quantities.for_department("Бар") == 2
    assert quantities.for_department("Кухня") == 3
