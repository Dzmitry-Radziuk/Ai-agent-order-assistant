"""Проверяет общие границы количества, фасовки и пользовательского интерфейса."""

from restaurant_bot.catalog.evidence import numeric_evidence
from restaurant_bot.catalog.resolver import CatalogResolver
from restaurant_bot.domain.models import (
    CartItem,
    CatalogProduct,
    ConversationState,
    ExtractedItem,
    Intent,
)
from restaurant_bot.orders.catalog_resolution import CatalogResolutionService
from restaurant_bot.orders.quantity_provenance import (
    QuantityProvenance,
    reconcile_order_quantity_evidence,
)
from restaurant_bot.parsing.ai.reconciliation import recover_omitted_explicit_items
from restaurant_bot.parsing.products import parse_product_lines
from restaurant_bot.presentation.telegram.replies import final_review_reply, small_talk_reply


def test_catalog_identity_tail_is_not_created_as_a_second_item() -> None:
    """Объединяет брендовый хвост с товаром и сохраняет последнее количество заказа."""
    source = "Уксус яблочный шестипроцентный 0,5 литра ПЭТ 12 штук, Relish 3 штуки."
    items = [
        ExtractedItem(
            product_query="Уксус яблочный шестипроцентный 0,5 литра ПЭТ",
            quantity=12,
            unit="шт",
            source_line=source,
        ),
        ExtractedItem(product_query="Relish", quantity=3, unit="шт", source_line=source),
    ]
    catalog = [
        CatalogProduct(
            product_id="vinegar-relish",
            name="Уксус яблочный шестипроцентный 0,5 л ПЭТ*12 шт Relish",
            unit="шт",
        ),
        CatalogProduct(product_id="plain-vinegar", name="Уксус яблочный 1 л", unit="шт"),
    ]

    result = CatalogResolutionService(CatalogResolver()).merge_catalog_qualified_items(
        items,
        catalog,
    )

    assert len(result) == 1
    assert result[0].product_query.endswith("Relish")
    assert result[0].quantity == 3
    assert result[0].unit == "шт"


def test_catalog_identity_evidence_does_not_merge_independent_products() -> None:
    """Оставляет самостоятельные товары отдельными позициями без общего каталожного имени."""
    source = "Масло оливковое 2 литра, хлеб 3 штуки."
    items = [
        ExtractedItem(product_query="Масло оливковое", quantity=2, unit="л", source_line=source),
        ExtractedItem(product_query="хлеб", quantity=3, unit="шт", source_line=source),
    ]
    catalog = [
        CatalogProduct(product_id="oil", name="Масло оливковое", unit="л"),
        CatalogProduct(product_id="bread", name="Хлеб", unit="шт"),
    ]

    result = CatalogResolutionService(CatalogResolver()).merge_catalog_qualified_items(
        items,
        catalog,
    )

    assert len(result) == 2


def test_multword_quantity_item_is_not_merged_into_catalog_identity_tail() -> None:
    """Не объединяет самостоятельный многословный товар с предыдущей позицией."""
    items = [
        ExtractedItem(
            product_query="Тахини",
            quantity=1,
            unit="кг",
            source_line="Тахини 1 кг, паста фисташковая стопроцентная 3 кг.",
        ),
        ExtractedItem(
            product_query="паста фисташковая стопроцентная",
            quantity=3,
            unit="кг",
            source_line="Тахини 1 кг, паста фисташковая стопроцентная 3 кг.",
        ),
    ]
    catalog = [
        CatalogProduct(
            product_id="combined",
            name="Тахини паста фисташковая стопроцентная",
            unit="кг",
        )
    ]

    result = CatalogResolutionService(CatalogResolver()).merge_catalog_qualified_items(
        items,
        catalog,
    )

    assert [(item.product_query, item.quantity) for item in result] == [
        ("Тахини", 1),
        ("паста фисташковая стопроцентная", 3),
    ]


def test_catalog_identity_fragment_can_follow_a_split_ai_source_span() -> None:
    """Объединяет отдельный голосовой фрагмент, если каталог подтверждает единую позицию."""
    items = [
        ExtractedItem(
            product_query="Уксус яблочный шестипроцентный 0,5 литра ПЭТ",
            quantity=12,
            unit="шт",
            source_line="Уксус яблочный шестипроцентный 0,5 литра ПЭТ, 12 штук.",
            source_span="Уксус яблочный шестипроцентный 0,5 литра ПЭТ, 12 штук.",
        ),
        ExtractedItem(
            product_query="релиш",
            quantity=3,
            unit="шт",
            source_line="релиш 3 штуки.",
            source_span="релиш 3 штуки.",
        ),
    ]
    catalog = [
        CatalogProduct(
            product_id="vinegar-relish",
            name='Уксус "Яблочный" 6% 0,5л ПЭТ*12шт Relish',
            unit="шт",
        ),
        CatalogProduct(product_id="plain-vinegar", name="Уксус яблочный 1 л", unit="шт"),
    ]

    result = CatalogResolutionService(CatalogResolver()).merge_catalog_qualified_items(
        items,
        catalog,
    )

    assert len(result) == 1
    assert result[0].quantity == 3
    assert result[0].unit == "шт"


