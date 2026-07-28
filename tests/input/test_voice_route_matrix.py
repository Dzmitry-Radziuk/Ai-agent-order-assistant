import pytest

from restaurant_bot.domain.models import Intent
from restaurant_bot.services.parser import infer_intent


@pytest.mark.parametrize(
    ("phrase", "intent"),
    [
        ("старт", Intent.GREETING),
        ("покажи помощь", Intent.HELP),
        ("покажи черновик", Intent.SHOW_CART),
        ("открой корзину", Intent.SHOW_CART),
        ("сбрось черновик", Intent.CLEAR_CART),
        ("новый заказ", Intent.START_NEW_ORDER),
        ("добавить товары", Intent.ADD_MORE),
        ("добавь товары", Intent.ADD_MORE),
        ("давай добавим товары", Intent.ADD_MORE),
        ("отправить в корзину", Intent.SUBMIT_REQUEST),
        ("отправить поставщику", Intent.SUBMIT_AS_IS),
        ("отменить", Intent.CANCEL),
    ],
)
def test_n8n_global_voice_routes_are_deterministic(phrase: str, intent: Intent) -> None:
    """Проверяет голосовой маршрут каждой команды меню до вызова ИИ."""
    command = infer_intent(phrase)

    assert command.intent is intent
    assert command.items == []


def test_voice_add_more_command_with_transcriber_terminal_punctuation() -> None:
    """Проверяет, что голос добавление ещё команда with транскриптор конечная punctuation."""
    command = infer_intent("Добавить еще товары.")

    assert command.intent is Intent.ADD_MORE
    assert command.items == []


@pytest.mark.parametrize(
    ("phrase", "intent"),
    [
        ("Ну давай добавим ещё товары, пожалуйста.", Intent.ADD_MORE),
        ("Хочу еще позиции", Intent.ADD_MORE),
        ("Можно добавить ещё что-нибудь?", Intent.ADD_MORE),
        ("Вернись в черновик", Intent.SHOW_CART),
        ("Открой текущую заявку", Intent.SHOW_CART),
        ("Удали всё из корзины", Intent.CLEAR_CART),
        ("Обнули всю заявку", Intent.CLEAR_CART),
        ("Проверь статус последней заявки", Intent.ORDER_STATUS),
        ("Проверь минималку", Intent.CHECK_MIN_SUM),
        ("Покажи, что нужно уточнить", Intent.CLARIFY_CURRENT),
        ("Поставь рекомендованное количество", Intent.ACCEPT_SUGGESTED_QUANTITY),
        ("Ничего не меняй", Intent.KEEP_CURRENT_QUANTITY),
        ("Используй как в каталоге", Intent.USE_CATALOG_UNIT),
        ("Отправь заявку как есть", Intent.SUBMIT_AS_IS),
        ("Риск принимаю", Intent.SUBMIT_AS_IS),
        ("Ничего не подходит", Intent.MANUAL_CURRENT),
        ("Не добавляй эту позицию", Intent.SKIP_CURRENT),
        ("Ну давай продолжим", Intent.CONTINUE_CURRENT),
        ("Новый заказ.", Intent.START_NEW_ORDER),
        ("Хочу оформить ещё одну заявку", Intent.START_NEW_ORDER),
        ("Начать заново", Intent.START_NEW_ORDER),
        ("Заказ заново", Intent.START_NEW_ORDER),
    ],
)
def test_live_voice_wording_routes_before_product_parsing(phrase: str, intent: Intent) -> None:
    """Проверяет, что реальная голос формулировка маршрутизирует до товар parsing."""
    command = infer_intent(phrase)

    assert command.intent is intent
    assert command.items == []


