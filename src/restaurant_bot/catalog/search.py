"""Определяет заменяемую границу получения кандидатов каталога."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from restaurant_bot.catalog.resolver import CatalogResolver, CatalogSearchResult
from restaurant_bot.domain.models import Candidate, CatalogProduct, SearchScope


class CatalogSearch(Protocol):
    """Описывает bounded-поиск без требования загрузить весь каталог в caller."""

    def search(
        self,
        query: str,
        *,
        supplier_hint: str = "",
        search_scope: SearchScope | None = None,
    ) -> CatalogSearchResult:
        """Возвращает ограниченный список кандидатов в нужной области."""

    def product_for(self, candidate: Candidate) -> CatalogProduct | None:
        """Возвращает полную строку каталога для выбранного кандидата."""


class CatalogSearchProvider(Protocol):
    """Поставляет только каталог, относящийся к указанной области поиска."""

    def __call__(self, search_scope: SearchScope | None) -> Sequence[CatalogProduct]:
        """Возвращает scoped-проекцию каталога."""


class ListCatalogSearch:
    """Адаптирует текущий list/cache каталог к заменяемому search-порту."""

    def __init__(
        self,
        resolver: CatalogResolver,
        provider: CatalogSearchProvider | Sequence[CatalogProduct],
    ) -> None:
        """Сохраняет resolver и источник scoped-каталога."""
        self._resolver = resolver
        if callable(provider):
            self._provider = provider
        else:
            products = tuple(provider)
            self._provider = lambda _scope: products
        self._last_catalog: tuple[CatalogProduct, ...] = ()

    def search(
        self,
        query: str,
        *,
        supplier_hint: str = "",
        search_scope: SearchScope | None = None,
    ) -> CatalogSearchResult:
        """Получает scoped-проекцию и применяет существующий resolver."""
        self._last_catalog = tuple(self._provider(search_scope))
        return self._resolver.search(
            query,
            list(self._last_catalog),
            supplier_hint,
            search_scope,
        )

    def product_for(self, candidate: Candidate) -> CatalogProduct | None:
        """Находит выбранный товар в последней scoped-проекции."""
        return self._resolver.product_for(candidate, list(self._last_catalog))


def list_catalog_provider(catalog: Sequence[CatalogProduct]) -> CatalogSearchProvider:
    """Создаёт provider для совместимого in-memory или cache каталога."""
    products = tuple(catalog)
    return lambda _scope: products
