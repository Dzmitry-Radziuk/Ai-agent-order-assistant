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
from restaurant_bot.integrations.cache import google_submission_lock_key
from restaurant_bot.integrations.google_sheets import (
    GoogleSheetsError,
    OrderSubmissionResult,
    PreparedOrderSubmission,
)
from restaurant_bot.services import submission as submission_module
from restaurant_bot.services.submission import (
    SubmissionService,
    build_order_status_text,
    submission_disabled_reply,
    submission_dispatch_uncertain_reply,
    submission_failure_reply,
    submission_success_reply,
)


def test_submission_success_card_matches_n8n() -> None:
    """Проверяет, что отправка заявки успех карточка соответствует n8n."""
    reply = submission_success_reply(type("State", (), {"ui_revision": 4})(), "20260722-001")

    assert reply.text == "✅ <b>Заявка отправлена</b>\n\nНомер заявки: 20260722-001"
    assert [(button.text, button.callback_data) for row in reply.rows for button in row] == [
        ("Проверить статус", "v2:orders:r4"),
        ("Новая заявка", "v2:clear:r4"),
    ]


def test_submission_failure_card_matches_n8n() -> None:
    """Проверяет, что отправка заявки сбой карточка соответствует n8n."""
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
    """Проверяет, что заказ статус renderer группирует строки by заказ число."""
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


def test_order_status_renderer_uses_aggregated_history_fields() -> None:
    """Показывает поставщика, товары и контакты из сводного листа «История»."""
    text = build_order_status_text(
        [
            {
                "Номер заявки": "A-1",
                "Условное название поставщика": "Раджабов",
                "Список товаров": "Кухня:\n1. Говядина — 3 кг",
                "Стадия": "Заявка подтверждена",
                "ФИО менеджера Поставщика": "Иван Петров",
                "Телефон": "+375 29 000-00-00",
                "Дата поставки": "24.07.2026",
            }
        ],
        type("State", (), {"last_order_no": "A-1", "submitted_order_numbers": []})(),
    )

    assert "Поставщик: <b>Раджабов</b>" in text
    assert "Статус: <b>Заявка подтверждена</b>" in text
    assert "Кухня:\n1. Говядина — 3 кг" in text
    assert "Дата поставки: <b>24 июля 2026</b>" in text
    assert "Контакт поставщика: Иван Петров, +375 29 000-00-00" in text


def test_order_status_renderer_shows_each_supplier_separately() -> None:
    """Не смешивает статусы разных поставщиков одной заявки."""
    text = build_order_status_text(
        [
            {
                "Номер заявки": "A-1",
                "Условное название поставщика": "Раджабов",
                "Список товаров": "Говядина — 3 кг",
                "Стадия": "Подтверждена",
            },
            {
                "Номер заявки": "A-1",
                "Условное название поставщика": "МБР",
                "Список товаров": "Сироп Роза — 2 шт",
                "Стадия": "Ожидает подтверждения",
            },
        ],
        type("State", (), {"last_order_no": "A-1", "submitted_order_numbers": []})(),
    )

    assert text.count("Поставщик:") == 2
    assert "Поставщик: <b>Раджабов</b>" in text
    assert "Поставщик: <b>МБР</b>" in text
    assert "Статус: <b>Подтверждена</b>" in text
    assert "Статус: <b>Ожидает подтверждения</b>" in text


def test_order_status_renderer_handles_history_without_delivery_date() -> None:
    """Работает с текущей «Историей», где дата поставки ещё отсутствует."""
    text = build_order_status_text(
        [
            {
                "Номер заявки": "A-1",
                "Условное название поставщика": "Раджабов",
                "Список товаров": "Говядина — 3 кг",
                "Стадия": "Новая заявка",
            }
        ],
        type("State", (), {"last_order_no": "A-1", "submitted_order_numbers": []})(),
    )

    assert "Статус: <b>Новая заявка</b>" in text
    assert "Дата поставки:" not in text


