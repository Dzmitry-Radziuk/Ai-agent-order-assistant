from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from time import perf_counter
from typing import Any, cast

import structlog
from redis import Redis
from redis.lock import Lock

from restaurant_bot.config import Settings
from restaurant_bot.domain.models import CatalogProduct
from restaurant_bot.integrations.google_sheets import GoogleSheetsError, GoogleSheetsGateway

logger = structlog.get_logger(__name__)


class CatalogCache:
    """Кэширует каталоги заведений в Redis."""

    KEY = "restaurant-bot:catalog:v2"

    def __init__(self, settings: Settings, redis: Redis[Any], sheets: GoogleSheetsGateway):
        """Инициализирует компонент."""
        self.settings = settings
        self.redis = redis
        self.sheets = sheets

    def _key(self, spreadsheet_id: str) -> str:
        """Формирует ключ кэша каталога заведения."""
        digest = hashlib.sha256(spreadsheet_id.encode("utf-8")).hexdigest()[:20]
        return f"{self.KEY}:{digest}"

    def get(self, spreadsheet_id: str, force_refresh: bool = False) -> list[CatalogProduct]:
        """Возвращает каталог из кэша или обновляет его."""
        if not spreadsheet_id.strip():
            raise GoogleSheetsError("Venue spreadsheet ID is required")
        key = self._key(spreadsheet_id)
        if not force_refresh:
            cached = self.redis.get(key)
            if cached:
                payload = json.loads(cast(str | bytes | bytearray, cached))
                catalog = [CatalogProduct.model_validate(item) for item in payload]
                logger.info(
                    "catalog_cache_hit",
                    spreadsheet_id=spreadsheet_id,
                    product_count=len(catalog),
                )
                return catalog
        started_at = perf_counter()
        catalog = self.sheets.load_catalog(spreadsheet_id)
        self.redis.setex(
            key,
            self.settings.catalog_cache_ttl_seconds,
            json.dumps([item.model_dump(mode="json") for item in catalog], ensure_ascii=False),
        )
        logger.info(
            "catalog_cache_refreshed",
            spreadsheet_id=spreadsheet_id,
            product_count=len(catalog),
            duration_ms=round((perf_counter() - started_at) * 1000),
            forced=force_refresh,
        )
        return catalog

    def invalidate(self, spreadsheet_id: str) -> None:
        """Удаляет каталог заведения из кэша."""
        if not spreadsheet_id.strip():
            raise GoogleSheetsError("Venue spreadsheet ID is required")
        self.redis.delete(self._key(spreadsheet_id))
        logger.info("catalog_cache_invalidated", spreadsheet_id=spreadsheet_id)


@contextmanager
def chat_lock(redis: Redis[Any], chat_id: str, timeout: int = 120) -> Iterator[Lock]:
    """Последовательно обрабатывает сообщения одного чата."""
    lock = redis.lock(
        f"restaurant-bot:chat-lock:{chat_id}",
        timeout=timeout,
        blocking_timeout=15,
    )
    started_at = perf_counter()
    acquired = lock.acquire(blocking=True)
    logger.info(
        "telegram_chat_lock_acquired",
        wait_ms=round((perf_counter() - started_at) * 1000),
    )
    if not acquired:
        raise TimeoutError(f"Could not acquire lock for chat {chat_id}")
    try:
        yield lock
    finally:
        if lock.owned():
            lock.release()
