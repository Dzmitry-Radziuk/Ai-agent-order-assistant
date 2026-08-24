"""Проверяет поведение, связанное с модулем «test venue catalog isolation»."""

import json
from unittest.mock import MagicMock

import pytest

from restaurant_bot.domain.models import CatalogProduct
from restaurant_bot.integrations.cache import CatalogCache
from restaurant_bot.integrations.google_sheets import GoogleSheetsError


def test_catalog_cache_is_isolated_by_spreadsheet(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что кэш каталога изолирован по таблице заведения."""
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
    assert redis.setex.call_args_list[0].args[1] == 3600
    assert redis.setex.call_args_list[1].args[1] == 3600


def test_catalog_cache_rejects_missing_venue_spreadsheet(settings) -> None:  # type: ignore[no-untyped-def]
    """Не создаёт общий кэш для пользователя без привязки."""
    redis = MagicMock()
    sheets = MagicMock()
    cache = CatalogCache(settings, redis, sheets)

    with pytest.raises(GoogleSheetsError, match="spreadsheet ID is required"):
        cache.get("")

    redis.get.assert_not_called()
    sheets.load_catalog.assert_not_called()


def test_catalog_cache_invalidate_deletes_deterministic_key(settings) -> None:  # type: ignore[no-untyped-def]
    """Удаляет только детерминированный ключ каталога заведения."""
    redis = MagicMock()
    cache = CatalogCache(settings, redis, MagicMock())

    cache.invalidate("sheet-a")

    redis.delete.assert_called_once_with(cache._key("sheet-a"))


def test_forced_catalog_refresh_bypasses_cached_order_values(settings) -> None:  # type: ignore[no-untyped-def]
    """При финальной проверке читает актуальные значения из Google Sheets."""
    redis = MagicMock()
    redis.get.return_value = json.dumps(
        [
            CatalogProduct(
                product_id="beef",
                name="Говядина",
                supplier_current_sum=350,
            ).model_dump(mode="json")
        ],
        ensure_ascii=False,
    )
    sheets = MagicMock()
    sheets.load_catalog.return_value = [
        CatalogProduct(
            product_id="beef",
            name="Говядина",
            supplier_current_sum=2900,
        )
    ]

    catalog = CatalogCache(settings, redis, sheets).get("sheet-a", force_refresh=True)

    assert catalog[0].supplier_current_sum == 2900
    sheets.load_catalog.assert_called_once_with("sheet-a")


def test_forced_catalog_refresh_falls_back_to_cache_on_google_failure(settings) -> None:  # type: ignore[no-untyped-def]
    """Не прерывает заявку при временном сбое финального чтения Google Sheets."""
    cached = CatalogProduct(
        product_id="beef",
        name="Говядина",
        supplier_current_sum=2550,
    )
    redis = MagicMock()
    redis.get.return_value = json.dumps([cached.model_dump(mode="json")], ensure_ascii=False)
    sheets = MagicMock()
    sheets.load_catalog.side_effect = TimeoutError("temporary Google timeout")

    catalog = CatalogCache(settings, redis, sheets).get("sheet-a", force_refresh=True)

    assert catalog == [cached]
