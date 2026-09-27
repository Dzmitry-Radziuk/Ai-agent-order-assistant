"""Проверяет блокировку отправки вне графика поставщика."""

from datetime import date
from types import SimpleNamespace

from restaurant_bot.application.conversation.contracts import ConversationInput
from restaurant_bot.domain.models import CartItem, ConversationState, ItemStatus
from restaurant_bot.orders.supplier_schedules import supplier_schedule_warnings
from restaurant_bot.services.engine_submission_preparation import (
    EngineSubmissionPreparationService,
)


def _scheduled_item() -> CartItem:
    """Создаёт позицию с графиком из баг-репорта тестировщика."""
    return CartItem(
        id="beef",
        source_query="Говядина вырезка",
        catalog_product_id="beef",
        catalog_name="Говядина вырезка",
        supplier="Тестовый поставщик",
        catalog_unit="кг",
        quantity=5,
        unit="кг",
        price=100,
        comment="Доставить завтра до 20:00",
        supplier_order_schedule="Ср, Вс",
        supplier_delivery_schedule="Пн, Чт",
        department="Кухня",
        department_confirmed=True,
        status=ItemStatus.MATCHED,
    )


def test_schedule_warning_finds_next_order_and_delivery_days() -> None:
    """Находит среду для приёма и четверг для доставки из сценария понедельника."""
    warnings = supplier_schedule_warnings([_scheduled_item()], today=date(2026, 9, 21))

    assert len(warnings) == 1
    warning = warnings[0]
    assert warning.order_date == date(2026, 9, 21)
    assert warning.nearest_order_date == date(2026, 9, 23)
    assert warning.requested_delivery_date == date(2026, 9, 22)
    assert warning.nearest_delivery_date == date(2026, 9, 24)


def test_submission_requires_explicit_second_confirmation_for_schedule(monkeypatch) -> None:
    """Не создаёт снимок заявки до отдельного подтверждения предупреждения о графике."""
    owner = SimpleNamespace(
        settings=SimpleNamespace(
            app_timezone="Europe/Minsk",
            google_order_submission_enabled=False,
        )
    )
    service = EngineSubmissionPreparationService(owner, cart_renderer=lambda state: None)
    state = ConversationState(
        restaurant="Тестовый ресторан",
        spreadsheet_id="sheet",
        cart=[_scheduled_item()],
    )
    event = ConversationInput(1, "chat-1234", actor_id="user")

    class FrozenDateTime:
        """Фиксирует локальную и UTC дату для детерминированной проверки."""

        @classmethod
        def now(cls, tz=None):
            """Возвращает понедельник из сценария тестировщика."""
            from datetime import datetime

            value = datetime(2026, 9, 21, 12, 0, 0)
            return value.replace(tzinfo=tz) if tz is not None else value

    monkeypatch.setattr(
        "restaurant_bot.services.engine_submission_preparation.datetime",
        FrozenDateTime,
    )

    first = service.prepare(event, state)

    assert first.enqueue_submission is False
    assert first.state.pending_submission is None
    assert "сегодня заявки не принимаются" in first.reply.text
    assert "22.09.2026" in first.reply.text
    assert "24.09.2026" in first.reply.text

    second = service.prepare(event, first.state)

    assert second.enqueue_submission is True
    assert second.state.pending_submission is not None
