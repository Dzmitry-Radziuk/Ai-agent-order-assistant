from unittest.mock import MagicMock

import pytest

from restaurant_bot.domain.models import CatalogProduct
from restaurant_bot.integrations.cache import CatalogCache
from restaurant_bot.integrations.google_sheets import GoogleSheetsError


def test_catalog_cache_is_isolated_by_spreadsheet(settings) -> None:  # type: ignore[no-untyped-def]
    redis = MagicMock()
    redis.get.return_value = None
    sheets = MagicMock()
    sheets.load_catalog.side_effect = [
        [CatalogProduct(product_id="a", name="Товар А")],
        [CatalogProduct(product_id="b", name="Товар Б")],
    ]
    cache = CatalogCache(settings, redis, sheets)

    first = cache.get("sheet-a")
    second = cache.get("sheet-b")

    assert first[0].product_id == "a"
    assert second[0].product_id == "b"
    assert sheets.load_catalog.call_args_list[0].args == ("sheet-a",)
    assert sheets.load_catalog.call_args_list[1].args == ("sheet-b",)
    assert redis.setex.call_args_list[0].args[0] != redis.setex.call_args_list[1].args[0]


def test_catalog_cache_rejects_missing_venue_spreadsheet(settings) -> None:  # type: ignore[no-untyped-def]
    """Не создаёт общий кэш для пользователя без привязки."""
    redis = MagicMock()
    sheets = MagicMock()
    cache = CatalogCache(settings, redis, sheets)

    with pytest.raises(GoogleSheetsError, match="spreadsheet ID is required"):
        cache.get("")

    redis.get.assert_not_called()
    sheets.load_catalog.assert_not_called()
