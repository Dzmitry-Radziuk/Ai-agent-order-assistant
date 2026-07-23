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
    item = CartItem(id="missing", source_query="Креветки королевские", status=ItemStatus.NOT_FOUND)

    reply = issue_reply(item, 0)

    assert "Товар не найден" in reply.text
    assert [[button.text for button in row] for row in reply.rows] == [
        ["Отправить запрос снабженцу"],
        ["Изменить название"],
        ["Не добавлять"],
    ]


def test_ambiguous_card_shows_only_catalog_choices_and_safe_recovery() -> None:
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
        "🔎 <b>Выберите подходящий товар</b>\n\n"
        "По запросу: сироп роза\n\n"
        "1. Сироп Роза, 1л\n\n"
        "2. Сироп Фейхоа, 1л\n\n"
        "Не нашли нужный товар среди вариантов? Отправьте запрос менеджеру по снабжению."
    )
    assert labels == [
        "1. Сироп Роза, 1л",
        "2. Сироп Фейхоа, 1л",
        "Отправить запрос снабженцу",
        "Изменить название",
        "Не добавлять",
    ]
    assert "Ввести иначе" not in labels


def test_draft_uses_source_button_names() -> None:
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
