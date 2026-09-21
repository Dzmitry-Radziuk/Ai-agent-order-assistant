"""Проверяет поведение, связанное с модулем «test parser»."""

import pytest

from restaurant_bot.domain.models import Intent
from restaurant_bot.parsing.commands.api import infer_intent
from restaurant_bot.parsing.commands.item_commands import has_unrepresented_order_quantity_evidence
from restaurant_bot.parsing.products import parse_product_lines


def _assert_intent(phrase: str, expected: Intent) -> None:
    """Проверяет результат детерминированной маршрутизации фразы."""
    assert infer_intent(phrase).intent is expected


def test_parses_quantity_and_unit_after_product_name() -> None:
    """Проверяет, что парсер разбирает количество и единицу измерения после названия товара."""
    item = parse_product_lines("Сироп роза 10 штук")[0]
    assert item.product_query == "Сироп роза"
    assert item.quantity == 10
    assert item.unit == "шт"


def test_terminal_punctuation_is_not_a_supplier_comment() -> None:
    """Не превращает точку расшифровки голоса в комментарий."""
    item = parse_product_lines("Сыр швейцарский Сыробогатов 10 штук.")[0]

    assert (item.product_query, item.quantity, item.unit) == (
        "Сыр швейцарский Сыробогатов",
        10,
        "шт",
    )
    assert item.comment == ""
    assert item.user_comment_to_supplier == ""


def test_parses_multiple_products_in_one_message() -> None:
    """Проверяет, что разбирает кратность товары в один сообщение."""
    items = parse_product_lines("говядина 10 кг, курица 10 шт")
    assert [(item.product_query, item.quantity, item.unit) for item in items] == [
        ("говядина", 10, "кг"),
        ("курица", 10, "шт"),
    ]


@pytest.mark.parametrize(
    ("phrase", "product_query", "quantity", "unit"),
    [
        ("Хочу заказать морковь.", "морковь", None, ""),
        ("дабавь яблак 15 кг", "яблак", 15, "кг"),
        ("Я хочу заказать филе форели 2 кг.", "филе форели", 2, "кг"),
        ("Хочу заказать Coca-Cola 5 шт.", "Coca-Cola", 5, "шт"),
    ],
)
def test_explicit_order_leadin_is_not_part_of_product_query(
    phrase: str,
    product_query: str,
    quantity: float | None,
    unit: str,
) -> None:
    """Отделяет разговорную команду заказа от названия товара."""
    command = infer_intent(phrase)

    assert command.intent is Intent.ADD_ITEMS
    assert command.explicit_add_items is True
    assert len(command.items) == 1
    assert command.items[0].product_query == product_query
    assert command.items[0].quantity == quantity
    assert command.items[0].unit == unit


def test_incomplete_deterministic_list_yields_to_semantic_parser() -> None:
    """Не выдаёт повреждённый голосовой список за готовую команду добавления."""
    source = (
        "Для лосося 0.8-1.3 килограмма, мне нужно 10 килограмм, "
        "обязательно зачищенное, лук зеленый 5 килограмм, паста соевая – 10 штук."
    )
    deterministic_items = parse_product_lines(source)
    command = infer_intent(source)

    assert len(deterministic_items) == 1
    assert has_unrepresented_order_quantity_evidence(source, deterministic_items)
    assert command.intent is Intent.UNKNOWN
    assert command.items == []


@pytest.mark.parametrize(
    ("phrase", "expected_target", "expected_comment"),
    [
        ("для лосося обязательно зачищенное", "лосося", "обязательно зачищенное"),
        (
            "для лосося 0.8-1.3 кг, обязательно зачищенное",
            "",
            "обязательно зачищенное",
        ),
        ("добавь комментарий к лососю: нужно 10 кг", "лососю", "нужно 10 кг"),
    ],
)
def test_comment_command_keeps_safe_markerless_and_explicit_cases(
    phrase: str,
    expected_target: str,
    expected_comment: str,
) -> None:
    """Сохраняет комментарии без маркера, но не захватывает новый заказ."""
    command = infer_intent(phrase)

    assert command.intent is Intent.EDIT_COMMENT
    if expected_target:
        assert command.comment_target_query == expected_target
    assert command.comment_text == expected_comment


def test_submit_phrases_are_not_treated_as_product_search() -> None:
    """Проверяет, что фразы отправки не принимаются за поиск товара."""
    assert infer_intent("отправить").intent is Intent.SUBMIT_REQUEST
    assert infer_intent("отправить поставщику").intent is Intent.SUBMIT_AS_IS