def test_packaging_preference_is_removed_from_catalog_query() -> None:
    """Сохраняет пожелание к упаковке отдельно от поискового названия товара."""
    source = "Нужен лук зелёный, 5 кг, в упаковках пластиковых или в контейнер."

    item = parse_product_lines(source)[0]

    assert item.product_query == "Нужен лук зелёный"
    assert item.comment == "в упаковках пластиковых или в контейнер"
    assert item.packaging_role == "user_preference"


def test_ai_query_with_packaging_tail_is_reconciled_to_canonical_product() -> None:
    """Удаляет упаковочный хвост из запроса ИИ и сохраняет пожелание пользователя."""
    source = "Нужен лук зелёный, 5 кг, в упаковках пластиковых или в контейнер."
    payload = {
        "intent": Intent.ADD_ITEMS.value,
        "global_comment": "",
        "items": [
            {
                "product_query": "лук зелёный в упаковках пластиковых или в контейнер",
                "quantity": 5,
                "unit": "кг",
                "comment": "",
                "user_comment_to_supplier": "",
                "source_line": source,
                "packaging_role": "none",
            }
        ],
    }

    item = recover_omitted_explicit_items(payload, source)["items"][0]

    assert item["product_query"] == "лук зелёный"
    assert item["comment"] == "в упаковках пластиковых или в контейнер"
    assert item["packaging_role"] == "user_preference"


def test_action_prefix_is_not_retained_in_product_query() -> None:
    """Удаляет слова действия из названия товара после восстановления ИИ."""
    source = "Добавь в заявку мясо мидий вес 5 кг."
    payload = {
        "intent": Intent.ADD_ITEMS.value,
        "global_comment": "",
        "items": [
            {
                "product_query": "в заявку мясо мидий вес",
                "quantity": 5,
                "unit": "кг",
                "comment": "",
                "source_line": source,
            }
        ],
    }

    item = recover_omitted_explicit_items(payload, source)["items"][0]

    assert item["product_query"] == "мясо мидий"


def test_explicit_quantity_followed_by_comment_beats_catalog_measurement() -> None:
    """Считает количество заказом, если после него явно указано пожелание."""
    source = "Икра чёрная осётр 5 килограмм, только крупная."

    authorization = reconcile_order_quantity_evidence(
        source,
        5,
        "кг",
        catalog_name="Икра чёрная осётр 5 кг",
        packaging_role="catalog_attribute",
    )

    assert authorization.quantity == 5
    assert authorization.unit == "кг"
    assert authorization.provenance is QuantityProvenance.ORDER


def test_root_processing_comment_does_not_erase_order_quantity() -> None:
    """Сохраняет количество лука перед комментарием о срезе корня."""
    item = CartItem(
        id="green-onion",
        source_query="лук зелёный",
        source_line="лук зелёный 10 килограмм, срез корня от 5 сантиметров",
        source_span="лук зелёный 10 килограмм",
        quantity=10,
        unit="кг",
        comment="срез корня от 5 сантиметров",
    )
    catalog = [
        CatalogProduct(
            product_id="green-onion",
            name="Лук зелёный 10 кг",
            unit="кг",
        )
    ]

    CatalogResolutionService(CatalogResolver()).match_item(item, catalog)

    assert item.quantity == 10
    assert item.unit == "кг"
    assert item.comment == "срез корня от 5 сантиметров"


def test_multi_item_voice_source_keeps_explicit_quantity_matching_packaging() -> None:
    """Сохраняет количество позиции в длинном списке при совпадении с фасовкой."""
    item = CartItem(
        id="kataifi",
        source_query="тесто катаифи",
        source_line="Тахини 1 кг, тесто катаифи 10 кг.",
        source_span="тесто катаифи 10 кг",
        quantity=10,
        unit="кг",
    )
    catalog = [
        CatalogProduct(
            product_id="kataifi",
            name="Тесто катаифи 10 кг",
            unit="шт",
        )
    ]

    CatalogResolutionService(CatalogResolver()).match_item(item, catalog)

    assert item.quantity == 10
    assert item.unit == "кг"


def test_numeric_evidence_keeps_decimal_measurement_and_following_numbers_separate() -> None:
    """Разделяет десятичную фасовку и последующие числовые признаки."""
    evidence = numeric_evidence("Соус чили сладкий 5,4 кг Таиланд 4 к 1")

    assert [(item.value, item.upper_value, item.unit) for item in evidence] == [
        (5.4, None, "кг"),
        (4.0, None, ""),
        (1.0, None, ""),
    ]
    assert evidence[0].raw == "5,4 кг"


def test_unrecognized_action_card_always_offers_product_addition() -> None:
    """Показывает кнопку добавления товара даже без заполненного черновика."""
    reply = small_talk_reply(ConversationState())

    assert [[button.text for button in row] for row in reply.rows] == [["Добавить товары"]]


def test_final_review_explains_submission_and_return_to_draft() -> None:
    """Объясняет отправку заявки и доступные изменения до подтверждения."""
    reply = final_review_reply(ConversationState())

    assert "После нажатия «Отправить заявку» заявка будет передана в обработку." in reply.text
    assert "нажмите «К черновику»" in reply.text
    assert "изменить количество или удалить позицию" in reply.text
