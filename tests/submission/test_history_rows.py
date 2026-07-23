from restaurant_bot.domain.models import (
    CatalogProduct,
    ConversationState,
    ExtractedItem,
    InputKind,
    Intent,
    ParsedCommand,
    TelegramEvent,
)
from restaurant_bot.services.engine import ConversationEngine


def test_submission_row_uses_add_sheet_contract_headers(settings) -> None:  # type: ignore[no-untyped-def]
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
