"""Проверяет границу каталожных признаков, фасовки и заказа."""

from __future__ import annotations

from restaurant_bot.config import Settings
from restaurant_bot.conversation.comments import remove_catalog_fact_comments
from restaurant_bot.domain.models import (
    CartItem,
    CatalogProduct,
    ConversationState,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    TelegramEvent,
)
from restaurant_bot.orders.quantity_provenance import (
    QuantityProvenance,
    reconcile_order_quantity_evidence,
)
from restaurant_bot.parsing.products import parse_product_lines
from restaurant_bot.services.engine import ConversationEngine

_PEPPER_SOURCE = (
    "\u041f\u0435\u0440\u0435\u0446 \u0425\u0430\u043b\u0430\u043f\u0435\u043d\u044c\u043e \u0437\u0435\u043b\u0435\u043d\u044b\u0439 \u0440\u0435\u0437\u0430\u043d\u044b\u0439 HELCOM, "
    "\u0441\u0442/\u0431, 720 \u043c\u043b/680 \u0433\u0440/330 \u0433\u0440, 8 \u0448\u0442/\u043a\u043e\u0440, \u041f\u043e\u043b\u044c\u0448\u0430"
)
_MUSTARD_SOURCE = (
    "\u0413\u043e\u0440\u0447\u0438\u0446\u0430 \u0417\u0435\u0440\u043d\u0438\u0441\u0442\u0430\u044f CHATEL, \u0432\u0435\u0434\u0440\u043e, "
    "1 \u043a\u0433, 6 \u0448\u0442/\u043a\u043e\u0440, \u0424\u0440\u0430\u043d\u0446\u0438\u044f"
)
_COCONUT_SOURCE = (
    '\u041c\u0430\u0441\u043b\u043e \u041a\u043e\u043a\u043e\u0441\u043e\u0432\u043e\u0435 Exrta Virgin \u0422\u041c "Aroy-D", '
    "450\u043c\u043b, \u0441\u0442\u0435\u043a\u043b.\u0431, 12 \u0448\u0442/\u043a\u043e\u0440, \u0418\u043d\u0434\u043e\u043d\u0435\u0437\u0438\u044f, 10 \u0448\u0442\u0443\u043a"
)


def test_catalog_facts_stay_one_product_line() -> None:
    """Сохраняет признаки одной сложной позиции в одной строке."""
    items = parse_product_lines(_PEPPER_SOURCE)

    assert len(items) == 1
    assert items[0].source_line == _PEPPER_SOURCE
    assert items[0].quantity is None
    assert items[0].unit == ""
    assert "\u0441\u0442/\u0431" in items[0].product_query
    assert "\u041f\u043e\u043b\u044c\u0448\u0430" in items[0].product_query


def test_packaging_ratio_is_not_order_quantity() -> None:
    """Не принимает количество коробок из каталожной фасовки за заказ."""
    item = parse_product_lines(_MUSTARD_SOURCE)[0]

    assert item.quantity is None
    assert item.unit == ""
    assert item.packaging_role == "catalog_attribute"


def test_long_catalog_title_with_attribute_tail_stays_one_order_item() -> None:
    """Сохраняет длинное название каталога и хвост фасовки одной позицией заказа."""
    items = parse_product_lines(_COCONUT_SOURCE)

    assert len(items) == 1
    item = items[0]
    assert item.source_line == _COCONUT_SOURCE
    assert item.quantity == 10
    assert item.unit == "\u0448\u0442"
    assert item.product_query.endswith("\u0418\u043d\u0434\u043e\u043d\u0435\u0437\u0438\u044f")
    assert "\u0441\u0442\u0435\u043a\u043b.\u0431" in item.product_query
    assert "12 \u0448\u0442/\u043a\u043e\u0440" in item.product_query
    assert item.packaging_role == "catalog_attribute"
    assert "450\u043c\u043b" in item.packaging_text
    assert "12 \u0448\u0442/\u043a\u043e\u0440" in item.packaging_text


