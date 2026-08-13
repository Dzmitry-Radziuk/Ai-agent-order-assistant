"""Проверяет поведение, связанное с модулем «test voice quantity context»."""

import pytest

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
from restaurant_bot.parsing.commands.api import infer_intent
from restaurant_bot.services.engine import ConversationEngine


def _voice(text: str) -> TelegramEvent:
    """Создаёт тестовое голосовое событие Telegram."""
    return TelegramEvent(
        update_id=1, chat_id="voice-quantity", input_type=InputKind.VOICE, text=text
    )


def _multiple_state(engine: ConversationEngine) -> ConversationState:
    """Создаёт состояние уточнения кратности товара."""
    item = engine._build_item(ExtractedItem(product_query="Глазной мускул", quantity=5, unit="кг"))
    item.id = "multiple"
    item.status = ItemStatus.MATCHED
    item.catalog_product_id = "beef"
    item.catalog_name = "Глазной мускул"
    item.catalog_unit = "кг"
    item.minimum_multiple = 20
    item.existing_quantity = 20
    item.suggested_quantity = 20
    return ConversationState(current_issue_item_id=item.id, cart=[item])


def _unit_mismatch_state(engine: ConversationEngine) -> ConversationState:
    """Создаёт состояние уточнения единицы измерения."""
    item = engine._build_item(ExtractedItem(product_query="Курица", quantity=4, unit="шт"))
    item.id = "unit"
    item.status = ItemStatus.UNIT_MISMATCH
    item.catalog_product_id = "chicken"
    item.catalog_name = "Курица"
    item.catalog_unit = "кг"
    return ConversationState(current_issue_item_id=item.id, cart=[item])


def _missing_quantity_state(engine: ConversationEngine) -> ConversationState:
    """Создаёт карточку товара без количества на финальной проверке."""
    item = engine._build_item(ExtractedItem(product_query="Вино белое"))
    item.id = "wine"
    item.status = ItemStatus.MISSING_QTY
    item.catalog_product_id = "wine-product"
    item.catalog_name = "Вино белое"
    item.catalog_unit = "шт"
    return ConversationState(
        stage=SessionStage.REVIEW,
        current_issue_item_id=item.id,
        cart=[item],
    )


def test_voice_generic_correction_repeats_fix_quantity_button(settings) -> None:  # type: ignore[no-untyped-def]
    """Открывает варианты и не меняет количество без решения пользователя."""
    engine = ConversationEngine(settings)

    result = engine.handle(
        _voice("давайте исправим количество"),
        ParsedCommand(intent=Intent.UNKNOWN),
        _multiple_state(engine),
        [],
    )

    assert result.state.stage.value == "await_submit_confirm"
    assert result.state.cart[0].quantity == 5
    assert "Выберите количество" in result.reply.text
    assert [row[0].text for row in result.reply.rows[:3]] == [
        "Выбрать 20 кг",
        "Ввести другое количество",
        "Оставить 5 кг",
    ]


