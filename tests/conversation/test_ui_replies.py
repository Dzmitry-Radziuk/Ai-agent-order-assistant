"""Проверяет поведение, связанное с модулем «test ui replies»."""

from restaurant_bot.domain.models import (
    Candidate,
    CartItem,
    ConversationState,
    DepartmentQuantities,
    ItemStatus,
)
from restaurant_bot.presentation.telegram.formatting import heading, product_name
from restaurant_bot.presentation.telegram.replies import (
    cart_reply,
    department_selection_reply,
    final_review_reply,
    help_reply,
    issue_reply,
    small_talk_reply,
)


def test_product_names_and_headings_escape_before_formatting() -> None:
    """Экранирует пользовательский текст до добавления HTML-выделения."""
    assert product_name("Сыр <премиум> & соус") == "<b>Сыр &lt;премиум&gt; &amp; соус</b>"
    assert heading("Проверка <заявки>") == "<b><u>Проверка &lt;заявки&gt;</u></b>"


def test_department_selection_reply_paginates_all_distributed_items() -> None:
    """Показывает все распознанные позиции на страницах без скрытого хвоста."""
    state = ConversationState(
        cart=[
            CartItem(
                id=f"photo-{index}",
                source_query=f"Товар {index}",
                quantity=1,
                unit="шт",
                status=ItemStatus.MATCHED,
                department_quantities=DepartmentQuantities(
                    hall=index if index % 3 == 0 else None,
                    bar=index if index % 3 == 1 else None,
                    kitchen=index if index % 3 == 2 else None,
                ),
            )
            for index in range(1, 42)
        ],
        department_selection_page=0,
    )

    first_page = department_selection_reply(state)
    first_callbacks = {button.callback_data for row in first_page.rows for button in row}

    assert "Позиции 1–10 из 41" in first_page.text
    assert all(f"Товар {index}" in first_page.text for index in range(1, 11))
    assert "Товар 11" not in first_page.text
    assert "ещё" not in first_page.text
    assert "v2:deptpage:1" in first_callbacks
    assert "v2:dept:preserve" in first_callbacks

    state.department_selection_page = 1
    second_page = department_selection_reply(state)
    second_callbacks = {button.callback_data for row in second_page.rows for button in row}

    assert "Позиции 11–20 из 41" in second_page.text
    assert all(f"Товар {index}" in second_page.text for index in range(11, 21))
    assert "v2:deptpage:0" in second_callbacks
    assert "v2:deptpage:2" in second_callbacks

    state.department_selection_page = 4
    last_page = department_selection_reply(state)
    last_callbacks = {button.callback_data for row in last_page.rows for button in row}

    assert "Позиции 41–41 из 41" in last_page.text
    assert "Товар 41" in last_page.text
    assert "v2:deptpage:3" in last_callbacks
    assert "v2:deptpage:5" not in last_callbacks


def test_department_selection_reply_uses_singular_and_spacing_for_one_position() -> None:
    """Показывает одну позицию отдельным блоком с понятным заголовком."""
    state = ConversationState(
        cart=[
            CartItem(
                id="bread",
                source_query="Хлеб Бородинский",
                quantity=3,
                unit="шт",
                status=ItemStatus.MATCHED,
                department_quantities=DepartmentQuantities(bar=3),
            )
        ]
    )

    reply = department_selection_reply(state)

    assert "Позиция 1 из 1:\n\n• <b>Хлеб Бородинский</b>: Бар 3 шт\n\nЕсли" in reply.text
    assert "Позиции 1–1 из 1" not in reply.text


def test_final_review_does_not_repeat_quantity_for_single_department() -> None:
    """Показывает одно количество у товара, но сохраняет распределение по отделам."""
    state = ConversationState(
        cart=[
            CartItem(
                id="kitchen",
                source_query="Сыр",
                catalog_name="Сыр",
                quantity=6,
                unit="шт",
                status=ItemStatus.MATCHED,
                department_quantities=DepartmentQuantities(kitchen=6),
            ),
            CartItem(
                id="split",
                source_query="Хлеб",
                catalog_name="Хлеб",
                quantity=6,
                unit="шт",
                status=ItemStatus.MATCHED,
                department_quantities=DepartmentQuantities(hall=2, bar=4),
            ),
        ]
    )

    reply = final_review_reply(state)

    assert "1. <b>Сыр</b> — 6 шт · Кухня" in reply.text
    assert "Кухня 6 шт" not in reply.text
    assert "2. <b>Хлеб</b> — 6 шт · Зал 2 шт, Бар 4 шт" in reply.text