def test_long_catalog_title_creates_one_cart_item(settings: Settings) -> None:
    """Добавляет длинное каталожное название в черновик одной позицией."""
    catalog_name = _COCONUT_SOURCE.removesuffix(", 10 \u0448\u0442\u0443\u043a")
    command = ParsedCommand(intent=Intent.ADD_ITEMS, items=parse_product_lines(_COCONUT_SOURCE))
    result = ConversationEngine(settings).handle(
        TelegramEvent(
            update_id=1,
            chat_id="boundary-test",
            input_type=InputKind.TEXT,
            text=_COCONUT_SOURCE,
        ),
        command,
        ConversationState(),
        [CatalogProduct(product_id="coconut", name=catalog_name, unit="\u0448\u0442")],
    )

    assert len(result.state.cart) == 1
    assert result.state.cart[0].quantity == 10
    assert result.state.cart[0].unit == "\u0448\u0442"
    assert result.state.cart[0].catalog_name == catalog_name
    assert result.state.cart[0].status is ItemStatus.MATCHED


def test_text_and_voice_transcripts_share_product_boundary_result() -> None:
    """Сводит текстовый ввод и транскрипцию голоса к одной позиции заказа."""
    text_items = parse_product_lines(_COCONUT_SOURCE)
    voice_items = parse_product_lines(_COCONUT_SOURCE)

    assert [
        (item.product_query, item.quantity, item.unit, item.packaging_role) for item in text_items
    ] == [
        (item.product_query, item.quantity, item.unit, item.packaging_role) for item in voice_items
    ]


def test_plain_comma_lists_are_split_only_when_all_parts_are_products() -> None:
    """Разделяет короткие списки товаров и не принимает каталожный хвост за позицию."""
    assert [
        item.product_query
        for item in parse_product_lines(
            "\u043b\u0443\u043a, \u043a\u0430\u0440\u0442\u043e\u0448\u043a\u0430, \u043c\u043e\u0440\u043a\u043e\u0432\u044c"
        )
    ] == [
        "\u043b\u0443\u043a",
        "\u043a\u0430\u0440\u0442\u043e\u0448\u043a\u0430",
        "\u043c\u043e\u0440\u043a\u043e\u0432\u044c",
    ]
    assert [
        item.product_query
        for item in parse_product_lines(
            "\u043c\u043e\u043b\u043e\u043a\u043e, \u0445\u043b\u0435\u0431, \u044f\u0439\u0446\u0430"
        )
    ] == [
        "\u043c\u043e\u043b\u043e\u043a\u043e",
        "\u0445\u043b\u0435\u0431",
        "\u044f\u0439\u0446\u0430",
    ]
    packaged = parse_product_lines(
        "\u0421\u043e\u0443\u0441 XYZ, 450 \u043c\u043b/12 \u0448\u0442, \u0418\u043d\u0434\u043e\u043d\u0435\u0437\u0438\u044f, 10 \u0448\u0442"
    )[0]
    assert (packaged.quantity, packaged.unit) == (10, "\u0448\u0442")
    assert packaged.packaging_role == "catalog_attribute"
    assert packaged.packaging_text == "450 \u043c\u043b/12 \u0448\u0442"
    assert (
        len(
            parse_product_lines(
                "\u0421\u043e\u0443\u0441 XYZ, 700 \u043c\u043b, \u043f\u043b.\u0431., 6 \u0448\u0442/\u043a\u043e\u0440, \u0422\u0430\u0438\u043b\u0430\u043d\u0434"
            )
        )
        == 1
    )


def test_attribute_only_packaging_fragment_is_not_admitted_as_product() -> None:
    """Не принимает одну фасовочную характеристику за товарную позицию."""
    assert parse_product_lines("500 г 15 шт/упак") == []


