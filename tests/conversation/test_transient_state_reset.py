"""Проверяет поведение, связанное с модулем «test transient state reset»."""

from copy import deepcopy

from restaurant_bot.conversation.state.transitions import clear_transient_dialog_state
from restaurant_bot.domain.models import ConversationState, SearchScope


def test_clear_transient_dialog_state_preserves_exact_reset_contract() -> None:
    """Transient reset очищает только зафиксированные поля старого контракта."""
    state = ConversationState(
        current_issue_item_id="item",
        current_issue_kind="quantity",
        search_scope=SearchScope.ANY_SUPPLIER,
        supplier_search_locked=True,
        supplier_hint_context="ферма",
        manual_item_index=1,
        unit_item_index=2,
        edit_multiple_index=3,
        pending_added_items_count=4,
        pending_comment_items=[],
        pending_comment_existing_item_ids=["item"],
        pending_comment_text="текст",
        pending_comment_global_comment="общий",
        pending_product_add_item_index=1,
        pending_product_add_request_id="request",
        product_add_write_in_progress=True,
        metadata={"keep": True},
    )
    before = deepcopy(state.model_dump())

    clear_transient_dialog_state(state)

    expected = before.copy()
    expected.update(
        {
            "current_issue_item_id": "",
            "current_issue_kind": None,
            "search_scope": SearchScope.SUPPLIER_ONLY,
            "supplier_search_locked": False,
            "supplier_hint_context": "",
            "manual_item_index": None,
            "unit_item_index": None,
            "edit_multiple_index": None,
            "pending_added_items_count": 0,
            "pending_comment_items": [],
            "pending_comment_existing_item_ids": [],
            "pending_comment_text": "",
            "pending_comment_global_comment": "",
            "pending_product_add_item_index": None,
            "pending_product_add_request_id": "",
            "product_add_write_in_progress": False,
        }
    )
    assert state.model_dump() == expected
