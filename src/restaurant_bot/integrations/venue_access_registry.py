"""Адаптер центрального реестра доступа заведений."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

import structlog
from redis import Redis

from restaurant_bot.config import Settings
from restaurant_bot.domain.text import clean_text, normalize_text
from restaurant_bot.integrations.google_sheets import GoogleSheetsGateway
from restaurant_bot.integrations.venue_directory import VenueDirectoryError
from restaurant_bot.venues.codes import normalize_code

logger = structlog.get_logger(__name__)


@dataclass(slots=True, frozen=True)
class VenueAccessEntry:
    """Описывает право пользователя на работу с заведением."""

    channel: str
    user_id: str
    chat_id: str
    venue_code: str
    active: bool


class VenueAccessRegistry:
    """Читает права доступа из центрального листа и кратковременно кэширует их."""

    CACHE_KEY = "restaurant-bot:venue-access:v2"
    ACTIVE_VALUES = frozenset({"true", "истина", "да", "1", "активен", "yes"})
    VENUE_TYPES = frozenset({"заведение", "venue"})
    REQUIRED_HEADERS = (
        ("Тип компании", "company_type"),
        ("Канал", "channel"),
        ("Код", "Код заведения", "venue_code"),
        ("Chat ID", "chat_id"),
        ("User ID", "user_id"),
        ("Активен", "active"),
    )

    def __init__(
        self,
        settings: Settings,
        redis: Redis[Any],
        sheets: GoogleSheetsGateway,
    ):
        """Инициализирует компонент."""
        self.settings = settings
        self.redis = redis
        self.sheets = sheets

    def decision(
        self,
        user_id: str,
        chat_id: str,
        venue_code: str,
        *,
        force_refresh: bool = False,
    ) -> bool | None:
        """Возвращает решение реестра или None при недоступности Google."""
        try:
            entries = self._entries(force_refresh=force_refresh)
        except Exception as exc:
            logger.warning("venue_access_registry_unavailable", error_type=type(exc).__name__)
            return None
        identity = (
            "telegram",
            clean_text(user_id),
            clean_text(chat_id),
            normalize_code(venue_code),
        )
        matches = [
            entry
            for entry in entries
            if (entry.channel, entry.user_id, entry.chat_id, entry.venue_code) == identity
        ]
        if not matches:
            return False
        if len({entry.active for entry in matches}) > 1:
            logger.warning(
                "venue_access_registry_conflict",
                telegram_user_id=identity[1],
                venue_code=identity[3],
            )
            return False
        return all(entry.active for entry in matches)

    def invalidate(self) -> None:
        """Удаляет кэш после изменения регистрационного листа."""
        try:
            self.redis.delete(self.CACHE_KEY)
        except Exception as exc:
            logger.warning(
                "venue_access_cache_invalidation_failed",
                error_type=type(exc).__name__,
            )

    def _entries(self, *, force_refresh: bool) -> list[VenueAccessEntry]:
        """Возвращает нормализованный снимок прав из кэша или Google."""
        cached: str | bytes | bytearray | None = None
        if not force_refresh:
            try:
                cached = self.redis.get(self.CACHE_KEY)
            except Exception as exc:
                logger.warning("venue_access_cache_read_failed", error_type=type(exc).__name__)
        if cached:
            try:
                payload = json.loads(cached)
                if not isinstance(payload, list):
                    raise ValueError("venue access cache must contain a list")
                return [VenueAccessEntry(**item) for item in payload if isinstance(item, dict)]
            except (TypeError, ValueError, KeyError):
                self.invalidate()

        rows = self.sheets.read_venue_registrations()
        self._validate_headers(rows)
        entries = [entry for row in rows if (entry := self._entry(row)) is not None]
        try:
            self.redis.setex(
                self.CACHE_KEY,
                self.settings.venue_access_cache_ttl_seconds,
                json.dumps([asdict(entry) for entry in entries], ensure_ascii=False),
            )
        except Exception as exc:
            logger.warning("venue_access_cache_write_failed", error_type=type(exc).__name__)
        return entries

    @classmethod
    def _validate_headers(cls, rows: list[dict[str, Any]]) -> None:
        """Отклоняет другой лист вместо корректного реестра доступа."""
        if not rows:
            return
        headers = {normalize_text(header) for row in rows for header in row if clean_text(header)}
        missing = [
            aliases[0]
            for aliases in cls.REQUIRED_HEADERS
            if not any(normalize_text(alias) in headers for alias in aliases)
        ]
        if missing:
            raise VenueDirectoryError(
                f"venue access registry missing headers: {', '.join(missing)}"
            )

    @classmethod
    def _entry(cls, row: dict[str, Any]) -> VenueAccessEntry | None:
        """Преобразует строку регистрационного листа в право доступа."""

        def first(*keys: str) -> str:
            """Возвращает первое заполненное значение поддерживаемого столбца."""
            for key in keys:
                value = clean_text(row.get(key))
                if value:
                    return value
            return ""

        company_type = normalize_text(first("Тип компании", "company_type"))
        channel = normalize_text(first("Канал", "channel"))
        user_id = first("User ID", "user_id")
        chat_id = first("Chat ID", "chat_id")
        venue_code = normalize_code(first("Код", "Код заведения", "venue_code"))
        if (
            company_type not in cls.VENUE_TYPES
            or channel != "telegram"
            or not user_id
            or not chat_id
            or not venue_code
        ):
            return None
        active = normalize_text(first("Активен", "active")) in cls.ACTIVE_VALUES
        return VenueAccessEntry(
            channel=channel,
            user_id=user_id,
            chat_id=chat_id,
            venue_code=venue_code,
            active=active,
        )
