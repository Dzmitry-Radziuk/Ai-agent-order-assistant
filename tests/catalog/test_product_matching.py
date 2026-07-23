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
    assert has_complete_query_evidence("сироп роза", "Сироп Роза, 1л")
    assert not has_complete_query_evidence("сироп роза", "Сироп Тархун, 1л")


def test_exact_product_is_auto_selected_but_category_query_is_not() -> None:
    catalog = [
        CatalogProduct(product_id="rose", name="Сироп Роза", supplier="Сиропы", unit="шт"),
        CatalogProduct(product_id="feijoa", name="Сироп Фейхоа", supplier="Сиропы", unit="шт"),
    ]

    assert can_auto_select(rank_candidates("сироп роза", catalog))
    assert not can_auto_select(rank_candidates("сироп", catalog))


def test_one_word_category_never_auto_selects_the_only_catalog_candidate(
    settings: Settings,
) -> None:
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
    product = CatalogProduct(product_id="milk", name="Молоко", unit="л")

    assert not is_broad_category_query("молоко", rank_candidates("молоко", [product]))


def test_unsupported_beef_qualifier_cannot_choose_between_equal_category_matches() -> None:
    catalog = [
        CatalogProduct(product_id="thin", name="Говядина Тонкий край", unit="кг"),
        CatalogProduct(product_id="bones", name="Говядина Кости ПРОДОЛЬНЫЙ распил", unit="кг"),
    ]
    candidates = rank_candidates("говядина мраморная", catalog)

    assert is_broad_category_query("говядина мраморная", candidates)


def test_unsupported_beef_qualifier_becomes_comment_before_candidate_choice(
    settings: Settings,
) -> None:
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
