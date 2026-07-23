from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from restaurant_bot.domain.models import (
    Candidate,
    CartItem,
    CatalogProduct,
    ConversationState,
    ExtractedItem,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    SessionStage,
    TelegramEvent,
)
from restaurant_bot.services import submission as submission_module
from restaurant_bot.services.engine import ConversationEngine
from restaurant_bot.services.replies import issue_reply, product_add_requests_reply
from restaurant_bot.services.submission import SubmissionService


def _event(update_id: int = 1, text: str = "") -> TelegramEvent:
    return TelegramEvent(
        update_id=update_id, chat_id="123456", input_type=InputKind.TEXT, text=text
    )


def test_product_add_request_removes_unresolved_item_and_enqueues_sheet_write(settings) -> None:  # type: ignore[no-untyped-def]
    engine = ConversationEngine(settings)
    missing = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[ExtractedItem(product_query="Креветки королевские", quantity=10, unit="кг")],
        ),
        ConversationState(),
        [],
    )
    assert missing.state.cart[0].status is ItemStatus.NOT_FOUND

    prompt = engine.handle(
        _event(), ParsedCommand(intent=Intent.PRODUCT_ADD, callback_target="0"), missing.state, []
    )
    assert prompt.state.pending_product_add_item_index == 0

    saved = engine.handle(
        _event(2, "Креветки королевские, замороженные, упаковка 1 кг"),
        ParsedCommand(
            intent=Intent.UNKNOWN, text="Креветки королевские, замороженные, упаковка 1 кг"
        ),
        prompt.state,
        [],
    )
    request = saved.state.product_add_requests[0]
    assert saved.enqueue_product_add is True
    assert saved.state.cart[0].status is ItemStatus.SKIPPED
    assert request["original_query"] == "Креветки королевские"
    assert request["description"] == "Креветки королевские, замороженные, упаковка 1 кг"
    assert request["status"] == "pending_write"
    assert saved.reply.text == "<b>Отправляю запрос менеджеру…</b>"


def test_product_add_keeps_other_unresolved_items_in_draft(settings) -> None:  # type: ignore[no-untyped-def]
    engine = ConversationEngine(settings)
    catalog = [CatalogProduct(product_id="rose", name="Сироп Роза", supplier="Сиропы", unit="шт")]
    added = engine.handle(
        _event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(product_query="Креветки королевские", quantity=10, unit="кг"),
                ExtractedItem(product_query="Сироп Роза"),
            ],
        ),
        ConversationState(),
        catalog,
    )
    prompt = engine.handle(
        _event(),
        ParsedCommand(intent=Intent.PRODUCT_ADD, callback_target="0"),
        added.state,
        catalog,
    )
    saved = engine.handle(
        _event(2, "Креветки королевские"),
        ParsedCommand(intent=Intent.UNKNOWN, text="Креветки королевские"),
        prompt.state,
        catalog,
    )

    assert [item.status for item in saved.state.cart] == [
        ItemStatus.SKIPPED,
        ItemStatus.MISSING_QTY,
    ]
    assert saved.reply.text == "<b>Отправляю запрос менеджеру…</b>"


def _button_texts(reply) -> list[str]:  # type: ignore[no-untyped-def]
    return [button.text for row in reply.rows for button in row]


def test_product_add_is_available_from_not_found_and_ambiguous_screens() -> None:
    not_found = issue_reply(
        CartItem(id="missing", source_query="Мисо-паста Genzo", status=ItemStatus.NOT_FOUND)
    )
    assert _button_texts(not_found) == [
        "Отправить запрос снабженцу",
        "Изменить название",
        "Не добавлять",
    ]

    final_not_found = issue_reply(
        CartItem(
            id="missing",
            source_query="Мисо-паста Genzo",
            status=ItemStatus.NOT_FOUND,
            rename_attempted=True,
        ),
    )
    assert _button_texts(final_not_found) == ["Да, отправить", "Нет"]

    ambiguous = issue_reply(
        CartItem(
            id="ambiguous",
            source_query="Мисо",
            status=ItemStatus.AMBIGUOUS,
            candidates=[Candidate(product_id="miso", name="Мисо-паста Genzo", unit="кг")],
        ),
    )
    assert "Отправить запрос снабженцу" in _button_texts(ambiguous)


def test_product_add_prompt_requests_all_details_in_one_message(settings) -> None:  # type: ignore[no-untyped-def]
    engine = ConversationEngine(settings)
    state = ConversationState(
        cart=[CartItem(id="missing", source_query="Мисо-паста Genzo", status=ItemStatus.NOT_FOUND)],
        current_issue_item_id="missing",
    )
    result = engine.handle(
        _event(), ParsedCommand(intent=Intent.PRODUCT_ADD, callback_target="0"), state, []
    )

    assert result.state.stage.value == "await_product_add_details"
    assert result.state.pending_product_add_item_index == 0
    assert "Опишите товар одним сообщением" in result.reply.text
    assert "название, бренд, фасовку или объём" in result.reply.text
    assert "Мисо-паста Genzo" in result.reply.text


