import pytest

from restaurant_bot.domain.models import (
    CartItem,
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
    """Создаёт тестовое событие Telegram."""
    return TelegramEvent(update_id=1, chat_id="123456", input_type=InputKind.TEXT)


def test_catalog_and_user_comments_are_joined_once_in_source_order(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что каталог и пользователь комментарии являются слитное один раз в исходный заказ."""
    engine = ConversationEngine(settings)

    assert (
        engine._merge_comments("доставка утром", "охлаждённым", "Доставка утром.")
        == "доставка утром; охлаждённым"
    )


def test_comment_is_not_part_of_product_name_and_reaches_submission_row(settings) -> None:  # type: ignore[no-untyped-def]
    """Передаёт поставщику пожелание пользователя без примечания каталога."""
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
    assert added.state.cart[0].catalog_comment == "доставка утром"
    assert added.state.cart[0].catalog_comment_source == "catalog"
    assert added.state.cart[0].comment == "охлаждённым"
    pending = engine._prepare_submission(_event(), added.state)
    assert pending.state.pending_submission is not None
    assert pending.state.pending_submission.rows[0]["Комментарий"] == "охлаждённым"


def test_global_comment_is_appended_to_every_item_comment_and_order_row(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что общий комментарий является appended в каждый позиция комментарий и заказ строка."""
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


def test_global_comment_is_separate_from_catalog_reference(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    """Хранит примечание каталога отдельно от комментария новой заявки."""
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(
            product_id="rose",
            name="Сироп Роза, 1л",
            supplier="МБР",
            unit="шт",
            comment="в банках; на завтра; на завтра или послезавтра",
        ),
        CatalogProduct(
            product_id="tarhun",
            name="Сироп Тархун, 1л",
            supplier="МБР",
            unit="шт",
            comment="в ведрах; на завтра; в вагонах",
        ),
    ]
    result = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            global_comment="на завтра",
            items=[
                ExtractedItem(
                    product_query="Сироп Роза",
                    quantity=5,
                    unit="шт",
                    comment="главное быстро",
                ),
                ExtractedItem(
                    product_query="Сироп Тархун",
                    quantity=10,
                    unit="шт",
                    comment="всё это дело на завтра",
                ),
            ],
        ),
        ConversationState(restaurant="Кафе"),
        catalog,
    )

    assert [item.catalog_comment for item in result.state.cart] == [
        "в банках; на завтра; на завтра или послезавтра",
        "в ведрах; на завтра; в вагонах",
    ]
    assert [item.comment for item in result.state.cart] == [
        "главное быстро; на завтра",
        "на завтра",
    ]


def test_late_global_comment_applies_to_existing_and_new_items_without_overlap(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    """Применяет поздний общий комментарий ко всей заявке без повторения у нового товара."""
    engine = ConversationEngine(settings)
    existing = engine._build_item(ExtractedItem(product_query="срп трхн", quantity=10, unit="шт"))
    existing.catalog_product_id = "tarhun"
    existing.catalog_name = "Сироп Тархун, 1л"
    existing.status = ItemStatus.MATCHED
    skipped = engine._build_item(ExtractedItem(product_query="кальмар", quantity=1, unit="шт"))
    skipped.status = ItemStatus.SKIPPED
    state = ConversationState(restaurant="Кафе", cart=[skipped, existing])
    catalog = [
        CatalogProduct(
            product_id="rose",
            name="Сироп Роза, 1л",
            supplier="МБР",
            unit="шт",
            comment="тест",
        )
    ]

    result = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            global_comment="желательно на завтра",
            items=[
                ExtractedItem(
                    product_query="срп роза",
                    quantity=1,
                    unit="шт",
                    comment="в банках и всё желательно на завтра",
                )
            ],
        ),
        state,
        catalog,
    )

    assert result.state.cart[0].comment == ""
    assert result.state.cart[1].comment == "желательно на завтра"
    assert result.state.cart[2].catalog_comment == "тест"
    assert result.state.cart[2].comment == "в банках; желательно на завтра"
    pending = engine._prepare_submission(_event(), result.state)
    assert pending.state.pending_submission is not None
    assert [row["Комментарий"] for row in pending.state.pending_submission.rows] == [
        "желательно на завтра",
        "в банках; желательно на завтра",
    ]


