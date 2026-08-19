"""Проверяет детерминированное владение комментариями после intake."""

from restaurant_bot.domain.models import (
    CartItem,
    CatalogProduct,
    ConversationState,
    ExtractedItem,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    SessionStage,
    TelegramEvent,
)
from restaurant_bot.services.engine import ConversationEngine


def _event(text: str, update_id: int = 1) -> TelegramEvent:
    """Создаёт текстовое событие для comment ownership сценария."""
    return TelegramEvent(
        update_id=update_id,
        chat_id="comment-ownership",
        input_type=InputKind.TEXT,
        text=text,
    )


def _catalog(*names: str) -> list[CatalogProduct]:
    """Создаёт минимальный каталог с единицей штуки."""
    return [
        CatalogProduct(product_id=str(index), name=name, unit="шт", supplier="Овощи")
        for index, name in enumerate(names, 1)
    ]


def _item(query: str, quantity: float = 1) -> ExtractedItem:
    """Создаёт структурированную товарную позицию."""
    return ExtractedItem(product_query=query, quantity=quantity, unit="шт")


def test_one_new_product_binds_trailing_comment_without_scope_question(settings) -> None:  # type: ignore[no-untyped-def]
    """Автоматически связывает комментарий с единственной новой позицией."""
    source = "Горчица домашняя 5 штук. Привезти завтра до восьми."
    result = ConversationEngine(settings).handle(
        _event(source),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            text=source,
            items=[_item("Горчица домашняя", 5)],
            comment_clarification="Привезти завтра до восьми",
        ),
        ConversationState(),
        _catalog("Горчица домашняя"),
    )

    assert len(result.state.cart) == 1
    assert result.state.cart[0].quantity == 5
    assert result.state.cart[0].comment == "Привезти завтра до восьми"
    assert result.state.stage is not SessionStage.AWAIT_COMMENT_SCOPE
    assert "К каким товарам относится" not in result.reply.text


