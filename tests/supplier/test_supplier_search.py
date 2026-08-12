from restaurant_bot.catalog.retrieval import rank_candidates
from restaurant_bot.domain.models import CatalogProduct


def test_supplier_hint_limits_candidate_catalog() -> None:
    """Проверяет, что поставщик hint ограничивает кандидат каталог."""
    catalog = [
        CatalogProduct(product_id="rose-a", name="Сироп Роза", supplier="Поставщик А", unit="шт"),
        CatalogProduct(product_id="rose-b", name="Сироп Роза", supplier="Поставщик Б", unit="шт"),
    ]

    candidates = rank_candidates("сироп роза", catalog, "Поставщик Б")

    assert [candidate.product_id for candidate in candidates] == ["rose-b"]
