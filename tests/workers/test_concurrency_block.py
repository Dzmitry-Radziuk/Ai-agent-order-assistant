from datetime import UTC, datetime
from threading import Event
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from restaurant_bot.integrations.cache import (
    ChatLease,
    ChatLeaseLostError,
    ChatLockBusyError,
    chat_lock,
)
from restaurant_bot.repositories.updates import UpdateSequenceDeferred
from restaurant_bot.services import orchestrator as orchestrator_module
from restaurant_bot.services.orchestrator import UpdateOrchestrator
from restaurant_bot.workers import tasks


def test_chat_lock_reports_busy_as_explicit_concurrency_signal() -> None:
    """Возвращает отдельный сигнал при занятой блокировке чата."""
    redis = MagicMock()
    lock = MagicMock()
    lock.acquire.return_value = False
    redis.lock.return_value = lock

    with pytest.raises(ChatLockBusyError), chat_lock(redis, "chat-1"):
        raise AssertionError("busy lock must not enter the critical section")


def test_claim_defers_later_update_while_lower_update_is_unfinished(mocker) -> None:  # type: ignore[no-untyped-def]
    """Не применяет позднее обновление до завершения более раннего."""
    db = MagicMock()
    update = SimpleNamespace(
        update_id=102,
        chat_id="chat-1",
        payload={},
        result=None,
        state_applied=False,
        reply_sent=False,
        tasks_enqueued=False,
        status="queued",
        updated_at=datetime.now(UTC),
        attempts=0,
        error=None,
    )
    db.scalar.side_effect = [update, 101]
    session_local = mocker.patch.object(orchestrator_module, "SessionLocal")
    session_local.begin.return_value.__enter__.return_value = db
    service = object.__new__(UpdateOrchestrator)

    with pytest.raises(UpdateSequenceDeferred):
        service._claim(102)

    assert update.status == "queued"
    assert update.attempts == 0


def test_claim_marks_later_update_after_predecessor_is_terminal(mocker) -> None:  # type: ignore[no-untyped-def]
    """Передаёт обновление в обработку после исчезновения блокировки порядка."""
    db = MagicMock()
    update = SimpleNamespace(
        update_id=102,
        chat_id="chat-1",
        payload={"update_id": 102},
        result=None,
        state_applied=False,
        reply_sent=False,
        tasks_enqueued=False,
        status="queued",
        updated_at=datetime.now(UTC),
        attempts=0,
        error=None,
    )
    db.scalar.side_effect = [update, None]
    session_local = mocker.patch.object(orchestrator_module, "SessionLocal")
    session_local.begin.return_value.__enter__.return_value = db
    service = object.__new__(UpdateOrchestrator)

    claim = service._claim(102)

    assert claim is not None
    assert update.status == "processing"
    assert update.attempts == 1


def test_fresh_processing_update_is_not_reclaimed_by_duplicate_task(mocker) -> None:  # type: ignore[no-untyped-def]
    """Не забирает свежее обрабатываемое обновление повторной задачей."""
    db = MagicMock()
    update = SimpleNamespace(
        update_id=101,
        chat_id="chat-1",
        payload={"update_id": 101},
        result=None,
        state_applied=False,
        reply_sent=False,
        tasks_enqueued=False,
        status="processing",
        updated_at=datetime.now(UTC),
        attempts=1,
        error=None,
    )
    db.scalar.side_effect = [update, None]
    session_local = mocker.patch.object(orchestrator_module, "SessionLocal")
    session_local.begin.return_value.__enter__.return_value = db
    service = object.__new__(UpdateOrchestrator)

    assert service._claim(101) is None
    assert update.status == "processing"
    assert update.attempts == 1


def test_chat_lock_keys_are_scoped_per_chat() -> None:
    """Формирует разные ключи блокировки для независимых чатов."""
    redis = MagicMock()
    first_lock = MagicMock()
    second_lock = MagicMock()
    first_lock.acquire.return_value = True
    second_lock.acquire.return_value = True
    first_lock.owned.return_value = True
    second_lock.owned.return_value = True
    redis.lock.side_effect = [first_lock, second_lock]

    with chat_lock(redis, "chat-a"):
        pass
    with chat_lock(redis, "chat-b"):
        pass

    assert redis.lock.call_args_list[0].args[0] != redis.lock.call_args_list[1].args[0]


