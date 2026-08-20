"""Проверяет поведение, связанное с модулем «test add more prompt»."""

from restaurant_bot.domain.models import (
    CatalogProduct,
    ConversationState,
    DialogueResponse,
    ExtractedItem,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    SessionStage,
    TelegramEvent,
)
from restaurant_bot.input.telegram_callbacks import parse_callback
from restaurant_bot.parsing.commands.api import enrich_command, infer_intent
from restaurant_bot.services.engine import ConversationEngine


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


def test_successful_addition_returns_to_draft(settings) -> None:  # type: ignore[no-untyped-def]
    """Сразу возвращает пользователя в черновик после успешного добавления."""
    result = _add_syrup(settings)

    assert result.state.stage is SessionStage.REVIEW
    assert "<i>Товар добавлен</i>" in result.reply.text
    assert "Добавляйте товары текстом, голосом или фотографией списка" in result.reply.text
    assert [[button.text, button.callback_data] for row in result.reply.rows for button in row] == [
        ["Добавить в корзину и проверить", "v2:cart"],
        ["Сбросить и начать заново", "v2:clear"],
    ]


def test_voice_yes_continues_product_collection(settings) -> None:  # type: ignore[no-untyped-def]
    """Не требует промежуточного согласия после голосового добавления."""
    added = _add_syrup(settings)

    result = ConversationEngine(settings).handle(
        _voice("Да, давай добавим ещё", update_id=2),
        ParsedCommand(intent=Intent.CONFIRM, text="Да, давай добавим ещё"),
        added.state,
        [],
    )

    assert result.state.stage is SessionStage.REVIEW
    assert result.state.cart[0].status is ItemStatus.MATCHED
    assert "Добавить ещё товары?" not in result.reply.text


def test_text_yes_continues_product_collection(settings) -> None:  # type: ignore[no-untyped-def]
    """Текстовый ответ не открывает удалённый промежуточный вопрос."""
    added = _add_syrup(settings)
    phrase = "Да, давай добавим ещё"
    result = ConversationEngine(settings).handle(
        TelegramEvent(
            update_id=20,
            chat_id="add-more",
            input_type=InputKind.TEXT,
            text=phrase,
        ),
        infer_intent(phrase),
        added.state,
        [],
    )

    assert result.state.stage is SessionStage.REVIEW
    assert "Добавить ещё товары?" not in result.reply.text


def test_voice_no_returns_to_draft(settings) -> None:  # type: ignore[no-untyped-def]
    """Успешное добавление сразу показывает черновик без отказа от продолжения."""
    added = _add_syrup(settings)
    assert added.state.stage is SessionStage.REVIEW
    assert "<b>Сироп Роза, 1л</b> — 10 шт" in added.reply.text


