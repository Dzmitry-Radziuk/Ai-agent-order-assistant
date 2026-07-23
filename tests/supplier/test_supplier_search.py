from restaurant_bot.domain.models import CatalogProduct
from restaurant_bot.services.matching import rank_candidates


def test_supplier_hint_limits_candidate_catalog() -> None:
    catalog = [
        CatalogProduct(product_id="rose-a", name="Сироп Роза", supplier="Поставщик А", unit="шт"),
        CatalogProduct(product_id="rose-b", name="Сироп Роза", supplier="Поставщик Б", unit="шт"),
    ]

    candidates = rank_candidates("сироп роза", catalog, "Поставщик Б")

    assert [candidate.product_id for candidate in candidates] == ["rose-b"]