def test_parses_trailing_supplier_comment_after_quantity() -> None:
    """Проверяет, что парсер разбирает комментарий поставщика после количества."""
    item = parse_product_lines("Сироп роза 5 шт, желательно охлаждённым")[0]
    assert (item.product_query, item.quantity, item.unit) == ("Сироп роза", 5, "шт")
    assert item.comment == "желательно охлаждённым"
    assert item.user_comment_to_supplier == "желательно охлаждённым"


def test_parses_spoken_word_quantity_between_product_and_comment() -> None:
    """Связывает словесное количество с товаром между разговорными паузами."""
    items = parse_product_lines("Сироп роза, одна штука, желательно холодный.")

    assert len(items) == 1
    assert (items[0].product_query, items[0].quantity, items[0].unit) == (
        "сироп роза",
        1,
        "шт",
    )
    assert items[0].comment == "желательно холодный"
    assert items[0].user_comment_to_supplier == items[0].comment


def test_parser_keeps_multiword_comment_after_quantity_without_punctuation() -> None:
    """Проверяет, что парсер сохраняет многофразовый комментарий после количества без знаков препинания."""
    item = parse_product_lines("Сироп роза 5 штук обязательно позвонить перед доставкой")[0]

    assert (item.product_query, item.quantity, item.unit) == ("Сироп роза", 5, "шт")
    assert item.comment == "обязательно позвонить перед доставкой"
    assert item.user_comment_to_supplier == item.comment


def test_parser_recovers_every_product_in_a_conjoined_spoken_list() -> None:
    """Проверяет, что парсер восстанавливает каждый товар в списке, соединённом союзом."""
    items = parse_product_lines("Сироп роза 10 штук говядина 5 кг и джем 10 штук")

    assert [(item.product_query, item.quantity, item.unit) for item in items] == [
        ("Сироп роза", 10, "шт"),
        ("говядина", 5, "кг"),
        ("джем", 10, "шт"),
    ]


@pytest.mark.parametrize(
    ("phrase", "target", "quantity", "unit"),
    [
        ("Поменяй количество сыра на 5 кг", "сыра", 5, "кг"),
        ("Измени у молока количество на 3 штуки", "молока", 3, "шт"),
        ("Для молока поставь 3 литра", "молока", 3, "л"),
        ("Молоко измени на 3 л", "молоко", 3, "л"),
        ("Поставь сливки 33% 5 штук", "сливки 33%", 5, "шт"),
        ("Поставь для сиропа фисташка 7 штук", "сиропа фисташка", 7, "шт"),
        ("Сироп фисташка сделай 7 штук", "сироп фисташка", 7, "шт"),
        ("Исправь сыр на пять килограммов", "сыр", 5, "кг"),
        ("Замени у сахара количество на двадцать пять кг", "сахара", 25, "кг"),
        ("Исправь на сорок килограммов", "", 40, "кг"),
    ],
)
def test_parses_quantity_edits_with_live_word_order(
    phrase: str,
    target: str,
    quantity: float,
    unit: str,
) -> None:
    """Сохраняет товар и число в разговорных командах изменения."""
    command = infer_intent(phrase)

    assert command.intent is Intent.EDIT_QUANTITY
    assert command.target_query == target
    assert command.edit_quantity == quantity
    assert command.edit_unit == unit
    assert command.items == []


@pytest.mark.parametrize(
    ("phrase", "target"),
    [
        ("Убери из заявки сыр", "сыр"),
        ("Убери крупу кукурузную из заявки", "крупу кукурузную"),
        ("Удали сыр из текущего заказа", "сыр"),
        ("Вычеркни курицу из моей корзины, пожалуйста", "курицу"),
        ("Давай уберём молоко из списка", "молоко"),
        ("Убери из списка товаров масло", "масло"),
        ("Не заказывай сахар в этой заявке", "сахар"),
        ("Удалить позицию молоко", "молоко"),
        ("Мне не нужен сахар", "сахар"),
        ("Исключить из черновика говядину", "говядину"),
    ],
)
def test_parses_single_product_removal_phrasings(phrase: str, target: str) -> None:
    """Проверяет, что разбирает один товар удаление phrasings."""
    command = infer_intent(phrase)

    assert command.intent is Intent.REMOVE_ITEM
    assert command.target_query == target
    assert command.target_queries == [target]


