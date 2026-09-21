"""Проверяет поведение, связанное с модулем «test submission»."""

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
    CatalogMutationVerification,
    GoogleSheetsError,
    OrderSubmissionResult,
    PreparedOrderSubmission,
)
from restaurant_bot.presentation.telegram.formatting import format_status
from restaurant_bot.presentation.telegram.replies import cart_reply
from restaurant_bot.presentation.telegram.submission import (
    build_order_status_list_reply,
    order_status_detail_page_count,
)
from restaurant_bot.services import submission as submission_module
from restaurant_bot.services.submission import (
    SubmissionService,
    build_order_status_text,
    submission_catalog_conflict_reply,
    submission_catalog_uncertain_reply,
    submission_dispatch_uncertain_reply,
    submission_failure_reply,
    submission_local_saved_reply,
    submission_recalculation_uncertain_reply,
    submission_success_reply,
)


def test_submission_success_card_matches_n8n() -> None:
    """Проверяет, что отправка заявки успех карточка соответствует n8n."""
    reply = submission_success_reply(type("State", (), {"ui_revision": 4})(), "20260722-001")

    assert reply.text == "<i>Заявка отправлена</i>\n\nНомер заявки: 20260722-001"
    assert [(button.text, button.callback_data) for row in reply.rows for button in row] == [
        ("Проверить статус", "v2:orders:r4"),
        ("Новая заявка", "v2:clear:r4"),
    ]


def test_recalculation_uncertain_reply_explains_background_recovery() -> None:
    """Сообщение не требует повторно оформлять заявку и подсказывает reset."""
    reply = submission_recalculation_uncertain_reply(ConversationState(), "ORDER-1")

    assert "автоматически проверит" in reply.text
    assert "/reset" in reply.text
    assert "повторно оформлять" in reply.text


def test_large_draft_has_navigation_without_hiding_items() -> None:
    """Показывает большой черновик частями и даёт перейти к следующей странице."""
    state = ConversationState(
        cart=[
            CartItem(
                id=f"item-{index}",
                source_query=f"Товар {index}",
                catalog_name=f"Товар {index}",
                quantity=1,
                unit="шт",
            )
            for index in range(45)
        ]
    )

    first = cart_reply(state)
    state.cart_page = 4
    last = cart_reply(state)

    assert "Страница 1 из 5" in first.text
    assert "Товар 0" in first.text
    assert "Товар 10" not in first.text
    assert any(button.callback_data == "v2:cartpage:1" for row in first.rows for button in row)
    assert "Страница 5 из 5" in last.text
    assert "Товар 44" in last.text


def test_large_status_detail_has_pages() -> None:
    """Разбивает длинный список товаров поставщика на страницы истории."""
    product_list = "\n".join(f"{index}. Товар {index} — 1 шт" for index in range(25))
    rows = [
        {
            "Номер заявки": "A-1",
            "Условное название поставщика": "Поставщик",
            "Список товаров": product_list,
            "Стадия": "Новая заявка",
        }
    ]
    state = type("State", (), {"last_order_no": "A-1", "submitted_order_numbers": []})()

    assert order_status_detail_page_count(rows, state) > 1
    first = build_order_status_text(rows, state, detail_page=0)
    last = build_order_status_text(rows, state, detail_page=10)

    assert "Страница 1 из" in first
    assert "Товар 0" in first
    assert "Товар 10" not in first
    assert "Товар 24" in last


def test_submission_failure_card_matches_n8n() -> None:
    """Проверяет, что отправка заявки сбой карточка соответствует n8n."""
    reply = submission_failure_reply(type("State", (), {"ui_revision": 4})(), "20260722-001")

    assert reply.text == (
        "🔸 <b><u>Отправка не завершена</u></b>\n\n"
        "Заявка: 20260722-001\n\n"
        "Нажмите «Повторить отправку». Уже выполненные этапы будут пропущены."
    )
    assert [(button.text, button.callback_data) for row in reply.rows for button in row] == [
        ("Повторить отправку", "v2:submit:r4"),
        ("К черновику", "v2:back:r4"),
    ]


def test_catalog_recovery_replies_are_clear_and_have_no_retry_button() -> None:
    """Показывает понятное сообщение о сохранённой заявке без технических слов."""
    state = type("State", (), {"ui_revision": 4})()
    technical_words = ("checkpoint", "batchupdate", "read-back", "payload", "mutation", "state")

    for reply in (
        submission_catalog_uncertain_reply(state, "ORDER-1"),
        submission_catalog_conflict_reply(state, "ORDER-1"),
    ):
        text = reply.text.lower()
        assert not any(word in text for word in technical_words)
        assert all(button.text != "Повторить отправку" for row in reply.rows for button in row)


def test_order_status_renderer_groups_rows_by_order_number() -> None:
    """Проверяет, что представление статуса заказа группирует строки по номеру заказа."""
    text = build_order_status_text(
        [
            {"order_no": "A-1", "product_name": "Сироп Роза", "status": "Новая заявка"},
            {"order_no": "A-1", "product_name": "Говядина", "status": "Новая заявка"},
        ],
        type("State", (), {"last_order_no": "", "submitted_order_numbers": ["A-1"]})(),
    )

    assert "A-1" not in text
    assert "<b>1. Заявка</b>" in text
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
    assert "Кухня:\n1. <b>Говядина</b> — 3 кг" in text
    assert "Дата поставки: <b>24 июля 2026</b>" in text
    assert "Контакт поставщика: Иван Петров, +375 29 000-00-00" in text


def test_order_status_renderer_reuses_history_product_line_parser() -> None:
    """Показывает live-хвост цены отдельно и сохраняет дефис в названии товара."""
    text = build_order_status_text(
        [
            {
                "Номер заявки": "A-1",
                "Условное название поставщика": "Раджабов",
                "Список товаров": (
                    "Кухня:\n1. Вино белое Д/Я КУХНИ - 5 шт - 1037 руб.\n2. Соус - острый"
                ),
                "Стадия": "Заявка подтверждена",
            }
        ],
        type("State", (), {"last_order_no": "A-1", "submitted_order_numbers": []})(),
    )

    assert "1. <b>Вино белое Д/Я КУХНИ</b> - 5 шт - 1037 руб." in text
    assert "2. <b>Соус - острый</b>" in text


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


def test_local_submission_clears_draft_and_marks_state_saved_locally() -> None:
    """Завершает локальный черновик отдельно от реальной отправки."""
    state = ConversationState(
        order_trace_id="trace-local",
        cart=[CartItem(id="rose", source_query="Сироп Роза")],
        pending_submission=PendingSubmission(order_no="ORDER-LOCAL"),
    )

    SubmissionService._apply_successful_submission_state(
        state,
        "ORDER-LOCAL",
        status="saved_locally",
    )

    assert state.cart == []
    assert state.pending_submission is None
    assert state.stage is SessionStage.SUBMITTED
    assert state.status == "saved_locally"
    assert state.last_order_no == "ORDER-LOCAL"


def test_history_order_number_supports_current_and_legacy_aliases() -> None:
    """Автовосстановление читает номер заявки из всех поддержанных колонок."""
    assert SubmissionService._history_order_number({"Номер заявки": " A-1 "}) == "A-1"
    assert SubmissionService._history_order_number({"№ Заявки": "A-2"}) == "A-2"
    assert SubmissionService._history_order_number({"ID заявки": "A-3"}) == "A-3"
    assert SubmissionService._history_order_number({}) == ""