def test_recovered_local_comments_reach_each_own_order_row(settings) -> None:  # type: ignore[no-untyped-def]
    """Записывает восстановленные локальные комментарии только в строки их товаров."""
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(
            product_id="tarhun",
            name="Сироп Тархун, 1л",
            supplier="МБР",
            unit="шт",
        ),
        CatalogProduct(
            product_id="rose",
            name="Сироп Роза, 1л",
            supplier="МБР",
            unit="шт",
        ),
    ]
    command = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        global_comment="желательно на завтра",
        items=[
            ExtractedItem(
                product_query="Сироп Тархун",
                quantity=10,
                unit="шт",
                comment="в бутылках",
                source_line="Сироп Тархун в бутылках 10 штук",
            ),
            ExtractedItem(
                product_query="Сироп Роза",
                quantity=1,
                unit="шт",
                comment="в банках",
                source_line="Сироп Роза 1 штука в банках",
            ),
        ],
    )

    result = engine.handle(
        _event(),
        command,
        ConversationState(restaurant="Кафе"),
        catalog,
    )

    assert [item.comment for item in result.state.cart] == [
        "в бутылках; желательно на завтра",
        "в банках; желательно на завтра",
    ]
    pending = engine._prepare_submission(_event(), result.state)
    assert pending.state.pending_submission is not None
    assert [row["Комментарий"] for row in pending.state.pending_submission.rows] == [
        "в бутылках; желательно на завтра",
        "в банках; желательно на завтра",
    ]


def test_standalone_global_comment_updates_active_draft_once(settings) -> None:  # type: ignore[no-untyped-def]
    """Обрабатывает отдельную голосовую команду общего комментария без ложных товаров."""
    engine = ConversationEngine(settings)
    first = engine._build_item(ExtractedItem(product_query="сироп тархун", quantity=10, unit="шт"))
    first.status = ItemStatus.MATCHED
    second = engine._build_item(
        ExtractedItem(
            product_query="сироп роза",
            quantity=1,
            unit="шт",
            comment="в банках",
        )
    )
    second.status = ItemStatus.MATCHED
    state = ConversationState(cart=[first, second])
    event = TelegramEvent(
        update_id=2,
        chat_id="123456",
        input_type=InputKind.VOICE,
        text="Добавь общий комментарий: желательно на завтра",
    )
    command = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        text=event.text,
        global_comment="желательно на завтра",
    )

    result = engine.handle(event, command, state, [])
    repeated = engine.handle(event, command, result.state, [])

    assert len(repeated.state.cart) == 2
    assert repeated.state.cart[0].comment == "желательно на завтра"
    assert repeated.state.cart[1].comment == "в банках; желательно на завтра"
    assert "Применён ко всем товарам: 2" in repeated.reply.text


def test_parser_keeps_unmarked_tail_in_product_name() -> None:
    """Оставляет неоднозначное слово после количества в названии товара."""
    items = parse_product_lines("сироп роза 10 штук охлаждённым")

    assert len(items) == 1
    assert items[0].product_query == "сироп роза охлаждённым"
    assert items[0].quantity == 10
    assert items[0].unit == "шт"
    assert items[0].comment == ""
    assert items[0].comment_source == "none"


@pytest.mark.parametrize(
    ("source", "expected_query"),
    [
        ("грудинка 5 кг Блэк Ангус", "грудинка Блэк Ангус"),
        ("томаты 5 кг Турция", "томаты Турция"),
        ("сироп роза 5 шт холодным", "сироп роза холодным"),
    ],
)
def test_unmarked_tail_stays_in_product_query(
    source: str,
    expected_query: str,
) -> None:
    """Не отправляет характеристику из остатка фразы как комментарий."""
    item = parse_product_lines(source)[0]

    assert item.product_query == expected_query
    assert item.quantity == 5
    assert item.comment == ""
    assert item.user_comment_to_supplier == ""
    assert item.comment_source == "none"