def test_product_add_description_event_is_idempotent(settings) -> None:  # type: ignore[no-untyped-def]
    engine = ConversationEngine(settings)
    prompt_state = ConversationState(
        stage="await_product_add_details",
        status="await_product_add_details",
        pending_product_add_item_index=0,
        pending_product_add_request_id="add-stable",
        cart=[CartItem(id="missing", source_query="Мисо-паста Genzo", status=ItemStatus.NOT_FOUND)],
    )
    command = ParsedCommand(intent=Intent.UNKNOWN, text="Мисо-паста Genzo, 1 кг, белая упаковка")
    first = engine.handle(_event(7654321, command.text), command, prompt_state, [])
    second = engine.handle(_event(7654321, command.text), command, first.state, [])

    assert len(first.state.product_add_requests) == 1
    assert len(second.state.product_add_requests) == 1
    assert second.state.product_add_requests[0]["request_id"] == "add-stable"


def test_voice_product_add_details_keep_sender_and_are_idempotent(settings) -> None:  # type: ignore[no-untyped-def]
    engine = ConversationEngine(settings)
    state = ConversationState(
        stage=SessionStage.AWAIT_PRODUCT_ADD_DETAILS,
        status="await_product_add_details",
        pending_product_add_item_index=0,
        pending_product_add_request_id="add-voice",
        cart=[CartItem(id="missing", source_query="Креветки", status=ItemStatus.NOT_FOUND)],
    )
    event = TelegramEvent(
        update_id=9001,
        chat_id="123456",
        input_type=InputKind.VOICE,
        text="Одну тонну креветок королевских, размер 16/20, бренд Polar",
        telegram_user_id="77",
        telegram_username="cook_test",
        telegram_first_name="Анна",
        telegram_last_name="Повар",
    )
    command = ParsedCommand(
        intent=Intent.ADD_ITEMS,
        text=event.text,
        items=[ExtractedItem(product_query="креветки", quantity=1, unit="т")],
    )

    first = engine.handle(event, command, state, [])
    second = engine.handle(event, command, first.state, [])

    assert len(second.state.product_add_requests) == 1
    request = second.state.product_add_requests[0]
    assert request["description"] == event.text
    assert request["telegram_user_id"] == "77"
    assert request["telegram_username"] == "cook_test"
    assert request["telegram_first_name"] == "Анна"
    assert request["telegram_last_name"] == "Повар"
    assert request["request_id"] == "add-voice"
    assert second.enqueue_product_add is False
    assert second.state.cart[0].status is ItemStatus.SKIPPED


def test_product_add_retry_reuses_id_and_never_retries_uncertain_write(settings) -> None:  # type: ignore[no-untyped-def]
    engine = ConversationEngine(settings)
    state = ConversationState(
        product_add_requests=[
            {"request_id": "add-stable", "description": "Мисо", "status": "write_failed"}
        ]
    )
    retry = engine.handle(
        _event(),
        ParsedCommand(intent=Intent.PRODUCT_ADD_RETRY, callback_target="add-stable"),
        state,
        [],
    )

    assert retry.enqueue_product_add is True
    assert retry.state.pending_product_add_request_id == "add-stable"
    assert retry.state.product_add_requests[0]["request_id"] == "add-stable"

    retry.state.product_add_requests[0]["status"] = "write_uncertain"
    uncertain = engine.handle(
        _event(),
        ParsedCommand(intent=Intent.PRODUCT_ADD_RETRY, callback_target="add-stable"),
        retry.state,
        [],
    )
    assert uncertain.enqueue_product_add is False
    assert "Повторно отправлять запрос не нужно" in uncertain.reply.text


def test_product_add_write_outcomes_are_persisted_before_the_reply_is_built() -> None:
    state = ConversationState(
        stage=SessionStage.AWAIT_PRODUCT_ADD_DETAILS,
        status="await_product_add_details",
        pending_product_add_request_id="add-1",
        product_add_write_in_progress=True,
        product_add_requests=[
            {"request_id": "add-1", "description": "Мисо-паста Genzo", "status": "pending_write"}
        ],
    )

    submitted = SubmissionService._apply_product_add_write_outcome(state, "add-1", "submitted")

    assert submitted is not None
    assert submitted["status"] == "submitted"
    assert submitted["submitted_at"] == submitted["updated_at"]
    assert state.pending_product_add_request_id == ""
    assert state.product_add_write_in_progress is False
    assert state.stage is SessionStage.REVIEW
    assert state.status == "review"

    failed = SubmissionService._apply_product_add_write_outcome(
        state, "add-1", "write_failed", "403"
    )
    assert failed is not None and failed["status"] == "write_failed"
    assert failed["sheets_error"] == "403"

    uncertain = SubmissionService._apply_product_add_write_outcome(
        state, "add-1", "write_uncertain", "timeout"
    )

    assert uncertain is not None and uncertain["status"] == "write_uncertain"
    assert uncertain["sheets_error"] == "timeout"