def test_pending_submission_checker_retries_active_snapshot_and_checks_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Планировщик разделяет безопасный retry и проверку уже начатой отправки."""

    class _ScalarResult:
        """Возвращает три контрольные записи для разных этапов восстановления."""

        def all(self) -> list[SimpleNamespace]:
            """Возвращает записи для retry, проверки отправки и перерасчёта."""
            return [
                SimpleNamespace(
                    order_no="ORDER-RETRY",
                    telegram_id="chat-retry",
                    payload=PendingSubmission(order_no="ORDER-RETRY").model_dump(mode="json"),
                    dispatch_started=False,
                    dispatch_completed=False,
                    recalc_status="pending",
                    created_at=None,
                ),
                SimpleNamespace(
                    order_no="ORDER-CHECK",
                    telegram_id="chat-check",
                    payload=PendingSubmission(order_no="ORDER-CHECK").model_dump(mode="json"),
                    dispatch_started=True,
                    dispatch_completed=False,
                    recalc_status="completed",
                    created_at=None,
                ),
                SimpleNamespace(
                    order_no="ORDER-RECALC",
                    telegram_id="chat-recalc",
                    payload=PendingSubmission(order_no="ORDER-RECALC").model_dump(mode="json"),
                    dispatch_started=False,
                    dispatch_completed=False,
                    recalc_status="uncertain",
                    created_at=None,
                ),
            ]

    class _Session:
        """Изображает минимальную транзакционную сессию для планировщика."""

        def __enter__(self):  # type: ignore[no-untyped-def]
            """Открывает тестовую сессию."""
            return self

        def __exit__(self, *_args: object) -> None:
            """Закрывает тестовую сессию без изменения результата."""
            return None

        def scalars(self, _query: object) -> _ScalarResult:
            """Возвращает подготовленные записи вместо обращения к базе."""
            return _ScalarResult()

    service = object.__new__(SubmissionService)
    service._has_retryable_pending_state = MagicMock(  # type: ignore[method-assign]
        return_value=True
    )
    service.submit = MagicMock()  # type: ignore[method-assign]
    service._recover_dispatch_from_history = MagicMock(  # type: ignore[method-assign]
        return_value=True
    )
    service._recover_recalculation = MagicMock(  # type: ignore[method-assign]
        return_value=True
    )
    service._has_active_pending_state = MagicMock(  # type: ignore[method-assign]
        return_value=False
    )
    service._finalize_detached_local_recalculation = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr(submission_module, "SessionLocal", lambda: _Session())

    assert service.check_pending_submissions() == 2
    service.submit.assert_called_once_with("chat-retry", report_failure=False)
    service._recover_dispatch_from_history.assert_called_once()
    service._recover_recalculation.assert_called_once()
    assert service._recover_recalculation.call_args.args[0] == "chat-recalc"
    assert service._recover_recalculation.call_args.args[1].order_no == "ORDER-RECALC"
    service._finalize_detached_local_recalculation.assert_called_once()
    assert (
        service._finalize_detached_local_recalculation.call_args.args[0].order_no == "ORDER-RECALC"
    )


def test_recalculation_recovery_reuses_snapshot_without_table_schema_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Восстановление повторяет только Apps Script-вызов из локального снимка."""

    class _Sessions:
        """Возвращает состояние чата и имитирует блокировку восстановления."""

        def get_for_update(self, _chat_id: str) -> tuple[None, ConversationState]:
            """Возвращает состояние заведения для теста перерасчёта."""
            return None, ConversationState(spreadsheet_id="venue-sheet")

    class _Session:
        """Изображает контекст транзакции при восстановлении перерасчёта."""

        def __enter__(self):  # type: ignore[no-untyped-def]
            """Открывает тестовую транзакцию."""
            return self

        def __exit__(self, *_args: object) -> None:
            """Закрывает тестовую транзакцию без побочного эффекта."""
            return None

    service = object.__new__(SubmissionService)
    service.redis = MagicMock()
    chat_redis_lock = MagicMock()
    chat_redis_lock.acquire.return_value = True
    chat_redis_lock.owned.return_value = True
    service.redis.lock.return_value = chat_redis_lock
    service.sheets = MagicMock()
    service.sheets.prepare_recalculation.return_value = MagicMock()
    service._has_current_access = MagicMock(return_value=True)  # type: ignore[method-assign]
    service._claim_recalculation_recovery = MagicMock(return_value=True)  # type: ignore[method-assign]
    service._mark_recalc_completed = MagicMock()  # type: ignore[method-assign]
    service._mark_recalc_uncertain = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr(submission_module, "SessionLocal", lambda: _Session())
    monkeypatch.setattr(submission_module, "SessionRepository", lambda _db: _Sessions())

    pending = PendingSubmission(order_no="ORDER-RECALC", spreadsheet_id="venue-sheet")

    assert service._recover_recalculation("chat-1", pending) is True
    service.sheets.prepare_recalculation.assert_called_once_with("venue-sheet")
    service.sheets.send_recalculation.assert_called_once()
    service._mark_recalc_completed.assert_called_once_with("ORDER-RECALC")
    service._mark_recalc_uncertain.assert_not_called()


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

    assert "<b>1. Заявка A-0</b>" not in text
    assert "<b>10. Заявка A-9</b>" not in text
    assert "<b>11. Заявка A-10</b>" not in text
    assert "24 июля 2026" in text


def _record(**overrides: object) -> SimpleNamespace:
    """Создаёт тестовую запись этапов отправки."""
    values = {
        "order_no": "ORDER-1",
        "catalog_updated": False,
        "catalog_update_status": "pending",
        "catalog_update_plan": {},
        "catalog_update_operation_id": "",
        "catalog_update_started_at": None,
        "catalog_update_completed_at": None,
        "recalc_done": False,
        "recalc_status": "pending",
        "recalc_operation_id": "",
        "recalc_started_at": None,
        "recalc_completed_at": None,
        "dispatch_started": False,
        "dispatch_completed": False,
        "dispatch_uncertain_notified": False,
        "external_order_no": None,
        "completion_notified": False,
        "completion_notification_status": "pending",
        "completion_notification_started_at": None,
        "completion_notification_completed_at": None,
        "last_error": None,
    }
    values.update(overrides)
    if "catalog_update_status" not in overrides:
        values["catalog_update_status"] = "completed" if values["catalog_updated"] else "pending"
    if "recalc_status" not in overrides:
        values["recalc_status"] = "completed" if values["recalc_done"] else "pending"
    if "completion_notification_status" not in overrides:
        values["completion_notification_status"] = (
            "completed" if values["completion_notified"] else "pending"
        )
    return SimpleNamespace(**values)


def _prepared_request(order_no: str = "ORDER-1") -> PreparedOrderSubmission:
    """Создаёт безопасный тестовый запрос без реального HTTP-вызова."""
    return PreparedOrderSubmission(
        url="https://example.test/submit",
        payload={"clientRequestId": order_no},
        timeout_seconds=60,
    )