def test_chat_lease_renews_from_a_shared_token_thread() -> None:
    """Продлевает срок блокировки отдельным потоком с общим токеном."""
    redis = MagicMock()
    lock = MagicMock()
    lock.acquire.return_value = True
    lock.owned.return_value = True
    renewed = Event()

    def extend(*_args, **_kwargs):
        """Фиксирует вызов продления блокировки."""
        renewed.set()

    lock.extend.side_effect = extend
    redis.lock.return_value = lock

    with chat_lock(redis, "chat-renew", timeout=1, heartbeat_interval=0.05) as lease:
        assert renewed.wait(1)
        lease.ensure_owned()

    assert not lease._heartbeat.is_alive()
    assert redis.lock.call_args.kwargs["thread_local"] is False
    assert lock.release.called


def test_chat_lease_stops_heartbeat_on_context_exit() -> None:
    """Останавливает поток продления при выходе из контекста."""
    redis = MagicMock()
    lock = MagicMock()
    lock.acquire.return_value = True
    lock.owned.return_value = True
    redis.lock.return_value = lock

    with chat_lock(redis, "chat-stop", timeout=1, heartbeat_interval=0.05) as lease:
        heartbeat = lease._heartbeat
        assert heartbeat.is_alive()

    assert not heartbeat.is_alive()
    lock.release.assert_called_once_with()


def test_chat_lease_raises_when_renewal_fails() -> None:
    """Сигнализирует о потере владения после ошибки продления."""
    redis = MagicMock()
    lock = MagicMock()
    lock.acquire.return_value = True
    lock.owned.return_value = True
    lock.extend.side_effect = RuntimeError("redis unavailable")
    redis.lock.return_value = lock

    with chat_lock(redis, "chat-lost", timeout=1, heartbeat_interval=0.05) as lease:
        assert lease._lost.wait(1)
        with pytest.raises(ChatLeaseLostError):
            lease.ensure_owned()

    lock.release.assert_not_called()


def test_chat_lease_fences_changed_owner_and_old_close() -> None:
    """Не позволяет старому владельцу менять данные или снимать новую блокировку."""
    lock = MagicMock()
    lock.owned.return_value = False
    lease = ChatLease(lock, timeout=30, heartbeat_interval=10)

    with pytest.raises(ChatLeaseLostError):
        lease.ensure_owned()
    lease.close()

    lock.release.assert_not_called()


def test_different_chat_leases_are_independent() -> None:
    """Сохраняет независимость блокировок разных чатов."""
    redis = MagicMock()
    first_lock = MagicMock()
    second_lock = MagicMock()
    first_lock.acquire.return_value = True
    second_lock.acquire.return_value = True
    first_lock.owned.return_value = True
    second_lock.owned.return_value = True
    redis.lock.side_effect = [first_lock, second_lock]

    with (
        chat_lock(redis, "chat-a", timeout=1, heartbeat_interval=0.2),
        chat_lock(redis, "chat-b", timeout=1, heartbeat_interval=0.2),
    ):
        pass

    assert redis.lock.call_count == 2
    assert redis.lock.call_args_list[0].args[0] != redis.lock.call_args_list[1].args[0]


def test_side_effect_enqueue_is_fenced_before_task_publish(mocker) -> None:  # type: ignore[no-untyped-def]
    """Не публикует фоновую задачу после потери владения чатом."""
    lease = MagicMock()
    lease.ensure_owned.side_effect = ChatLeaseLostError("lost")
    result = SimpleNamespace(
        enqueue_submission=True,
        enqueue_order_status=False,
        enqueue_product_add=False,
        enqueue_review_submission=False,
    )
    service = object.__new__(UpdateOrchestrator)
    service.background_tasks = MagicMock()

    with pytest.raises(ChatLeaseLostError):
        service._enqueue_side_effects("chat-1", result, lease=lease)

    service.background_tasks.submit_order.assert_not_called()


def test_redrive_enqueues_oldest_recoverable_updates(mocker) -> None:  # type: ignore[no-untyped-def]
    """Переотправляет в фоновую обработку выбранные репозиторием обновления."""
    db = MagicMock()
    session_local = mocker.patch.object(tasks, "SessionLocal")
    session_local.begin.return_value.__enter__.return_value = db
    repository = mocker.patch.object(tasks, "UpdateRepository").return_value
    repository.recoverable_for_redrive.return_value = [
        SimpleNamespace(update_id=101),
        SimpleNamespace(update_id=201),
    ]
    delay = mocker.patch.object(tasks.process_telegram_update, "delay")

    assert tasks.redrive_telegram_updates.run() == 2

    assert [call.args[0] for call in delay.call_args_list] == [101, 201]