def test_unanchored_gibberish_stays_ambiguous(settings: Settings) -> None:
    """Оставляет бессмысленный запрос на уточнение без подстановки каталога."""
    item = CartItem(
        id="gibberish",
        source_query="абра кадабра фыва олдж",
        source_line="абра кадабра фыва олдж",
    )
    ConversationEngine(settings).catalog_resolution.match_item(
        item,
        [CatalogProduct(product_id="onion", name="Лук репчатый", unit="кг")],
    )

    assert item.status is ItemStatus.AMBIGUOUS
    assert item.catalog_product_id == ""
    assert item.catalog_name == ""


def test_genuine_duplicate_requests_remain_separate() -> None:
    """Не подавляет две самостоятельные заявки на один и тот же товар."""
    items = parse_product_lines(
        "\u043b\u0443\u043a 5 \u043a\u0433, \u043b\u0443\u043a 3 \u043a\u0433"
    )

    assert [(item.product_query, item.quantity, item.unit) for item in items] == [
        ("\u043b\u0443\u043a", 5, "\u043a\u0433"),
        ("\u043b\u0443\u043a", 3, "\u043a\u0433"),
    ]


def test_repeated_value_uses_residual_order_occurrence() -> None:
    """Сохраняет второе совпадение числа как заказанное количество."""
    source = "\u0424\u0430\u0441\u043e\u043b\u044c 240 \u0433\u0440, \u043d\u0443\u0436\u043d\u043e 240 \u0433\u0440"
    authorization = reconcile_order_quantity_evidence(
        source,
        240,
        "\u0433",
        catalog_name="\u0424\u0430\u0441\u043e\u043b\u044c 240 \u0433\u0440",
    )

    assert authorization.provenance is QuantityProvenance.ORDER
    assert authorization.quantity == 240


def test_mixed_catalog_fact_comment_keeps_real_wish() -> None:
    """Удаляет каталожные числа и сохраняет самостоятельное пожелание."""
    comment = "\u043d\u0430 400 \u0433\u0440 \u043e\u0442\u0434\u0435\u043b\u044c\u043d\u043e \u043a\u0440\u0443\u043f\u043d\u0435\u0435"
    product = "\u0424\u0430\u0441\u043e\u043b\u044c FIAMMA 500 \u043c\u043b 400 \u0433\u0440 240 \u0433\u0440 1/24 \u0436/\u0431"

    assert remove_catalog_fact_comments(
        comment, "\u0424\u0430\u0441\u043e\u043b\u044c", product
    ) == (
        "\u043e\u0442\u0434\u0435\u043b\u044c\u043d\u043e \u043a\u0440\u0443\u043f\u043d\u0435\u0435"
    )


def test_complex_catalog_line_requires_order_quantity_after_resolution(settings: Settings) -> None:
    """Оставляет сложную позицию без количества до явного заказа."""
    product = CatalogProduct(
        product_id="pepper",
        name=(
            "\u041f\u0435\u0440\u0435\u0446 \u0425\u0430\u043b\u0430\u043f\u0435\u043d\u044c\u043e \u0437\u0435\u043b\u0435\u043d\u044b\u0439 \u0440\u0435\u0437\u0430\u043d\u044b\u0439 HELCOM, "
            "\u0441\u0442/\u0431, 720 \u043c\u043b/680 \u0433\u0440/330 \u0433\u0440, 8 \u0448\u0442/\u043a\u043e\u0440, \u041f\u043e\u043b\u044c\u0448\u0430"
        ),
        unit="\u0448\u0442",
    )
    item = CartItem(
        id="pepper",
        source_query="\u041f\u0435\u0440\u0435\u0446 \u0425\u0430\u043b\u0430\u043f\u0435\u043d\u044c\u043e HELCOM",
        source_line=_PEPPER_SOURCE,
        quantity=330,
        unit="\u0433",
    )

    ConversationEngine(settings).catalog_resolution.match_item(item, [product])

    assert item.quantity is None
    assert item.unit == ""
    assert item.status in {ItemStatus.MISSING_QTY, ItemStatus.AMBIGUOUS}


