from restaurant_bot.domain.models import (
    CatalogProduct,
    ConversationState,
    ExtractedItem,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    TelegramEvent,
)
from restaurant_bot.services.engine import ConversationEngine
from restaurant_bot.services.parser import parse_product_lines


def _event() -> TelegramEvent:
    return TelegramEvent(update_id=1, chat_id="123456", input_type=InputKind.TEXT)


def test_catalog_and_user_comments_are_joined_once_in_source_order(settings) -> None:  # type: ignore[no-untyped-def]
    engine = ConversationEngine(settings)

    assert (
        engine._merge_comments("доставка утром", "охлаждённым", "Доставка утром.")
        == "доставка утром; охлаждённым"
    )


def test_comment_is_not_part_of_product_name_and_reaches_submission_row(settings) -> None:  # type: ignore[no-untyped-def]
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(
            product_id="rose",
            name="Сироп Роза",
            supplier="Сиропы",
            unit="шт",
            price=100,
            comment="доставка утром",
        )
    ]
    added = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(
                    product_query="Сироп Роза", quantity=5, unit="шт", comment="охлаждённым"
                )
            ],
        ),
        ConversationState(restaurant="Кафе"),
        catalog,
    )

    assert added.state.cart[0].catalog_name == "Сироп Роза"
    assert added.state.cart[0].comment == "доставка утром; охлаждённым"
    pending = engine._prepare_submission(_event(), added.state)
    assert pending.state.pending_submission is not None
    assert pending.state.pending_submission.rows[0]["Комментарий"] == "доставка утром; охлаждённым"


def test_global_comment_is_appended_to_every_item_comment_and_order_row(settings) -> None:  # type: ignore[no-untyped-def]
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(
            product_id="rose", name="Сироп Роза", supplier="Сиропы", unit="шт", price=100
        ),
        CatalogProduct(product_id="beef", name="Говядина", supplier="Мясо", unit="кг", price=200),
    ]
    result = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            global_comment="на завтра",
            items=[
                ExtractedItem(
                    product_query="Сироп Роза", quantity=10, unit="шт", comment="охлаждённым"
                ),
                ExtractedItem(
                    product_query="Говядина", quantity=10, unit="кг", comment="мраморная"
                ),
            ],
        ),
        ConversationState(restaurant="Кафе"),
        catalog,
    )

    assert [item.comment for item in result.state.cart] == [
        "охлаждённым; на завтра",
        "мраморная; на завтра",
    ]
    pending = engine._prepare_submission(_event(), result.state)
    assert pending.state.pending_submission is not None
    assert [row["Комментарий"] for row in pending.state.pending_submission.rows] == [
        "охлаждённым; на завтра",
        "мраморная; на завтра",
    ]


def test_parser_keeps_trailing_comment_after_quantity_out_of_product_name() -> None:
    items = parse_product_lines("сироп роза 10 штук охлаждённым")

    assert len(items) == 1
    assert items[0].product_query == "сироп роза"
    assert items[0].quantity == 10
    assert items[0].unit == "шт"
    assert items[0].comment == "охлаждённым"


def test_parser_keeps_multiword_delivery_preferences_as_a_single_comment() -> None:
    cases = [
        ("сироп роза 5 штук желательно охлаждённым", "желательно охлаждённым"),
        (
            "сироп роза 5 штук по возможности привезти до 10 утра",
            "по возможности привезти до 10 утра",
        ),
        (
            "сироп роза 5 штук обязательно позвонить перед доставкой",
            "обязательно позвонить перед доставкой",
        ),
        ("сироп роза 5 штук если можно без замены", "если можно без замены"),
        ("сироп роза 5 штук просьба выбрать самый свежий", "просьба выбрать самый свежий"),
    ]

    for text, expected_comment in cases:
        items = parse_product_lines(text)
        assert len(items) == 1, text
        assert items[0].product_query == "сироп роза", text
        assert items[0].quantity == 5, text
        assert items[0].unit == "шт", text
        assert items[0].comment == expected_comment, text
        assert items[0].user_comment_to_supplier == expected_comment, text


def test_comment_before_quantity_is_recovered_after_catalog_match(settings) -> None:  # type: ignore[no-untyped-def]
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(
            product_id="rose",
            name="Сироп Роза, 1л",
            supplier="МБР",
            unit="шт",
            price=359,
        )
    ]
    parsed_items = parse_product_lines("сироп роза холодным 10 штук")

    # Reproduce the actual failure: the basic parser cannot know where the
    # catalog name ends, so it initially leaves the comment in product_query.
    assert parsed_items[0].product_query == "сироп роза холодным"
    assert parsed_items[0].comment == ""

    result = engine.handle(
        _event(),
        ParsedCommand(intent=Intent.ADD_ITEMS, items=parsed_items),
        ConversationState(restaurant="Тестовый ресторан"),
        catalog,
    )

    assert result.state.cart[0].catalog_name == "Сироп Роза, 1л"
    assert result.state.cart[0].comment == "холодным"
    pending = engine._prepare_submission(_event(), result.state)
    assert pending.state.pending_submission is not None
    assert pending.state.pending_submission.rows[0]["Комментарий"] == "холодным"


def test_arbitrary_item_comment_survives_ambiguous_choice_and_submission(settings) -> None:  # type: ignore[no-untyped-def]
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(
            product_id="thin",
            name="Говядина Тонкий край",
            supplier="Мясо",
            unit="кг",
            price=500,
        ),
        CatalogProduct(
            product_id="bones",
            name="Говядина Кости ПРОДОЛЬНЫЙ распил",
            supplier="Мясо",
            unit="кг",
            price=100,
        ),
    ]
    added = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(
                    product_query="говядина мраморная без кожи",
                    quantity=10,
                    unit="кг",
                )
            ],
        ),
        ConversationState(restaurant="Кафе"),
        catalog,
    )

    item = added.state.cart[0]
    assert item.status is ItemStatus.AMBIGUOUS
    assert item.source_query == "говядина"
    assert item.comment == "мраморная без кожи"

    selected = engine.handle(
        _event(),
        ParsedCommand(intent=Intent.SELECT_CANDIDATE, selected_index=1, callback_target="0"),
        added.state,
        catalog,
    )

    assert selected.state.cart[0].status is ItemStatus.MATCHED
    assert selected.state.cart[0].comment == "мраморная без кожи"
    pending = engine._prepare_submission(_event(), selected.state)
    assert pending.state.pending_submission is not None
    assert pending.state.pending_submission.rows[0]["Комментарий"] == "мраморная без кожи"


def test_existing_comment_shadow_is_removed_from_persisted_draft(settings) -> None:  # type: ignore[no-untyped-def]
    engine = ConversationEngine(settings)
    owner = engine._build_item(
        ExtractedItem(product_query="сироп роза", quantity=3, unit="шт", comment="холодным")
    )
    owner.catalog_product_id = "rose"
    owner.catalog_name = "Сироп Роза, 1л"
    shadow = engine._build_item(ExtractedItem(product_query="холодным"))
    state = ConversationState(
        cart=[owner, shadow],
        current_issue_item_id=shadow.id,
    )

    engine._remove_cart_comment_shadows(state)

    assert [item.id for item in state.cart] == [owner.id]
    assert state.current_issue_item_id == ""
