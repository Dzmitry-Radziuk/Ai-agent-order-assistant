"""Проверяет разделение числовых признаков каталога и количества заказа."""

from __future__ import annotations

import pytest

from restaurant_bot.config import Settings
from restaurant_bot.conversation.item_intake import build_cart_item
from restaurant_bot.conversation.selection import resolve_candidate_reference
from restaurant_bot.domain.models import (
    Candidate,
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
from restaurant_bot.orders.quantity_provenance import (
    QuantityProvenance,
    reconcile_order_quantity_evidence,
)
from restaurant_bot.services.engine import ConversationEngine

_MUSTARD_NAME = "\u0413\u043e\u0440\u0447\u0438\u0446\u0430 \u0414\u043e\u043c\u0430\u0448\u043d\u044f\u044f 450 \u043c\u043b 1/6 \u0422\u0423\u0411\u0410"
_MUSTARD_SOURCE = (
    "\u0433\u043e\u0440\u0447\u0438\u0446\u0430 450 \u043c\u043b 1/6 \u0442\u0443\u0431\u0430"
)
_MUSTARD_ORDER_SOURCE = f"{_MUSTARD_SOURCE} 5 \u0448\u0442"


def _candidate_state() -> ConversationState:
    """Создаёт состояние с открытым выбором товара горчицы."""
    item = CartItem(
        id="mustard",
        source_query="горчица",
        status=ItemStatus.AMBIGUOUS,
        candidates=[Candidate(product_id="mustard", name=_MUSTARD_NAME, unit="шт")],
    )
    return ConversationState(cart=[item], current_issue_item_id=item.id)


def _catalog() -> list[CatalogProduct]:
    """Возвращает каталог для регрессионного сценария выбора горчицы."""
    return [CatalogProduct(product_id="mustard", name=_MUSTARD_NAME, unit="шт")]


def _command(source: str, quantity: float | None, unit: str) -> ParsedCommand:
    """Создаёт команду добавления с предложенным количеством."""
    return ParsedCommand(
        intent=Intent.ADD_ITEMS,
        text=source,
        items=[
            ExtractedItem(
                product_query="горчица",
                quantity=quantity,
                unit=unit,
                source_line=source,
            )
        ],
    )


def test_catalog_identity_numbers_are_not_order_quantity() -> None:
    """Отбрасывает 450 мл и 1/6 как числовые признаки названия каталога."""
    authorization = reconcile_order_quantity_evidence(
        _MUSTARD_SOURCE,
        450,
        "мл",
        catalog_name=_MUSTARD_NAME,
    )

    assert authorization.provenance is QuantityProvenance.CATALOG_IDENTITY
    assert authorization.quantity is None


def test_dough_packaging_in_catalog_name_is_not_order_quantity() -> None:
    """Не считает фасовку теста количеством заказа после проверки каталога."""
    source = "\u0422\u0435\u0441\u0442\u043e \u0441\u043b\u043e\u0435\u043d\u043e\u0435 \u0434\u0440\u043e\u0436\u0436\u0435\u0432\u043e\u0435 10 \u043a\u0433"
    authorization = reconcile_order_quantity_evidence(
        source,
        10,
        "\u043a\u0433",
        catalog_name=source,
    )

    assert authorization.provenance is QuantityProvenance.CATALOG_IDENTITY
    assert authorization.quantity is None


def test_residual_order_quantity_survives_catalog_identity() -> None:
    """Оставляет отдельное количество заказа после исключения фасовки каталога."""
    authorization = reconcile_order_quantity_evidence(
        _MUSTARD_ORDER_SOURCE,
        5,
        "шт",
        catalog_name=_MUSTARD_NAME,
    )

    assert authorization.provenance is QuantityProvenance.ORDER
    assert authorization.quantity == 5
    assert authorization.unit == "шт"


def test_cart_item_uses_local_source_span_for_multi_item_voice_quantity() -> None:
    """Не стирает количество позиции из-за чисел в соседней части транскрипта."""
    source_line = (
        "Мне нужно филе лосося 0,8-1,2 килограмма 5 кг, "
        "а также лук зелёный 10 килограмм срез корня от 5 сантиметров."
    )
    extracted = ExtractedItem(
        product_query="лук зелёный",
        quantity=10,
        unit="кг",
        source_line=source_line,
        source_span="лук зелёный 10 килограмм срез корня от 5 сантиметров",
    )

    cart_item = build_cart_item(extracted, default_department="")

    assert (cart_item.quantity, cart_item.unit) == (10.0, "кг")


def test_repeated_packaging_and_order_occurrences_use_the_latter_order_span() -> None:
    """Различает фасовочные и заказные одинаковые числа по их occurrence span."""
    source = (
        "\u0413\u043e\u0440\u0447\u0438\u0446\u0430 1 \u043a\u0433, 6 \u0448\u0442\u0443\u043a \u0432 \u043a\u043e\u0440\u043e\u0431\u043a\u0435. "
        "\u041d\u0443\u0436\u043d\u043e 6 \u0448\u0442\u0443\u043a"
    )
    catalog_name = "\u0413\u043e\u0440\u0447\u0438\u0446\u0430 1 \u043a\u0433 6 \u0448\u0442\u0443\u043a \u0432 \u043a\u043e\u0440\u043e\u0431\u043a\u0435"

    authorization = reconcile_order_quantity_evidence(
        source,
        6,
        "\u0448\u0442",
        catalog_name=catalog_name,
        packaging_role="catalog_attribute",
    )

    assert authorization.provenance is QuantityProvenance.ORDER
    assert authorization.quantity == 6


def test_catalog_packaging_occurrence_does_not_authorize_cart_quantity() -> None:
    """Отклоняет число фасовки, если независимого order occurrence нет."""
    source = "\u0413\u043e\u0440\u0447\u0438\u0446\u0430 1 \u043a\u0433, 6 \u0448\u0442\u0443\u043a \u0432 \u043a\u043e\u0440\u043e\u0431\u043a\u0435"
    catalog_name = "\u0413\u043e\u0440\u0447\u0438\u0446\u0430 1 \u043a\u0433 6 \u0448\u0442\u0443\u043a \u0432 \u043a\u043e\u0440\u043e\u0431\u043a\u0435"

    authorization = reconcile_order_quantity_evidence(
        source,
        6,
        "\u0448\u0442",
        catalog_name=catalog_name,
        packaging_role="catalog_attribute",
    )

    assert authorization.provenance is QuantityProvenance.CATALOG_IDENTITY
    assert authorization.quantity is None


def test_cucumber_catalog_dimensions_leave_only_residual_order_quantity() -> None:
    """Отделяет размеры и фасовку огурцов от отдельного количества заказа."""
    authorization = reconcile_order_quantity_evidence(
        (
            "\u041e\u0433\u0443\u0440\u0446\u044b 40/45 10 \u043b\u0438\u0442\u0440\u043e\u0432 "
            "9700 \u0433\u0440\u0430\u043c\u043c 5600 \u0433\u0440\u0430\u043c\u043c \u0413\u0435\u0440\u043c\u0430\u043d\u0438\u044f 5 \u0448\u0442"
        ),
        5,
        "\u0448\u0442",
        catalog_name=(
            "\u041e\u0433\u0443\u0440\u0446\u044b 40/45 10 \u043b 9700 \u0433 "
            "5600 \u0433 \u0413\u0435\u0440\u043c\u0430\u043d\u0438\u044f"
        ),
    )

    assert authorization.provenance is QuantityProvenance.ORDER
    assert authorization.quantity == 5
    assert authorization.unit == "\u0448\u0442"


def test_cucumber_catalog_dimensions_without_order_quantity_are_not_order() -> None:
    """Не считает размеры и фасовку огурцов самостоятельным заказом."""
    authorization = reconcile_order_quantity_evidence(
        "\u041e\u0433\u0443\u0440\u0446\u044b 40/45 10 \u043b 9700 \u0433 5600 \u0433 \u0413\u0435\u0440\u043c\u0430\u043d\u0438\u044f",
        10,
        "\u043b",
        catalog_name=(
            "\u041e\u0433\u0443\u0440\u0446\u044b 40/45 10 \u043b 9700 \u0433 "
            "5600 \u0433 \u0413\u0435\u0440\u043c\u0430\u043d\u0438\u044f"
        ),
    )

    assert authorization.provenance is QuantityProvenance.CATALOG_IDENTITY
    assert authorization.quantity is None


def test_catalog_weight_170_grams_is_not_order_quantity() -> None:
    """Не принимает вес упаковки 170 грамм за количество заказа."""
    authorization = reconcile_order_quantity_evidence(
        "\u0423\u0442\u043a\u0430 \u0444\u0438\u043b\u0435 170 \u0433",
        170,
        "\u0433",
        catalog_name="\u0423\u0442\u043a\u0430 \u0444\u0438\u043b\u0435 170 \u0433",
    )

    assert authorization.provenance is QuantityProvenance.CATALOG_IDENTITY
    assert authorization.quantity is None


@pytest.mark.parametrize("input_kind", [InputKind.TEXT, InputKind.VOICE])
def test_candidate_identity_and_order_quantity_converge_for_text_and_voice(
    settings: Settings,
    input_kind: InputKind,
) -> None:
    """Сводит текстовый и голосовой путь к одной позиции с количеством заказа."""
    state = _candidate_state()
    result = ConversationEngine(settings).handle(
        TelegramEvent(update_id=1, chat_id="1", input_type=input_kind, text=_MUSTARD_ORDER_SOURCE),
        _command(_MUSTARD_ORDER_SOURCE, 5, "шт"),
        state,
        _catalog(),
    )

    item = result.state.cart[0]
    assert item.catalog_product_id == "mustard"
    assert item.quantity == 5
    assert item.unit == "шт"
    assert item.status is ItemStatus.MATCHED


def test_candidate_title_without_order_quantity_requires_quantity(settings: Settings) -> None:
    """Выбор варианта с фасовкой каталога оставляет позицию без количества заказа."""
    state = _candidate_state()
    result = ConversationEngine(settings).handle(
        TelegramEvent(update_id=2, chat_id="1", input_type=InputKind.TEXT, text=_MUSTARD_SOURCE),
        _command(_MUSTARD_SOURCE, 450, "мл"),
        state,
        _catalog(),
    )

    item = result.state.cart[0]
    assert item.catalog_product_id == "mustard"
    assert item.quantity is None
    assert item.status is ItemStatus.MISSING_QTY


def test_candidate_reference_is_limited_to_current_candidates() -> None:
    """Разрешает ссылку только на единственный показанный вариант каталога."""
    state = _candidate_state()
    reference = resolve_candidate_reference(state.cart[0], _MUSTARD_SOURCE)

    assert reference.candidate is state.cart[0].candidates[0]
