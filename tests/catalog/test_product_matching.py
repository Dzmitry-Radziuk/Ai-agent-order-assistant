from restaurant_bot.config import Settings
from restaurant_bot.domain.models import CartItem, CatalogProduct, ItemStatus
from restaurant_bot.services.engine import ConversationEngine
from restaurant_bot.services.matching import (
    can_auto_select,
    has_complete_query_evidence,
    is_broad_category_query,
    rank_candidates,
)


def test_complete_query_evidence_rejects_category_only_match() -> None:
    """Проверяет, что полный query evidence отклоняет категория только сопоставление."""
    assert has_complete_query_evidence("сироп роза", "Сироп Роза, 1л")
    assert not has_complete_query_evidence("сироп роза", "Сироп Тархун, 1л")


def test_exact_product_is_auto_selected_but_category_query_is_not() -> None:
    """Проверяет, что точный товар является auto выбранный but категория query является не."""
    catalog = [
        CatalogProduct(product_id="rose", name="Сироп Роза", supplier="Сиропы", unit="шт"),
        CatalogProduct(product_id="feijoa", name="Сироп Фейхоа", supplier="Сиропы", unit="шт"),
    ]

    assert can_auto_select(rank_candidates("сироп роза", catalog))
    assert not can_auto_select(rank_candidates("сироп", catalog))


def test_one_word_category_never_auto_selects_the_only_catalog_candidate(
    settings: Settings,
) -> None:
    """Проверяет, что один слово категория никогда не auto выбирает только каталог кандидат."""
    engine = ConversationEngine(settings)
    cases = [
        ("говядина", CatalogProduct(product_id="beef", name="Говядина Тонкий край", unit="кг")),
        ("сироп", CatalogProduct(product_id="rose", name="Сироп Роза, 1л", unit="шт")),
    ]

    for query, product in cases:
        item = CartItem(id=query, source_query=query, quantity=10, unit=product.unit)
        engine._match_item(item, [product])

        assert item.status is ItemStatus.AMBIGUOUS
        assert item.catalog_product_id == ""
        assert [candidate.product_id for candidate in item.candidates] == [product.product_id]


def test_inflected_one_word_category_never_auto_selects_catalog_variant(
    settings: Settings,
) -> None:
    """Проверяет, что склонённая один слово категория никогда не auto выбирает каталог вариант."""
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(product_id="thin", name="Говядина Тонкий край", unit="кг"),
        CatalogProduct(product_id="bones", name="Говядина Кости ПРОДОЛЬНЫЙ распил", unit="кг"),
    ]
    item = CartItem(id="beef", source_query="говядины", quantity=10, unit="кг")

    engine._match_item(item, catalog)

    assert item.status is ItemStatus.AMBIGUOUS
    assert item.catalog_product_id == ""
    assert [candidate.product_id for candidate in item.candidates] == ["thin", "bones"]


def test_exact_one_word_catalog_product_is_not_treated_as_a_category() -> None:
    """Проверяет, что точный один слово каталог товар является не treated как a категория."""
    product = CatalogProduct(product_id="milk", name="Молоко", unit="л")

    assert not is_broad_category_query("молоко", rank_candidates("молоко", [product]))


def test_unsupported_beef_qualifier_cannot_choose_between_equal_category_matches() -> None:
    """Проверяет, что неподдерживаемый говядина qualifier не может choose between equal категория соответствует."""
    catalog = [
        CatalogProduct(product_id="thin", name="Говядина Тонкий край", unit="кг"),
        CatalogProduct(product_id="bones", name="Говядина Кости ПРОДОЛЬНЫЙ распил", unit="кг"),
    ]
    candidates = rank_candidates("говядина мраморная", catalog)

    assert is_broad_category_query("говядина мраморная", candidates)


def test_unsupported_beef_qualifier_becomes_comment_before_candidate_choice(
    settings: Settings,
) -> None:
    """Проверяет, что неподдерживаемый говядина qualifier becomes комментарий до кандидат выбор."""
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(product_id="thin", name="Говядина Тонкий край", unit="кг"),
        CatalogProduct(product_id="bones", name="Говядина Кости ПРОДОЛЬНЫЙ распил", unit="кг"),
    ]
    item = CartItem(
        id="beef",
        source_query="говядина мраморная",
        quantity=10,
        unit="кг",
    )

    engine._match_item(item, catalog)

    assert item.source_query == "говядина"
    assert item.comment == "мраморная"
    assert item.status is ItemStatus.AMBIGUOUS
    assert item.catalog_product_id == ""


