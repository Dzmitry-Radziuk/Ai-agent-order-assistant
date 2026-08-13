"""Приёмочные характеристики финальной готовности диалога."""

from restaurant_bot.domain.models import (
    CartItem,
    CatalogProduct,
    CommentSource,
    ConversationState,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    TelegramEvent,
)
from restaurant_bot.services.engine import ConversationEngine


def _event() -> TelegramEvent:
    """Создаёт текстовое событие для приёмочного сценария."""
    return TelegramEvent(update_id=901, chat_id="acceptance", input_type=InputKind.TEXT)


def _active_state() -> ConversationState:
    """Создаёт черновик с двумя связанными с каталогом позициями."""
    return ConversationState(
        restaurant="Кафе",
        cart=[
            CartItem(
                id="item-tomatoes",
                source_query="томаты",
                quantity=4,
                unit="кг",
                comment="спелые; без повреждений; в красной упаковке",
                comment_source=CommentSource.SEMANTIC,
                catalog_product_id="tomatoes",
                catalog_name="Томаты свежие",
                catalog_unit="кг",
                status=ItemStatus.MATCHED,
            ),
            CartItem(
                id="item-herbs",
                source_query="зелень",
                quantity=2,
                unit="кг",
                comment="крупная фасовка",
                comment_source=CommentSource.SEMANTIC,
                catalog_product_id="herbs",
                catalog_name="Зелень",
                catalog_unit="кг",
                status=ItemStatus.MATCHED,
            ),
        ],
    )


def test_remove_all_comments_keeps_cart_and_catalog_bindings(settings) -> None:  # type: ignore[no-untyped-def]
    """Удаление комментариев не очищает товары, количества и связи с каталогом."""
    engine = ConversationEngine(settings)
    state = _active_state()
    catalog = [
        CatalogProduct(product_id="tomatoes", name="Томаты свежие", unit="кг"),
        CatalogProduct(product_id="herbs", name="Зелень", unit="кг"),
    ]
    before = [
        (item.id, item.quantity, item.unit, item.catalog_product_id, item.catalog_name)
        for item in state.cart
    ]
    before_stage = state.stage

    result = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.EDIT_COMMENT,
            comment_action="remove",
            comment_scope="order",
        ),
        state,
        catalog,
    )

    after = [
        (item.id, item.quantity, item.unit, item.catalog_product_id, item.catalog_name)
        for item in result.state.cart
    ]
    assert after == before
    assert [item.comment for item in result.state.cart] == ["", ""]
    assert result.state.stage is before_stage
    assert "комментарий" in result.reply.text.lower()
    assert "удалён" in result.reply.text


def test_comment_correction_replaces_only_corrected_fact(settings) -> None:  # type: ignore[no-untyped-def]
    """Локальная правка комментария сохраняет независимые пожелания позиции."""
    engine = ConversationEngine(settings)
    state = _active_state()
    result = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.EDIT_COMMENT,
            comment_target_query="томаты",
            comment_text="не в красной упаковке, а в зелёной",
            comment_action="add",
            comment_scope="item",
        ),
        state,
        [CatalogProduct(product_id="tomatoes", name="Томаты свежие", unit="кг")],
    )

    comment = result.state.cart[0].comment
    assert "спелые" in comment
    assert "без повреждений" in comment
    assert "в зелёной" in comment
    assert "в красной упаковке" not in comment
    assert result.state.cart[0].quantity == 4
    assert result.state.cart[0].catalog_product_id == "tomatoes"