def test_explicit_order_quantity_survives_complex_catalog_resolution(settings: Settings) -> None:
    """Сохраняет отдельное количество заказа после выбора сложной позиции."""
    product = CatalogProduct(
        product_id="pepper",
        name=_PEPPER_SOURCE,
        unit="\u0448\u0442",
    )
    source = f"{_PEPPER_SOURCE}, \u043d\u0443\u0436\u043d\u043e 5 \u0448\u0442"
    item = CartItem(
        id="pepper-order",
        source_query=_PEPPER_SOURCE,
        source_line=source,
        quantity=5,
        unit="\u0448\u0442",
    )

    ConversationEngine(settings).catalog_resolution.match_item(item, [product])

    assert item.catalog_product_id == "pepper"
    assert item.quantity == 5
    assert item.unit == "\u0448\u0442"
    assert item.status is ItemStatus.MATCHED


def test_simple_product_query_remains_primary_catalog_retrieval(settings: Settings) -> None:
    """Ищет простой товар по названию, а не по полной фразе пользователя."""
    item = CartItem(
        id="onion",
        source_query="\u043b\u0443\u043a",
        source_line="\u041c\u043d\u0435 \u043d\u0443\u0436\u0435\u043d \u043b\u0443\u043a, \u0441\u0440\u0435\u0437 \u043a\u043e\u0440\u043d\u044f \u043e\u0442 \u043f\u044f\u0442\u0438 \u0441\u0430\u043d\u0442\u0438\u043c\u0435\u0442\u0440\u043e\u0432, 10 \u043a\u0438\u043b\u043e\u0433\u0440\u0430\u043c\u043c.",
        quantity=10,
        unit="\u043a\u0433",
        comment="\u0441\u0440\u0435\u0437 \u043a\u043e\u0440\u043d\u044f \u043e\u0442 \u043f\u044f\u0442\u0438 \u0441\u0430\u043d\u0442\u0438\u043c\u0435\u0442\u0440\u043e\u0432",
    )
    product = CatalogProduct(
        product_id="onion",
        name="\u041b\u0443\u043a \u0440\u0435\u043f\u0447\u0430\u0442\u044b\u0439",
        unit="\u043a\u0433",
    )

    ConversationEngine(settings).catalog_resolution.match_item(item, [product])

    assert item.source_query == "\u043b\u0443\u043a"
    assert item.candidates
    assert item.candidates[0].product_id == "onion"


def test_source_numeric_evidence_distinguishes_catalog_variants(settings: Settings) -> None:
    """Использует фасовку источника для выбора варианта после базового поиска."""
    source = "\u041f\u0435\u0440\u0435\u0446 \u0425\u0430\u043b\u0430\u043f\u0435\u043d\u044c\u043e HELCOM, \u0441\u0442/\u0431, 720 \u043c\u043b/680 \u0433\u0440/330 \u0433\u0440, 8 \u0448\u0442/\u043a\u043e\u0440, \u041f\u043e\u043b\u044c\u0448\u0430"
    item = CartItem(
        id="pepper-evidence",
        source_query="\u041f\u0435\u0440\u0435\u0446 \u0425\u0430\u043b\u0430\u043f\u0435\u043d\u044c\u043e HELCOM",
        source_line=source,
    )
    catalog = [
        CatalogProduct(product_id="exact", name=source, unit="\u0448\u0442"),
        CatalogProduct(
            product_id="other",
            name="\u041f\u0435\u0440\u0435\u0446 \u0425\u0430\u043b\u0430\u043f\u0435\u043d\u044c\u043e HELCOM, \u0441\u0442/\u0431, 370 \u043c\u043b, 12 \u0448\u0442/\u043a\u043e\u0440, \u041f\u043e\u043b\u044c\u0448\u0430",
            unit="\u0448\u0442",
        ),
    ]

    ConversationEngine(settings).catalog_resolution.match_item(item, catalog)

    assert item.catalog_product_id == "exact"
    assert [candidate.product_id for candidate in item.candidates] == ["exact"]
    assert item.status is ItemStatus.MISSING_QTY