def test_live_mustard_case_binds_delivery_comment_to_new_item(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет точный live-кейс горчицы с характеристиками каталога."""
    source = (
        "Горчица домашняя, калорийная, 170 грамм, СТБ Россия 112, острая. "
        "Мне нужно 5 штук. Привезти завтра до 8 вечера."
    )
    result = ConversationEngine(settings).handle(
        _event(source),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            text=source,
            items=[_item("Горчица домашняя калорийная", 5)],
            comment_clarification="Привезти завтра до 8 вечера",
        ),
        ConversationState(),
        _catalog("Горчица домашняя калорийная 170 грамм СТБ Россия 112 острая"),
    )

    assert len(result.state.cart) == 1
    assert result.state.cart[0].quantity == 5
    assert result.state.cart[0].comment == "Привезти завтра до 8 вечера"
    assert result.state.stage is not SessionStage.AWAIT_COMMENT_SCOPE


def test_one_new_product_owns_comment_over_existing_cart(settings) -> None:  # type: ignore[no-untyped-def]
    """Не переносит комментарий новой позиции на старые товары корзины."""
    existing = CartItem(
        id="onion",
        source_query="Лук",
        catalog_name="Лук",
        quantity=5,
        unit="кг",
        status=ItemStatus.MATCHED,
    )
    source = "Добавь горчицу 5 штук. Привезти завтра до восьми."
    state = ConversationState(cart=[existing])
    result = ConversationEngine(settings).handle(
        _event(source),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            text=source,
            items=[_item("горчица", 5)],
            comment_clarification="Привезти завтра до восьми",
        ),
        state,
        _catalog("горчица"),
    )

    mustard = next(item for item in result.state.cart if item.source_query == "горчица")
    assert mustard.comment == "Привезти завтра до восьми"
    assert existing.comment == ""
    assert result.state.pending_comment_target_item_ids == []


def test_detached_comment_is_pending_after_two_products_are_admitted(settings) -> None:  # type: ignore[no-untyped-def]
    """Сохраняет реальную неоднозначность после создания обеих позиций."""
    source = "Лук 5 кг и картошка 10 кг. Привезти завтра."
    result = ConversationEngine(settings).handle(
        _event(source),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            text=source,
            items=[_item("Лук", 5), _item("картошка", 10)],
            comment_clarification="Привезти завтра",
        ),
        ConversationState(),
        _catalog("Лук", "Картошка"),
    )

    assert [item.source_query for item in result.state.cart] == ["Лук", "картошка"]
    assert result.state.stage is SessionStage.AWAIT_COMMENT_SCOPE
    assert result.state.pending_comment_items == []
    assert set(result.state.pending_comment_target_item_ids) == {
        item.id for item in result.state.cart
    }


def test_explicit_item_comment_is_bound_without_clarification(settings) -> None:  # type: ignore[no-untyped-def]
    """Связывает комментарий с названной позицией среди двух товаров."""
    source = "Лук 5 кг и картошка 10 кг. Картошку привезти завтра."
    result = ConversationEngine(settings).handle(
        _event(source),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            text=source,
            items=[_item("Лук", 5), _item("картошка", 10)],
            comment_clarification="Привезти завтра",
        ),
        ConversationState(),
        _catalog("Лук", "Картошка"),
    )

    assert result.state.stage is not SessionStage.AWAIT_COMMENT_SCOPE
    assert result.state.cart[0].comment == ""
    assert result.state.cart[1].comment == "Привезти завтра"


def test_explicit_global_comment_is_applied_to_all_active_items(settings) -> None:  # type: ignore[no-untyped-def]
    """Применяет только явно глобальный комментарий ко всей заявке."""
    source = "Лук 5 кг и картошка 10 кг. Всем товарам привезти завтра."
    result = ConversationEngine(settings).handle(
        _event(source),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            text=source,
            items=[_item("Лук", 5), _item("картошка", 10)],
            global_comment="привезти завтра",
        ),
        ConversationState(),
        _catalog("Лук", "Картошка"),
    )

    assert [item.comment for item in result.state.cart] == [
        "привезти завтра",
        "привезти завтра",
    ]
    assert result.state.pending_comment_target_item_ids == []


def test_explicit_group_comment_excludes_third_product(settings) -> None:  # type: ignore[no-untyped-def]
    """Применяет групповой комментарий только к названным двум позициям."""
    source = "Лук 5 кг, картошка 10 кг, морковь 5 кг. Лук и картошку привезти завтра."
    result = ConversationEngine(settings).handle(
        _event(source),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            text=source,
            items=[_item("Лук", 5), _item("картошка", 10), _item("морковь", 5)],
            comment_clarification="Привезти завтра",
        ),
        ConversationState(),
        _catalog("Лук", "Картошка", "Морковь"),
    )

    assert [item.comment for item in result.state.cart] == [
        "Привезти завтра",
        "Привезти завтра",
        "",
    ]
    assert result.state.stage is not SessionStage.AWAIT_COMMENT_SCOPE


def test_order_quantity_comment_is_rejected_deterministically(settings) -> None:  # type: ignore[no-untyped-def]
    """Не превращает semantic ORDER_QUANTITY в комментарий поставщику."""
    source = "Горчица 13 штук. Мне нужно 13 штук."
    result = ConversationEngine(settings).handle(
        _event(source),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            text=source,
            items=[_item("горчица", 13)],
            comment_clarification="Мне нужно 13 штук",
        ),
        ConversationState(),
        _catalog("Горчица"),
    )

    assert result.state.cart[0].comment == ""
    assert result.state.pending_comment_text == ""


def test_product_issue_is_shown_before_comment_scope(settings) -> None:  # type: ignore[no-untyped-def]
    """Оставляет комментарий до завершения product issue и не блокирует его."""
    source = "Неизвестная горчица 5 штук. Привезти завтра."
    started = ConversationEngine(settings).handle(
        _event(source),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            text=source,
            items=[_item("Неизвестная горчица", 5)],
            comment_clarification="Привезти завтра",
        ),
        ConversationState(),
        [],
    )

    assert started.state.stage is SessionStage.COLLECTING
    assert started.state.current_issue_item_id == started.state.cart[0].id
    assert started.state.cart[0].comment == "Привезти завтра"
    assert started.state.pending_comment_target_item_ids == []
    assert "К каким товарам относится" not in started.reply.text

    resolved = ConversationEngine(settings).handle(
        _event("Не добавлять", 2),
        ParsedCommand(intent=Intent.SKIP_CURRENT, text="Не добавлять"),
        started.state,
        [],
    )

    assert resolved.state.stage is SessionStage.REVIEW
    assert resolved.state.cart[0].comment == "Привезти завтра"
