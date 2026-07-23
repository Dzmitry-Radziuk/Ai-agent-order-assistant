from __future__ import annotations

from functools import lru_cache

from redis import Redis

from restaurant_bot.config import get_settings
from restaurant_bot.integrations.google_sheets import GoogleSheetsGateway
from restaurant_bot.integrations.openai_client import OpenAIService
from restaurant_bot.integrations.telegram import TelegramClient
from restaurant_bot.services.orchestrator import UpdateOrchestrator
from restaurant_bot.services.submission import SubmissionService
from restaurant_bot.workers.celery_app import celery_app

SUBMISSION_MAX_RETRIES = 8


@lru_cache(maxsize=1)
def dependencies() -> tuple[UpdateOrchestrator, SubmissionService]:
    """Создаёт и кэширует зависимости фонового процесса."""
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    telegram = TelegramClient(settings)
    sheets = GoogleSheetsGateway(settings)
    openai_service = OpenAIService(settings)
    return (
        UpdateOrchestrator(settings, redis, telegram, openai_service, sheets),
        SubmissionService(settings, redis, telegram, sheets),
    )


@celery_app.task(
    bind=True,
    name="restaurant_bot.process_telegram_update",
)
def process_telegram_update(self, update_id: int) -> None:  # type: ignore[no-untyped-def]
    """Обрабатывает сохранённое обновление Telegram."""
    orchestrator, _ = dependencies()
    orchestrator.process(update_id)


@celery_app.task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": SUBMISSION_MAX_RETRIES},
    name="restaurant_bot.submit_order",
)
def submit_order(self, chat_id: str) -> None:  # type: ignore[no-untyped-def]
    """Отправляет заявку с повторами временных ошибок."""
    _, submission = dependencies()
    submission.submit(
        chat_id,
        report_failure=self.request.retries >= SUBMISSION_MAX_RETRIES,
    )


@celery_app.task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": 4},
    name="restaurant_bot.submit_product_add",
)
def submit_product_add(self, chat_id: str) -> None:  # type: ignore[no-untyped-def]
    """Отправляет запрос на добавление нового товара."""
    _, submission = dependencies()
    submission.submit_product_add(chat_id)


@celery_app.task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": 4},
    name="restaurant_bot.send_order_status",
)
def send_order_status(self, chat_id: str) -> None:  # type: ignore[no-untyped-def]
    """Отправляет последние статусы заявок."""
    _, submission = dependencies()
    submission.send_status(chat_id)
