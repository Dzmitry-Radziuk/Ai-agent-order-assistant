"""Проверяет поведение, связанное с модулем «test submission lease fencing»."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from restaurant_bot.domain.models import ConversationState, PendingSubmission
from restaurant_bot.integrations.cache import ChatLeaseLostError
from restaurant_bot.integrations.google_sheets import (
    CatalogMutationVerification,
    OrderSubmissionResult,
)
from restaurant_bot.services import submission as submission_module
from restaurant_bot.services.submission import SubmissionService


class _FakeLease:
    """Управляемый тестовый lease без ожидания времени."""

    def __init__(self) -> None:
        """Создаёт действующий lease."""
        self.lost = False
        self.ensure_calls = 0
        self.lose_on_call: int | None = None

    def ensure_owned(self) -> None:
        """Останавливает работу после принудительной потери владения."""
        self.ensure_calls += 1
        if self.lose_on_call == self.ensure_calls:
            self.lost = True
        if self.lost:
            raise ChatLeaseLostError("lease lost")


class _LeaseContext:
    """Передаёт управляемый lease в сервис отправки."""

    def __init__(self, lease: _FakeLease) -> None:
        """Сохраняет lease для контекстного менеджера."""
        self.lease = lease

    def __enter__(self) -> _FakeLease:
        """Возвращает lease владельцу операции."""
        return self.lease

    def __exit__(self, *_args: object) -> None:
        """Не изменяет результат тестовой операции."""
        return None


def _plan() -> dict[str, object]:
    """Создаёт валидный минимальный план изменения каталога."""
    return {
        "schema_version": 1,
        "operation_id": "catalog:ORDER-1",
        "order_no": "ORDER-1",
        "spreadsheet_id": "venue-sheet",
        "mutations": [
            {
                "kind": "quantity",
                "range": "'Заявка'!B7",
                "product_id": "rose",
                "department": "Кухня",
                "before": 10,
                "increment": 5,
                "expected_after": 15,
            }
        ],
    }


def _pending() -> PendingSubmission:
    """Создаёт минимальную заявку для submission-проверок."""
    return PendingSubmission(order_no="ORDER-1", spreadsheet_id="venue-sheet", rows=[])


def _record(**overrides: object) -> SimpleNamespace:
    """Создаёт запись с состоянием каталога и последующих этапов."""
    values: dict[str, object] = {
        "order_no": "ORDER-1",
        "catalog_update_status": "pending",
        "catalog_updated": False,
        "catalog_update_plan": None,
        "catalog_update_operation_id": "",
        "last_error": "",
        "recalc_status": "completed",
        "recalc_done": True,
        "dispatch_started": False,
        "dispatch_completed": False,
        "external_order_no": "",
        "finalized": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _catalog_service(lease: _FakeLease) -> SubmissionService:
    """Создаёт сервис с управляемым каталоговым внешним вызовом."""
    service = object.__new__(SubmissionService)
    service.sheets = MagicMock()
    service.sheets.prepare_catalog_mutation.return_value = _plan()
    service._persist_catalog_started = MagicMock()  # type: ignore[method-assign]
    service._checkpoint = MagicMock()  # type: ignore[method-assign]
    service._mark_catalog_conflict = MagicMock()  # type: ignore[method-assign]
    service._mark_catalog_uncertain = MagicMock()  # type: ignore[method-assign]
    service._send_catalog_conflict_reply = MagicMock()  # type: ignore[method-assign]
    service._send_catalog_uncertain_reply = MagicMock()  # type: ignore[method-assign]
    return service


def test_catalog_apply_loss_stops_before_readback_and_checkpoint() -> None:
    """Останавливает старого владельца после потери lease во время записи каталога."""
    lease = _FakeLease()
    service = _catalog_service(lease)

    def apply(_plan: dict[str, object]) -> None:
        """Имитирует внешний вызов, после которого lease потерян."""
        lease.lost = True

    service.sheets.apply_catalog_mutation.side_effect = apply

    with pytest.raises(ChatLeaseLostError):
        service._run_catalog_update("chat-1", _pending(), _record(), lease=lease)

    service.sheets.verify_catalog_mutation.assert_not_called()
    service._checkpoint.assert_not_called()
    service._mark_catalog_conflict.assert_not_called()
    service._mark_catalog_uncertain.assert_not_called()
    service._send_catalog_conflict_reply.assert_not_called()
    service._send_catalog_uncertain_reply.assert_not_called()


def test_catalog_apply_exception_loss_stops_before_readback() -> None:
    """Не читает каталог после ошибки записи, если lease потерян во время вызова."""
    lease = _FakeLease()
    service = _catalog_service(lease)

    def apply(_plan: dict[str, object]) -> None:
        """Имитирует ошибку внешней записи и потерю lease."""
        lease.lost = True
        raise TimeoutError("write timeout")

    service.sheets.apply_catalog_mutation.side_effect = apply

    with pytest.raises(ChatLeaseLostError):
        service._run_catalog_update("chat-1", _pending(), _record(), lease=lease)

    service.sheets.verify_catalog_mutation.assert_not_called()
    service._mark_catalog_uncertain.assert_not_called()
    service._mark_catalog_conflict.assert_not_called()


def test_catalog_verify_loss_stops_before_completion() -> None:
    """Не фиксирует результат после потери lease во время read-back каталога."""
    lease = _FakeLease()
    service = _catalog_service(lease)
    plan = _plan()
    record = _record(
        catalog_update_status="started",
        catalog_update_plan=plan,
        catalog_update_operation_id="catalog:ORDER-1",
    )

    def verify(_plan: dict[str, object]) -> CatalogMutationVerification:
        """Имитирует успешный read-back после потери владения."""
        lease.lost = True
        return CatalogMutationVerification.APPLIED

    service.sheets.verify_catalog_mutation.side_effect = verify

    with pytest.raises(ChatLeaseLostError):
        service._run_catalog_update("chat-1", _pending(), record, lease=lease)

    service._checkpoint.assert_not_called()
    service._mark_catalog_conflict.assert_not_called()
    service._mark_catalog_uncertain.assert_not_called()


def test_controlled_catalog_apply_is_blocked_after_readback_loss() -> None:
    """Не запускает controlled apply, если lease потерян после проверки before."""
    lease = _FakeLease()
    service = _catalog_service(lease)
    plan = _plan()
    record = _record(
        catalog_update_status="started",
        catalog_update_plan=plan,
        catalog_update_operation_id="catalog:ORDER-1",
    )

    def verify(_plan: dict[str, object]) -> CatalogMutationVerification:
        """Имитирует состояние NOT_APPLIED и потерю lease перед восстановлением."""
        lease.lost = True
        return CatalogMutationVerification.NOT_APPLIED

    service.sheets.verify_catalog_mutation.side_effect = verify

    with pytest.raises(ChatLeaseLostError):
        service._run_catalog_update("chat-1", _pending(), record, lease=lease)

    service.sheets.apply_catalog_mutation.assert_not_called()


def test_finalize_loss_before_save_keeps_state_and_record_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Не очищает корзину и не завершает запись после потери lease перед сохранением."""
    lease = _FakeLease()
    service = object.__new__(SubmissionService)
    db = MagicMock()
    session_local = MagicMock()
    session_local.begin.return_value.__enter__.return_value = db
    monkeypatch.setattr(submission_module, "SessionLocal", session_local)
    sessions = MagicMock()
    monkeypatch.setattr(submission_module, "SessionRepository", MagicMock(return_value=sessions))
    state = ConversationState(cart=[])
    row = SimpleNamespace()
    record = _record()
    sessions.get_for_update.return_value = (row, state)
    db.scalar.return_value = record
    lease.lose_on_call = 3

    with pytest.raises(ChatLeaseLostError):
        service._finalize("chat-1", "ORDER-1", "ORDER-1", lease=lease)

    sessions.save.assert_not_called()
    assert record.finalized is not True


