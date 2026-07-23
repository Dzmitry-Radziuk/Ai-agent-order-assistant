from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from restaurant_bot.domain.models import (
    CartItem,
    ConversationState,
    PendingSubmission,
    SessionStage,
)
from restaurant_bot.integrations.google_sheets import GoogleSheetsError
from restaurant_bot.services import submission as submission_module
from restaurant_bot.services.submission import (
    SubmissionService,
    build_order_status_text,
    submission_failure_reply,
    submission_success_reply,
)


def test_submission_success_card_matches_n8n() -> None:
    reply = submission_success_reply(type("State", (), {"ui_revision": 4})(), "20260722-001")

    assert reply.text == "✅ <b>Заявка отправлена</b>\n\nНомер заявки: 20260722-001"
    assert [(button.text, button.callback_data) for row in reply.rows for button in row] == [
        ("Проверить статус", "v2:orders:r4"),
        ("Новая заявка", "v2:clear:r4"),
    ]


def test_submission_failure_card_matches_n8n() -> None:
    reply = submission_failure_reply(type("State", (), {"ui_revision": 4})(), "20260722-001")

    assert reply.text == (
        "⚠️ <b>Отправка не завершена</b>\n\n"
        "Заявка: 20260722-001\n\n"
        "Нажмите «Повторить отправку». Уже выполненные этапы будут пропущены."
    )
    assert [(button.text, button.callback_data) for row in reply.rows for button in row] == [
        ("Повторить отправку", "v2:submit:r4"),
        ("К черновику", "v2:back:r4"),
    ]


def test_order_status_renderer_groups_rows_by_order_number() -> None:
    text = build_order_status_text(
        [
            {"order_no": "A-1", "product_name": "Сироп Роза", "status": "Новая заявка"},
            {"order_no": "A-1", "product_name": "Говядина", "status": "Новая заявка"},
        ],
        type("State", (), {"last_order_no": "", "submitted_order_numbers": ["A-1"]})(),
    )

    assert "A-1" in text
    assert "Сироп Роза" in text
    assert "Говядина" in text


def test_successful_submission_clears_cart_checkpoint_and_marks_state_submitted() -> None:
    state = ConversationState(
        cart=[CartItem(id="rose", source_query="Сироп Роза")],
        current_issue_item_id="rose",
        pending_submission=PendingSubmission(order_no="20260722-001"),
    )

    SubmissionService._apply_successful_submission_state(state, "20260722-001")

    assert state.cart == []
    assert state.pending_submission is None
    assert state.current_issue_item_id == ""
    assert state.stage is SessionStage.SUBMITTED
    assert state.status == "submitted"
    assert state.last_order_no == "20260722-001"
    assert state.submitted_order_numbers == ["20260722-001"]


def test_order_status_limits_to_ten_tracked_orders_and_formats_delivery_date() -> None:
    numbers = [f"A-{index}" for index in range(12)]
    rows = [
        {
            "order_no": number,
            "product_name": f"Товар {number}",
            "status": "В пути",
            "delivery_date": "24.07.2026",
        }
        for number in numbers
    ]
    text = build_order_status_text(
        rows,
        type("State", (), {"last_order_no": "", "submitted_order_numbers": numbers})(),
    )

    assert "A-0" in text
    assert "A-9" in text
    assert "A-10" not in text
    assert "24 июля 2026" in text


def test_transient_submission_error_is_retried_without_premature_failure_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = object.__new__(SubmissionService)
    service.redis = MagicMock()
    service.telegram = MagicMock()
    service.sheets = MagicMock()
    service.sheets.append_history.side_effect = TimeoutError("temporary timeout")
    pending = PendingSubmission(
        order_no="20260722-retry",
        spreadsheet_id="venue-sheet",
        rows=[{"Комментарий": "холодным"}],
    )
    service._load_pending = MagicMock(return_value=pending)  # type: ignore[method-assign]
    service._record = MagicMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(history_written=False)
    )
    service._remember_transient_error = MagicMock()  # type: ignore[method-assign]
    service._fail = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr(submission_module, "chat_lock", lambda *_args, **_kwargs: nullcontext())

    with pytest.raises(TimeoutError, match="temporary timeout"):
        service.submit("123", report_failure=False)

    service._remember_transient_error.assert_called_once_with("20260722-retry", "temporary timeout")
    service._fail.assert_not_called()
    service.telegram.send_reply.assert_not_called()


def test_retry_delivers_success_card_after_order_was_already_finalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = object.__new__(SubmissionService)
    service.redis = MagicMock()
    service._load_pending = MagicMock(return_value=None)  # type: ignore[method-assign]
    state = ConversationState(last_order_no="20260722-complete", status="submitted")
    service._load_unnotified_completion = MagicMock(  # type: ignore[method-assign]
        return_value=("20260722-complete", state)
    )
    service._send_completion = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr(submission_module, "chat_lock", lambda *_args, **_kwargs: nullcontext())

    service.submit("123")

    service._send_completion.assert_called_once_with("123", state, "20260722-complete")


def test_success_card_is_checkpointed_only_after_telegram_accepts_it() -> None:
    service = object.__new__(SubmissionService)
    service.telegram = MagicMock()
    service._checkpoint = MagicMock()  # type: ignore[method-assign]
    state = ConversationState(last_order_no="A-1", status="submitted")

    service._send_completion("123", state, "A-1")

    service.telegram.send_reply.assert_called_once()
    service._checkpoint.assert_called_once_with("A-1", "completion_notified")


