from restaurant_bot.domain.models import (
    CartItem,
    ConversationState,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    TelegramEvent,
)
from restaurant_bot.services.engine import ConversationEngine


def test_switch_supplier_skips_failed_item_and_shows_supplier_choices(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что смена поставщик пропускает failed позиция и показывает поставщик choices."""
    engine = ConversationEngine(settings)
    state = ConversationState(
        supplier_hint_context="Поставщик А",
        cart=[
            CartItem(
                id="rose",
                source_query="Сироп Роза",
                status=ItemStatus.NOT_FOUND,
                supplier_hint="Поставщик А",
            )
        ],
    )
    event = TelegramEvent(update_id=1, chat_id="123456", input_type=InputKind.CALLBACK)

    result = engine.handle(
        event, ParsedCommand(intent=Intent.SWITCH_SUPPLIER, callback_target="0"), state, []
    )

    assert result.state.cart[0].status is ItemStatus.SKIPPED
    assert result.state.supplier_hint_context == ""
    assert result.enqueue_submission is False
