"""Проверяет поведение, связанное с модулем «test global voice navigation»."""

import pytest

from restaurant_bot.domain.models import (
    CartItem,
    ConversationState,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    SessionStage,
    TelegramEvent,
)
from restaurant_bot.services.engine import ConversationEngine


@pytest.mark.parametrize("stage", list(SessionStage))
def test_global_voice_show_cart_works_from_every_stage(settings, stage) -> None:  # type: ignore[no-untyped-def]
    """Открывает черновик голосом независимо от текущего этапа."""
    state = ConversationState(
        stage=stage,
        cart=[
            CartItem(
                id="syrup",
                source_query="Сироп роза",
                catalog_name="Сироп Роза, 1л",
                quantity=1,
                unit="шт",
                status=ItemStatus.MATCHED,
            )
        ],
    )
    command = ParsedCommand(intent=Intent.SHOW_CART, text="Покажи черновик")

    result = ConversationEngine(settings).handle(
        TelegramEvent(
            update_id=1,
            chat_id="1",
            input_type=InputKind.VOICE,
            text="Покажи черновик",
        ),
        command,
        state,
        [],
    )

    assert "Черновик заявки" in result.reply.text
    assert "Сироп Роза, 1л" in result.reply.text


def test_generic_show_products_opens_supplier_details_on_final_review(settings) -> None:  # type: ignore[no-untyped-def]
    """Повторяет голосом кнопку показа товаров поставщика."""
    state = ConversationState(
        stage=SessionStage.AWAIT_SUBMIT_CONFIRM,
        cart=[
            CartItem(
                id="syrup",
                source_query="Сироп роза",
                catalog_name="Сироп Роза, 1л",
                supplier="МБР",
                quantity=1,
                unit="шт",
                price=359,
                supplier_minimum_amount=1500,
                status=ItemStatus.MATCHED,
            )
        ],
    )
    command = ParsedCommand(intent=Intent.SHOW_CART, text="Давай посмотрим товары")

    result = ConversationEngine(settings).handle(
        TelegramEvent(
            update_id=2,
            chat_id="1",
            input_type=InputKind.VOICE,
            text="Давай посмотрим товары",
        ),
        command,
        state,
        [],
    )

    assert "Минимальная сумма не набрана" in result.reply.text
    assert "Поставщик: МБР" in result.reply.text
    assert "Сироп Роза, 1л" in result.reply.text


@pytest.mark.parametrize("stage", list(SessionStage))
def test_procurement_request_list_voice_command_works_from_every_stage(settings, stage) -> None:  # type: ignore[no-untyped-def]
    """Открывает запросы снабженцу голосом с любого экрана."""
    state = ConversationState(
        stage=stage,
        product_add_requests=[
            {"request_id": "request-1", "description": "Редкий соус", "status": "submitted"}
        ],
    )
    phrase = "Давай посмотрим запросы снабженцу"

    result = ConversationEngine(settings).handle(
        TelegramEvent(
            update_id=3,
            chat_id="1",
            input_type=InputKind.VOICE,
            text=phrase,
        ),
        ParsedCommand(intent=Intent.PRODUCT_ADD_LIST, text=phrase),
        state,
        [],
    )

    assert "Запросы снабженцу" in result.reply.text
    assert "Редкий соус" in result.reply.text
