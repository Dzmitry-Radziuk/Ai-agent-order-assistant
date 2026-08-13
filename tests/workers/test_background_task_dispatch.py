"""Проверяет поведение, связанное с модулем «test background task dispatch»."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from restaurant_bot.integrations.cache import ChatLeaseLostError
from restaurant_bot.services.orchestrator import UpdateOrchestrator
from restaurant_bot.workers import tasks


class _RecordingDispatcher:
    """Записывает постановку фоновых действий в порядке вызова."""

    def __init__(self) -> None:
        """Создаёт пустой список постановленных действий."""
        self.events: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def submit_order(self, chat_id: str) -> None:
        """Записывает отправку заявки."""
        self.events.append(("submit_order", (chat_id,), {}))

    def send_order_status(
        self,
        chat_id: str,
        *,
        page: int,
        detail_page: int,
        selected_index: int | None,
        order_number: str,
    ) -> None:
        """Записывает отправку статуса заявки."""
        self.events.append(
            (
                "send_order_status",
                (chat_id,),
                {
                    "page": page,
                    "detail_page": detail_page,
                    "selected_index": selected_index,
                    "order_number": order_number,
                },
            )
        )

    def submit_product_add(self, chat_id: str) -> None:
        """Записывает добавление нового товара."""
        self.events.append(("submit_product_add", (chat_id,), {}))

    def submit_review_order(self, chat_id: str, token: str) -> None:
        """Записывает отправку заявки из review-ссылки."""
        self.events.append(("submit_review_order", (chat_id, token), {}))


def _service(dispatcher: _RecordingDispatcher) -> UpdateOrchestrator:
    """Создаёт оркестратор только с зависимостью фоновых задач."""
    service = object.__new__(UpdateOrchestrator)
    service.background_tasks = dispatcher
    return service


def _result(mask: int) -> SimpleNamespace:
    """Создаёт результат для одной комбинации четырёх флагов."""
    return SimpleNamespace(
        enqueue_submission=bool(mask & 1),
        enqueue_order_status=bool(mask & 2),
        enqueue_product_add=bool(mask & 4),
        enqueue_review_submission=bool(mask & 8),
        order_status_page=3,
        order_status_detail_page=2,
        order_status_selected_index=1,
        order_status_order_number="ORDER-42",
        state=SimpleNamespace(review_token="review-token"),
    )


def _expected_events(mask: int) -> list[tuple[str, tuple[object, ...], dict[str, object]]]:
    """Возвращает ожидаемый контракт постановки baseline-реализации."""
    events: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
    if mask & 1:
        events.append(("submit_order", ("chat-1",), {}))
    if mask & 2:
        events.append(
            (
                "send_order_status",
                ("chat-1",),
                {
                    "page": 3,
                    "detail_page": 2,
                    "selected_index": 1,
                    "order_number": "ORDER-42",
                },
            )
        )
    if mask & 4:
        events.append(("submit_product_add", ("chat-1",), {}))
    if mask & 8:
        events.append(("submit_review_order", ("chat-1", "review-token"), {}))
    return events


def test_dispatch_matches_baseline_for_all_flag_combinations() -> None:
    """Сохраняет порядок и payload для всех шестнадцати комбинаций флагов."""
    for mask in range(16):
        dispatcher = _RecordingDispatcher()

        _service(dispatcher)._enqueue_side_effects("chat-1", _result(mask))

        assert dispatcher.events == _expected_events(mask)


def test_dispatch_checks_lease_before_each_task() -> None:
    """Проверяет отсутствие лишних lease-проверок и проверку перед каждой задачей."""
    dispatcher = _RecordingDispatcher()
    service = _service(dispatcher)
    lease = MagicMock()

    service._enqueue_side_effects("chat-1", _result(0), lease=lease)
    assert lease.ensure_owned.call_count == 0

    service._enqueue_side_effects("chat-1", _result(15), lease=lease)
    assert lease.ensure_owned.call_count == 4


def test_dispatch_stops_after_lease_loss_between_tasks() -> None:
    """Сохраняет частичный dispatch при потере lease между задачами."""
    dispatcher = _RecordingDispatcher()
    lease = MagicMock()
    lease.ensure_owned.side_effect = [None, ChatLeaseLostError("lost")]

    with pytest.raises(ChatLeaseLostError):
        _service(dispatcher)._enqueue_side_effects("chat-1", _result(3), lease=lease)

    assert dispatcher.events == [("submit_order", ("chat-1",), {})]


def test_celery_dispatcher_preserves_task_payload(mocker) -> None:  # type: ignore[no-untyped-def]
    """Передаёт Celery-задачам исходные аргументы и именованные параметры."""
    submit_order = mocker.patch.object(tasks.submit_order, "delay")
    send_order_status = mocker.patch.object(tasks.send_order_status, "delay")
    submit_product_add = mocker.patch.object(tasks.submit_product_add, "delay")
    submit_review_order = mocker.patch.object(tasks.submit_review_order, "delay")

    dispatcher = tasks.CeleryBackgroundTaskDispatcher()
    dispatcher.submit_order("chat-1")
    dispatcher.send_order_status(
        "chat-1",
        page=3,
        detail_page=2,
        selected_index=1,
        order_number="ORDER-42",
    )
    dispatcher.submit_product_add("chat-1")
    dispatcher.submit_review_order("chat-1", "review-token")

    submit_order.assert_called_once_with("chat-1")
    send_order_status.assert_called_once_with(
        "chat-1",
        page=3,
        detail_page=2,
        selected_index=1,
        order_number="ORDER-42",
    )
    submit_product_add.assert_called_once_with("chat-1")
    submit_review_order.assert_called_once_with("chat-1", "review-token")
