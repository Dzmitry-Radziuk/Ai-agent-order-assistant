"""Проверяет отправку наблюдений Langfuse."""

from pathlib import Path
from types import SimpleNamespace

from restaurant_bot.config import Settings
from restaurant_bot.observability.tracing import Tracer


def _langfuse_settings() -> Settings:
    """Возвращает настройки с включённым тестовым Langfuse."""
    return Settings(
        telegram_bot_token="test-token",
        telegram_webhook_secret="test-secret",
        openai_api_key="test-openai",
        google_service_account_file=Path("/tmp/google.json"),
        google_recalc_url="https://example.test/recalc",
        google_order_submission_enabled=True,
        google_order_submission_url="https://example.test/submit",
        google_order_submission_secret="test-submit-secret",
        langfuse_enabled=True,
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
        langfuse_base_url="https://langfuse.example.test",
        langfuse_tracing_environment="test",
    )


def test_langfuse_client_uses_bounded_batch_settings(mocker) -> None:  # type: ignore[no-untyped-def]
    """Передаёт SDK короткое окно фоновой отправки событий."""
    langfuse_client = mocker.Mock()
    langfuse_factory = mocker.Mock(return_value=langfuse_client)
    mocker.patch.dict("sys.modules", {"langfuse": SimpleNamespace(Langfuse=langfuse_factory)})

    settings = _langfuse_settings()
    Tracer(settings)

    assert langfuse_factory.call_args.kwargs["flush_at"] == 32
    assert langfuse_factory.call_args.kwargs["flush_interval"] == 2.0


def test_root_observation_flushes_without_propagating_flush_error(mocker) -> None:  # type: ignore[no-untyped-def]
    """Отправляет корневую трассу и не ломает запрос при сбое Langfuse."""
    tracer = object.__new__(Tracer)
    tracer.client = mocker.Mock()
    observation_context = mocker.MagicMock()
    observation_context.__enter__.return_value = mocker.Mock()
    tracer.client.start_as_current_observation.return_value = observation_context
    tracer.client.flush.side_effect = RuntimeError("Langfuse unavailable")

    with tracer.observation("telegram_update"):
        pass

    tracer.client.flush.assert_called_once_with()
