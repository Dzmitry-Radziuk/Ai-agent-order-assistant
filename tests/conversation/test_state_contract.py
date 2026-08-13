"""Проверяет поведение, связанное с модулем «test state contract»."""

from restaurant_bot.domain.models import CartItem, ConversationState, ItemStatus


def test_dialog_state_round_trip_preserves_draft_and_product_add_requests() -> None:
    """Проверяет, что dialog состояние округление trip сохраняет черновик и товар добавление запросы."""
    state = ConversationState(
        ui_revision=4,
        telegram_user_id="77",
        telegram_chat_id="77",
        venue_code="6461W6",
        venue_name="Качели",
        spreadsheet_id="venue-sheet-id",
        current_issue_item_id="rose",
        visible_actions=[
            {"label": "Показать черновик", "action_id": "v2:back:r4"},
        ],
        cart=[
            CartItem(
                id="rose",
                source_query="Сироп Роза",
                source_line="Сироп Роза 5 шт",
                catalog_product_id="rose-1",
                catalog_name="Сироп Роза",
                quantity=5,
                unit="шт",
                status=ItemStatus.MATCHED,
            )
        ],
        product_add_requests=[
            {"request_id": "add-1", "description": "Креветки", "status": "pending_write"}
        ],
    )

    restored = ConversationState.model_validate_json(state.model_dump_json())

    assert restored.ui_revision == 4
    assert restored.venue_code == "6461W6"
    assert restored.spreadsheet_id == "venue-sheet-id"
    assert restored.current_issue_item_id == "rose"
    assert restored.visible_actions == state.visible_actions
    assert restored.cart[0].catalog_product_id == "rose-1"
    assert restored.cart[0].source_line == "Сироп Роза 5 шт"
    assert restored.product_add_requests == state.product_add_requests
