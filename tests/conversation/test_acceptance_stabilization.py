"""Приёмочные характеристики финальной готовности диалога."""

import pytest

from restaurant_bot.conversation.comments import reconcile_comment_target
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
from restaurant_bot.parsing.commands.api import infer_intent
from restaurant_bot.services.engine import ConversationEngine


def _event(text: str = "") -> TelegramEvent:
    """Создаёт текстовое событие для приёмочного сценария."""
    return TelegramEvent(update_id=901, chat_id="acceptance", input_type=InputKind.TEXT, text=text)


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
    event = _event("удали все комментарии")
    catalog = [
        CatalogProduct(product_id="tomatoes", name="Томаты свежие", unit="кг"),
        CatalogProduct(product_id="herbs", name="Зелень", unit="кг"),
    ]
    before = [
        (item.id, item.quantity, item.unit, item.catalog_product_id, item.catalog_name)
        for item in state.cart
    ]
    before_stage = state.stage
    command = infer_intent(event.text)
    assert command.intent is Intent.EDIT_COMMENT
    assert command.comment_action == "clear_all"
    assert command.comment_scope == "order"

    result = engine.handle(
        event,
        command,
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
    assert "Все пользовательские комментарии удалены" in result.reply.text


@pytest.mark.parametrize(
    "text",
    [
        "удали комментарии у всех товаров",
        "убери все комментарии",
        "очисти все комментарии",
        "удали все комментарии у всех товаров",
        "удали все комментарии из заявки",
    ],
)
def test_remove_all_comment_variants_use_order_scope(text: str) -> None:
    """Распознаёт естественные варианты удаления комментариев всей заявки."""
    command = infer_intent(text)

    assert command.intent is Intent.EDIT_COMMENT
    assert command.comment_action == "clear_all"
    assert command.comment_scope == "order"
    assert command.comment_target_query == ""
    assert command.items == []


def test_text_and_voice_comment_removal_have_same_semantics(settings) -> None:  # type: ignore[no-untyped-def]
    """Одинаково очищает комментарии после текста и расшифровки голоса."""
    text_command = infer_intent("удали все комментарии")
    voice_command = infer_intent("Удали все комментарии у всех товаров.")
    assert text_command.model_dump(exclude={"text"}) == voice_command.model_dump(exclude={"text"})

    engine = ConversationEngine(settings)
    text_result = engine.handle(
        _event("удали все комментарии"),
        text_command,
        _active_state(),
        [],
    )
    voice_event = TelegramEvent(
        update_id=902,
        chat_id="acceptance",
        input_type=InputKind.VOICE,
        text="Удали все комментарии у всех товаров.",
    )
    voice_result = engine.handle(voice_event, voice_command, _active_state(), [])

    def snapshot(result):  # type: ignore[no-untyped-def]
        """Собирает данные корзины для сравнения результатов каналов."""
        return [
            (
                item.id,
                item.quantity,
                item.unit,
                item.catalog_product_id,
                item.catalog_name,
                item.comment,
            )
            for item in result.state.cart
        ]

    assert snapshot(text_result) == snapshot(voice_result)
    assert "Все пользовательские комментарии удалены" in text_result.reply.text
    assert "Все пользовательские комментарии удалены" in voice_result.reply.text


def test_remove_all_comments_without_comments_is_safe(settings) -> None:  # type: ignore[no-untyped-def]
    """Не создаёт товар и не сообщает об отсутствии позиции без комментариев."""
    state = _active_state()
    for item in state.cart:
        item.comment = ""
        item.comment_source = CommentSource.NONE
    event = _event("удали все комментарии")

    result = ConversationEngine(settings).handle(
        event,
        infer_intent(event.text),
        state,
        [],
    )

    assert len(result.state.cart) == 2
    assert all(not item.comment for item in result.state.cart)
    assert "Не нашёл такую позицию" not in result.reply.text
    assert "Пользовательских комментариев для удаления не найдено" in result.reply.text


@pytest.mark.parametrize(
    ("text", "intent", "target"),
    [
        ("удали комментарий у огурцов", Intent.EDIT_COMMENT, "огурцов"),
        ("удали все комментарии у горчицы", Intent.EDIT_COMMENT, "горчицы"),
        ("удали огурцы", Intent.REMOVE_ITEM, "огурцы"),
        ("удали все товары", Intent.REMOVE_ITEM, "все товары"),
    ],
)
def test_item_removal_commands_do_not_become_comment_removal(
    text: str,
    intent: Intent,
    target: str,
) -> None:
    """Сохраняет отдельную семантику удаления товара и его комментария."""
    command = infer_intent(text)

    assert command.intent is intent
    if intent is Intent.EDIT_COMMENT:
        assert command.comment_action == "remove"
        assert command.comment_scope == "item"
        assert command.comment_target_query == target
    else:
        assert command.target_query == target


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


@pytest.mark.parametrize(
    ("target", "comment"),
    [
        ("бородинскому", "хлебу в кирпичиках"),
        ("хлебу", "бородинскому без нарезки"),
        ("бородинскому", "хлебу: в кирпичиках"),
        ("свежим", "огурцам только мелкие"),
    ],
)
@pytest.mark.parametrize("input_kind", [InputKind.TEXT, InputKind.VOICE])
def test_comment_target_reconciliation_keeps_product_words_out_of_comment(
    settings,
    target: str,
    comment: str,
    input_kind: InputKind,
) -> None:  # type: ignore[no-untyped-def]
    """Разделяет цель комментария по фактическому товару одинаково для текста и голоса."""
    product = "Хлеб Бородинский" if "хлеб" in f"{target} {comment}" else "Огурцы свежие"
    state = ConversationState(
        cart=[
            CartItem(
                id="comment-target",
                source_query=product,
                catalog_name=product,
                status=ItemStatus.MATCHED,
            )
        ]
    )
    command = ParsedCommand(
        intent=Intent.EDIT_COMMENT,
        comment_target_query=target,
        comment_text=comment,
        comment_action="add",
        comment_scope="item",
    )

    result = ConversationEngine(settings).handle(
        _event(f"{target} {comment}").model_copy(update={"input_type": input_kind}),
        command,
        state,
        [],
    )

    assert result.state.cart[0].comment in {"в кирпичиках", "без нарезки", "только мелкие"}
    assert "хлебу" not in result.state.cart[0].comment
    assert "бородинскому" not in result.state.cart[0].comment
    assert "огурцам" not in result.state.cart[0].comment


def test_comment_target_reconciliation_rejects_ambiguous_cart_boundary() -> None:
    """Не выбирает товар, если граница цели совпадает с несколькими позициями."""
    state = ConversationState(
        cart=[
            CartItem(id="one", source_query="Огурцы свежие", status=ItemStatus.MATCHED),
            CartItem(id="two", source_query="Огурцы тепличные", status=ItemStatus.MATCHED),
        ]
    )

    resolution = reconcile_comment_target(state, "огурцам", "только мелкие")

    assert resolution.ambiguous is True
    assert resolution.item is None

    removal = reconcile_comment_target(state, "огурцам", "")
    assert removal.ambiguous is True
    assert removal.item is None