@pytest.mark.parametrize(
    ("phrase", "intent"),
    [
        ("Давай пойдём и добавим товары", Intent.ADD_MORE),
        ("Давай добавим товаров", Intent.ADD_MORE),
        ("Может, пойдём добавлять новые позиции?", Intent.ADD_MORE),
        ("Хотелось бы перейти к добавлению товаров", Intent.ADD_MORE),
        ("Давайте внесём ещё что-нибудь в заявку", Intent.ADD_MORE),
        ("Пойдём посмотрим наши статусы", Intent.ORDER_STATUS),
        ("Давай посмотрим наши статусы", Intent.ORDER_STATUS),
        ("Как там моя последняя заявка?", Intent.ORDER_STATUS),
        ("Можно вернуться и посмотреть, что у нас в черновике?", Intent.SHOW_CART),
        ("Показать товары", Intent.SHOW_CART),
        ("Давай посмотрим товары", Intent.SHOW_CART),
        ("Давай откроем список продуктов", Intent.SHOW_CART),
        ("Показать товары поставщика", Intent.CHECK_MIN_SUM),
        ("Давай посмотрим позиции этого поставщика", Intent.CHECK_MIN_SUM),
        ("Запросы снабженцу", Intent.PRODUCT_ADD_LIST),
        ("Посмотреть запросы", Intent.PRODUCT_ADD_LIST),
        ("Давай посмотрим запросы снабженцу", Intent.PRODUCT_ADD_LIST),
        ("Покажи наши запросы менеджеру", Intent.PRODUCT_ADD_LIST),
        ("Открой список запросов", Intent.PRODUCT_ADD_LIST),
        ("Давай полностью очистим текущую заявку", Intent.CLEAR_CART),
        ("Ну что, оформляем и отправляем заявку", Intent.SUBMIT_REQUEST),
        ("Хотелось бы перейти к финальной проверке", Intent.SHOW_FINAL_REVIEW),
        ("Давай посмотрим, что там с минималкой", Intent.CHECK_MIN_SUM),
        ("Вернёмся к минимальной сумме", Intent.CHECK_MIN_SUM),
        ("Повтори отправку", Intent.SUBMIT_AS_IS),
        ("Давай повторим отправку заявки", Intent.SUBMIT_AS_IS),
        ("Покажи проблемные позиции, разберёмся с ними", Intent.CLARIFY_CURRENT),
        ("Давай поставим рекомендованное количество", Intent.ACCEPT_SUGGESTED_QUANTITY),
        ("Давайте исправим количество", Intent.FIX_MULTIPLE),
        ("Давайте выберем количество", Intent.FIX_MULTIPLE),
        ("Я хочу выбрать количество", Intent.FIX_MULTIPLE),
        ("Можно подобрать количество?", Intent.FIX_MULTIPLE),
        ("Хочу поменять текущее количество", Intent.FIX_MULTIPLE),
        ("Оставим текущее количество как есть", Intent.KEEP_CURRENT_QUANTITY),
        ("Давайте оставим всё как было", Intent.KEEP_CURRENT_QUANTITY),
        ("Оставь как указано", Intent.KEEP_CURRENT_QUANTITY),
        ("Используем единицу из каталога", Intent.USE_CATALOG_UNIT),
        ("Хочу указать другое количество", Intent.ENTER_OTHER_QUANTITY),
        ("Ввести в килограммы", Intent.ENTER_OTHER_QUANTITY),
        ("Ввести в килограммах", Intent.ENTER_OTHER_QUANTITY),
        ("Давайте укажем количество в кг", Intent.ENTER_OTHER_QUANTITY),
        ("Давайте создадим следующий заказ", Intent.START_NEW_ORDER),
        ("Нужно сделать повторную заявку", Intent.START_NEW_ORDER),
    ],
)
def test_free_form_voice_navigation_ignores_fillers_and_word_order(
    phrase: str, intent: Intent
) -> None:
    """Распознаёт живые команды без перечисления каждой полной фразы."""
    command = infer_intent(phrase)

    assert command.intent is intent
    assert command.items == []


@pytest.mark.parametrize(
    "phrase",
    [
        "Добавь товар сироп роза",
        "Давай добавим молоко",
        "Добавь товар говядина 5 кг",
    ],
)
def test_free_form_navigation_does_not_consume_real_product_names(phrase: str) -> None:
    """Не принимает конкретный товар за переход к экрану добавления."""
    command = infer_intent(phrase)

    assert command.intent is Intent.ADD_ITEMS
    assert command.items


