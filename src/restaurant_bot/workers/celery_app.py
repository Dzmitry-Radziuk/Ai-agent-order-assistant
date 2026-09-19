"""Описывает фоновые задачи «celery app»."""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from restaurant_bot.config import get_settings
from restaurant_bot.observability import configure_logging

settings = get_settings()
configure_logging(
    settings.log_level,
    include_user_content=settings.log_user_content,
    max_content_length=settings.log_content_max_length,
)
celery_app = Celery(
    "restaurant_bot",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["restaurant_bot.workers.tasks"],
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone=settings.app_timezone,
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    broker_connection_retry_on_startup=True,
    result_expires=86400,
    beat_schedule={
        "redrive-telegram-updates": {
            "task": "restaurant_bot.redrive_telegram_updates",
            "schedule": 60.0,
        },
        "check-pending-submissions": {
            "task": "restaurant_bot.check_pending_submissions",
            "schedule": 300.0,
        },
        "cleanup-expired-audit-data": {
            "task": "restaurant_bot.cleanup_expired_audit_data",
            "schedule": crontab(hour=3, minute=15),
        },
    },
)
