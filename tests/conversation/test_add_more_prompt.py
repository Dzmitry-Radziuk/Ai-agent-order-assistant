from restaurant_bot.domain.models import (
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
from restaurant_bot.services.parser import infer_intent


def _voice(text: str, update_id: int = 1) -> TelegramEvent:
    """Создаёт голосовое событие для теста."""
    return TelegramEvent(
        update_id=update_id,
        chat_id="add-more",
        input_type=InputKind.VOICE,
        text=text,
    )


def _add_syrup(settings):  # type: ignore[no-untyped-def]
    """Добавляет сироп в пустой черновик."""
    return ConversationEngine(settings).handle(
        _voice("Сироп роза 10 штук"),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            text="Сироп роза 10 штук",
            items=[
                ExtractedItem(
                    product_query="Сироп роза",
                    quantity=10,
                    unit="шт",
                )
            ],
        ),
        ConversationState(),
        [
            CatalogProduct(
                product_id="syrup",
                name="Сироп Роза, 1л",
                unit="шт",
                supplier="МБР",
            )
        ],
    )


def test_successful_addition_asks_whether_to_add_more(settings) -> None:  # type: ignore[no-untyped-def]
    """Спрашивает о продолжении после успешного добавления."""
    result = _add_syrup(settings)

    assert result.state.stage is SessionStage.AWAIT_ADD_MORE_CONFIRM
    assert result.reply.text == (
        "✅ <b>Товар добавлен в черновик заказа</b>\n\nДобавить ещё товары?"
    )
    assert [[button.text, button.callback_data] for row in result.reply.rows for button in row] == [
        ["Да, добавить товары", "v2:add"],
        ["Нет, к черновику", "v2:back"],
    ]


def test_voice_yes_continues_product_collection(settings) -> None:  # type: ignore[no-untyped-def]
    """Продолжает добавление после голосового согласия."""
    added = _add_syrup(settings)

    result = ConversationEngine(settings).handle(
        _voice("Да, давай добавим ещё", update_id=2),
        ParsedCommand(intent=Intent.CONFIRM, text="Да, давай добавим ещё"),
        added.state,
        [],
    )

    assert result.state.stage is SessionStage.COLLECTING
    assert (
        result.reply.text == "Отправьте товары текстом, голосом или фото — я добавлю их в текущий "
        "черновик заказа."
    )


def test_voice_no_returns_to_draft(settings) -> None:  # type: ignore[no-untyped-def]
    """Возвращает черновик после голосового отказа."""
    added = _add_syrup(settings)
    phrase = "Нет, больше не надо"

    result = ConversationEngine(settings).handle(
        _voice(phrase, update_id=3),
        infer_intent(phrase),
        added.state,
        [],
    )

    assert result.state.stage is SessionStage.REVIEW
    assert "Черновик заявки" in result.reply.text
    assert "Сироп Роза, 1л — 10 шт" in result.reply.text


def test_voice_submit_wins_over_wrong_add_more_intent(settings) -> None:  # type: ignore[no-untyped-def]
    """Открывает проверку заявки, даже если ИИ ошибочно вернул добавление товаров."""
    added = _add_syrup(settings)
    phrase = "Да, отправляй"

    result = ConversationEngine(settings).handle(
        _voice(phrase, update_id=4),
        ParsedCommand(intent=Intent.ADD_MORE, text=phrase),
        added.state,
        [],
    )

    assert result.state.stage is SessionStage.AWAIT_SUBMIT_CONFIRM
    assert "Финальная проверка" in result.reply.text
    assert result.enqueue_submission is False


def test_product_sent_from_add_more_prompt_opens_duplicate_in_collecting_stage(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    """Сбрасывает вопрос о продолжении, когда пользователь сразу прислал товар."""
    engine = ConversationEngine(settings)
    added = _add_syrup(settings)
    phrase = "Сироп роза 10 штук"
    duplicate = engine.handle(
        _voice(phrase, update_id=4),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            text=phrase,
            items=[
                ExtractedItem(
                    product_query="Сироп роза",
                    quantity=10,
                    unit="шт",
                )
            ],
        ),
        added.state,
        [
            CatalogProduct(
                product_id="syrup",
                name="Сироп Роза, 1л",
                unit="шт",
                supplier="МБР",
            )
        ],
    )

    assert duplicate.state.stage is SessionStage.COLLECTING
    assert duplicate.state.current_item() is not None
    assert duplicate.state.current_item().status is ItemStatus.DUPLICATE_PENDING
    assert "Товар уже в черновике" in duplicate.reply.text

    merged = engine.handle(
        _voice("Добавить", update_id=5),
        infer_intent("Добавить"),
        duplicate.state,
        [],
    )

    assert merged.state.cart[0].quantity == 20
    assert merged.state.cart[1].status is ItemStatus.SKIPPED
    assert "Добавить ещё товары?" in merged.reply.text