def _catalog_plan(
    order_no: str = "ORDER-1", mutations: list[dict[str, object]] | None = None
) -> dict[str, object]:
    """Создаёт сериализуемый тестовый план изменения каталога."""
    return {
        "schema_version": 1,
        "operation_id": f"catalog:{order_no}",
        "order_no": order_no,
        "spreadsheet_id": "venue-sheet",
        "mutations": mutations
        if mutations is not None
        else [
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


def test_catalog_write_timeout_becomes_uncertain_and_is_not_reapplied() -> None:
    """После недоступной проверки не повторяет внешнюю запись вслепую."""
    service = object.__new__(SubmissionService)
    service.sheets = MagicMock()
    service.sheets.prepare_catalog_mutation.return_value = _catalog_plan()
    service.sheets.apply_catalog_mutation.side_effect = TimeoutError("write timeout")
    service.sheets.verify_catalog_mutation.return_value = CatalogMutationVerification.UNAVAILABLE
    service._persist_catalog_started = MagicMock()  # type: ignore[method-assign]
    service._mark_catalog_uncertain = MagicMock()  # type: ignore[method-assign]
    service._send_catalog_uncertain_reply = MagicMock()  # type: ignore[method-assign]
    pending = PendingSubmission(order_no="ORDER-1", spreadsheet_id="venue-sheet", rows=[])

    assert service._run_catalog_update("chat-1", pending, _record()) is False
    service.sheets.apply_catalog_mutation.assert_called_once()
    service._mark_catalog_uncertain.assert_called_once_with("chat-1", "ORDER-1", "write timeout")

    uncertain = _record(
        catalog_update_status="uncertain",
        catalog_update_plan=_catalog_plan(),
        catalog_update_operation_id="catalog:ORDER-1",
        last_error="write timeout",
    )
    assert service._run_catalog_update("chat-1", pending, uncertain) is False
    service.sheets.apply_catalog_mutation.assert_called_once()
    service.sheets.prepare_catalog_mutation.assert_called_once()


def test_catalog_success_marks_completed_after_one_exact_plan_apply() -> None:
    """Применяет сохранённый план и отмечает completed после проверки ячеек."""
    service = object.__new__(SubmissionService)
    service.sheets = MagicMock()
    plan = _catalog_plan()
    service.sheets.prepare_catalog_mutation.return_value = plan
    service.sheets.verify_catalog_mutation.return_value = CatalogMutationVerification.APPLIED
    service._persist_catalog_started = MagicMock()  # type: ignore[method-assign]
    service._checkpoint = MagicMock()  # type: ignore[method-assign]
    pending = PendingSubmission(order_no="ORDER-1", spreadsheet_id="venue-sheet", rows=[])

    assert service._run_catalog_update("chat-1", pending, _record()) is True
    service._persist_catalog_started.assert_called_once_with("ORDER-1", plan)
    service.sheets.apply_catalog_mutation.assert_called_once_with(plan)
    service._checkpoint.assert_called_once_with("ORDER-1", "catalog_updated")


def test_empty_catalog_plan_completes_without_external_write() -> None:
    """Завершает пустой план без вызова Google Sheets."""
    service = object.__new__(SubmissionService)
    service.sheets = MagicMock()
    plan = _catalog_plan(mutations=[])
    service.sheets.prepare_catalog_mutation.return_value = plan
    service._persist_catalog_completed = MagicMock()  # type: ignore[method-assign]
    pending = PendingSubmission(order_no="ORDER-1", spreadsheet_id="venue-sheet", rows=[])

    assert service._run_catalog_update("chat-1", pending, _record()) is True
    service._persist_catalog_completed.assert_called_once_with("ORDER-1", plan)
    service.sheets.apply_catalog_mutation.assert_not_called()


def test_empty_catalog_plan_is_rejected_for_positive_order_rows() -> None:
    """Не сообщает об успехе, если план потерял товар из заявки."""
    service = object.__new__(SubmissionService)
    service.settings = SimpleNamespace(default_department="Кухня")
    service.sheets = MagicMock()
    plan = _catalog_plan(mutations=[])
    service.sheets.prepare_catalog_mutation.return_value = plan
    service._mark_catalog_conflict = MagicMock()  # type: ignore[method-assign]
    service._send_catalog_conflict_reply = MagicMock()  # type: ignore[method-assign]
    pending = PendingSubmission(
        order_no="ORDER-1",
        spreadsheet_id="venue-sheet",
        rows=[{"ID товара": "rose", "Кол-во": 5, "_department": "Кухня"}],
    )

    assert service._run_catalog_update("chat-1", pending, _record()) is False
    service._mark_catalog_conflict.assert_called_once()
    service.sheets.apply_catalog_mutation.assert_not_called()


def test_partial_catalog_plan_is_rejected_for_two_departments() -> None:
    """Не допускает потерю Зала или Бара из одной фотографии."""
    service = object.__new__(SubmissionService)
    service.settings = SimpleNamespace(default_department="Кухня")
    plan = _catalog_plan()
    pending = PendingSubmission(
        order_no="ORDER-1",
        spreadsheet_id="venue-sheet",
        rows=[
            {"ID товара": "rose", "Кол-во": 5, "_department": "Кухня"},
            {"ID товара": "rose", "Кол-во": 2, "_department": "Бар"},
        ],
    )

    error = service._catalog_plan_validation_error(plan, pending, _record())

    assert "не все позиции и подразделения" in error


def test_catalog_checkpoint_failure_keeps_started_gate_for_next_retry() -> None:
    """После сбоя checkpoint следующий retry не повторяет внешний write."""
    service = object.__new__(SubmissionService)
    service.sheets = MagicMock()
    service.sheets.prepare_catalog_mutation.return_value = _catalog_plan()
    service.sheets.verify_catalog_mutation.return_value = CatalogMutationVerification.APPLIED
    service._persist_catalog_started = MagicMock()  # type: ignore[method-assign]
    service._checkpoint = MagicMock(side_effect=RuntimeError("database unavailable"))  # type: ignore[method-assign]
    pending = PendingSubmission(order_no="ORDER-1", spreadsheet_id="venue-sheet", rows=[])

    with pytest.raises(RuntimeError, match="database unavailable"):
        service._run_catalog_update("chat-1", pending, _record())

    service._checkpoint = MagicMock()  # type: ignore[method-assign]
    started = _record(
        catalog_update_status="started",
        catalog_update_plan=_catalog_plan(),
        catalog_update_operation_id="catalog:ORDER-1",
        last_error="database unavailable",
    )
    assert service._run_catalog_update("chat-1", pending, started) is True
    service.sheets.apply_catalog_mutation.assert_called_once()


def test_completed_catalog_checkpoint_skips_mutation() -> None:
    """Не вызывает Google Sheets для уже завершённого catalog stage."""
    service = object.__new__(SubmissionService)
    service.sheets = MagicMock()
    pending = PendingSubmission(order_no="ORDER-1", spreadsheet_id="venue-sheet", rows=[])

    assert service._run_catalog_update("chat-1", pending, _record(catalog_updated=True)) is True
    service.sheets.prepare_catalog_mutation.assert_not_called()
    service.sheets.apply_catalog_mutation.assert_not_called()


def test_catalog_cache_failure_after_completion_does_not_reapply() -> None:
    """Не повторяет запись, если сбой произошёл после checkpoint при очистке кэша."""
    service = object.__new__(SubmissionService)
    service.sheets = MagicMock()
    plan = _catalog_plan()
    service.sheets.prepare_catalog_mutation.return_value = plan
    service.sheets.verify_catalog_mutation.return_value = CatalogMutationVerification.APPLIED
    service._persist_catalog_started = MagicMock()  # type: ignore[method-assign]
    service._checkpoint = MagicMock()  # type: ignore[method-assign]
    service.catalog_cache = MagicMock()
    pending = PendingSubmission(order_no="ORDER-1", spreadsheet_id="venue-sheet", rows=[])

    assert service._run_catalog_update("chat-1", pending, _record()) is True
    service.catalog_cache.invalidate.side_effect = RuntimeError("cache unavailable")
    with pytest.raises(RuntimeError, match="cache unavailable"):
        service.catalog_cache.invalidate("venue-sheet")

    completed = _record(catalog_updated=True)
    assert service._run_catalog_update("chat-1", pending, completed) is True
    service.sheets.apply_catalog_mutation.assert_called_once_with(plan)


def _cache_submission_service(
    monkeypatch: pytest.MonkeyPatch,
    records: list[SimpleNamespace],
    *,
    invalidate_results: list[Exception | None],
    recalc_results: list[Exception | None] | None = None,
) -> SubmissionService:
    """Создаёт сервис для проверки границы кэша и пересчёта."""
    service = object.__new__(SubmissionService)
    service.settings = SimpleNamespace(google_order_submission_enabled=False)
    service.redis = MagicMock()
    service.redis.lock.return_value = nullcontext()
    service.telegram = MagicMock()
    service.sheets = MagicMock()
    service.catalog_cache = MagicMock()
    service.catalog_cache.invalidate.side_effect = invalidate_results
    service.sheets.prepare_recalculation.return_value = MagicMock()
    if recalc_results is not None:
        service.sheets.send_recalculation.side_effect = recalc_results
    pending = PendingSubmission(order_no="ORDER-CACHE", spreadsheet_id="venue-sheet", rows=[])
    service._load_pending = MagicMock(return_value=pending)  # type: ignore[method-assign]
    service._record = MagicMock(return_value=records[0])  # type: ignore[method-assign]
    service._get_record = MagicMock(side_effect=records)  # type: ignore[method-assign]
    service._run_catalog_update = MagicMock(return_value=True)  # type: ignore[method-assign]
    service._checkpoint = MagicMock()  # type: ignore[method-assign]
    service._mark_recalc_started = MagicMock()  # type: ignore[method-assign]
    service._mark_recalc_completed = MagicMock()  # type: ignore[method-assign]
    service._mark_recalc_uncertain = MagicMock()  # type: ignore[method-assign]
    service._send_recalc_uncertain_reply = MagicMock()  # type: ignore[method-assign]
    service._finalize = MagicMock(  # type: ignore[method-assign]
        return_value=ConversationState(status="saved_locally")
    )
    service._send_local_completion = MagicMock()  # type: ignore[method-assign]
    service._remember_transient_error = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr(submission_module, "chat_lock", lambda *_args, **_kwargs: nullcontext())
    return service


def test_completed_catalog_with_unfinished_recalc_invalidates_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Инвалидирует кэш перед незавершённым пересчётом без новой записи каталога."""
    record = _record(catalog_updated=True, recalc_done=False)
    service = _cache_submission_service(
        monkeypatch,
        [record, record, _record(catalog_updated=True, recalc_done=True)],
        invalidate_results=[None],
        recalc_results=[None],
    )

    service.submit("chat-1", report_failure=False)

    service.catalog_cache.invalidate.assert_called_once_with("venue-sheet")
    service.sheets.send_recalculation.assert_called_once()
    service.sheets.prepare_catalog_mutation.assert_not_called()
    service.sheets.apply_catalog_mutation.assert_not_called()


def test_cache_failure_after_catalog_completion_stops_before_recalc_and_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Останавливает submission до пересчёта и отправки при сбое кэша."""
    record = _record(catalog_updated=True, recalc_done=False)
    service = _cache_submission_service(
        monkeypatch,
        [record, record],
        invalidate_results=[RuntimeError("cache unavailable")],
        recalc_results=[None],
    )

    with pytest.raises(RuntimeError, match="cache unavailable"):
        service.submit("chat-1", report_failure=False)

    service.catalog_cache.invalidate.assert_called_once_with("venue-sheet")
    service.sheets.send_recalculation.assert_not_called()
    service._mark_recalc_started.assert_not_called()
    service.sheets.prepare_order_submission.assert_not_called()
    service.sheets.send_order_submission.assert_not_called()
    service._finalize.assert_not_called()


def test_cache_failure_retry_invalidates_again_without_catalog_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Повторяет удаление кэша после сбоя без повторной мутации каталога."""
    record = _record(catalog_updated=True, recalc_done=False)
    service = _cache_submission_service(
        monkeypatch,
        [record, record, record, record, _record(catalog_updated=True, recalc_done=True)],
        invalidate_results=[RuntimeError("cache unavailable"), None],
        recalc_results=[None],
    )

    with pytest.raises(RuntimeError, match="cache unavailable"):
        service.submit("chat-1", report_failure=False)
    service.submit("chat-1", report_failure=False)

    assert service.catalog_cache.invalidate.call_count == 2
    service.sheets.send_recalculation.assert_called_once()
    service.sheets.prepare_catalog_mutation.assert_not_called()
    service.sheets.apply_catalog_mutation.assert_not_called()


def test_repeated_cache_failure_never_reapplies_catalog_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Не выполняет каталожную запись при повторных сбоях кэша."""
    record = _record(catalog_updated=True, recalc_done=False)
    service = _cache_submission_service(
        monkeypatch,
        [record, record, record, record],
        invalidate_results=[RuntimeError("cache unavailable"), RuntimeError("cache unavailable")],
        recalc_results=[None],
    )

    for _ in range(2):
        with pytest.raises(RuntimeError, match="cache unavailable"):
            service.submit("chat-1", report_failure=False)

    assert service.catalog_cache.invalidate.call_count == 2
    service.sheets.send_recalculation.assert_not_called()
    service.sheets.prepare_catalog_mutation.assert_not_called()
    service.sheets.apply_catalog_mutation.assert_not_called()


def test_recalc_failure_blocks_retry_without_second_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Блокирует повтор после неизвестного результата пересчёта."""
    record = _record(catalog_updated=True, recalc_done=False)
    started = _record(catalog_updated=True, recalc_status="uncertain")
    service = _cache_submission_service(
        monkeypatch,
        [record, record, record, started],
        invalidate_results=[None],
        recalc_results=[RuntimeError("recalc unavailable")],
    )

    service.submit("chat-1", report_failure=False)
    service.submit("chat-1", report_failure=False)

    assert service.catalog_cache.invalidate.call_count == 1
    assert service.sheets.send_recalculation.call_count == 1
    assert service._send_recalc_uncertain_reply.call_count == 2
    service.sheets.prepare_catalog_mutation.assert_not_called()
    service.sheets.apply_catalog_mutation.assert_not_called()


def test_completed_recalc_skips_unnecessary_cache_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Не удаляет кэш повторно, если пересчёт уже завершён."""
    record = _record(catalog_updated=True, recalc_done=True)
    service = _cache_submission_service(
        monkeypatch,
        [record, record, record],
        invalidate_results=[None],
        recalc_results=[None],
    )

    service.submit("chat-1", report_failure=False)

    service.catalog_cache.invalidate.assert_not_called()
    service.sheets.send_recalculation.assert_not_called()


def test_started_recalc_stops_without_cache_or_second_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Останавливает заявку при сохранённом started без нового вызова."""
    started = _record(catalog_updated=True, recalc_status="started")
    service = _cache_submission_service(
        monkeypatch,
        [started, started],
        invalidate_results=[None],
        recalc_results=[None],
    )

    service.submit("chat-1", report_failure=False)

    service.catalog_cache.invalidate.assert_not_called()
    service.sheets.send_recalculation.assert_not_called()
    service._mark_recalc_uncertain.assert_called_once()
    service._finalize.assert_not_called()


def test_recalculation_prepare_failure_stays_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Оставляет пересчёт pending при безопасном сбое подготовки."""
    record = _record(catalog_updated=True, recalc_done=False)
    service = _cache_submission_service(
        monkeypatch,
        [record, record],
        invalidate_results=[None],
        recalc_results=[None],
    )
    service.sheets.prepare_recalculation.side_effect = GoogleSheetsError("bad settings")

    with pytest.raises(GoogleSheetsError, match="bad settings"):
        service.submit("chat-1", report_failure=False)

    service.catalog_cache.invalidate.assert_not_called()
    service.sheets.send_recalculation.assert_not_called()
    service._mark_recalc_started.assert_not_called()


def test_recalculation_checkpoint_failure_blocks_follow_up_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Не повторяет пересчёт после сбоя checkpoint успешного вызова."""
    pending = _record(catalog_updated=True, recalc_done=False)
    started = _record(catalog_updated=True, recalc_status="started")
    service = _cache_submission_service(
        monkeypatch,
        [pending, pending, started, started],
        invalidate_results=[None],
        recalc_results=[None],
    )
    service._mark_recalc_completed.side_effect = RuntimeError("database unavailable")

    service.submit("chat-1", report_failure=False)
    service.submit("chat-1", report_failure=False)

    assert service.sheets.send_recalculation.call_count == 1
    service._mark_recalc_uncertain.assert_called()
    service._finalize.assert_not_called()


def test_downstream_prepare_failure_does_not_reclassify_completed_recalc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Относит сбой подготовки dispatch к последующему этапу."""
    pending = _record(catalog_updated=True, recalc_done=False)
    completed = _record(catalog_updated=True, recalc_done=True)
    service = _cache_submission_service(
        monkeypatch,
        [pending, pending, completed],
        invalidate_results=[None],
        recalc_results=[None],
    )
    service.settings.google_order_submission_enabled = True
    service.sheets.prepare_order_submission.side_effect = RuntimeError("dispatch prepare failed")

    with pytest.raises(RuntimeError, match="dispatch prepare failed"):
        service.submit("chat-1", report_failure=False)

    service._mark_recalc_uncertain.assert_not_called()
    service.sheets.send_recalculation.assert_called_once()
    service._finalize.assert_not_called()


def test_finalize_failure_does_not_reclassify_completed_recalc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Относит сбой финализации к последующему этапу заявки."""
    pending = _record(catalog_updated=True, recalc_done=False)
    completed = _record(catalog_updated=True, recalc_done=True)
    service = _cache_submission_service(
        monkeypatch,
        [pending, pending, completed],
        invalidate_results=[None],
        recalc_results=[None],
    )
    service._finalize.side_effect = RuntimeError("finalize failed")

    with pytest.raises(RuntimeError, match="finalize failed"):
        service.submit("chat-1", report_failure=False)

    service._mark_recalc_uncertain.assert_not_called()
    service.sheets.send_recalculation.assert_called_once()


def _recovery_service(*verifications: CatalogMutationVerification) -> SubmissionService:
    """Создаёт сервис с контролируемым ответом проверки каталога."""
    service = object.__new__(SubmissionService)
    service.sheets = MagicMock()
    service.sheets.verify_catalog_mutation.side_effect = list(verifications)
    service._checkpoint = MagicMock()  # type: ignore[method-assign]
    service._mark_catalog_uncertain = MagicMock()  # type: ignore[method-assign]
    service._send_catalog_uncertain_reply = MagicMock()  # type: ignore[method-assign]
    service._mark_catalog_conflict = MagicMock()  # type: ignore[method-assign]
    service._send_catalog_conflict_reply = MagicMock()  # type: ignore[method-assign]
    return service


def _started_record(
    plan: dict[str, object],
    status: str = "started",
) -> SimpleNamespace:
    """Создаёт запись с уже сохранённым планом каталога."""
    return _record(
        order_no="ORDER-1",
        catalog_update_status=status,
        catalog_update_plan=plan,
        catalog_update_operation_id="catalog:ORDER-1",
    )


def test_started_applied_completes_without_catalog_write() -> None:
    """Переводит started в completed после подтверждения expected_after без записи."""
    plan = _catalog_plan()
    service = _recovery_service(CatalogMutationVerification.APPLIED)
    pending = PendingSubmission(order_no="ORDER-1", spreadsheet_id="venue-sheet", rows=[])

    assert service._run_catalog_update("chat-1", pending, _started_record(plan)) is True
    service.sheets.apply_catalog_mutation.assert_not_called()
    service._checkpoint.assert_called_once_with("ORDER-1", "catalog_updated")


def test_uncertain_applied_completes_without_catalog_write() -> None:
    """Восстанавливает uncertain по фактическому expected_after без записи."""
    plan = _catalog_plan()
    service = _recovery_service(CatalogMutationVerification.APPLIED)
    pending = PendingSubmission(order_no="ORDER-1", spreadsheet_id="venue-sheet", rows=[])

    assert (
        service._run_catalog_update("chat-1", pending, _started_record(plan, "uncertain")) is True
    )
    service.sheets.apply_catalog_mutation.assert_not_called()


def test_started_before_allows_one_controlled_write_then_completes() -> None:
    """Разрешает одну запись после before и подтверждает её expected_after."""
    plan = _catalog_plan()
    service = _recovery_service(
        CatalogMutationVerification.NOT_APPLIED,
        CatalogMutationVerification.APPLIED,
    )
    pending = PendingSubmission(order_no="ORDER-1", spreadsheet_id="venue-sheet", rows=[])

    assert service._run_catalog_update("chat-1", pending, _started_record(plan)) is True
    service.sheets.apply_catalog_mutation.assert_called_once_with(plan)


def test_uncertain_before_allows_one_controlled_write_then_completes() -> None:
    """Применяет тот же controlled recovery для статуса uncertain."""
    plan = _catalog_plan()
    service = _recovery_service(
        CatalogMutationVerification.NOT_APPLIED,
        CatalogMutationVerification.APPLIED,
    )
    pending = PendingSubmission(order_no="ORDER-1", spreadsheet_id="venue-sheet", rows=[])

    assert (
        service._run_catalog_update("chat-1", pending, _started_record(plan, "uncertain")) is True
    )
    service.sheets.apply_catalog_mutation.assert_called_once_with(plan)


def test_controlled_write_exception_recovers_when_readback_is_applied() -> None:
    """Считает операцию успешной после сбоя ответа и подтверждённого read-back."""
    plan = _catalog_plan()
    service = _recovery_service(
        CatalogMutationVerification.NOT_APPLIED,
        CatalogMutationVerification.APPLIED,
    )
    service.sheets.apply_catalog_mutation.side_effect = TimeoutError("write timeout")
    pending = PendingSubmission(order_no="ORDER-1", spreadsheet_id="venue-sheet", rows=[])

    assert service._run_catalog_update("chat-1", pending, _started_record(plan)) is True
    service.sheets.apply_catalog_mutation.assert_called_once_with(plan)


def test_controlled_write_exception_without_readback_stays_uncertain() -> None:
    """Оставляет uncertain, если controlled recovery нельзя проверить."""
    plan = _catalog_plan()
    service = _recovery_service(
        CatalogMutationVerification.NOT_APPLIED,
        CatalogMutationVerification.UNAVAILABLE,
    )
    service.sheets.apply_catalog_mutation.side_effect = TimeoutError("write timeout")
    pending = PendingSubmission(order_no="ORDER-1", spreadsheet_id="venue-sheet", rows=[])

    assert service._run_catalog_update("chat-1", pending, _started_record(plan)) is False
    service._mark_catalog_uncertain.assert_called_once()
    service.sheets.apply_catalog_mutation.assert_called_once_with(plan)


def test_conflict_stops_without_write_recalculation_or_dispatch() -> None:
    """Останавливает конфликт до записи, пересчёта и отправки заявки."""
    plan = _catalog_plan()
    service = _recovery_service(CatalogMutationVerification.CONFLICT)
    pending = PendingSubmission(order_no="ORDER-1", spreadsheet_id="venue-sheet", rows=[])

    assert service._run_catalog_update("chat-1", pending, _started_record(plan)) is False
    service.sheets.apply_catalog_mutation.assert_not_called()
    service._mark_catalog_conflict.assert_called_once()


def test_mixed_values_stop_without_write() -> None:
    """Не перезаписывает каталог при смешанном состоянии ячеек."""
    plan = _catalog_plan()
    service = _recovery_service(CatalogMutationVerification.CONFLICT)
    pending = PendingSubmission(order_no="ORDER-1", spreadsheet_id="venue-sheet", rows=[])

    assert service._run_catalog_update("chat-1", pending, _started_record(plan)) is False
    service.sheets.apply_catalog_mutation.assert_not_called()


def test_verification_unavailable_stops_without_write() -> None:
    """Останавливает восстановление без записи при недоступном чтении."""
    plan = _catalog_plan()
    service = _recovery_service(CatalogMutationVerification.UNAVAILABLE)
    pending = PendingSubmission(order_no="ORDER-1", spreadsheet_id="venue-sheet", rows=[])

    assert service._run_catalog_update("chat-1", pending, _started_record(plan)) is False
    service.sheets.apply_catalog_mutation.assert_not_called()
    service._mark_catalog_uncertain.assert_called_once()


def test_initial_apply_is_verified_before_completion() -> None:
    """Завершает первый путь только после подтверждения expected_after."""
    plan = _catalog_plan()
    service = _recovery_service(CatalogMutationVerification.APPLIED)
    service.sheets.prepare_catalog_mutation.return_value = plan
    service._persist_catalog_started = MagicMock()  # type: ignore[method-assign]
    pending = PendingSubmission(order_no="ORDER-1", spreadsheet_id="venue-sheet", rows=[])

    assert service._run_catalog_update("chat-1", pending, _record()) is True
    service.sheets.apply_catalog_mutation.assert_called_once_with(plan)
    service._checkpoint.assert_called_once_with("ORDER-1", "catalog_updated")


def test_initial_apply_success_without_readback_does_not_retry() -> None:
    """Не выполняет второй write, если первый ответ есть, а read-back недоступен."""
    plan = _catalog_plan()
    service = _recovery_service(CatalogMutationVerification.UNAVAILABLE)
    service.sheets.prepare_catalog_mutation.return_value = plan
    service._persist_catalog_started = MagicMock()  # type: ignore[method-assign]
    pending = PendingSubmission(order_no="ORDER-1", spreadsheet_id="venue-sheet", rows=[])

    assert service._run_catalog_update("chat-1", pending, _record()) is False
    service.sheets.apply_catalog_mutation.assert_called_once_with(plan)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("operation_id", "catalog:OTHER"),
        ("order_no", "ORDER-OTHER"),
        ("spreadsheet_id", "other-sheet"),
    ],
)
def test_invalid_persisted_plan_marks_conflict_without_write(field: str, value: str) -> None:
    """Не применяет план с чужими идентификаторами и фиксирует безопасный конфликт."""
    plan = _catalog_plan()
    plan[field] = value
    service = _recovery_service()
    pending = PendingSubmission(order_no="ORDER-1", spreadsheet_id="venue-sheet", rows=[])

    assert service._run_catalog_update("chat-1", pending, _started_record(plan)) is False
    service.sheets.apply_catalog_mutation.assert_not_called()
    service._mark_catalog_conflict.assert_called_once()


def test_google_submission_lock_is_isolated_by_venue_spreadsheet() -> None:
    """Разные таблицы заведений не конкурируют за одну блокировку."""
    first_key = google_submission_lock_key("venue-sheet-1")
    same_key = google_submission_lock_key("  venue-sheet-1  ")
    second_key = google_submission_lock_key("venue-sheet-2")

    assert first_key == same_key
    assert first_key != second_key
    assert "venue-sheet-1" not in first_key


def test_catalog_prepare_failure_before_apply_can_be_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Разрешает повтор ошибки чтения каталога до внешней мутации."""
    service = object.__new__(SubmissionService)
    service.redis = MagicMock()
    service.redis.lock.return_value = nullcontext()
    service.telegram = MagicMock()
    service.sheets = MagicMock()
    service.catalog_cache = MagicMock()
    service.sheets.prepare_catalog_mutation.side_effect = TimeoutError("catalog read failed")
    pending = PendingSubmission(order_no="ORDER-RETRY", spreadsheet_id="venue-sheet", rows=[])
    service._load_pending = MagicMock(return_value=pending)  # type: ignore[method-assign]
    service._record = MagicMock(return_value=_record(order_no="ORDER-LOCAL"))  # type: ignore[method-assign]
    service._get_record = MagicMock(return_value=_record())  # type: ignore[method-assign]
    service._remember_transient_error = MagicMock()  # type: ignore[method-assign]
    service._fail = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr(submission_module, "chat_lock", lambda *_args, **_kwargs: nullcontext())

    with pytest.raises(TimeoutError, match="catalog read failed"):
        service.submit("123", report_failure=False)

    service._remember_transient_error.assert_called_once_with("ORDER-RETRY", "catalog read failed")
    service._fail.assert_not_called()
    service.sheets.apply_catalog_mutation.assert_not_called()
    service.sheets.prepare_order_submission.assert_not_called()
    service.sheets.send_order_submission.assert_not_called()


def test_transient_pre_dispatch_error_can_be_retried_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сохраняет старый scenario id только для definitely-before-apply ошибки."""
    test_catalog_prepare_failure_before_apply_can_be_retried(monkeypatch)


def test_retry_delivers_success_card_after_order_was_already_finalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Повторно доставляет подтверждение уже завершённой заявки."""
    service = object.__new__(SubmissionService)
    service.redis = MagicMock()
    service._load_pending = MagicMock(return_value=None)  # type: ignore[method-assign]
    state = ConversationState(last_order_no="№00V63II4-000024", status="submitted")
    service._load_unnotified_completion = MagicMock(  # type: ignore[method-assign]
        return_value=("ORDER-INTERNAL", "№00V63II4-000024", state, True)
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


def test_retry_delivers_local_saved_card_without_claiming_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Повторно доставляет подтверждение локальной записи без ложной отправки."""
    service = object.__new__(SubmissionService)
    service.redis = MagicMock()
    service._load_pending = MagicMock(return_value=None)  # type: ignore[method-assign]
    state = ConversationState(last_order_no="ORDER-LOCAL", status="saved_locally")
    service._load_unnotified_completion = MagicMock(  # type: ignore[method-assign]
        return_value=("ORDER-LOCAL", "ORDER-LOCAL", state, False)
    )
    service._send_completion = MagicMock()  # type: ignore[method-assign]
    service._send_local_completion = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr(submission_module, "chat_lock", lambda *_args, **_kwargs: nullcontext())

    service.submit("123")

    service._send_completion.assert_not_called()
    service._send_local_completion.assert_called_once_with(
        "123",
        state,
        "ORDER-LOCAL",
    )


def test_success_card_is_checkpointed_only_after_telegram_accepts_it() -> None:
    """Фиксирует уведомление после успешного ответа Telegram."""
    service = object.__new__(SubmissionService)
    service.telegram = MagicMock()
    lifecycle: list[str] = []
    service._mark_completion_notification_started = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda _order_no: lifecycle.append("started") or True
    )
    service._mark_completion_notification_completed = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda _order_no: lifecycle.append("completed")
    )
    service._mark_completion_notification_uncertain = MagicMock()  # type: ignore[method-assign]
    service.telegram.send_reply.side_effect = lambda *_args: lifecycle.append("telegram")
    state = ConversationState(last_order_no="A-1", status="submitted")

    service._send_completion("123", state, "A-1", "ORDER-INTERNAL")

    service.telegram.send_reply.assert_called_once()
    assert lifecycle == ["started", "telegram", "completed"]
    service._mark_completion_notification_uncertain.assert_not_called()


def test_completion_notification_does_not_send_when_start_checkpoint_fails() -> None:
    """Не отправляет карточку, если контрольная точка старта не сохранена."""
    service = object.__new__(SubmissionService)
    service.telegram = MagicMock()
    service._mark_completion_notification_started = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("database unavailable")
    )
    state = ConversationState(last_order_no="A-1", status="submitted")

    with pytest.raises(RuntimeError, match="database unavailable"):
        service._send_completion("123", state, "A-1", "ORDER-INTERNAL")

    service.telegram.send_reply.assert_not_called()


def test_completion_notification_send_failure_marks_uncertain_without_retry() -> None:
    """Помечает неизвестный результат Telegram и не повторяет отправку автоматически."""
    service = object.__new__(SubmissionService)
    service.telegram = MagicMock()
    service.telegram.send_reply.side_effect = TimeoutError("telegram timeout")
    lifecycle: list[str] = []
    service._mark_completion_notification_started = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda _order_no: lifecycle.append("started") or True
    )
    service._mark_completion_notification_uncertain = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda _order_no, _error: lifecycle.append("uncertain")
    )
    service._mark_completion_notification_completed = MagicMock()  # type: ignore[method-assign]
    state = ConversationState(last_order_no="A-1", status="submitted")

    with pytest.raises(TimeoutError, match="telegram timeout"):
        service._send_completion("123", state, "A-1", "ORDER-INTERNAL")

    assert lifecycle == ["started", "uncertain"]
    service.telegram.send_reply.assert_called_once()
    service._mark_completion_notification_completed.assert_not_called()


def test_completion_notification_checkpoint_failure_blocks_the_next_send() -> None:
    """Блокирует повтор после принятой Telegram карточки и сбоя completion checkpoint."""
    service = object.__new__(SubmissionService)
    service.telegram = MagicMock()
    service._mark_completion_notification_started = MagicMock(return_value=True)  # type: ignore[method-assign]
    service._mark_completion_notification_completed = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("checkpoint unavailable")
    )
    state = ConversationState(last_order_no="A-1", status="submitted")

    with pytest.raises(RuntimeError, match="checkpoint unavailable"):
        service._send_completion("123", state, "A-1", "ORDER-INTERNAL")

    service.telegram.send_reply.assert_called_once()

    recovery = object.__new__(SubmissionService)
    recovery.telegram = MagicMock()
    recovery._mark_completion_notification_started = MagicMock(  # type: ignore[method-assign]
        return_value=False
    )
    recovery._send_completion("123", state, "A-1", "ORDER-INTERNAL")

    recovery.telegram.send_reply.assert_not_called()


def test_completion_notification_started_or_completed_status_skips_send() -> None:
    """Не повторяет уведомление после начатой или завершённой попытки."""
    state = ConversationState(last_order_no="A-1", status="submitted")
    for _status in ("started", "uncertain", "completed"):
        service = object.__new__(SubmissionService)
        service.telegram = MagicMock()
        service._mark_completion_notification_started = MagicMock(  # type: ignore[method-assign]
            return_value=False
        )

        service._send_completion("123", state, "A-1", "ORDER-INTERNAL")

        service.telegram.send_reply.assert_not_called()


def test_local_completion_uses_the_same_notification_lifecycle() -> None:
    """Применяет те же контрольные точки к локальному сохранению заявки."""
    service = object.__new__(SubmissionService)
    service.telegram = MagicMock()
    lifecycle: list[str] = []
    service._mark_completion_notification_started = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda _order_no: lifecycle.append("started") or True
    )
    service._mark_completion_notification_completed = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda _order_no: lifecycle.append("completed")
    )
    service._mark_completion_notification_uncertain = MagicMock()  # type: ignore[method-assign]
    service.telegram.send_reply.side_effect = lambda *_args: lifecycle.append("telegram")
    state = ConversationState(last_order_no="A-1", status="saved_locally")

    service._send_local_completion("123", state, "A-1")

    service.telegram.send_reply.assert_called_once()
    assert lifecycle == ["started", "telegram", "completed"]
    service._mark_completion_notification_uncertain.assert_not_called()


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
    catalog_plan = {
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
                "before": 0,
                "increment": 5,
                "expected_after": 5,
            }
        ],
    }
    service.sheets.prepare_catalog_mutation.return_value = catalog_plan
    service.sheets.verify_catalog_mutation.return_value = CatalogMutationVerification.APPLIED
    service.sheets.prepare_recalculation.return_value = MagicMock()
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
    service._mark_recalc_started = MagicMock()  # type: ignore[method-assign]
    service._mark_recalc_completed = MagicMock()  # type: ignore[method-assign]
    service._persist_catalog_started = MagicMock()  # type: ignore[method-assign]
    service._mark_dispatch_started = MagicMock()  # type: ignore[method-assign]
    service._mark_dispatch_completed = MagicMock()  # type: ignore[method-assign]
    final_state = ConversationState(last_order_no=result.order_number, status="submitted")
    service._finalize = MagicMock(return_value=final_state)  # type: ignore[method-assign]
    service._send_completion = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr(submission_module, "chat_lock", lambda *_args, **_kwargs: nullcontext())

    service.submit("chat-1")

    service.sheets.prepare_catalog_mutation.assert_called_once_with(
        pending.rows,
        "venue-sheet",
        operation_id="catalog:ORDER-1",
        order_no="ORDER-1",
    )
    service.sheets.apply_catalog_mutation.assert_called_once_with(catalog_plan)
    service._persist_catalog_started.assert_called_once_with("ORDER-1", catalog_plan)
    service.sheets.prepare_recalculation.assert_called_once_with("venue-sheet")
    service.sheets.send_recalculation.assert_called_once()
    service.sheets.trigger_recalculation.assert_not_called()
    service._mark_recalc_started.assert_called_once_with("ORDER-1")
    service._mark_recalc_completed.assert_called_once_with("ORDER-1")
    service.sheets.prepare_order_submission.assert_called_once_with("venue-sheet", "ORDER-1")
    service._mark_dispatch_started.assert_called_once_with("ORDER-1")
    service.sheets.send_order_submission.assert_called_once_with(_prepared_request())
    service._mark_dispatch_completed.assert_called_once_with("ORDER-1", result)
    service._checkpoint.assert_called_once_with("ORDER-1", "catalog_updated")
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
    service.sheets.prepare_catalog_mutation.side_effect = RuntimeError("Google unavailable")
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


def test_dispatch_uncertain_reply_offers_read_only_check() -> None:
    """Предлагает проверить результат без опасного повтора заявки."""
    state = ConversationState(ui_revision=7)

    reply = submission_dispatch_uncertain_reply(state, "ORDER-6")

    assert "не отправляйте её повторно" in reply.text.lower()
    assert "ORDER-6" in reply.text
    assert [(button.text, button.callback_data) for row in reply.rows for button in row] == [
        ("Проверить отправку", "v2:check_submission:r7"),
        ("К черновику", "v2:back:r7"),
    ]


def test_local_saved_reply_is_explicit_and_has_only_new_order_button() -> None:
    """Подтверждает запись заявки без утверждения об отправке."""
    reply = submission_local_saved_reply(
        type("State", (), {"ui_revision": 4})(),
        "ORDER-LOCAL",
    )

    assert reply.text == (
        "<i>Заявка записана</i>\n\n"
        "Товары добавлены в таблицу заказа, расчёты обновлены.\n"
        "Заявку поставщикам отправит ответственный сотрудник."
    )
    assert "тест" not in reply.text.lower()
    assert "ORDER-LOCAL" not in reply.text
    assert "черновик" not in reply.text.lower()
    assert [(button.text, button.callback_data) for row in reply.rows for button in row] == [
        ("Новая заявка", "v2:clear:r4")
    ]


def test_revoked_access_stops_submission_before_google_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Не записывает подготовленную заявку после отзыва доступа."""
    service = object.__new__(SubmissionService)
    service.redis = MagicMock()
    service.telegram = MagicMock()
    service.sheets = MagicMock()
    service.registration = MagicMock()
    service.registration.context_for_identity.return_value = None
    pending = PendingSubmission(
        order_no="ORDER-BLOCKED",
        telegram_user_id="user-1",
        telegram_chat_id="chat-1",
        spreadsheet_id="venue-sheet",
        venue_code="VENUE-1",
        rows=[{"ID товара": "rose", "Кол-во": 5}],
    )
    service._load_pending = MagicMock(return_value=pending)  # type: ignore[method-assign]
    monkeypatch.setattr(submission_module, "chat_lock", lambda *_args, **_kwargs: nullcontext())

    service.submit("chat-1")

    service.registration.context_for_identity.assert_called_once_with(
        "user-1",
        "chat-1",
        force_refresh=True,
    )
    service.sheets.prepare_catalog_mutation.assert_not_called()
    service.sheets.apply_catalog_mutation.assert_not_called()
    service.sheets.send_recalculation.assert_not_called()
    service.sheets.send_order_submission.assert_not_called()
    reply = service.telegram.send_reply.call_args.args[1]
    assert "Доступ к заведению отключён" in reply.text


def test_disabled_dispatch_writes_and_recalculates_but_never_calls_submission_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Готовит заявку для ручной отправки и блокирует центральный POST."""
    service = object.__new__(SubmissionService)
    service.settings = SimpleNamespace(google_order_submission_enabled=False)
    service.redis = MagicMock()
    service.redis.lock.return_value = nullcontext()
    service.telegram = MagicMock()
    service.sheets = MagicMock()
    service.catalog_cache = MagicMock()
    service.sheets.prepare_catalog_mutation.return_value = {
        "schema_version": 1,
        "operation_id": "catalog:ORDER-LOCAL",
        "order_no": "ORDER-LOCAL",
        "spreadsheet_id": "venue-sheet",
        "mutations": [
            {
                "kind": "quantity",
                "range": "'Заявка'!B7",
                "product_id": "rose",
                "department": "Кухня",
                "before": 0,
                "increment": 5,
                "expected_after": 5,
            }
        ],
    }
    service.sheets.verify_catalog_mutation.return_value = CatalogMutationVerification.APPLIED
    service.sheets.prepare_recalculation.return_value = MagicMock()
    pending = PendingSubmission(
        order_no="ORDER-LOCAL",
        spreadsheet_id="venue-sheet",
        rows=[{"ID товара": "rose", "Кол-во": 5}],
    )
    service._load_pending = MagicMock(return_value=pending)  # type: ignore[method-assign]
    service._record = MagicMock(return_value=_record(order_no="ORDER-LOCAL"))  # type: ignore[method-assign]
    service._get_record = MagicMock(  # type: ignore[method-assign]
        side_effect=[
            _record(order_no="ORDER-LOCAL"),
            _record(order_no="ORDER-LOCAL", catalog_updated=True),
            _record(order_no="ORDER-LOCAL", catalog_updated=True, recalc_done=True),
        ]
    )
    service._checkpoint = MagicMock()  # type: ignore[method-assign]
    service._mark_recalc_started = MagicMock()  # type: ignore[method-assign]
    service._mark_recalc_completed = MagicMock()  # type: ignore[method-assign]
    service._persist_catalog_started = MagicMock()  # type: ignore[method-assign]
    final_state = ConversationState(last_order_no="ORDER-LOCAL", status="saved_locally")
    service._finalize = MagicMock(return_value=final_state)  # type: ignore[method-assign]
    service._send_local_completion = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr(submission_module, "chat_lock", lambda *_args, **_kwargs: nullcontext())

    service.submit("chat-local")

    service.sheets.prepare_catalog_mutation.assert_called_once()
    service.sheets.apply_catalog_mutation.assert_called_once()
    service.sheets.prepare_recalculation.assert_called_once_with("venue-sheet")
    service.sheets.send_recalculation.assert_called_once()
    service.sheets.trigger_recalculation.assert_not_called()
    service._mark_recalc_started.assert_called_once_with("ORDER-LOCAL")
    service._mark_recalc_completed.assert_called_once_with("ORDER-LOCAL")
    service.sheets.prepare_order_submission.assert_not_called()
    service.sheets.send_order_submission.assert_not_called()
    service._finalize.assert_called_once_with(
        "chat-local",
        "ORDER-LOCAL",
        "ORDER-LOCAL",
        dispatched=False,
    )
    service._send_local_completion.assert_called_once_with(
        "chat-local",
        final_state,
        "ORDER-LOCAL",
    )


def _mock_status_session(
    monkeypatch: pytest.MonkeyPatch,
    state: ConversationState,
) -> None:
    """Подменяет только чтение локальной сессии для тестов статусов."""
    session = MagicMock()
    session_context = MagicMock()
    session_context.__enter__.return_value = session
    session_context.__exit__.return_value = False
    monkeypatch.setattr(submission_module, "SessionLocal", MagicMock(return_value=session_context))
    repository = MagicMock()
    repository.get_for_update.return_value = (MagicMock(), state)
    monkeypatch.setattr(
        submission_module,
        "SessionRepository",
        MagicMock(return_value=repository),
    )
    monkeypatch.setattr(submission_module, "chat_lock", lambda *_args, **_kwargs: nullcontext())


def _history_status_row(
    order_number: str,
    supplier: str = "Поставщик",
) -> dict[str, str]:
    """Создаёт сводную строку реальной «Истории»."""
    return {
        "Номер заявки": order_number,
        "Время создания заявки": "29.07.2026 13:16:15",
        "Условное название поставщика": supplier,
        "Условное наз-ие заведения": "Тестовое кафе",
        "Список товаров": "Кухня:\n1. Мука — 4 шт",
        "Стадия": "Отправлено поставщику",
    }


def test_statuses_show_five_recent_orders_of_current_venue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Показывает пять реальных заявок и кнопку следующей страницы."""
    service = object.__new__(SubmissionService)
    service.redis = MagicMock()
    service.telegram = MagicMock()
    service.sheets = MagicMock()
    state = ConversationState(
        spreadsheet_id="venue-sheet",
        venue_name="Тестовое кафе",
    )
    _mock_status_session(monkeypatch, state)
    service.sheets.read_recent_order_statuses.return_value = [
        _history_status_row(f"ORDER-{index}") for index in range(1, 7)
    ]

    service.send_status("chat-status")

    service.sheets.read_recent_order_statuses.assert_called_once_with(
        "venue-sheet",
        "Тестовое кафе",
        offset=0,
        limit=6,
    )
    service.sheets.read_order_statuses.assert_not_called()
    reply = service.telegram.send_reply.call_args.args[1]
    assert "ORDER-1" not in reply.text
    assert "ORDER-5" not in reply.text
    assert "ORDER-6" not in reply.text
    assert "<b>1. Заявка от 29.07.2026</b>" in reply.text
    assert reply.rows[0][0].text == "1. Заявка от 29.07.2026"
    assert "Покажи вторую" in reply.text
    assert reply.rows[-1][0].text == "Старее →"


def test_statuses_open_selected_order_from_shown_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Открывает подробности выбранной заявки по стабильному списку сессии."""
    service = object.__new__(SubmissionService)
    service.redis = MagicMock()
    service.telegram = MagicMock()
    service.sheets = MagicMock()
    _mock_status_session(
        monkeypatch,
        ConversationState(
            spreadsheet_id="venue-sheet",
            venue_name="Тестовое кафе",
            order_status_view_active=True,
            order_status_page=0,
            order_status_order_numbers=["ORDER-1", "ORDER-2"],
        ),
    )
    service.sheets.read_order_statuses.return_value = [_history_status_row("ORDER-2", "Раджабов")]

    service.send_status("chat-status", selected_index=2)

    service.sheets.read_order_statuses.assert_called_once_with(
        ["ORDER-2"],
        "venue-sheet",
        "Тестовое кафе",
    )
    reply = service.telegram.send_reply.call_args.args[1]
    assert "ORDER-2" not in reply.text
    assert "<b>2. Заявка от 29.07.2026</b>" in reply.text
    assert "Раджабов" in reply.text
    assert reply.rows[-1][0].text == "← К списку заявок"


def test_order_status_list_uses_human_date_and_shared_status_formatter() -> None:
    """Показывает дату и очищенный статус, сохраняя callback выбранной заявки."""
    reply = build_order_status_list_reply(
        [
            {
                "Номер заявки": "01UKMJ35-000001",
                "Время создания заявки": "31.07.2026 10:15:00",
                "Условное название поставщика": "Раджабов",
                "Стадия": "Вручную | Заявка подтверждена поставщиком.",
                "Список товаров": "Вино белое Д/Я КУХНИ - 5 шт - 1037 руб.",
            }
        ],
        page=0,
        has_more=False,
    )

    assert "📋 <b><u>Мои заявки</u></b>" in reply.text
    assert "<b>1. Заявка от 31.07.2026</b>" in reply.text
    assert "Поставщиков: <b>1</b>" in reply.text
    assert "Статус: <b>Заявка подтверждена поставщиком</b>" in reply.text
    assert "01UKMJ35-000001" not in reply.text
    assert reply.rows[0][0].text == "1. Заявка от 31.07.2026"
    assert reply.rows[0][0].callback_data == "v2:order:1"
    assert format_status("Вручную | Заявка подтверждена поставщиком.") == (
        "Заявка подтверждена поставщиком"
    )
    assert format_status("Заявка подтверждена поставщиком") == ("Заявка подтверждена поставщиком")
    assert format_status("Новая внутренняя стадия") == "Новая внутренняя стадия"


def test_statuses_read_older_page_without_using_local_draft_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Берёт следующую страницу «Истории», игнорируя номер локального черновика."""
    service = object.__new__(SubmissionService)
    service.redis = MagicMock()
    service.telegram = MagicMock()
    service.sheets = MagicMock()
    _mock_status_session(
        monkeypatch,
        ConversationState(
            spreadsheet_id="venue-sheet",
            venue_name="Тестовое кафе",
            last_order_no="OLD-LOCAL-ORDER",
        ),
    )
    service.sheets.read_recent_order_statuses.return_value = [_history_status_row("ORDER-6")]

    service.send_status("chat-status", page=1)

    service.sheets.read_recent_order_statuses.assert_called_once_with(
        "venue-sheet",
        "Тестовое кафе",
        offset=5,
        limit=6,
    )
    service.sheets.read_order_statuses.assert_not_called()
    reply = service.telegram.send_reply.call_args.args[1]
    assert "Страница 2" in reply.text
    assert reply.rows[-1][0].text == "← Новее"


def test_statuses_explain_when_real_history_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сообщает, что у текущего заведения нет реальных заявок."""
    service = object.__new__(SubmissionService)
    service.redis = MagicMock()
    service.telegram = MagicMock()
    service.sheets = MagicMock()
    _mock_status_session(
        monkeypatch,
        ConversationState(
            spreadsheet_id="venue-sheet",
            venue_name="Тестовое кафе",
        ),
    )
    service.sheets.read_recent_order_statuses.return_value = []

    service.send_status("chat-status")

    service.sheets.read_recent_order_statuses.assert_called_once_with(
        "venue-sheet",
        "Тестовое кафе",
        offset=0,
        limit=6,
    )
    reply = service.telegram.send_reply.call_args.args[1]
    assert reply.text == (
        "📋 <b><u>Мои заявки</u></b>\n\nУ этого заведения пока нет отправленных заявок в листе «История»."
    )