def test_department_selection_paginates_mixed_items_and_blocks_partial_preserve() -> None:
    """Показывает неразмеченный товар на следующей странице и не подтверждает его чужим отделом."""
    state = ConversationState(
        cart=[
            CartItem(
                id=str(index),
                source_query=f"Товар {index}",
                quantity=1,
                status=ItemStatus.MATCHED,
                department_quantities=DepartmentQuantities(bar=1),
            )
            for index in range(10)
        ]
        + [CartItem(id="last", source_query="Новый товар", quantity=2, status=ItemStatus.MATCHED)]
    )
    first = department_selection_reply(state)
    assert "Позиции 1–10 из 11" in first.text
    assert "v2:dept:preserve" not in [b.callback_data for row in first.rows for b in row]
    state.department_selection_page = 1
    last = department_selection_reply(state)
    assert "Новый товар" in last.text and "подразделение не указано" in last.text
    assert "Позиции 11–11 из 11" in last.text


def test_cart_and_final_review_limit_product_pages_to_ten_items() -> None:
    """Ограничивает карточки черновика и проверки десятью товарами на странице."""
    state = ConversationState(
        cart=[
            CartItem(
                id=f"item-{index}",
                source_query=f"Товар {index}",
                quantity=1,
                unit="шт",
                status=ItemStatus.MATCHED,
            )
            for index in range(11)
        ]
    )

    draft = cart_reply(state)
    final = final_review_reply(state)

    assert "Страница 1 из 2" in draft.text
    assert "Товар 9" in draft.text
    assert "Товар 10" not in draft.text
    assert "Страница 1 из 2" in final.text
    assert "Товар 9" in final.text
    assert "Товар 10" not in final.text


def test_help_explains_how_to_include_product_and_order_comments() -> None:
    """Справка показывает, как указать пожелания вместе с товарами."""
    reply = help_reply()
    text = reply.text.lower()

    assert "пожелание к товару — напишите его сразу после количества" in reply.text
    assert "общее пожелание — добавьте его в конце сообщения" in reply.text
    assert "курица 5 кг без кожи" in text
    assert "желательно на завтра" in text


def test_small_talk_reply_uses_neutral_action_free_wording() -> None:
    """Не описывает действие, которое бот ещё не определил, и не использует точку с запятой."""
    reply = small_talk_reply(ConversationState())

    assert "Сообщение не распознано как действие с заявкой." in reply.text
    assert "Заявка не изменена, товар не добавлен." in reply.text
    assert "открыть инструкцию — команда /help." in reply.text
    assert ";" not in reply.text


def test_not_found_card_has_only_source_recovery_actions() -> None:
    """Проверяет, что не found карточка имеет только исходный восстановление действия."""
    item = CartItem(id="missing", source_query="Креветки королевские", status=ItemStatus.NOT_FOUND)

    reply = issue_reply(item, 0)

    assert reply.text == (
        "🔸 <b><u>Товар не найден</u></b>\n\n"
        "По запросу «<b>Креветки королевские</b>» ничего не найдено.\n\n"
        "Вы можете изменить название, отправить запрос менеджеру по снабжению "
        "или не добавлять товар."
    )
    assert [[button.text for button in row] for row in reply.rows] == [
        ["Отправить запрос снабженцу"],
        ["Изменить название"],
        ["Не добавлять"],
    ]


def test_ambiguous_card_shows_only_catalog_choices_and_safe_recovery() -> None:
    """Проверяет, что неоднозначный карточка показывает только каталог choices и безопасный восстановление."""
    item = CartItem(
        id="ambiguous",
        source_query="сироп роза",
        status=ItemStatus.AMBIGUOUS,
        candidates=[
            Candidate(product_id="rose", name="Сироп Роза, 1л"),
            Candidate(product_id="feijoa", name="Сироп Фейхоа, 1л"),
        ],
    )

    reply = issue_reply(item, 0)
    labels = [button.text for row in reply.rows for button in row]

    assert reply.text == (
        "Найдено несколько вариантов товара.\n\n"
        "Уточните, какой товар вы имели в виду:\n\n"
        "1. <b>Сироп Роза, 1л</b>\n\n"
        "2. <b>Сироп Фейхоа, 1л</b>\n\n"
        "Не нашли нужный вариант? Отправьте запрос менеджеру по снабжению."
    )
    assert labels == [
        "1. Сироп Роза, 1л",
        "2. Сироп Фейхоа, 1л",
        "Отправить запрос снабженцу",
        "Изменить название",
        "Не добавлять",
    ]
    assert "Ввести иначе" not in labels


def test_empty_ambiguous_card_uses_not_found_recovery_copy() -> None:
    """Не называет выбором карточку без вариантов для выбора."""
    item = CartItem(
        id="empty-ambiguous",
        source_query="Неизвестный товар",
        status=ItemStatus.AMBIGUOUS,
    )

    reply = issue_reply(item, 0)

    assert "Найдено несколько вариантов товара." not in reply.text
    assert "Товар не найден" in reply.text
    assert [button.text for row in reply.rows for button in row] == [
        "Отправить запрос снабженцу",
        "Изменить название",
        "Не добавлять",
    ]