@pytest.mark.parametrize(
    ("source", "expected_comment"),
    [
        ("сироп роза 5 шт, комментарий: привезти холодным", "привезти холодным"),
        ("сироп роза 5 шт желательно охлаждённым", "желательно охлаждённым"),
        ("сироп роза 5 шт без замены", "без замены"),
    ],
)
def test_explicit_comment_marker_sets_provenance(
    source: str,
    expected_comment: str,
) -> None:
    """Сохраняет только явно обозначенное пожелание поставщику."""
    item = parse_product_lines(source)[0]

    assert item.product_query == "сироп роза"
    assert item.quantity == 5
    assert item.comment == expected_comment
    assert item.comment_source == "explicit_marker"


def test_catalog_comment_is_reference_only_and_not_submitted(settings) -> None:  # type: ignore[no-untyped-def]
    """Не отправляет сохранённый комментарий каталога в новую заявку."""
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(
            product_id="rose",
            name="Сироп Роза",
            supplier="Сиропы",
            unit="шт",
            price=100,
            comment="старое примечание каталога",
        )
    ]

    added = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(
                    product_query="Сироп Роза",
                    quantity=5,
                    unit="шт",
                    comment="привезти холодным",
                )
            ],
        ),
        ConversationState(restaurant="Кафе"),
        catalog,
    )

    item = added.state.cart[0]
    assert item.catalog_comment == "старое примечание каталога"
    assert item.catalog_comment_source == "catalog"
    assert item.comment == "привезти холодным"
    assert item.comment_source == "semantic"
    pending = engine._prepare_submission(_event(), added.state)
    assert pending.state.pending_submission is not None
    assert pending.state.pending_submission.rows[0]["Комментарий"] == "привезти холодным"


def test_legacy_merged_catalog_comment_is_split_before_submission(settings) -> None:  # type: ignore[no-untyped-def]
    """Очищает старый черновик, где примечание каталога смешано с пожеланием."""
    engine = ConversationEngine(settings)
    product = CatalogProduct(
        product_id="rose",
        name="Сироп Роза",
        supplier="Сиропы",
        unit="шт",
        price=100,
        comment="старое примечание каталога",
    )
    state = ConversationState(
        restaurant="Кафе",
        cart=[
            CartItem(
                id="legacy",
                source_query="Сироп Роза",
                quantity=5,
                unit="шт",
                comment="старое примечание каталога; привезти холодным",
                status=ItemStatus.MATCHED,
                catalog_product_id="rose",
                catalog_name="Сироп Роза",
                supplier="Сиропы",
                catalog_unit="шт",
                price=100,
            )
        ],
    )

    engine._refresh_cart_order_values(state, [product])

    assert state.cart[0].catalog_comment == "старое примечание каталога"
    assert state.cart[0].comment == "привезти холодным"
    pending = engine._prepare_submission(_event(), state)
    assert pending.state.pending_submission is not None
    assert pending.state.pending_submission.rows[0]["Комментарий"] == "привезти холодным"


def test_parser_keeps_multiword_delivery_preferences_as_a_single_comment() -> None:
    """Проверяет, что парсер сохраняет многословный delivery preferences как a один комментарий."""
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


def test_unmarked_attribute_before_quantity_requires_catalog_clarification(settings) -> None:  # type: ignore[no-untyped-def]
    """Не превращает неподтверждённую характеристику в пожелание поставщику."""
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

    assert result.state.cart[0].status is ItemStatus.AMBIGUOUS
    assert result.state.cart[0].source_query == "сироп роза холодным"
    assert result.state.cart[0].catalog_name == ""
    assert result.state.cart[0].comment == ""