def _submission_service(
    monkeypatch: pytest.MonkeyPatch,
    lease: _FakeLease,
    *,
    dispatch_enabled: bool,
) -> tuple[SubmissionService, MagicMock, SimpleNamespace]:
    """Создаёт submission-сервис для проверки внешнего вызова и lease."""
    service = object.__new__(SubmissionService)
    service.settings = SimpleNamespace(google_order_submission_enabled=dispatch_enabled)
    service.redis = MagicMock()
    service.redis.lock.return_value = nullcontext()
    service.telegram = MagicMock()
    service.sheets = MagicMock()
    service.catalog_cache = MagicMock()
    pending = _pending()
    record = _record(recalc_status="pending", recalc_done=False)
    service._load_pending = MagicMock(return_value=pending)  # type: ignore[method-assign]
    service._has_current_access = MagicMock(return_value=True)  # type: ignore[method-assign]
    service._record = MagicMock(return_value=record)  # type: ignore[method-assign]
    service._get_record = MagicMock(side_effect=[record, record, record])  # type: ignore[method-assign]
    service._run_catalog_update = MagicMock(return_value=True)  # type: ignore[method-assign]
    service._mark_recalc_started = MagicMock()  # type: ignore[method-assign]
    service._mark_recalc_completed = MagicMock()  # type: ignore[method-assign]
    service._mark_recalc_uncertain = MagicMock()  # type: ignore[method-assign]
    service._send_recalc_uncertain_reply = MagicMock()  # type: ignore[method-assign]
    service._mark_dispatch_started = MagicMock()  # type: ignore[method-assign]
    service._mark_dispatch_completed = MagicMock()  # type: ignore[method-assign]
    service._mark_dispatch_uncertain = MagicMock()  # type: ignore[method-assign]
    service._send_dispatch_uncertain_once = MagicMock()  # type: ignore[method-assign]
    service._finalize = MagicMock(return_value=ConversationState())  # type: ignore[method-assign]
    service._send_completion = MagicMock()  # type: ignore[method-assign]
    service._send_local_completion = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr(
        submission_module, "chat_lock", lambda *_args, **_kwargs: _LeaseContext(lease)
    )
    return service, service.sheets, record


