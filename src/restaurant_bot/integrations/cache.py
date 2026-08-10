from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from time import perf_counter
from typing import Any

import structlog
from redis import Redis
from redis.lock import Lock

from restaurant_bot.config import Settings
from restaurant_bot.domain.models import CatalogProduct
from restaurant_bot.integrations.google_sheets import GoogleSheetsError, GoogleSheetsGateway

logger = structlog.get_logger(__name__)


def google_submission_lock_key(spreadsheet_id: str) -> str:
    """Формирует отдельный ключ отправки для таблицы заведения."""
    target_id = spreadsheet_id.strip()
    if not target_id:
        raise GoogleSheetsError("Venue spreadsheet ID is required")
    digest = hashlib.sha256(target_id.encode("utf-8")).hexdigest()[:20]
    return f"lock:google-order-submission:{digest}"


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
        cached = self.redis.get(key)
        if cached and not force_refresh:
            catalog = self._deserialize(cached)
            logger.info(
                "catalog_cache_hit",
                spreadsheet_id=spreadsheet_id,
                product_count=len(catalog),
            )
            return catalog
        started_at = perf_counter()
        try:
            catalog = self.sheets.load_catalog(spreadsheet_id)
        except Exception as exc:
            if not cached:
                raise
            catalog = self._deserialize(cached)
            logger.warning(
                "catalog_refresh_failed_using_cache",
                spreadsheet_id=spreadsheet_id,
                product_count=len(catalog),
                error_type=type(exc).__name__,
            )
            return catalog
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

    @staticmethod
    def _deserialize(cached: str | bytes | bytearray) -> list[CatalogProduct]:
        """Восстанавливает проверенные товары из значения Redis."""
        payload = json.loads(cached)
        return [CatalogProduct.model_validate(item) for item in payload]

    def invalidate(self, spreadsheet_id: str) -> None:
        """Удаляет каталог заведения из кэша."""
        if not spreadsheet_id.strip():
            raise GoogleSheetsError("Venue spreadsheet ID is required")
        self.redis.delete(self._key(spreadsheet_id))
        logger.info("catalog_cache_invalidated", spreadsheet_id=spreadsheet_id)


class ChatLockBusyError(TimeoutError):
    """Сообщает, что другой обработчик временно владеет блокировкой чата."""


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
    if not acquired:
        logger.info(
            "telegram_chat_lock_busy",
            wait_ms=round((perf_counter() - started_at) * 1000),
        )
        raise ChatLockBusyError(f"Could not acquire lock for chat {chat_id}")
    logger.info(
        "telegram_chat_lock_acquired",
        wait_ms=round((perf_counter() - started_at) * 1000),
    )
    try:
        yield lock
    finally:
        if lock.owned():
            lock.release()
