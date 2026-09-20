"""Проверяет поведение, связанное с модулем «test callback contract»."""

from restaurant_bot.domain.models import (
    CartItem,
    ConversationState,
    InputKind,
    Intent,
    ItemStatus,
    TelegramEvent,
)
from restaurant_bot.input.telegram_callbacks import parse_callback
from restaurant_bot.services.engine import ConversationEngine


def test_callback_actions_have_explicit_state_machine_meanings() -> None:
    """Проверяет, что callback действия имеют явный состояние machine meanings."""
    assert parse_callback("v2:cart").intent is Intent.SHOW_FINAL_REVIEW
    assert parse_callback("v2:new").intent is Intent.START_NEW_ORDER
    assert parse_callback("v2:back").intent is Intent.BACK
    assert parse_callback("v2:submit").intent is Intent.SUBMIT_AS_IS
    assert parse_callback("v2:addreq:2").intent is Intent.PRODUCT_ADD
    assert parse_callback("v2:dupmerge:3:0").intent is Intent.MERGE_DUPLICATE
    department = parse_callback("v2:dept:bar:r4")
    assert department.intent is Intent.SELECT_DEPARTMENT
    assert department.callback_target == "bar"
    assert department.callback_revision == 4


def test_pagination_callbacks_have_explicit_targets() -> None:
    """Разбирает страницы черновика и подробностей заявки без двусмысленности."""
    cart = parse_callback("v2:cartpage:2")
    final_review = parse_callback("v2:finalpage:2")
    detail = parse_callback("v2:orderitems:3:4")

    assert cart.intent is Intent.SHOW_CART
    assert cart.callback_target == "page:2"
    assert final_review.intent is Intent.SHOW_FINAL_REVIEW
    assert final_review.callback_target == "page:2"
    assert detail.intent is Intent.ORDER_STATUS
    assert detail.selected_index == 3
    assert detail.order_status_detail_page == 4
    assert detail.callback_target == "detail:4"


def test_submission_check_callback_uses_read_only_status_route() -> None:
    """Проверка отправки использует read-only маршрут статуса заявки."""
    detail = parse_callback("v2:check_submission:r4")

    assert detail.intent is Intent.ORDER_STATUS
    assert detail.callback_target == "submission_check"
    assert detail.callback_revision == 4


def test_candidate_callbacks_use_human_numbering_and_keep_revision() -> None:
    """Проверяет, что callback кандидата использует нумерацию для человека и сохраняет ревизию."""
    command = parse_callback("v2:sel:4:0:r7")

    assert command.intent is Intent.SELECT_CANDIDATE
    assert command.callback_target == "4"
    assert command.selected_index == 1
    assert command.callback_revision == 7


def test_stale_callback_cannot_mutate_current_draft(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что stale callback не может изменяет текущий черновик."""
    engine = ConversationEngine(settings)
    state = ConversationState(
        ui_revision=3,
        cart=[
            CartItem(
                id="rose",
                source_query="Сироп Роза",
                catalog_name="Сироп Роза",
                quantity=5,
                unit="шт",
                status=ItemStatus.MATCHED,
            )
        ],
    )
    event = TelegramEvent(update_id=1, chat_id="123456", input_type=InputKind.CALLBACK)

    result = engine.handle(event, parse_callback("v2:clear:r2"), state, [])

    assert result.state.stage.value == "collecting"
    assert len(result.state.cart) == 1
    assert "Сироп Роза" in result.reply.text


def test_replayed_callback_with_new_update_id_cannot_repeat_mutation(settings) -> None:  # type: ignore[no-untyped-def]
    """Защищает черновик от повторного callback после смены ревизии."""
    engine = ConversationEngine(settings)
    state = ConversationState(
        ui_revision=3,
        cart=[
            CartItem(
                id="old",
                source_query="Сироп Роза",
                catalog_name="Сироп Роза",
                quantity=5,
                unit="шт",
                status=ItemStatus.MATCHED,
            )
        ],
    )
    callback = parse_callback("v2:remove:old:r3")

    first = engine.handle(
        TelegramEvent(update_id=10, chat_id="123456", input_type=InputKind.CALLBACK),
        callback,
        state,
        [],
    )
    first.state.ui_revision = 4
    first.state.cart.append(
        CartItem(
            id="new",
            source_query="Сироп Роза",
            catalog_name="Сироп Роза",
            quantity=2,
            unit="шт",
            status=ItemStatus.MATCHED,
        )
    )

    replay = engine.handle(
        TelegramEvent(update_id=11, chat_id="123456", input_type=InputKind.CALLBACK),
        callback,
        first.state,
        [],
    )

    assert [item.id for item in replay.state.cart if item.status is not ItemStatus.SKIPPED] == [
        "new"
    ]
