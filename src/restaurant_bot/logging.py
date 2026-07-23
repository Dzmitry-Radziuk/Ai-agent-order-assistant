from __future__ import annotations

import hashlib
import logging
import re
import sys
from collections.abc import Mapping
from typing import Any

import structlog

_SECRET_KEYS = {"authorization", "credential", "password", "secret", "token", "api_key", "apikey"}
_SECRET_KEY_SUFFIXES = ("_authorization", "_credential", "_password", "_secret", "_token", "_api_key")
_IDENTIFIER_KEYS = {
    "chat_id",
    "spreadsheet_id",
    "telegram_chat_id",
    "telegram_id",
    "telegram_user_id",
    "user_id",
    "venue_code",
}
_CONTENT_KEYS = {
    "caption",
    "catalog_name",
    "comment",
    "global_comment",
    "input",
    "message_text",
    "order_entry_text",
    "printed_reference_text",
    "product_query",
    "reply_text",
    "source_line",
    "source_query",
    "selection_query",
    "target_query",
    "target_queries",
    "text",
    "transcript",
    "user_comment_to_supplier",
    "user_text",
}
_TOKEN_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(https://api\.telegram\.org/(?:file/)?bot)\d+:[A-Za-z0-9_-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
)


def _digest(value: str) -> str:
    """Возвращает короткий необратимый отпечаток значения."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _scrub_tokens(value: str) -> str:
    """Удаляет токены из произвольной строки журнала."""
    scrubbed = value
    for pattern in _TOKEN_PATTERNS:
        scrubbed = pattern.sub(
            lambda match: (
                f"{match.group(1)}<redacted>" if match.lastindex else "<redacted>"
            ),
            scrubbed,
        )
    return scrubbed


def sanitize_log_value(
    value: Any,
    *,
    key: str = "",
    include_user_content: bool = False,
    max_content_length: int = 500,
) -> Any:
    """Обезличивает рекурсивное значение перед записью в журнал."""
    normalized_key = key.lower()
    if normalized_key in _SECRET_KEYS or normalized_key.endswith(_SECRET_KEY_SUFFIXES):
        return "<redacted>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {
            str(child_key): sanitize_log_value(
                child_value,
                key=str(child_key),
                include_user_content=include_user_content,
                max_content_length=max_content_length,
            )
            for child_key, child_value in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [
            sanitize_log_value(
                item,
                key=key,
                include_user_content=include_user_content,
                max_content_length=max_content_length,
            )
            for item in value
        ]

    text = _scrub_tokens(str(value))
    if normalized_key in _IDENTIFIER_KEYS:
        return f"sha256:{_digest(text)}"
    if normalized_key in _CONTENT_KEYS:
        if not include_user_content:
            return f"<content sha256:{_digest(text)} length:{len(text)}>"
        if len(text) > max_content_length:
            return f"{text[:max_content_length]}…<truncated:{len(text)}>"
    return text


def _privacy_processor(
    include_user_content: bool,
    max_content_length: int,
) -> Any:
    """Создаёт процессор обезличивания структурированных событий."""

    def processor(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        return {
            key: sanitize_log_value(
                value,
                key=key,
                include_user_content=include_user_content,
                max_content_length=max_content_length,
            )
            for key, value in event_dict.items()
        }

    return processor


def configure_logging(
    level: str = "INFO",
    *,
    include_user_content: bool = False,
    max_content_length: int = 500,
) -> None:
    """Настраивает структурированные журналы приложения."""
    normalized_level = level.upper()
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=normalized_level,
        force=True,
    )
    # httpx includes the full request URL in its INFO access records. Telegram
    # encodes the bot token in that URL, so those records must never reach logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _privacy_processor(include_user_content, max_content_length),
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, normalized_level, logging.INFO)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
