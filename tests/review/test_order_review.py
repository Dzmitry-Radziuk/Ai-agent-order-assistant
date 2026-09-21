"""Проверяет поведение, связанное с модулем «test order review»."""

import re
from unittest.mock import MagicMock

from restaurant_bot.application.order_review.token import new_review_token
from restaurant_bot.domain.models import CatalogProduct, DepartmentQuantities
from restaurant_bot.presentation.telegram.order_review import preview_reply
from restaurant_bot.services.order_review import OrderReviewService
from restaurant_bot.services.venue_registration import VenueContext


def _context() -> VenueContext:
    """Создаёт контекст тестового заведения для проверки заявки."""
    return VenueContext(
        venue_code="6461W6",
        venue_name="Качели",
        spreadsheet_id="spreadsheet-id",
        spreadsheet_url="https://docs.google.com/spreadsheets/d/spreadsheet-id/edit",
        telegram_user_id="user-1",
        telegram_chat_id="chat-1",
    )


def _service(settings) -> OrderReviewService:  # type: ignore[no-untyped-def]
    """Создаёт сервис просмотра с изолированными внешними зависимостями."""
    sheets = MagicMock()
    sheets.load_catalog.return_value = [
        CatalogProduct(
            product_id="p1",
            name="Сироп роза, 1 л",
            supplier="МБР",
            unit="шт",
            department_quantities=DepartmentQuantities(kitchen=5),
            comment="на завтра",
        ),
        CatalogProduct(
            product_id="p2",
            name="Сливки 33%",
            supplier="Метро",
            unit="л",
            department_quantities=DepartmentQuantities(hall=2, bar=1),
        ),
        CatalogProduct(
            product_id="p3",
            name="Пустая позиция",
            supplier="МБР",
            unit="шт",
            department_quantities=DepartmentQuantities(),
        ),
    ]
    return OrderReviewService(settings, MagicMock(), MagicMock(), sheets)


def test_snapshot_reads_every_department_quantity_and_ignores_empty_rows(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что карточка собирает все ненулевые количества из листа заявки."""
    service = _service(settings)

    snapshot = service.snapshot(_context())

    assert [(item.name, item.quantity) for item in snapshot.items] == [
        ("Сироп роза, 1 л", 5),
        ("Сливки 33%", 3),
    ]
    assert snapshot.supplier_count == 2
    assert snapshot.items[0].department_quantities == DepartmentQuantities(kitchen=5)
    assert snapshot.items[1].department_quantities == DepartmentQuantities(hall=2, bar=1)
    assert (
        snapshot.fingerprint == "4c73ebb1863e422389021cf8b21b2d6ee5e004e85fd75c78572e45312c3d104d"
    )


def test_review_token_keeps_the_short_hex_contract() -> None:
    """Проверяет формат одноразового токена карточки проверки."""
    assert re.fullmatch(r"[0-9a-f]{20}", new_review_token())


def test_preview_lists_each_product_and_has_confirmation_buttons(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет понятную карточку с товарами и единственным подтверждением заявки."""
    service = _service(settings)
    reply = preview_reply(service.snapshot(_context()), "token")

    assert "Товаров: 2" in reply.text
    assert "• <b>Сироп роза, 1 л</b> — Кухня: 5 шт" in reply.text
    assert "• <b>Сливки 33%</b> — Зал: 2 л; Бар: 1 л" in reply.text
    assert [button.text for row in reply.rows for button in row] == [
        "Отправить заявку",
        "Отмена",
    ]
    assert reply.rows[0][0].callback_data == "v2:review_submit:token"


def test_preview_groups_products_by_supplier(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что карточка разделяет позиции по поставщикам и сохраняет комментарии."""
    service = _service(settings)

    reply = preview_reply(service.snapshot(_context()), "token")

    assert reply.text.index("МБР") < reply.text.index("Сироп роза")
    assert reply.text.index("Метро") < reply.text.index("Сливки 33%")
    assert reply.text.count("МБР") == 1
    assert "Комментарий: на завтра" in reply.text