@pytest.mark.parametrize(
    "phrase",
    [
        "Удали все товары из черновика.",
        "Удали товары из черновика все.",
        "Удалив все товары из черновика.",
        "Удали всё из черновика.",
    ],
)
def test_parses_whole_draft_removal_as_clear_cart(phrase: str) -> None:
    """Распознаёт удаление всех позиций черновика как его полную очистку."""
    command = infer_intent(phrase)

    assert command.intent is Intent.CLEAR_CART
    assert command.items == []


def test_comment_label_is_not_saved_as_part_of_supplier_wish() -> None:
    """Удаляет служебную метку комментария и сохраняет предлог в пожелании."""
    command = infer_intent(
        "Добавь к тестовому товару комментарий: комментарий: в упаковках по 10 штук."
    )

    assert command.intent is Intent.EDIT_COMMENT
    assert command.comment_target_query == "тестовому товару"
    assert command.comment_text == "в упаковках по 10 штук"


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("пожалуйста, не отправляй эту заявку", Intent.CANCEL),
        ("я не хочу добавлять товары", Intent.CANCEL),
        ("не очищай черновик", Intent.CANCEL),
        ("не используй единицу из каталога", Intent.CANCEL),
        ("не выбирай второй вариант", Intent.CANCEL),
        ("не объединяй с текущим", Intent.CANCEL),
        ("не подтверждаю отправку", Intent.CANCEL),
        ("этот товар нам не нужен", Intent.SKIP_CURRENT),
        ("пожалуйста не добавляйте эту позицию", Intent.SKIP_CURRENT),
        ("давай не будем добавлять", Intent.CANCEL),
        ("не исправляй количество", Intent.KEEP_CURRENT_QUANTITY),
        ("пропускаем", Intent.SKIP_CURRENT),
        ("карзин", Intent.SHOW_CART),
    ],
)
def test_negated_actions_never_become_mutating_commands(
    phrase: str,
    expected: Intent,
) -> None:
    """Отрицание не должно превращаться в противоположное действие."""
    _assert_intent(phrase, expected)


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("отправляй заявку", Intent.SUBMIT_REQUEST),
        ("очисти черновик", Intent.CLEAR_CART),
        ("выбираю второй вариант", Intent.SELECT_CANDIDATE),
        ("используй единицу из каталога", Intent.USE_CATALOG_UNIT),
        ("добавить товары", Intent.ADD_MORE),
    ],
)
def test_positive_actions_remain_available_after_negation_guard(
    phrase: str,
    expected: Intent,
) -> None:
    """Защита от отрицаний не блокирует явные положительные команды."""
    _assert_intent(phrase, expected)


def test_spoken_product_list_keeps_each_local_comment() -> None:
    """Сохраняет пожелание возле каждого товара в длинной голосовой фразе."""
    command = infer_intent(
        "Добавь сироп розы 5 штук на завтра и сироп сангрия 10 штук, желательно холодным."
    )

    assert command.intent is Intent.ADD_ITEMS
    assert [
        (item.product_query, item.quantity, item.unit, item.comment) for item in command.items
    ] == [
        ("сироп розы", 5, "шт", "на завтра"),
        ("сироп сангрия", 10, "шт", "желательно холодным"),
    ]


def test_spoken_product_list_separates_local_and_global_comments() -> None:
    """Отделяет общий комментарий от локального и не создаёт лишний товар."""
    command = infer_intent(
        "Добавь сироп роза 5 штук в банках и сироп сангрия 10 штук. Всё желательно привезти завтра."
    )

    assert command.intent is Intent.ADD_ITEMS
    assert command.global_comment == "желательно привезти завтра"
    assert [(item.product_query, item.quantity, item.comment) for item in command.items] == [
        ("сироп роза в банках", 5, ""),
        ("сироп сангрия", 10, ""),
    ]


def test_spoken_product_list_keeps_sentence_comments_with_their_products() -> None:
    """Не переносит начало следующего товара в комментарий предыдущего."""
    command = infer_intent(
        "Сироп Роза 5 штук, желательно холодным. "
        "Сироп Тархун 10 штук в банках. "
        "Говядина, кости продольный распил 10 килограмм. "
        "И куриные лапы 15 штук. Желательно завтра с 9 до 14."
    )

    assert command.intent is Intent.ADD_ITEMS
    assert command.global_comment == ""
    assert [
        (item.product_query, item.quantity, item.unit, item.comment) for item in command.items
    ] == [
        ("Сироп Роза", 5, "шт", "желательно холодным"),
        ("Сироп Тархун в банках", 10, "шт", ""),
        ("Говядина, кости продольный распил", 10, "кг", ""),
        ("куриные лапы", 15, "шт", "Желательно завтра с 9 до 14"),
    ]