def test_single_ambiguous_candidate_is_presented_only_as_a_similar_product() -> None:
    """Объясняет, что единственная слабая находка является лишь подсказкой."""
    item = CartItem(
        id="corn",
        source_query="кукуруза спелая",
        status=ItemStatus.AMBIGUOUS,
        candidates=[
            Candidate(
                product_id="cornmeal",
                name="Крупа кукурузная Алина 700г 1/7, шт",
            )
        ],
    )

    reply = issue_reply(item, 0)

    assert reply.text == (
        "🔎 <b><u>Точного совпадения не найдено</u></b>\n\n"
        "Есть похожий вариант товара. Проверьте его:\n\n"
        "1. <b>Крупа кукурузная Алина 700г 1/7, шт</b>\n\n"
        "Не нашли нужный вариант? Отправьте запрос менеджеру по снабжению."
    )


def test_long_candidate_button_name_ends_with_ellipsis() -> None:
    """Сокращает длинное название в кнопке, сохраняя понятное начало."""
    item = CartItem(
        id="long-name",
        source_query="вино",
        status=ItemStatus.AMBIGUOUS,
        candidates=[
            Candidate(
                product_id="long-wine",
                name="Вино экстра брют белое Аристов Кюве Александр Блан де Блан 0,75 л",
            )
        ],
    )

    reply = issue_reply(item, 0)
    label = reply.rows[0][0].text

    assert label.startswith("1. Вино экстра брют")
    assert label.endswith("...")
    assert len(label) <= 42


def test_product_issue_cards_escape_user_and_catalog_text() -> None:
    """Экранирует пользовательский запрос и названия вариантов для Telegram HTML."""
    not_found = issue_reply(
        CartItem(
            id="missing",
            source_query="Соус <острый> & сладкий",
            status=ItemStatus.NOT_FOUND,
        ),
        2,
    )
    ambiguous = issue_reply(
        CartItem(
            id="ambiguous",
            source_query="Сироп <роза>",
            status=ItemStatus.AMBIGUOUS,
            candidates=[
                Candidate(product_id="rose", name="Сироп Роза & Мята"),
            ],
        ),
        3,
    )

    assert (
        "По запросу «<b>Соус &lt;острый&gt; &amp; сладкий</b>» ничего не найдено." in not_found.text
    )
    assert "Сироп &lt;роза&gt;" not in ambiguous.text
    assert "Сироп Роза &amp; Мята" in ambiguous.text
    assert [row[0].callback_data for row in not_found.rows] == [
        "v2:addreq:2",
        "v2:rename:2",
        "v2:skip:2",
    ]
    assert [row[0].callback_data for row in ambiguous.rows] == [
        "v2:sel:3:0",
        "v2:addreq:3",
        "v2:rename:3",
        "v2:skip:3",
    ]


def test_draft_uses_source_button_names() -> None:
    """Проверяет, что черновик использует исходный кнопка names."""
    state = ConversationState(
        cart=[
            CartItem(
                id="rose",
                source_query="Сироп Роза",
                catalog_name="Сироп Роза",
                quantity=5,
                unit="шт",
                status=ItemStatus.MATCHED,
            )
        ]
    )

    reply = cart_reply(state)

    assert [[button.text for button in row] for row in reply.rows] == [
        ["Добавить в корзину и проверить"],
        ["Сбросить и начать заново"],
    ]
    assert "Добавляйте товары текстом, голосом или фотографией списка" in reply.text


def test_item_comment_is_italic_and_rendered_under_product_in_draft_and_final_review() -> None:
    """Показывает комментарий под товаром одинаково в черновике и финальной проверке."""
    state = ConversationState(
        cart=[
            CartItem(
                id="rose",
                source_query="Сироп Роза",
                catalog_name="Сироп Роза",
                quantity=5,
                unit="шт",
                comment="на завтра и без замены",
                status=ItemStatus.MATCHED,
            )
        ]
    )

    draft = cart_reply(state)
    final = final_review_reply(state)
    expected = "<i>Комментарий: на завтра и без замены</i>"

    assert expected in draft.text
    assert expected in final.text
    assert draft.text.index("Сироп Роза") < draft.text.index(expected)
    assert final.text.index("Сироп Роза") < final.text.index(expected)


def test_comment_action_notice_is_italic_without_success_icon() -> None:
    """Показывает уведомление о комментарии без галочки и лишнего акцента."""
    reply = cart_reply(ConversationState(), notice="Комментарий добавлен")

    assert "<i>Комментарий добавлен</i>" in reply.text
    assert "✅" not in reply.text
