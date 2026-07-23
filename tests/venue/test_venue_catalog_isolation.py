from unittest.mock import MagicMock

from restaurant_bot.domain.models import CatalogProduct
from restaurant_bot.integrations.cache import CatalogCache


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