def test_submission_runs_all_external_stages_and_checkpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Выполняет этапы отправки строго по контрольным точкам."""
    service = object.__new__(SubmissionService)
    service.redis = MagicMock()
    service.redis.lock.return_value = nullcontext()
    service.telegram = MagicMock()
    service.sheets = MagicMock()
    service.catalog_cache = MagicMock()
    pending = PendingSubmission(
        order_no="ORDER-1",
        spreadsheet_id="venue-sheet",
        rows=[{"№ Заявки": "ORDER-1"}],
    )
    service._load_pending = MagicMock(return_value=pending)  # type: ignore[method-assign]
    service._record = MagicMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(history_written=False)
    )
    service._get_record = MagicMock(  # type: ignore[method-assign]
        side_effect=[
            SimpleNamespace(catalog_updated=False),
            SimpleNamespace(recalc_done=False),
        ]
    )
    service._checkpoint = MagicMock()  # type: ignore[method-assign]
    final_state = ConversationState(last_order_no="ORDER-1", status="submitted")
    service._finalize = MagicMock(return_value=final_state)  # type: ignore[method-assign]
    service._send_completion = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr(submission_module, "chat_lock", lambda *_args, **_kwargs: nullcontext())

    service.submit("chat-1")

    service.sheets.append_history.assert_called_once_with(pending.rows, "venue-sheet")
    service.sheets.increment_catalog_quantities.assert_called_once_with(
        pending.rows, "venue-sheet"
    )
    service.sheets.trigger_recalculation.assert_called_once_with("ORDER-1", "venue-sheet")
    assert [call.args for call in service._checkpoint.call_args_list] == [
        ("ORDER-1", "history_written"),
        ("ORDER-1", "catalog_updated"),
        ("ORDER-1", "recalc_done"),
    ]
    service.catalog_cache.invalidate.assert_called_once_with("venue-sheet")
    service._send_completion.assert_called_once_with("chat-1", final_state, "ORDER-1")


def test_submission_without_venue_spreadsheet_never_writes_to_google(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Останавливает отправку, если у заявки потеряна таблица заведения."""
    service = object.__new__(SubmissionService)
    service.redis = MagicMock()
    service.telegram = MagicMock()
    service.sheets = MagicMock()
    service.catalog_cache = MagicMock()
    pending = PendingSubmission(order_no="ORDER-NO-SHEET", rows=[{"№ Заявки": "ORDER-NO-SHEET"}])
    service._load_pending = MagicMock(return_value=pending)  # type: ignore[method-assign]
    service._record = MagicMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(history_written=False)
    )
    service._remember_transient_error = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr(submission_module, "chat_lock", lambda *_args, **_kwargs: nullcontext())

    with pytest.raises(GoogleSheetsError, match="spreadsheet ID is required"):
        service.submit("chat-no-sheet", report_failure=False)

    service.sheets.assert_not_called()
    assert service.sheets.mock_calls == []


def test_submission_failure_is_persisted_and_reported_after_final_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Показывает карточку сбоя только после исчерпания повторов."""
    service = object.__new__(SubmissionService)
    service.redis = MagicMock()
    service.redis.lock.return_value = nullcontext()
    service.telegram = MagicMock()
    service.sheets = MagicMock()
    service.sheets.append_history.side_effect = RuntimeError("Google unavailable")
    pending = PendingSubmission(
        order_no="ORDER-2",
        spreadsheet_id="venue-sheet",
        rows=[{"№ Заявки": "ORDER-2"}],
    )
    service._load_pending = MagicMock(return_value=pending)  # type: ignore[method-assign]
    service._record = MagicMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(history_written=False)
    )
    failed_state = ConversationState(ui_revision=3)
    service._fail = MagicMock(return_value=failed_state)  # type: ignore[method-assign]
    service._remember_transient_error = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr(submission_module, "chat_lock", lambda *_args, **_kwargs: nullcontext())

    with pytest.raises(RuntimeError, match="Google unavailable"):
        service.submit("chat-2", report_failure=True)

    service._fail.assert_called_once_with("chat-2", "ORDER-2", "Google unavailable")
    reply = service.telegram.send_reply.call_args.args[1]
    assert "Отправка не завершена" in reply.text
    assert reply.rows[0][0].callback_data == "v2:submit:r3"
    service._remember_transient_error.assert_not_called()


def test_notification_failure_after_finalize_never_rolls_back_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Не откатывает заявку после успешной записи из-за сбоя Telegram."""
    service = object.__new__(SubmissionService)
    service.redis = MagicMock()
    service.redis.lock.return_value = nullcontext()
    service.telegram = MagicMock()
    service.sheets = MagicMock()
    service.catalog_cache = MagicMock()
    pending = PendingSubmission(order_no="ORDER-3", spreadsheet_id="venue-sheet", rows=[])
    service._load_pending = MagicMock(return_value=pending)  # type: ignore[method-assign]
    service._record = MagicMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(history_written=True)
    )
    service._get_record = MagicMock(  # type: ignore[method-assign]
        side_effect=[SimpleNamespace(catalog_updated=True), SimpleNamespace(recalc_done=True)]
    )
    final_state = ConversationState(last_order_no="ORDER-3", status="submitted")
    service._finalize = MagicMock(return_value=final_state)  # type: ignore[method-assign]
    service._send_completion = MagicMock(  # type: ignore[method-assign]
        side_effect=TimeoutError("Telegram timeout")
    )
    service._remember_transient_error = MagicMock()  # type: ignore[method-assign]
    service._fail = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr(submission_module, "chat_lock", lambda *_args, **_kwargs: nullcontext())

    with pytest.raises(TimeoutError, match="Telegram timeout"):
        service.submit("chat-3")

    service._remember_transient_error.assert_called_once_with("ORDER-3", "Telegram timeout")
    service._fail.assert_not_called()
