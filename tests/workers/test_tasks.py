from unittest.mock import MagicMock

from restaurant_bot.integrations.cache import ChatLeaseLostError, ChatLockBusyError
from restaurant_bot.integrations.telegram import TELEGRAM_TRANSIENT_ERRORS
from restaurant_bot.repositories.updates import UpdateSequenceDeferred
from restaurant_bot.workers import tasks
from restaurant_bot.workers.celery_app import celery_app


def test_dependencies_builds_and_caches_worker_services(settings, mocker) -> None:  # type: ignore[no-untyped-def]
    """Создаёт зависимости воркера один раз и повторно использует их."""
    tasks.dependencies.cache_clear()
    mocker.patch.object(tasks, "get_settings", return_value=settings)
    redis_cls = mocker.patch.object(tasks, "Redis")
    telegram_cls = mocker.patch.object(tasks, "TelegramClient")
    sheets_cls = mocker.patch.object(tasks, "GoogleSheetsGateway")
    openai_cls = mocker.patch.object(tasks, "OpenAIService")
    orchestrator_cls = mocker.patch.object(tasks, "UpdateOrchestrator")
    submission_cls = mocker.patch.object(tasks, "SubmissionService")

    first = tasks.dependencies()
    second = tasks.dependencies()

    assert first is second
    redis_cls.from_url.assert_called_once_with(settings.redis_url, decode_responses=True)
    telegram_cls.assert_called_once_with(settings)
    sheets_cls.assert_called_once_with(settings)
    openai_cls.assert_called_once_with(settings)
    orchestrator_cls.assert_called_once()
    submission_cls.assert_called_once()
    tasks.dependencies.cache_clear()


def test_process_update_task_delegates_to_orchestrator(mocker) -> None:  # type: ignore[no-untyped-def]
    """Передаёт идентификатор обновления оркестратору."""
    orchestrator = MagicMock()
    mocker.patch.object(tasks, "dependencies", return_value=(orchestrator, MagicMock()))

    tasks.process_telegram_update.run(42)

    orchestrator.process.assert_called_once_with(42)


def test_process_update_retries_only_transient_telegram_delivery_failures() -> None:
    """Не повторяет бизнес-логику после произвольной ошибки приложения."""
    assert tasks.process_telegram_update.autoretry_for == (
        *TELEGRAM_TRANSIENT_ERRORS,
        ChatLockBusyError,
        UpdateSequenceDeferred,
        ChatLeaseLostError,
    )
    assert tasks.process_telegram_update.retry_kwargs == {
        "max_retries": tasks.UPDATE_DELIVERY_MAX_RETRIES
    }
    assert tasks.process_telegram_update.retry_backoff is True
    assert tasks.process_telegram_update.retry_jitter is True


def test_submit_order_task_reports_failure_only_after_retry_limit(mocker) -> None:  # type: ignore[no-untyped-def]
    """Не сообщает финальный сбой до исчерпания повторов Celery."""
    submission = MagicMock()
    mocker.patch.object(tasks, "dependencies", return_value=(MagicMock(), submission))

    tasks.submit_order.run("chat-1")

    submission.submit.assert_called_once_with("chat-1", report_failure=False)


def test_product_add_and_status_tasks_delegate_to_submission(mocker) -> None:  # type: ignore[no-untyped-def]
    """Передаёт фоновые запросы соответствующим методам отправки."""
    submission = MagicMock()
    mocker.patch.object(tasks, "dependencies", return_value=(MagicMock(), submission))

    tasks.submit_product_add.run("chat-2")
    tasks.send_order_status.run("chat-3")

    submission.submit_product_add.assert_called_once_with("chat-2")
    submission.send_status.assert_called_once_with(
        "chat-3",
        page=0,
        selected_index=None,
        order_number="",
    )


def test_celery_configuration_preserves_delivery_guarantees() -> None:
    """Сохраняет безопасные настройки доставки фоновых задач."""
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1
    assert celery_app.conf.task_track_started is True
    assert celery_app.conf.task_serializer == "json"
    assert celery_app.conf.result_expires == 86400
    assert celery_app.conf.beat_schedule["redrive-telegram-updates"] == {
        "task": "restaurant_bot.redrive_telegram_updates",
        "schedule": 60.0,
    }
    assert (
        celery_app.conf.beat_schedule["cleanup-expired-audit-data"]["task"]
        == "restaurant_bot.cleanup_expired_audit_data"
    )


def test_cleanup_task_uses_configured_retention_periods(settings, mocker) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что cleanup task использует configured retention periods."""
    db = MagicMock()
    session_local = mocker.patch.object(tasks, "SessionLocal")
    session_local.begin.return_value.__enter__.return_value = db
    repository = mocker.patch.object(tasks, "OrderEventRepository").return_value
    repository.cleanup.return_value = (7, 11)
    mocker.patch.object(tasks, "get_settings", return_value=settings)

    result = tasks.cleanup_expired_audit_data.run()

    repository.cleanup.assert_called_once_with(
        event_retention_days=365,
        update_retention_days=30,
    )
    assert result == {
        "deleted_order_events": 7,
        "deleted_telegram_updates": 11,
    }
