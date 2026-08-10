from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

import restaurant_bot.api.app as api_module


class _DatabaseContext:
    """Имитирует контекст транзакции базы данных."""

    def __enter__(self) -> _DatabaseContext:
        """Открывает тестовый контекст зависимости."""
        return self

    def __exit__(self, *_: object) -> None:
        """Закрывает тестовый контекст зависимости."""
        return None

    def execute(self, _: object) -> None:
        """Имитирует выполнение запроса к базе данных."""
        return None


class _SessionFactory:
    """Создаёт тестовые контексты сессии базы данных."""

    def __call__(self) -> _DatabaseContext:
        """Возвращает тестовый контекст сессии."""
        return _DatabaseContext()

    def begin(self) -> _DatabaseContext:
        """Создаёт тестовый транзакционный контекст."""
        return _DatabaseContext()


class _RedisClient:
    """Имитирует минимальный клиент Redis для health-check."""

    ping_called = False
    close_called = False

    def ping(self) -> bool:
        """Имитирует успешную проверку Redis."""
        self.ping_called = True
        return True

    def close(self) -> None:
        """Имитирует закрытие клиента Redis."""
        self.close_called = True


def test_liveness_reports_running_process() -> None:
    """Проверяет, что liveness сообщает running process."""
    assert api_module.liveness() == {"status": "ok"}


def test_readiness_checks_postgres_and_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Проверяет, что readiness checks postgres и redis."""
    redis = _RedisClient()
    monkeypatch.setattr(api_module, "SessionLocal", _SessionFactory())
    monkeypatch.setattr(api_module.Redis, "from_url", lambda *_args, **_kwargs: redis)

    assert api_module.readiness() == {"status": "ready"}
    assert redis.ping_called is True
    assert redis.close_called is True


def test_webhook_rejects_an_invalid_secret() -> None:
    """Проверяет, что webhook отклоняет an неверный секрет."""
    with pytest.raises(HTTPException) as error:
        api_module.telegram_webhook({"update_id": 1}, "invalid")

    assert error.value.status_code == 401


def test_duplicate_webhook_is_acknowledged_without_a_second_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проверяет, что дубликат webhook является acknowledged без a второй task."""

    class DuplicateRepository:
        """Имитирует обнаружение повторного Telegram update."""

        def __init__(self, _: Any):
            """Инициализирует тестовый двойник зависимости."""
            pass

        def enqueue_once(self, update_id: int, chat_id: str, payload: dict[str, Any]) -> bool:
            """Имитирует идемпотентную постановку update в очередь."""
            assert update_id == 42
            assert chat_id == "7"
            assert payload["message"]["text"] == "/start"
            return False

        def get_status(self, update_id: int) -> str:
            """Возвращает terminal status для проверки отсутствия replay."""
            assert update_id == 42
            return "done"

    monkeypatch.setattr(api_module, "SessionLocal", _SessionFactory())
    monkeypatch.setattr(api_module, "UpdateRepository", DuplicateRepository)
    response = api_module.telegram_webhook(
        {
            "update_id": 42,
            "message": {"chat": {"id": 7}, "from": {"id": 7}, "text": "/start"},
        },
        "test-secret",
    )

    assert response.status_code == 200


def test_duplicate_webhook_redrives_existing_queued_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Переотправляет существующий queued update без новой строки."""

    class QueuedRepository:
        """Имитирует уже сохранённое recoverable обновление."""

        def __init__(self, _: Any):
            """Инициализирует тестовый repository."""

        def enqueue_once(self, update_id: int, chat_id: str, payload: dict[str, Any]) -> bool:
            """Сообщает, что update уже существует."""
            return False

        def get_status(self, update_id: int) -> str:
            """Возвращает recoverable status."""
            return "queued"

    class Task:
        """Имитирует Celery task для проверки re-drive."""

        def __init__(self) -> None:
            """Создаёт заглушку Celery-задачи для проверки повторной постановки."""
            self.calls: list[int] = []

        def delay(self, update_id: int) -> None:
            """Запоминает повторную постановку update."""
            self.calls.append(update_id)

    task = Task()
    monkeypatch.setattr(api_module, "SessionLocal", _SessionFactory())
    monkeypatch.setattr(api_module, "UpdateRepository", QueuedRepository)
    monkeypatch.setattr(
        "restaurant_bot.workers.tasks.process_telegram_update",
        task,
    )

    response = api_module.telegram_webhook(
        {
            "update_id": 43,
            "message": {"chat": {"id": 7}, "from": {"id": 7}, "text": "сыр"},
        },
        "test-secret",
    )

    assert response.status_code == 200
    assert task.calls == [43]
