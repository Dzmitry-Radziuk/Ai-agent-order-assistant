from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Event, Thread
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


class ChatLeaseLostError(RuntimeError):
    """Сообщает, что обработчик потерял право изменять данные чата."""


class ChatLease:
    """Продлевает Redis-блокировку и проверяет её владение."""

    def __init__(self, lock: Lock, timeout: int, heartbeat_interval: float):
        """Создаёт возобновляемую блокировку чата."""
        self.lock = lock
        self.timeout = timeout
        self.heartbeat_interval = heartbeat_interval
        self._stop = Event()
        self._lost = Event()
        self._heartbeat = Thread(target=self._run_heartbeat, daemon=True)
        self._heartbeat.start()

    @property
    def lost(self) -> bool:
        """Возвращает признак потери владения блокировкой."""
        return self._lost.is_set()

    def ensure_owned(self) -> None:
        """Проверяет право владельца продолжать работу с чатом."""
        if self._lost.is_set():
            raise ChatLeaseLostError("Chat lease ownership was lost")
        try:
            owned = self.lock.owned()
        except Exception as exc:
            self._mark_lost(type(exc).__name__)
            raise ChatLeaseLostError("Chat lease ownership could not be verified") from exc
        if not owned:
            self._mark_lost("ownership_changed")
            raise ChatLeaseLostError("Chat lease ownership was lost")

    def refresh(self) -> bool:
        """Продлевает срок блокировки до исходной длительности."""
        if self._lost.is_set():
            return False
        try:
            self.ensure_owned()
            self.lock.extend(self.timeout, replace_ttl=True)
            logger.debug("telegram_chat_lease_renewed")
            return True
        except ChatLeaseLostError:
            return False
        except Exception as exc:
            self._mark_lost(type(exc).__name__)
            return False

    def close(self) -> None:
        """Останавливает продление и безопасно освобождает свою блокировку."""
        self._stop.set()
        self._heartbeat.join(timeout=min(max(self.heartbeat_interval * 2, 0.1), 1.0))
        if self._heartbeat.is_alive():
            logger.warning("telegram_chat_lease_release_deferred")
            return
        if self._lost.is_set():
            return
        try:
            if self.lock.owned():
                self.lock.release()
        except Exception as exc:
            logger.warning("telegram_chat_lease_release_skipped", error_type=type(exc).__name__)

    def _run_heartbeat(self) -> None:
        """Периодически продлевает блокировку до выхода из контекста."""
        while not self._stop.wait(self.heartbeat_interval):
            if not self.refresh():
                return

    def _mark_lost(self, reason: str) -> None:
        """Фиксирует потерю владения и записывает одно предупреждение."""
        if not self._lost.is_set():
            self._lost.set()
            logger.warning("telegram_chat_lease_lost", reason=reason)


@contextmanager
def chat_lock(
    redis: Redis[Any],
    chat_id: str,
    timeout: int = 120,
    *,
    heartbeat_interval: float | None = None,
) -> Iterator[ChatLease]:
    """Последовательно обрабатывает сообщения одного чата."""
    interval = timeout / 3 if heartbeat_interval is None else heartbeat_interval
    if interval <= 0 or interval > timeout / 3:
        raise ValueError("heartbeat_interval must be positive and no greater than timeout / 3")
    lock = redis.lock(
        f"restaurant-bot:chat-lock:{chat_id}",
        timeout=timeout,
        blocking_timeout=15,
        thread_local=False,
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
    lease = ChatLease(lock, timeout, interval)
    try:
        yield lease
    finally:
        lease.close()
