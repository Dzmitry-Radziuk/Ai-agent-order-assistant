from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

import restaurant_bot.api.app as api_module


class _DatabaseContext:
    def __enter__(self) -> _DatabaseContext:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, _: object) -> None:
        return None


class _SessionFactory:
    def __call__(self) -> _DatabaseContext:
        return _DatabaseContext()

    def begin(self) -> _DatabaseContext:
        return _DatabaseContext()


class _RedisClient:
    ping_called = False
    close_called = False

    def ping(self) -> bool:
        self.ping_called = True
        return True

    def close(self) -> None:
        self.close_called = True


def test_liveness_reports_running_process() -> None:
    assert api_module.liveness() == {"status": "ok"}


def test_readiness_checks_postgres_and_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = _RedisClient()
    monkeypatch.setattr(api_module, "SessionLocal", _SessionFactory())
    monkeypatch.setattr(api_module.Redis, "from_url", lambda *_args, **_kwargs: redis)

    assert api_module.readiness() == {"status": "ready"}
    assert redis.ping_called is True
    assert redis.close_called is True


def test_webhook_rejects_an_invalid_secret() -> None:
    with pytest.raises(HTTPException) as error:
        api_module.telegram_webhook({"update_id": 1}, "invalid")

    assert error.value.status_code == 401


def test_duplicate_webhook_is_acknowledged_without_a_second_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DuplicateRepository:
        def __init__(self, _: Any):
            pass

        def enqueue_once(self, update_id: int, chat_id: str, payload: dict[str, Any]) -> bool:
            assert update_id == 42
            assert chat_id == "7"
            assert payload["message"]["text"] == "/start"
            return False

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