def test_misspelled_product_variant_is_not_moved_to_supplier_comment(
    settings: Settings,
) -> None:
    """Сохраняет опечатку варианта частью поискового запроса."""
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(product_id="rose", name="Сироп Роза, 1л", unit="шт"),
        CatalogProduct(product_id="tarhun", name="Сироп Тархун, 1л", unit="шт"),
        CatalogProduct(product_id="sangria", name="Сироп Сангрия, 1л", unit="шт"),
    ]
    item = CartItem(id="syrup", source_query="Сироп Снгря", quantity=2, unit="шт")

    engine._match_item(item, catalog)

    assert item.source_query == "Сироп Снгря"
    assert item.comment == ""
    assert item.status is ItemStatus.AMBIGUOUS
    assert item.candidates[0].product_id == "sangria"


def test_short_variant_typo_is_preserved_for_safe_candidate_resolution(
    settings: Settings,
) -> None:
    """Передаёт короткую опечатку подбору кандидата без потери слова."""
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(product_id="rose", name="Сироп Роза, 1л", unit="шт"),
        CatalogProduct(product_id="tarhun", name="Сироп Тархун, 1л", unit="шт"),
        CatalogProduct(product_id="feijoa", name="Сироп Фейхоа, 1л", unit="шт"),
        CatalogProduct(product_id="hazelnut", name="Сироп Фундук, 1л", unit="шт"),
        CatalogProduct(product_id="sangria", name="Сироп Сангрия, 1л", unit="шт"),
    ]
    item = CartItem(id="syrup", source_query="сироп рза")

    engine._match_item(item, catalog)

    assert item.source_query == "сироп рза"
    assert item.comment == ""
    assert item.catalog_product_id == ""
    assert item.status is ItemStatus.AMBIGUOUS
    assert item.candidates[0].product_id == "rose"


def test_multiword_product_family_requires_user_choice(settings: Settings) -> None:
    """Не выбирает первый вкус по общему названию товарной линейки."""
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(
            product_id="cherry-shiso",
            name="Кордиал ЛЬЮ Вишня/Шисо 1,0л",
            unit="шт",
        ),
        CatalogProduct(
            product_id="orange-vanilla",
            name="Кордиал ЛЬЮ Апельсин/Ваниль 1,0л",
            unit="шт",
        ),
        CatalogProduct(
            product_id="pear-tonka",
            name="Кордиал ЛЬЮ Груша/Тонка 1,0л",
            unit="шт",
        ),
    ]
    item = CartItem(id="cordial", source_query="Кордиал ЛЬЮ")

    engine._match_item(item, catalog)

    assert item.status is ItemStatus.AMBIGUOUS
    assert item.catalog_product_id == ""
    assert {candidate.product_id for candidate in item.candidates} == {
        "cherry-shiso",
        "orange-vanilla",
        "pear-tonka",
    }


def test_exact_product_inside_multiword_family_is_auto_selected(settings: Settings) -> None:
    """Выбирает точное полное название внутри товарной линейки."""
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(
            product_id="cherry-shiso",
            name="Кордиал ЛЬЮ Вишня/Шисо 1,0л",
            unit="шт",
        ),
        CatalogProduct(
            product_id="orange-vanilla",
            name="Кордиал ЛЬЮ Апельсин/Ваниль 1,0л",
            unit="шт",
        ),
    ]
    item = CartItem(
        id="cordial",
        source_query="Кордиал ЛЬЮ Вишня/Шисо 1,0л",
        quantity=2,
        unit="шт",
    )

    engine._match_item(item, catalog)

    assert item.status is ItemStatus.MATCHED
    assert item.catalog_product_id == "cherry-shiso"


def test_product_family_rule_scales_to_large_catalog(settings: Settings) -> None:
    """Сохраняет безопасный выбор при каталоге больше тысячи товаров."""
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(
            product_id=f"other-{index}",
            name=f"Прочий товар {index}",
            unit="шт",
        )
        for index in range(1_200)
    ]
    catalog.extend(
        CatalogProduct(
            product_id=f"cordial-{index}",
            name=f"Кордиал ЛЬЮ Вкус {index} 1,0л",
            unit="шт",
        )
        for index in range(8)
    )
    item = CartItem(id="cordial", source_query="Кордиал ЛЬЮ")

    engine._match_item(item, catalog)

    assert item.status is ItemStatus.AMBIGUOUS
    assert item.catalog_product_id == ""
    assert len(item.candidates) == 5
