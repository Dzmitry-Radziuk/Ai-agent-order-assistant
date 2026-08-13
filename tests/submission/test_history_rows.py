"""Проверяет поведение, связанное с модулем «test history rows»."""

from restaurant_bot.domain.models import (
    CatalogProduct,
    ConversationState,
    DepartmentQuantities,
    ExtractedItem,
    InputKind,
    Intent,
    ParsedCommand,
    TelegramEvent,
)
from restaurant_bot.services.engine import ConversationEngine


def test_submission_row_uses_add_sheet_contract_headers(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что отправка заявки строка использует добавление таблица контракт заголовки."""
    engine = ConversationEngine(settings)
    event = TelegramEvent(update_id=1, chat_id="11112222", input_type=InputKind.TEXT)
    catalog = [
        CatalogProduct(
            product_id="beef-1",
            name="Говядина Тонкий край",
            supplier="Мясной поставщик",
            unit="кг",
            price=1200,
            minimum_multiple=2,
            useful_volume=1,
            supplier_minimum_amount=5000,
            comment="охлаждённая",
        )
    ]
    added = engine.handle(
        event,
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[ExtractedItem(product_query="Говядина Тонкий край", quantity=4, unit="кг")],
        ),
        ConversationState(
            restaurant="Кафе",
            role="Повар",
            venue_code="6461W6",
            spreadsheet_id="venue-sheet-id",
        ),
        catalog,
    )

    result = engine._prepare_submission(event, added.state)
    row = result.state.pending_submission.rows[0]  # type: ignore[union-attr]

    assert row["ID товара"] == "beef-1"
    assert row["Наименование у поставщика"] == "Говядина Тонкий край"
    assert row["Ед.Изм. для заказа"] == "кг"
    assert row["Кол-во"] == 4
    assert row["Цена за Ед.Изм. для заказа"] == 1200
    assert row["Сумма по товару в заказе"] == 4800
    assert row["Сумма по заявке к поставщику"] == 4800
    assert row["Стадия"] == "Новая заявка"
    assert row["_department"] == "Кухня"
    assert result.state.pending_submission is not None
    assert result.state.pending_submission.venue_code == "6461W6"
    assert result.state.pending_submission.spreadsheet_id == "venue-sheet-id"


def test_submission_splits_photo_quantities_into_department_rows(settings) -> None:  # type: ignore[no-untyped-def]
    """Разносит количества с фото по отдельным колонкам отделов заявки."""
    engine = ConversationEngine(settings)
    event = TelegramEvent(update_id=2, chat_id="11112222", input_type=InputKind.TEXT)
    catalog = [
        CatalogProduct(
            product_id="rose-1",
            name="Сироп Роза",
            supplier="Мясной поставщик",
            unit="шт",
            price=100,
        )
    ]
    added = engine.handle(
        event,
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(
                    product_query="Сироп Роза",
                    quantity=6,
                    unit="шт",
                    department_quantities=DepartmentQuantities(hall=2, bar=1, kitchen=3),
                )
            ],
        ),
        ConversationState(
            restaurant="Кафе",
            role="Повар",
            venue_code="6461W6",
            spreadsheet_id="venue-sheet-id",
        ),
        catalog,
    )

    result = engine._prepare_submission(event, added.state)
    rows = result.state.pending_submission.rows  # type: ignore[union-attr]

    assert [(row["Роль"], row["Кол-во"], row["_department"]) for row in rows] == [
        ("Зал", 2, "Зал"),
        ("Бар", 1, "Бар"),
        ("Кухня", 3, "Кухня"),
    ]
    assert [row["Сумма по товару в заказе"] for row in rows] == [200, 100, 300]