def test_voice_submit_wins_over_wrong_add_more_intent(settings) -> None:  # type: ignore[no-untyped-def]
    """Открывает проверку заявки из черновика по явной команде пользователя."""
    added = _add_syrup(settings)
    phrase = "Покажи итог"

    result = ConversationEngine(settings).handle(
        _voice(phrase, update_id=4),
        ParsedCommand(intent=Intent.SHOW_FINAL_REVIEW, text=phrase),
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

    assert duplicate.state.stage is SessionStage.REVIEW
    assert duplicate.state.current_item() is not None
    assert duplicate.state.current_item().status is ItemStatus.DUPLICATE_PENDING
    assert "Товар уже есть в черновике" in duplicate.reply.text

    merged = engine.handle(
        _voice("Добавить", update_id=5),
        infer_intent("Добавить"),
        duplicate.state,
        [],
    )

    assert merged.state.cart[0].quantity == 20
    assert merged.state.cart[1].status is ItemStatus.SKIPPED
    assert merged.state.stage is SessionStage.REVIEW
    assert "Добавить ещё товары?" not in merged.reply.text


def test_add_more_dialogue_responses_are_normalized_before_state_policy() -> None:
    """Нормализует короткие ответы независимо от текущего modal-состояния."""
    expected = {
        "да": DialogueResponse.AFFIRM,
        "да, давай добавим ещё": DialogueResponse.AFFIRM,
        "давай ещё": DialogueResponse.AFFIRM,
        "нет": DialogueResponse.DECLINE,
        "нет, больше не надо": DialogueResponse.DECLINE,
        "хватит": DialogueResponse.DECLINE,
        "ну": DialogueResponse.UNCERTAIN,
        "не знаю": DialogueResponse.UNCERTAIN,
        "может быть": DialogueResponse.UNCERTAIN,
        "ладно": DialogueResponse.UNCERTAIN,
    }

    for phrase, response in expected.items():
        assert infer_intent(phrase).dialogue_response is response


def test_new_product_preempts_add_more_prompt_without_reusing_old_context(settings) -> None:
    """Новый товар прерывает вопрос и не наследует старый modal-контекст."""
    added = _add_syrup(settings)
    phrase = "Пармезан 3 кг"
    result = ConversationEngine(settings).handle(
        _voice(phrase, update_id=10),
        infer_intent(phrase),
        added.state,
        [
            CatalogProduct(
                product_id="parmesan",
                name="Пармезан",
                unit="кг",
                supplier="МБР",
            )
        ],
    )

    assert result.state.stage is SessionStage.REVIEW
    assert len(result.state.cart) == 2
    parmesan = result.state.cart[-1]
    assert parmesan.source_query == "Пармезан"
    assert parmesan.quantity == 3
    assert parmesan.unit == "кг"
    assert result.state.pending_added_items_count == 0


def test_uncertain_phrase_does_not_reopen_removed_prompt(settings) -> None:
    """Неуверенная фраза не возвращает удалённый промежуточный вопрос."""
    added = _add_syrup(settings)
    before = added.state.model_copy(deep=True)
    result = ConversationEngine(settings).handle(
        _voice("Спасибо", update_id=11),
        infer_intent("Спасибо"),
        added.state,
        [],
    )

    assert result.state.stage is SessionStage.REVIEW
    assert result.state.cart == before.cart
    assert result.state.pending_added_items_count == before.pending_added_items_count
    assert "Добавить ещё товары?" not in result.reply.text


def test_thanks_interrupts_add_more_prompt_without_adding_item(settings) -> None:
    """Независимая благодарность закрывает modal prompt без изменения корзины."""
    added = _add_syrup(settings)
    before = added.state.model_copy(deep=True)
    result = ConversationEngine(settings).handle(
        TelegramEvent(
            update_id=21,
            chat_id="add-more",
            input_type=InputKind.TEXT,
            text="Спасибо",
        ),
        infer_intent("Спасибо"),
        added.state,
        [],
    )

    assert result.state.stage is SessionStage.REVIEW
    assert result.state.cart == before.cart
    assert result.state.pending_added_items_count == 0


def test_add_more_callbacks_respect_revision(settings) -> None:
    """Свежие callbacks меняют modal-состояние, а устаревшие ничего не меняют."""
    added = _add_syrup(settings)
    added.state.ui_revision = 3
    stale = ConversationEngine(settings).handle(
        TelegramEvent(
            update_id=12,
            chat_id="add-more",
            input_type=InputKind.CALLBACK,
            callback_data="v2:add:r2",
        ),
        enrich_command("", parse_callback("v2:add:r2")),
        added.state,
        [],
    )
    assert stale.state.stage is SessionStage.REVIEW

    fresh = ConversationEngine(settings).handle(
        TelegramEvent(
            update_id=13,
            chat_id="add-more",
            input_type=InputKind.CALLBACK,
            callback_data="v2:add:r3",
        ),
        enrich_command("", parse_callback("v2:add:r3")),
        stale.state,
        [],
    )
    assert fresh.state.stage is SessionStage.COLLECTING