def test_voice_explicit_other_quantity_opens_manual_input(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что голос явный другое количество открывает ручной ввод."""
    engine = ConversationEngine(settings)

    result = engine.handle(
        _voice("давайте укажем другое количество"),
        ParsedCommand(intent=Intent.ENTER_OTHER_QUANTITY),
        _multiple_state(engine),
        [],
    )

    assert result.state.stage.value == "await_multiple_quantity"
    assert result.state.cart[0].quantity == 5
    assert "Укажите другое количество" in result.reply.text


def test_voice_fix_from_final_review_opens_choice(settings) -> None:  # type: ignore[no-untyped-def]
    """Открывает выбор с финальной проверки, где текущая позиция не выбрана."""
    engine = ConversationEngine(settings)
    state = _multiple_state(engine)
    state.current_issue_item_id = ""

    result = engine.handle(
        _voice("давай поменяем количество"),
        ParsedCommand(intent=Intent.UNKNOWN, text="давай поменяем количество"),
        state,
        [],
    )

    assert result.state.cart[0].quantity == 5
    assert result.state.current_issue_item_id == "multiple"
    assert "Выберите количество" in result.reply.text


def test_voice_choice_numbers_repeat_all_quantity_buttons(settings) -> None:  # type: ignore[no-untyped-def]
    """Распознаёт голосовые номера трёх вариантов количества."""
    engine = ConversationEngine(settings)

    first = engine.handle(
        _voice("давай первый вариант"),
        ParsedCommand(intent=Intent.SELECT_CANDIDATE, selected_index=1),
        _multiple_state(engine),
        [],
    )
    assert first.state.cart[0].quantity == 20

    second = engine.handle(
        _voice("выбираю второй вариант"),
        ParsedCommand(intent=Intent.SELECT_CANDIDATE, selected_index=2),
        _multiple_state(engine),
        [],
    )
    assert second.state.cart[0].quantity == 5
    assert second.state.stage.value == "await_multiple_quantity"

    third = engine.handle(
        _voice("оставим третий вариант"),
        ParsedCommand(intent=Intent.SELECT_CANDIDATE, selected_index=3),
        _multiple_state(engine),
        [],
    )
    assert third.state.cart[0].quantity == 5
    assert third.state.cart[0].suggested_quantity is None


def test_voice_explicit_number_changes_only_current_multiple(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что голос явный число изменяет только текущий кратность."""
    engine = ConversationEngine(settings)
    state = _multiple_state(engine)
    second = engine._build_item(ExtractedItem(product_query="Другой товар", quantity=7, unit="кг"))
    second.id = "other"
    second.status = ItemStatus.MATCHED
    state.cart.append(second)

    result = engine.handle(
        _voice("исправь на 40 килограммов"), ParsedCommand(intent=Intent.UNKNOWN), state, []
    )

    assert result.state.cart[0].quantity == 40
    assert result.state.cart[1].quantity == 7


def test_voice_add_without_product_word_accepts_current_multiple_recommendation(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что голос добавление без товар слово принимает текущий кратность recommendation."""
    engine = ConversationEngine(settings)

    result = engine.handle(
        _voice("добавь недостающее"),
        ParsedCommand(intent=Intent.ADD_MORE),
        _multiple_state(engine),
        [],
    )

    assert result.state.cart[0].quantity == 20
    assert result.state.cart[0].status is ItemStatus.MATCHED


def test_voice_add_more_products_is_not_consumed_as_quantity_correction(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что голос добавление ещё товары является не consumed как количество исправление."""
    engine = ConversationEngine(settings)

    result = engine.handle(
        _voice("добавить еще товары"),
        ParsedCommand(intent=Intent.ADD_MORE),
        _multiple_state(engine),
        [],
    )

    assert result.state.cart[0].quantity == 5
    assert result.state.stage.value == "collecting"


def test_voice_increment_phrase_adds_to_current_multiple_quantity(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что голос увеличение phrase добавляет в текущий кратность количество."""
    engine = ConversationEngine(settings)
    state = _multiple_state(engine)

    result = engine.handle(
        _voice("Добавить еще 15 килограмм"),
        infer_intent("Добавить еще 15 килограмм"),
        state,
        [],
    )

    assert result.state.cart[0].quantity == 20
    assert result.state.cart[0].status is ItemStatus.MATCHED


def test_voice_bare_contextual_add_accepts_missing_multiple(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что голос краткое контекстное добавление принимает отсутствующий кратность."""
    engine = ConversationEngine(settings)
    state = _multiple_state(engine)

    result = engine.handle(
        _voice("Добавить недостающее"),
        infer_intent("Добавить недостающее"),
        state,
        [],
    )

    assert result.state.cart[0].quantity == 20


def test_voice_unit_mismatch_accepts_catalog_unit_and_spoken_quantity(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что голос единица измерения mismatch принимает каталог единица измерения и произнесённый количество."""
    engine = ConversationEngine(settings)

    accepted = engine.handle(
        _voice("переведи в килограммы"),
        ParsedCommand(intent=Intent.UNKNOWN),
        _unit_mismatch_state(engine),
        [],
    )
    assert accepted.state.cart[0].status is ItemStatus.MATCHED
    assert accepted.state.cart[0].unit == "кг"

    changed = engine.handle(
        _voice("поставь 5 кг"),
        ParsedCommand(intent=Intent.UNKNOWN),
        _unit_mismatch_state(engine),
        [],
    )
    assert changed.state.cart[0].status is ItemStatus.MATCHED
    assert changed.state.cart[0].quantity == 5
    assert changed.state.cart[0].unit == "кг"


@pytest.mark.parametrize(
    "phrase",
    [
        "Ввести в килограмм",
        "Ввести в килограммы",
        "Ввести в килограммах",
        "Давайте укажем количество в килограммах",
        "Хочу указать вес в кг",
        "Лучше считать в килограммах",
        "Мне нужно в кг",
        "Поменяем единицы на килограммы",
    ],
)
def test_voice_unit_entry_phrases_open_quantity_input_without_new_items(
    settings,
    phrase: str,
) -> None:  # type: ignore[no-untyped-def]
    """Понимает живую речь как действие открытой карточки, а не новый товар."""
    engine = ConversationEngine(settings)
    state = _unit_mismatch_state(engine)
    state.stage = SessionStage.REVIEW
    command = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        text=phrase,
        items=[ExtractedItem(product_query=phrase)],
    )

    result = engine.handle(_voice(phrase), command, state, [])

    assert len(result.state.cart) == 1
    assert result.state.cart[0].status is ItemStatus.UNIT_MISMATCH
    assert result.state.stage is SessionStage.AWAIT_UNIT_QUANTITY
    assert "Укажите количество в кг" in result.reply.text


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("Поставь пять килограммов", 5),
        ("Давай один килограмм", 1),
        ("Мне нужно полтора кг", 1.5),
        ("Закажи 2 кг", 2),
        ("Пусть будет десять кило", 10),
        ("Возьмём три килограмма", 3),
        ("Укажи вес 4 кг", 4),
    ],
)
def test_voice_quantity_phrases_fix_open_unit_card_from_review(
    settings,
    phrase: str,
    expected: float,
) -> None:  # type: ignore[no-untyped-def]
    """Применяет произнесённое количество к текущей позиции из review."""
    engine = ConversationEngine(settings)
    state = _unit_mismatch_state(engine)
    state.stage = SessionStage.REVIEW
    command = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        text=phrase,
        items=[ExtractedItem(product_query=phrase)],
    )

    result = engine.handle(_voice(phrase), command, state, [])

    assert len(result.state.cart) == 1
    assert result.state.cart[0].status is ItemStatus.MATCHED
    assert result.state.cart[0].quantity == expected
    assert result.state.cart[0].unit == "кг"


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("10 килограмм", 10),
        ("десять килограмм", 10),
        ("10 кг", 10),
        ("нужно 10 кило", 10),
    ],
)
def test_unknown_voice_quantity_fills_current_missing_quantity(
    settings,
    phrase: str,
    expected: float,
) -> None:  # type: ignore[no-untyped-def]
    """Применяет короткий голосовой ответ к открытой карточке после unknown от AI."""
    engine = ConversationEngine(settings)
    state = _missing_quantity_state(engine)

    result = engine.handle(
        _voice(phrase),
        ParsedCommand(intent=Intent.UNKNOWN, text=phrase),
        state,
        [],
    )

    assert len(result.state.cart) == 1
    assert result.state.cart[0].quantity == expected
    assert result.state.cart[0].unit == "кг"
    assert result.state.cart[0].status is ItemStatus.UNIT_MISMATCH


@pytest.mark.parametrize("phrase", ["5", "пять", "один", "5 штук"])
def test_missing_quantity_number_overrides_candidate_intent(
    settings,
    phrase: str,
) -> None:  # type: ignore[no-untyped-def]
    """Считает короткий ответ количеством даже при ошибочном intent select_candidate."""
    engine = ConversationEngine(settings)
    state = _missing_quantity_state(engine)

    result = engine.handle(
        _voice(phrase),
        ParsedCommand(intent=Intent.SELECT_CANDIDATE, selected_index=1, text=phrase),
        state,
        [],
    )

    assert result.state.cart[0].quantity == (5 if phrase.startswith(("5", "пять")) else 1)
    assert result.state.cart[0].status is ItemStatus.MATCHED


def test_voice_unit_entry_removes_previous_false_command_items(settings) -> None:  # type: ignore[no-untyped-def]
    """Удаляет ложные товары, созданные прошлыми ответами на эту карточку."""
    engine = ConversationEngine(settings)
    state = _unit_mismatch_state(engine)
    state.stage = SessionStage.REVIEW
    for index, phrase in enumerate(("Ввести в килограммы", "Ввести в килограммах"), start=1):
        false_item = engine._build_item(ExtractedItem(product_query=phrase))
        false_item.id = f"false-{index}"
        false_item.status = ItemStatus.NOT_FOUND
        state.cart.append(false_item)

    phrase = "Хочу указать вес в килограммах"
    result = engine.handle(
        _voice(phrase),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            text=phrase,
            items=[ExtractedItem(product_query=phrase)],
        ),
        state,
        [],
    )

    assert [item.id for item in result.state.cart] == ["unit"]
    assert result.state.stage is SessionStage.AWAIT_UNIT_QUANTITY


def test_voice_product_list_is_not_consumed_by_open_quantity_card(settings) -> None:  # type: ignore[no-untyped-def]
    """Сохраняет весь новый список, даже если старому товару не хватало количества."""
    engine = ConversationEngine(settings)
    dill = engine._build_item(ExtractedItem(product_query="Укроп"))
    dill.id = "dill"
    dill.status = ItemStatus.MISSING_QTY
    dill.catalog_product_id = "dill-product"
    dill.catalog_name = "Укроп"
    dill.catalog_unit = "кг"
    state = ConversationState(cart=[dill], current_issue_item_id=dill.id)
    text = "Укроп 2 килограмма, петрушка 3 килограмма, сельдерей 5 килограммов."

    result = engine.handle(
        _voice(text),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            text=text,
            items=[
                ExtractedItem(product_query="Укроп", quantity=2, unit="кг"),
                ExtractedItem(product_query="Петрушка", quantity=3, unit="кг"),
                ExtractedItem(product_query="Сельдерей", quantity=5, unit="кг"),
            ],
        ),
        state,
        [
            CatalogProduct(product_id="dill-product", name="Укроп", unit="кг"),
            CatalogProduct(product_id="parsley", name="Петрушка", unit="кг"),
            CatalogProduct(product_id="celery", name="Сельдерей", unit="кг"),
        ],
    )

    assert [(item.catalog_product_id, item.quantity) for item in result.state.cart] == [
        ("dill-product", 2),
        ("parsley", 3),
        ("celery", 5),
    ]


@pytest.mark.parametrize("phrase", ["Ладно, бутылка.", "Одна бутылка."])
def test_voice_short_container_answer_fills_missing_quantity_in_review(
    settings,
    phrase: str,
) -> None:  # type: ignore[no-untyped-def]
    """Записывает одну текущую позицию для разговорного ответа с тарой."""
    engine = ConversationEngine(settings)
    state = _missing_quantity_state(engine)

    result = engine.handle(
        _voice(phrase),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            text=phrase,
            items=[ExtractedItem(product_query=phrase)],
        ),
        state,
        [],
    )

    assert len(result.state.cart) == 1
    assert result.state.cart[0].quantity == 1
    assert result.state.cart[0].unit == "шт"
    assert result.state.cart[0].status is ItemStatus.MATCHED