def test_unmarked_variant_survives_ambiguous_choice_without_becoming_comment(settings) -> None:  # type: ignore[no-untyped-def]
    """Сохраняет вариант товара до ручного выбора и не отправляет его комментарием."""
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
    assert item.source_query == "говядина мраморная без кожи"
    assert item.comment == ""

    selected = engine.handle(
        _event(),
        ParsedCommand(intent=Intent.SELECT_CANDIDATE, selected_index=1, callback_target="0"),
        added.state,
        catalog,
    )

    assert selected.state.cart[0].status is ItemStatus.MATCHED
    assert selected.state.cart[0].comment == ""
    pending = engine._prepare_submission(_event(), selected.state)
    assert pending.state.pending_submission is not None
    assert pending.state.pending_submission.rows[0]["Комментарий"] == ""


def test_typo_resolution_preserves_item_and_global_comments(settings) -> None:  # type: ignore[no-untyped-def]
    """Не теряет комментарии при исправлении опечатки в названии."""
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(
            product_id="rose",
            name="Сироп Роза, 1л",
            supplier="Сиропы",
            unit="шт",
            price=100,
        ),
        CatalogProduct(
            product_id="tarhun",
            name="Сироп Тархун, 1л",
            supplier="Сиропы",
            unit="шт",
            price=100,
        ),
    ]
    added = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            global_comment="доставить завтра до 10 утра",
            items=[
                ExtractedItem(
                    product_query="сироп рза",
                    quantity=5,
                    unit="шт",
                    comment="только охлаждённым",
                )
            ],
        ),
        ConversationState(restaurant="Кафе"),
        catalog,
    )

    item = added.state.cart[0]
    assert item.source_query == "сироп рза"
    assert item.comment == "только охлаждённым; доставить завтра до 10 утра"
    assert item.status is ItemStatus.AMBIGUOUS

    selected = engine.handle(
        _event(),
        ParsedCommand(intent=Intent.SELECT_CANDIDATE, selected_index=1, callback_target="0"),
        added.state,
        catalog,
    )

    assert selected.state.cart[0].catalog_product_id == "rose"
    assert selected.state.cart[0].comment == "только охлаждённым; доставить завтра до 10 утра"
    pending = engine._prepare_submission(_event(), selected.state)
    assert pending.state.pending_submission is not None
    assert (
        pending.state.pending_submission.rows[0]["Комментарий"]
        == "только охлаждённым; доставить завтра до 10 утра"
    )


def test_catalog_evidence_separates_many_product_typos_from_free_comments(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    """Разделяет опечатки и произвольные комментарии без словаря товаров."""
    engine = ConversationEngine(settings)
    cases = [
        (
            "сироп рза холодным",
            [
                CatalogProduct(product_id="rose", name="Сироп Роза, 1л", unit="шт"),
                CatalogProduct(product_id="tarhun", name="Сироп Тархун, 1л", unit="шт"),
            ],
            "сироп рза холодным",
            "",
            "",
        ),
        (
            "кордиал апелсин без льда",
            [
                CatalogProduct(
                    product_id="orange",
                    name="Кордиал Апельсин/Ваниль, 1л",
                    unit="шт",
                ),
                CatalogProduct(product_id="cherry", name="Кордиал Вишня, 1л", unit="шт"),
            ],
            "кордиал апелсин без льда",
            "",
            "",
        ),
        (
            "сыр пармезн натереть мелко",
            [
                CatalogProduct(product_id="parmesan", name="Сыр Пармезан", unit="кг"),
                CatalogProduct(product_id="gouda", name="Сыр Гауда", unit="кг"),
            ],
            "сыр пармезн",
            "натереть мелко",
            "",
        ),
    ]

    for raw_query, catalog, expected_query, expected_comment, product_id in cases:
        item = engine._build_item(ExtractedItem(product_query=raw_query))
        engine._match_item(item, catalog)

        assert item.source_query == expected_query
        assert item.comment == expected_comment
        assert item.candidates[0].product_id == catalog[0].product_id
        assert item.catalog_product_id == product_id


def test_existing_comment_shadow_is_removed_from_persisted_draft(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что существующий комментарий ложная позиция является removed из persisted черновик."""
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
