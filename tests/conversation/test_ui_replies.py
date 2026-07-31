from restaurant_bot.domain.models import Candidate, CartItem, ConversationState, ItemStatus
from restaurant_bot.services.replies import cart_reply, help_reply, issue_reply


def test_help_explains_how_to_include_product_and_order_comments() -> None:
    """Справка показывает, как указать пожелания вместе с товарами."""
    reply = help_reply()
    text = reply.text.lower()

    assert "пожелание к товару — напишите его сразу после количества" in reply.text
    assert "общее пожелание — добавьте его в конце сообщения" in reply.text
    assert "курица 5 кг без кожи" in text
    assert "желательно на завтра" in text


def test_not_found_card_has_only_source_recovery_actions() -> None:
    """Проверяет, что не found карточка имеет только исходный восстановление действия."""
    item = CartItem(id="missing", source_query="Креветки королевские", status=ItemStatus.NOT_FOUND)

    reply = issue_reply(item, 0)

    assert reply.text == (
        "⚠️ <b>Товар не найден</b>\n\n"
        "По вашему запросу «<b>Креветки королевские</b>» ничего не найдено.\n\n"
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
        "По запросу «<b>сироп роза</b>» найдено несколько вариантов.\n\n"
        "Уточните, какой товар вы имели в виду:\n\n"
        "1. Сироп Роза, 1л\n\n"
        "2. Сироп Фейхоа, 1л\n\n"
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
        "🔎 <b>Точного совпадения не найдено</b>\n\n"
        "По запросу «<b>кукуруза спелая</b>» найден похожий товар.\n\n"
        "Возможно, вы имели в виду:\n\n"
        "1. Крупа кукурузная Алина 700г 1/7, шт\n\n"
        "Не нашли нужный вариант? Отправьте запрос менеджеру по снабжению."
    )


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

    assert "Соус &lt;острый&gt; &amp; сладкий" in not_found.text
    assert "Сироп &lt;роза&gt;" in ambiguous.text
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
        ["Отправить в корзину"],
        ["Добавить товары"],
        ["Начать заново"],
    ]
