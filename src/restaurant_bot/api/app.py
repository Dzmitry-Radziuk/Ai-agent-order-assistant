from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Header, HTTPException, Response, status
from redis import Redis

from restaurant_bot.config import get_settings
from restaurant_bot.db import SessionLocal
from restaurant_bot.input.telegram import normalize_telegram_update
from restaurant_bot.logging import configure_logging
from restaurant_bot.repositories.updates import UpdateRepository

settings = get_settings()
logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Инициализирует жизненный цикл FastAPI."""
    configure_logging(
        settings.log_level,
        include_user_content=settings.log_user_content,
        max_content_length=settings.log_content_max_length,
    )
    yield


app = FastAPI(
    title="Restaurant Procurement Bot",
    version="1.0.0",
    docs_url=None if settings.app_env == "production" else "/docs",
    redoc_url=None if settings.app_env == "production" else "/redoc",
    lifespan=lifespan,
)


@app.get("/health/live")
def liveness() -> dict[str, str]:
    """Сообщает о работе процесса API."""
    return {"status": "ok"}


@app.get("/health/ready")
def readiness() -> dict[str, str]:
    """Проверяет готовность зависимостей приложения."""
    from sqlalchemy import text

    with SessionLocal() as db:
        db.execute(text("select 1"))
    redis = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    try:
        redis.ping()
    finally:
        redis.close()
    return {"status": "ready"}


@app.post("/webhooks/telegram", status_code=status.HTTP_200_OK)
def telegram_webhook(
    payload: dict[str, Any],
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> Response:
    """Проверяет, сохраняет и ставит обновление в очередь."""
    expected = settings.telegram_webhook_secret.get_secret_value()
    actual = x_telegram_bot_api_secret_token or ""
    if not secrets.compare_digest(actual, expected):
        logger.warning("telegram_webhook_rejected", reason="invalid_secret")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid webhook secret"
        )

    event = normalize_telegram_update(payload)
    if event.update_id <= 0:
        logger.warning("telegram_webhook_rejected", reason="missing_update_id")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="missing update_id")

    with SessionLocal.begin() as db:
        repository = UpdateRepository(db)
        created = repository.enqueue_once(event.update_id, event.chat_id, payload)
        existing_status = None if created else repository.get_status(event.update_id)

    should_enqueue = created or existing_status in {"queued", "processing"}

    if should_enqueue:
        from restaurant_bot.workers.tasks import process_telegram_update

        process_telegram_update.delay(event.update_id)
    logger.info(
        "telegram_webhook_received",
        update_id=event.update_id,
        chat_id=event.chat_id,
        input_type=event.input_type.value,
        text=event.text,
        has_file=bool(event.file_id),
        enqueued=should_enqueue,
        duplicate=not created,
    )
    return Response(status_code=status.HTTP_200_OK)