@pytest.mark.parametrize(
    "phrase",
    [
        "Не хочу новый заказ",
        "Не создавай новую заявку",
        "Новый заказ 5 кг",
        "Новый заказ соус 2 штуки",
    ],
)
def test_new_order_navigation_does_not_override_negation_or_product_quantity(
    phrase: str,
) -> None:
    """Не начинает новую заявку при отрицании или товарной строке с количеством."""
    command = infer_intent(phrase)

    assert command.intent is not Intent.START_NEW_ORDER
    if phrase.startswith("Не "):
        assert command.intent is Intent.CANCEL


@pytest.mark.parametrize(
    ("phrase", "selected_index"),
    [
        ("первый вариант", 1),
        ("второй вариант", 2),
        ("третий вариант", 3),
        ("четвёртый", 4),
        ("пятый вариант", 5),
        ("Давай первый вариант", 1),
        ("Беру номер два", 2),
        ("Мне третий", 3),
        ("Выбери вариант четыре", 4),
        ("Пятый", 5),
        ("Возьми третью", 3),
        ("Подойдёт первый", 1),
        ("Нужна четвёртая", 4),
    ],
)
def test_live_voice_candidate_selection_variants(phrase: str, selected_index: int) -> None:
    """Проверяет, что реальная голос кандидат выбор варианты."""
    command = infer_intent(phrase)

    assert command.intent is Intent.SELECT_CANDIDATE
    assert command.selected_index == selected_index


@pytest.mark.parametrize(
    ("phrase", "intent"),
    [
        ("Начнём", Intent.GREETING),
        ("Запускай", Intent.GREETING),
        ("Подскажи, что делать", Intent.HELP),
        ("Как сделать заказ", Intent.HELP),
        ("Что у меня в заявке", Intent.SHOW_CART),
        ("Что уже добавлено", Intent.SHOW_CART),
        ("Заявка ушла?", Intent.ORDER_STATUS),
        ("Что с моим заказом", Intent.ORDER_STATUS),
        ("Удали текущую заявку", Intent.CLEAR_CART),
        ("Я всё добавил", Intent.SUBMIT_REQUEST),
        ("Заявка готова", Intent.SUBMIT_REQUEST),
        ("Можно оформлять", Intent.SUBMIT_REQUEST),
        ("Предыдущий экран", Intent.BACK),
        ("Продолжим работу", Intent.CONTINUE_CURRENT),
        ("Покажи итог", Intent.SHOW_FINAL_REVIEW),
        ("Проверить перед отправкой", Intent.SHOW_FINAL_REVIEW),
        ("Что не найдено", Intent.CLARIFY_CURRENT),
        ("Покажи ошибки", Intent.CLARIFY_CURRENT),
        ("Введу название сам", Intent.MANUAL_CURRENT),
        ("Другой товар", Intent.MANUAL_CURRENT),
        ("Убери эту строку", Intent.SKIP_CURRENT),
        ("Исключи эту позицию", Intent.SKIP_CURRENT),
        ("Всё верно", Intent.CONFIRM),
        ("Да, всё верно", Intent.CONFIRM),
        ("Согласен", Intent.CONFIRM),
        ("Нет, спасибо", Intent.CANCEL),
        ("Не отправляй", Intent.CANCEL),
        ("Передумал", Intent.CANCEL),
    ],
)
def test_common_text_and_voice_phrasings_are_deterministic(
    phrase: str,
    intent: Intent,
) -> None:
    """Понимает частые живые формулировки до товарного парсинга."""
    command = infer_intent(phrase)

    assert command.intent is intent
    assert command.items == []


@pytest.mark.parametrize(
    "phrase",
    [
        "Сливки 33% 5 штук",
        "Сыр номер один 5 кг",
        "Старт 10 штук",
        "Другой товар 2 кг",
        "Согласен 3 упаковки",
    ],
)
def test_command_words_inside_product_lines_remain_products(phrase: str) -> None:
    """Не превращает товар с количеством в навигацию или выбор кнопки."""
    command = infer_intent(phrase)

    assert command.intent is Intent.ADD_ITEMS
    assert command.items