def test_order_status_renderer_escapes_sheet_html_and_preserves_lines() -> None:
    """Не позволяет данным таблицы внедрить HTML в Telegram-сообщение."""
    text = build_order_status_text(
        [
            {
                "Номер заявки": "A-1",
                "Условное название поставщика": "<Раджабов>",
                "Список товаров": "Кухня:\n<script>товар</script>",
                "Стадия": "<b>Новая</b>",
            }
        ],
        type("State", (), {"last_order_no": "A-1", "submitted_order_numbers": []})(),
    )

    assert "&lt;Раджабов&gt;" in text
    assert "Кухня:\n&lt;script&gt;товар&lt;/script&gt;" in text
    assert "<script>" not in text
    assert "Статус: <b>&lt;b&gt;Новая&lt;/b&gt;</b>" in text


def test_successful_submission_clears_cart_checkpoint_and_marks_state_submitted() -> None:
    """Проверяет, что успешная отправка заявки очищает черновик checkpoint и marks состояние submitted."""
    state = ConversationState(
        order_trace_id="trace-1",
        cart=[CartItem(id="rose", source_query="Сироп Роза")],
        current_issue_item_id="rose",
        pending_submission=PendingSubmission(order_no="20260722-001"),
    )

    SubmissionService._apply_successful_submission_state(state, "20260722-001")

    assert state.cart == []
    assert state.pending_submission is None
    assert state.order_trace_id == ""
    assert state.current_issue_item_id == ""
    assert state.stage is SessionStage.SUBMITTED
    assert state.status == "submitted"
    assert state.last_order_no == "20260722-001"
    assert state.submitted_order_numbers == ["20260722-001"]


def test_order_status_limits_to_ten_tracked_orders_and_formats_delivery_date() -> None:
    """Проверяет, что заказ статус ограничивает в ten отслеживаемые orders и форматирует delivery date."""
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


