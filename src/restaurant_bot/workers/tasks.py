from __future__ import annotations

from functools import lru_cache

from redis import Redis

from restaurant_bot.config import get_settings
from restaurant_bot.db import SessionLocal
from restaurant_bot.integrations.google_sheets import GoogleSheetsGateway
from restaurant_bot.integrations.openai_client import OpenAIService
from restaurant_bot.integrations.telegram import TELEGRAM_TRANSIENT_ERRORS, TelegramClient
from restaurant_bot.repositories.order_events import OrderEventRepository
from restaurant_bot.services.orchestrator import UpdateOrchestrator
from restaurant_bot.services.submission import SubmissionService
from restaurant_bot.workers.celery_app import celery_app

SUBMISSION_MAX_RETRIES = 8
UPDATE_DELIVERY_MAX_RETRIES = 4


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
    autoretry_for=TELEGRAM_TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": UPDATE_DELIVERY_MAX_RETRIES},
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
def send_order_status(  # type: ignore[no-untyped-def]
    self,
    chat_id: str,
    page: int = 0,
    selected_index: int | None = None,
    order_number: str = "",
    detail_page: int = 0,
) -> None:
    """Отправляет список заявок или подробности выбранной заявки."""
    _, submission = dependencies()
    kwargs = {
        "page": page,
        "selected_index": selected_index,
        "order_number": order_number,
    }
    if detail_page:
        kwargs["detail_page"] = detail_page
    submission.send_status(chat_id, **kwargs)


@celery_app.task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": SUBMISSION_MAX_RETRIES},
    name="restaurant_bot.submit_review_order",
)
def submit_review_order(self, chat_id: str, token: str) -> None:  # type: ignore[no-untyped-def]
    """Проверяет и отправляет заявку, открытую из deep-link таблицы."""
    orchestrator, _ = dependencies()
    orchestrator.order_review.submit(chat_id, token)


@celery_app.task(name="restaurant_bot.cleanup_expired_audit_data")
def cleanup_expired_audit_data() -> dict[str, int]:
    """Удаляет устаревший аудит и завершённые входящие обновления."""
    settings = get_settings()
    with SessionLocal.begin() as db:
        deleted_events, deleted_updates = OrderEventRepository(db).cleanup(
            event_retention_days=settings.order_event_retention_days,
            update_retention_days=settings.telegram_update_retention_days,
        )
    return {
        "deleted_order_events": deleted_events,
        "deleted_telegram_updates": deleted_updates,
    }
