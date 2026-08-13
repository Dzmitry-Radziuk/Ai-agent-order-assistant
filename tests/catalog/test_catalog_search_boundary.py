"""Проверяет поведение, связанное с модулем «test catalog search boundary»."""

from restaurant_bot.catalog.resolver import CatalogResolver
from restaurant_bot.catalog.search import ListCatalogSearch
from restaurant_bot.domain.models import CatalogProduct, SearchScope


def test_scoped_catalog_search_accepts_bounded_provider_without_full_catalog_argument() -> None:
    """Проверяет заменяемый bounded-поиск на большой проекции каталога."""
    products = tuple(
        CatalogProduct(
            product_id=f"product-{index}",
            name=f"Товар номер {index}",
            supplier="Поставщик",
            unit="шт",
        )
        for index in range(100_000)
    )
    requested_scopes: list[SearchScope | None] = []

    def provider(scope: SearchScope | None) -> tuple[CatalogProduct, ...]:
        """Возвращает ограниченную проекцию для переданной области."""
        requested_scopes.append(scope)
        return products[:100]

    search = ListCatalogSearch(CatalogResolver(), provider)
    result = search.search(
        "Товар номер 42",
        search_scope=SearchScope.SUPPLIER_ONLY,
    )

    assert requested_scopes == [SearchScope.SUPPLIER_ONLY]
    assert len(result.candidates) <= 5
    assert any(candidate.product_id == "product-42" for candidate in result.candidates)