def _record(**overrides: object) -> SimpleNamespace:
    """Создаёт тестовую запись этапов отправки."""
    values = {
        "catalog_updated": False,
        "recalc_done": False,
        "dispatch_started": False,
        "dispatch_completed": False,
        "dispatch_uncertain_notified": False,
        "external_order_no": None,
        "last_error": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _prepared_request(order_no: str = "ORDER-1") -> PreparedOrderSubmission:
    """Создаёт безопасный тестовый запрос без реального HTTP-вызова."""
    return PreparedOrderSubmission(
        url="https://example.test/submit",
        payload={"clientRequestId": order_no},
        timeout_seconds=60,
    )


def test_google_submission_lock_is_isolated_by_venue_spreadsheet() -> None:
    """Разные таблицы заведений не конкурируют за одну блокировку."""
    first_key = google_submission_lock_key("venue-sheet-1")
    same_key = google_submission_lock_key("  venue-sheet-1  ")
    second_key = google_submission_lock_key("venue-sheet-2")

    assert first_key == same_key
    assert first_key != second_key
    assert "venue-sheet-1" not in first_key


def test_transient_pre_dispatch_error_can_be_retried_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Разрешает повтор до начала необратимого вызова центрального скрипта."""
    service = object.__new__(SubmissionService)
    service.redis = MagicMock()
    service.redis.lock.return_value = nullcontext()
    service.telegram = MagicMock()
    service.sheets = MagicMock()
    service.catalog_cache = MagicMock()
    service.sheets.increment_catalog_quantities.side_effect = TimeoutError("temporary timeout")
    pending = PendingSubmission(order_no="ORDER-RETRY", spreadsheet_id="venue-sheet", rows=[])
    service._load_pending = MagicMock(return_value=pending)  # type: ignore[method-assign]
    service._record = MagicMock(return_value=_record())  # type: ignore[method-assign]
    service._get_record = MagicMock(return_value=_record())  # type: ignore[method-assign]
    service._remember_transient_error = MagicMock()  # type: ignore[method-assign]
    service._fail = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr(submission_module, "chat_lock", lambda *_args, **_kwargs: nullcontext())

    with pytest.raises(TimeoutError, match="temporary timeout"):
        service.submit("123", report_failure=False)

    service._remember_transient_error.assert_called_once_with("ORDER-RETRY", "temporary timeout")
    service._fail.assert_not_called()
    service.sheets.prepare_order_submission.assert_not_called()
    service.sheets.send_order_submission.assert_not_called()


def test_retry_delivers_success_card_after_order_was_already_finalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Повторно доставляет подтверждение уже завершённой заявки."""
    service = object.__new__(SubmissionService)
    service.redis = MagicMock()
    service._load_pending = MagicMock(return_value=None)  # type: ignore[method-assign]
    state = ConversationState(last_order_no="№00V63II4-000024", status="submitted")
    service._load_unnotified_completion = MagicMock(  # type: ignore[method-assign]
        return_value=("ORDER-INTERNAL", "№00V63II4-000024", state)
    )
    service._send_completion = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr(submission_module, "chat_lock", lambda *_args, **_kwargs: nullcontext())

    service.submit("123")

    service._send_completion.assert_called_once_with(
        "123",
        state,
        "№00V63II4-000024",
        "ORDER-INTERNAL",
    )


def test_success_card_is_checkpointed_only_after_telegram_accepts_it() -> None:
    """Фиксирует уведомление только после успешного ответа Telegram."""
    service = object.__new__(SubmissionService)
    service.telegram = MagicMock()
    service._checkpoint = MagicMock()  # type: ignore[method-assign]
    state = ConversationState(last_order_no="A-1", status="submitted")

    service._send_completion("123", state, "A-1", "ORDER-INTERNAL")

    service.telegram.send_reply.assert_called_once()
    service._checkpoint.assert_called_once_with("ORDER-INTERNAL", "completion_notified")


def test_submission_runs_all_external_stages_and_uses_external_order_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Обновляет заявку, вызывает скрипт один раз и сохраняет внешний номер."""
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
    result = OrderSubmissionResult(
        order_number="№00V63II4-000024",
        base_rows=2,
        request_rows=1,
        notifications={"telegram": {"sent": True}},
    )
    service.sheets.prepare_order_submission.return_value = _prepared_request()
    service.sheets.send_order_submission.return_value = result
    service._load_pending = MagicMock(return_value=pending)  # type: ignore[method-assign]
    service._record = MagicMock(return_value=_record())  # type: ignore[method-assign]
    service._get_record = MagicMock(  # type: ignore[method-assign]
        side_effect=[
            _record(),
            _record(catalog_updated=True),
            _record(catalog_updated=True, recalc_done=True),
        ]
    )
    service._checkpoint = MagicMock()  # type: ignore[method-assign]
    service._mark_dispatch_started = MagicMock()  # type: ignore[method-assign]
    service._mark_dispatch_completed = MagicMock()  # type: ignore[method-assign]
    final_state = ConversationState(last_order_no=result.order_number, status="submitted")
    service._finalize = MagicMock(return_value=final_state)  # type: ignore[method-assign]
    service._send_completion = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr(submission_module, "chat_lock", lambda *_args, **_kwargs: nullcontext())

    service.submit("chat-1")

    service.sheets.increment_catalog_quantities.assert_called_once_with(
        pending.rows,
        "venue-sheet",
    )
    service.sheets.trigger_recalculation.assert_called_once_with("ORDER-1", "venue-sheet")
    service.sheets.prepare_order_submission.assert_called_once_with("venue-sheet", "ORDER-1")
    service._mark_dispatch_started.assert_called_once_with("ORDER-1")
    service.sheets.send_order_submission.assert_called_once_with(_prepared_request())
    service._mark_dispatch_completed.assert_called_once_with("ORDER-1", result)
    assert [call.args for call in service._checkpoint.call_args_list] == [
        ("ORDER-1", "catalog_updated"),
        ("ORDER-1", "recalc_done"),
    ]
    service.catalog_cache.invalidate.assert_called_once_with("venue-sheet")
    service.redis.lock.assert_called_once_with(
        google_submission_lock_key("venue-sheet"),
        timeout=300,
        blocking_timeout=300,
    )
    service._finalize.assert_called_once_with("chat-1", "ORDER-1", result.order_number)
    service._send_completion.assert_called_once_with(
        "chat-1",
        final_state,
        result.order_number,
        "ORDER-1",
    )


def test_ambiguous_dispatch_failure_is_never_automatically_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Запрещает второй POST, когда результат первого вызова неизвестен."""
    service = object.__new__(SubmissionService)
    service.redis = MagicMock()
    service.redis.lock.return_value = nullcontext()
    service.telegram = MagicMock()
    service.sheets = MagicMock()
    service.catalog_cache = MagicMock()
    service.sheets.prepare_order_submission.return_value = _prepared_request("ORDER-2")
    service.sheets.send_order_submission.side_effect = TimeoutError("read timeout")
    pending = PendingSubmission(order_no="ORDER-2", spreadsheet_id="venue-sheet", rows=[])
    service._load_pending = MagicMock(return_value=pending)  # type: ignore[method-assign]
    service._record = MagicMock(return_value=_record())  # type: ignore[method-assign]
    completed_pre_dispatch = _record(catalog_updated=True, recalc_done=True)
    service._get_record = MagicMock(return_value=completed_pre_dispatch)  # type: ignore[method-assign]
    service._checkpoint = MagicMock()  # type: ignore[method-assign]
    service._mark_dispatch_started = MagicMock()  # type: ignore[method-assign]
    service._mark_dispatch_uncertain = MagicMock()  # type: ignore[method-assign]
    service._send_dispatch_uncertain_once = MagicMock()  # type: ignore[method-assign]
    service._finalize = MagicMock()  # type: ignore[method-assign]
    service._fail = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr(submission_module, "chat_lock", lambda *_args, **_kwargs: nullcontext())

    service.submit("chat-2")

    service._mark_dispatch_started.assert_called_once_with("ORDER-2")
    service.sheets.send_order_submission.assert_called_once()
    service._mark_dispatch_uncertain.assert_called_once_with("chat-2", "ORDER-2", "read timeout")
    service._send_dispatch_uncertain_once.assert_called_once_with("chat-2", "ORDER-2")
    service._finalize.assert_not_called()
    service._fail.assert_not_called()


def test_redelivery_after_started_dispatch_does_not_send_second_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Не повторяет центральный вызов после восстановления фоновой задачи."""
    service = object.__new__(SubmissionService)
    service.redis = MagicMock()
    service.redis.lock.return_value = nullcontext()
    service.telegram = MagicMock()
    service.sheets = MagicMock()
    service.catalog_cache = MagicMock()
    pending = PendingSubmission(order_no="ORDER-3", spreadsheet_id="venue-sheet", rows=[])
    service._load_pending = MagicMock(return_value=pending)  # type: ignore[method-assign]
    service._record = MagicMock(return_value=_record())  # type: ignore[method-assign]
    service._get_record = MagicMock(  # type: ignore[method-assign]
        return_value=_record(dispatch_started=True, last_error="worker restarted")
    )
    service._mark_dispatch_uncertain = MagicMock()  # type: ignore[method-assign]
    service._send_dispatch_uncertain_once = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr(submission_module, "chat_lock", lambda *_args, **_kwargs: nullcontext())

    service.submit("chat-3")

    service.sheets.prepare_order_submission.assert_not_called()
    service.sheets.send_order_submission.assert_not_called()
    service._mark_dispatch_uncertain.assert_called_once_with(
        "chat-3",
        "ORDER-3",
        "worker restarted",
    )
    service._send_dispatch_uncertain_once.assert_called_once_with("chat-3", "ORDER-3")


def test_submission_without_venue_spreadsheet_never_writes_to_google(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Останавливает отправку, если у заявки потеряна таблица заведения."""
    service = object.__new__(SubmissionService)
    service.redis = MagicMock()
    service.telegram = MagicMock()
    service.sheets = MagicMock()
    service.catalog_cache = MagicMock()
    pending = PendingSubmission(order_no="ORDER-NO-SHEET", rows=[])
    service._load_pending = MagicMock(return_value=pending)  # type: ignore[method-assign]
    service._record = MagicMock(return_value=_record())  # type: ignore[method-assign]
    service._remember_transient_error = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr(submission_module, "chat_lock", lambda *_args, **_kwargs: nullcontext())

    with pytest.raises(GoogleSheetsError, match="spreadsheet ID is required"):
        service.submit("chat-no-sheet", report_failure=False)

    assert service.sheets.mock_calls == []


def test_submission_failure_is_reported_before_dispatch_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Показывает безопасный повтор, если центральный вызов ещё не начинался."""
    service = object.__new__(SubmissionService)
    service.redis = MagicMock()
    service.redis.lock.return_value = nullcontext()
    service.telegram = MagicMock()
    service.sheets = MagicMock()
    service.catalog_cache = MagicMock()
    service.sheets.increment_catalog_quantities.side_effect = RuntimeError("Google unavailable")
    pending = PendingSubmission(order_no="ORDER-4", spreadsheet_id="venue-sheet", rows=[])
    service._load_pending = MagicMock(return_value=pending)  # type: ignore[method-assign]
    service._record = MagicMock(return_value=_record())  # type: ignore[method-assign]
    service._get_record = MagicMock(return_value=_record())  # type: ignore[method-assign]
    failed_state = ConversationState(ui_revision=3)
    service._fail = MagicMock(return_value=failed_state)  # type: ignore[method-assign]
    service._remember_transient_error = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr(submission_module, "chat_lock", lambda *_args, **_kwargs: nullcontext())

    with pytest.raises(RuntimeError, match="Google unavailable"):
        service.submit("chat-4", report_failure=True)

    service._fail.assert_called_once_with("chat-4", "ORDER-4", "Google unavailable")
    reply = service.telegram.send_reply.call_args.args[1]
    assert "Отправка не завершена" in reply.text
    assert reply.rows[0][0].callback_data == "v2:submit:r3"
    service.sheets.send_order_submission.assert_not_called()
    service._remember_transient_error.assert_not_called()


def test_notification_failure_after_finalize_never_rolls_back_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Не откатывает заявку после успешного скрипта из-за сбоя Telegram."""
    service = object.__new__(SubmissionService)
    service.redis = MagicMock()
    service.redis.lock.return_value = nullcontext()
    service.telegram = MagicMock()
    service.sheets = MagicMock()
    service.catalog_cache = MagicMock()
    pending = PendingSubmission(order_no="ORDER-5", spreadsheet_id="venue-sheet", rows=[])
    completed = _record(
        catalog_updated=True,
        recalc_done=True,
        dispatch_started=True,
        dispatch_completed=True,
        external_order_no="EXTERNAL-5",
    )
    service._load_pending = MagicMock(return_value=pending)  # type: ignore[method-assign]
    service._record = MagicMock(return_value=completed)  # type: ignore[method-assign]
    service._get_record = MagicMock(return_value=completed)  # type: ignore[method-assign]
    final_state = ConversationState(last_order_no="EXTERNAL-5", status="submitted")
    service._finalize = MagicMock(return_value=final_state)  # type: ignore[method-assign]
    service._send_completion = MagicMock(  # type: ignore[method-assign]
        side_effect=TimeoutError("Telegram timeout")
    )
    service._remember_transient_error = MagicMock()  # type: ignore[method-assign]
    service._fail = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr(submission_module, "chat_lock", lambda *_args, **_kwargs: nullcontext())

    with pytest.raises(TimeoutError, match="Telegram timeout"):
        service.submit("chat-5")

    service.sheets.send_order_submission.assert_not_called()
    service._remember_transient_error.assert_called_once_with("ORDER-5", "Telegram timeout")
    service._fail.assert_not_called()


def test_dispatch_uncertain_reply_has_no_repeat_button() -> None:
    """Не предлагает пользователю опасный повтор заявки."""
    state = ConversationState(ui_revision=7)

    reply = submission_dispatch_uncertain_reply(state, "ORDER-6")

    assert "не отправляйте её повторно" in reply.text.lower()
    assert "ORDER-6" in reply.text
    assert reply.rows == []


def test_disabled_submission_reply_is_explicit_and_has_no_buttons() -> None:
    """Объясняет безопасную блокировку без кнопки обхода."""
    reply = submission_disabled_reply()

    assert "Отправка отключена" in reply.text
    assert "Данные в таблицы и поставщикам не отправлялись" in reply.text
    assert reply.rows == []


def test_worker_guard_blocks_all_external_writes_when_submission_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Не позволяет старой фоновой задаче обойти выключатель отправки."""
    service = object.__new__(SubmissionService)
    service.settings = SimpleNamespace(google_order_submission_enabled=False)
    service.redis = MagicMock()
    service.telegram = MagicMock()
    service.sheets = MagicMock()
    pending = PendingSubmission(order_no="ORDER-DISABLED", spreadsheet_id="venue-sheet", rows=[])
    service._load_pending = MagicMock(return_value=pending)  # type: ignore[method-assign]
    service._record = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr(submission_module, "chat_lock", lambda *_args, **_kwargs: nullcontext())

    service.submit("chat-disabled")

    service._record.assert_not_called()
    assert service.sheets.mock_calls == []
    reply = service.telegram.send_reply.call_args.args[1]
    assert "Отправка отключена" in reply.text