def test_product_add_success_is_shown_before_a_fresh_draft() -> None:
    service = object.__new__(SubmissionService)
    service.telegram = MagicMock()
    service.telegram.send_reply.side_effect = [77, 88]
    service._persist_worker_ui = MagicMock()
    state = ConversationState(
        ui_message_id=55,
        ui_revision=4,
        product_add_requests=[
            {
                "request_id": "add-1",
                "description": "Креветки королевские",
                "status": "submitted",
            }
        ],
    )

    service._send_product_add_success(
        "123456",
        state,
        {"request_id": "add-1", "description": "Креветки королевские"},
    )

    calls = service.telegram.send_reply.call_args_list
    assert len(calls) == 2
    confirmation = calls[0].args[1]
    draft = calls[1].args[1]
    assert confirmation.edit_message_id == 55
    assert confirmation.text == ("<b>Запрос менеджеру отправлен</b>\n\nКреветки королевские")
    assert draft.edit_message_id is None
    assert "<b>Черновик заявки</b>" in draft.text
    assert "Запросы снабженцу: 1" in draft.text
    assert all(
        button.callback_data.endswith(":r5")
        for row in draft.rows
        for button in row
        if button.callback_data
    )
    service._persist_worker_ui.assert_called_once_with("123456", 5, 88, draft.text)


def test_product_add_request_list_keeps_request_separate_from_cart() -> None:
    state = ConversationState(
        product_add_requests=[
            {"request_id": "add-1", "description": "Мисо-паста Genzo, 1 кг", "status": "submitted"}
        ]
    )
    reply = product_add_requests_reply(state)

    assert "Мисо-паста Genzo, 1 кг" in reply.text
    assert _button_texts(reply) == ["К черновику"]


def _product_add_service(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sheet_error: Exception | None = None,
) -> tuple[SubmissionService, ConversationState, MagicMock]:
    """Создаёт сервис отправки нового товара с тестовой транзакцией."""
    service = object.__new__(SubmissionService)
    service.redis = MagicMock()
    service.telegram = MagicMock()
    service.sheets = MagicMock()
    if sheet_error is not None:
        service.sheets.append_product_request.side_effect = sheet_error
    service._send_product_add_success = MagicMock()  # type: ignore[method-assign]
    state = ConversationState(
        spreadsheet_id="venue-sheet",
        pending_product_add_request_id="add-1",
        product_add_write_in_progress=True,
        product_add_requests=[
            {
                "request_id": "add-1",
                "description": "Креветки королевские, упаковка 1 кг",
                "status": "pending_write",
            }
        ],
    )
    repository = MagicMock()
    repository.get_for_update.side_effect = [(None, state), (None, state)]
    repository_cls = MagicMock(return_value=repository)
    monkeypatch.setattr(submission_module, "SessionRepository", repository_cls)
    monkeypatch.setattr(
        submission_module,
        "SessionLocal",
        SimpleNamespace(begin=lambda: nullcontext(MagicMock())),
    )
    monkeypatch.setattr(
        submission_module,
        "chat_lock",
        lambda *_args, **_kwargs: nullcontext(),
    )
    return service, state, repository


def test_product_add_worker_persists_success_and_uses_only_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Записывает только описание и сохраняет успешный результат."""
    service, state, repository = _product_add_service(monkeypatch)

    service.submit_product_add("chat-1")

    service.sheets.append_product_request.assert_called_once_with(
        {"description": "Креветки королевские, упаковка 1 кг"},
        "venue-sheet",
    )
    assert state.product_add_requests[0]["status"] == "submitted"
    assert repository.save.call_count == 2
    service._send_product_add_success.assert_called_once()
    service.telegram.send_reply.assert_not_called()


def test_product_add_worker_offers_retry_for_confirmed_write_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Предлагает безопасный повтор при подтверждённом отказе записи."""
    service, state, _ = _product_add_service(
        monkeypatch,
        sheet_error=PermissionError("403 forbidden"),
    )

    service.submit_product_add("chat-1")

    assert state.product_add_requests[0]["status"] == "write_failed"
    reply = service.telegram.send_reply.call_args.args[1]
    assert "Запрос не отправлен" in reply.text
    assert reply.rows[0][0].callback_data == "v2:addreqretry:add-1"


def test_product_add_worker_blocks_duplicate_after_uncertain_network_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Запрещает повтор при неопределённом сетевом результате."""
    service, state, _ = _product_add_service(
        monkeypatch,
        sheet_error=TimeoutError("network timeout"),
    )

    service.submit_product_add("chat-1")

    assert state.product_add_requests[0]["status"] == "write_uncertain"
    reply = service.telegram.send_reply.call_args.args[1]
    assert "Не удалось подтвердить отправку" in reply.text
    assert reply.rows == []