def test_recalculation_loss_after_external_send_does_not_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Не завершает пересчёт после потери lease сразу после внешнего POST."""
    lease = _FakeLease()
    service, sheets, _record_value = _submission_service(monkeypatch, lease, dispatch_enabled=False)

    def send(_prepared: object) -> None:
        """Имитирует отправленный пересчёт и потерю lease."""
        lease.lost = True

    sheets.send_recalculation.side_effect = send

    with pytest.raises(ChatLeaseLostError):
        service.submit("chat-1", report_failure=False)

    sheets.send_recalculation.assert_called_once()
    service._mark_recalc_completed.assert_not_called()
    service._finalize.assert_not_called()


def test_dispatch_loss_after_external_send_does_not_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Не сохраняет номер dispatch после потери lease сразу после внешнего POST."""
    lease = _FakeLease()
    service, sheets, _record_value = _submission_service(monkeypatch, lease, dispatch_enabled=True)
    sheets.prepare_order_submission.return_value = MagicMock()

    def send(_prepared: object) -> OrderSubmissionResult:
        """Имитирует созданную заявку и потерю lease."""
        lease.lost = True
        return OrderSubmissionResult(
            order_number="CENTRAL-1",
            base_rows=1,
            request_rows=0,
            notifications={},
        )

    sheets.send_order_submission.side_effect = send

    with pytest.raises(ChatLeaseLostError):
        service.submit("chat-1", report_failure=False)

    sheets.send_order_submission.assert_called_once()
    service._mark_dispatch_completed.assert_not_called()
    service._finalize.assert_not_called()


def test_completion_notification_gate_remains_at_most_once_after_lease_loss() -> None:
    """Сохраняет H3c-gate после потери lease сразу после отправки карточки."""
    lease = _FakeLease()
    service = object.__new__(SubmissionService)
    service.telegram = MagicMock()
    service._mark_completion_notification_started = MagicMock(side_effect=[True, False])  # type: ignore[method-assign]
    service._mark_completion_notification_completed = MagicMock()  # type: ignore[method-assign]
    service.telegram.send_reply.side_effect = lambda *_args: setattr(lease, "lost", True)
    state = ConversationState()

    with pytest.raises(ChatLeaseLostError):
        service._send_completion(
            "chat-1",
            state,
            "CENTRAL-1",
            "ORDER-1",
            lease=lease,
        )

    lease.lost = False
    service._send_completion(
        "chat-1",
        state,
        "CENTRAL-1",
        "ORDER-1",
        lease=lease,
    )

    assert service.telegram.send_reply.call_count == 1
    service._mark_completion_notification_completed.assert_not_called()
