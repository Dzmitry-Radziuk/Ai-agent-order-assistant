"""Адаптер центрального справочника заведений."""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from typing import Any, ClassVar, cast
from urllib.parse import urlencode

import httpx
from redis import Redis

from restaurant_bot.config import Settings
from restaurant_bot.domain.text import clean_text, normalize_text
from restaurant_bot.venues.codes import normalize_code
from restaurant_bot.venues.contracts import Venue


class VenueDirectoryError(RuntimeError):
    """Сообщает об ошибке справочника заведений."""


def extract_spreadsheet_id(value: str) -> str:
    """Извлекает идентификатор Google-таблицы."""
    raw = clean_text(value)
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", raw)
    if match:
        return match.group(1)
    return raw if re.fullmatch(r"[A-Za-z0-9_-]{20,}", raw) else ""


class VenueDirectory:
    """Находит заведение по коду приглашения."""

    CACHE_KEY = "restaurant-bot:venue-directory:v1"
    HEADER_ALIASES: ClassVar[dict[str, tuple[str, ...]]] = {
        "code": ("Код", "Код заведения", "Invite код"),
        "name": (
            "Условное наз-ие заведения",
            "Условное название заведения",
            "Название заведения",
        ),
        "legal_name": (
            "Юр наз-ие компании",
            "Юр. Название компании",
            "Юридическое название",
        ),
        "spreadsheet": (
            "Ссылка на таблицу",
            "ID таблицы заведения",
            "Spreadsheet ID",
        ),
    }

    def __init__(self, settings: Settings, redis: Redis[Any], client: httpx.Client | None = None):
        """Инициализирует компонент."""
        self.settings = settings
        self.redis = redis
        self.client = client or httpx.Client(timeout=httpx.Timeout(10.0, connect=2.0))

    def find(self, code: str) -> list[Venue]:
        """Находит заведение по нормализованному коду."""
        wanted = normalize_code(code)
        return [venue for venue in self.all() if venue.code == wanted]

    def all(self, force_refresh: bool = False) -> list[Venue]:
        """Возвращает все заведения из кэша или справочника."""
        if not force_refresh and (cached := self.redis.get(self.CACHE_KEY)):
            payload = json.loads(cast(str | bytes | bytearray, cached))
            return [Venue(**item) for item in payload]
        try:
            response = self.client.get(self._directory_url())
            response.raise_for_status()
            venues = self.parse_gviz(response.text)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, VenueDirectoryError) as exc:
            raise VenueDirectoryError("venue directory is unavailable") from exc
        self.redis.setex(
            self.CACHE_KEY,
            self.settings.venue_directory_cache_ttl_seconds,
            json.dumps([asdict(venue) for venue in venues], ensure_ascii=False),
        )
        return venues

    def _directory_url(self) -> str:
        """Возвращает GViz URL справочника из URL или ID таблицы."""
        configured = clean_text(self.settings.google_venue_directory_url)
        if configured.startswith(("https://", "http://")):
            return configured
        spreadsheet_id = extract_spreadsheet_id(
            configured or self.settings.google_registration_spreadsheet_id
        )
        if not spreadsheet_id:
            raise VenueDirectoryError("venue directory is not configured")
        query = urlencode(
            {
                "tqx": "out:json",
                "sheet": self.settings.google_registration_sheet,
            }
        )
        return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/gviz/tq?{query}"

    @classmethod
    def parse_gviz(cls, text: str) -> list[Venue]:
        """Разбирает ответ публичного интерфейса GViz."""
        match = re.search(r"setResponse\((\{.*\})\);?\s*$", text.strip(), re.S)
        if not match:
            raise VenueDirectoryError("invalid GViz response")
        payload = json.loads(match.group(1))
        table = payload.get("table") or {}
        columns = table.get("cols") or []
        headers = [clean_text(column.get("label")) for column in columns]
        indexes: dict[str, int] = {}
        normalized_headers = {normalize_text(header): index for index, header in enumerate(headers)}
        for field, aliases in cls.HEADER_ALIASES.items():
            index = next(
                (
                    normalized_headers[normalize_text(alias)]
                    for alias in aliases
                    if normalize_text(alias) in normalized_headers
                ),
                None,
            )
            if index is None and field in {"code", "name", "spreadsheet"}:
                raise VenueDirectoryError(f"missing venue directory header: {aliases[0]}")
            if index is not None:
                indexes[field] = index

        venues: list[Venue] = []
        for raw_row in table.get("rows") or []:
            cells = raw_row.get("c") or []

            def value(field: str, row_cells: list[dict[str, Any] | None] = cells) -> str:
                """Возвращает нормализованное значение ячейки GViz."""
                index = indexes.get(field)
                if index is None or index >= len(row_cells) or not row_cells[index]:
                    return ""
                cell = row_cells[index]
                assert cell is not None
                return clean_text(cell.get("f") or cell.get("v"))

            code = normalize_code(value("code"))
            name = value("name")
            spreadsheet_url = value("spreadsheet")
            spreadsheet_id = extract_spreadsheet_id(spreadsheet_url)
            if not code or not name or not spreadsheet_id:
                continue
            venues.append(
                Venue(
                    code=code,
                    name=name,
                    legal_name=value("legal_name"),
                    spreadsheet_id=spreadsheet_id,
                    spreadsheet_url=spreadsheet_url,
                )
            )
        return venues
