"""Содержит нейтральные контракты подсистемы заведений."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class Venue:
    """Описывает заведение без привязки к каналу или инфраструктуре."""

    code: str
    name: str
    legal_name: str
    spreadsheet_id: str
    spreadsheet_url: str
