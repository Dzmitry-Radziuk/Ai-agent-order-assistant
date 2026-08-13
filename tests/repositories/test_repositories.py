from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from sqlalchemy.exc import IntegrityError

from restaurant_bot.domain.models import ConversationState, PendingSubmission, SessionStage
from restaurant_bot.persistence.models import (
    BotSession,
    SubmissionRecord,
    TelegramUpdate,
    VenueBinding,
)
from restaurant_bot.repositories.sessions import SessionRepository
from restaurant_bot.repositories.submissions import SubmissionRepository
from restaurant_bot.repositories.updates import UpdateRepository
from restaurant_bot.repositories.venue_bindings import VenueBindingRepository


def _binding(code: str, *, active: bool = True, binding_id: int = 1) -> VenueBinding:
    """Создаёт тестовую привязку заведения."""
    return VenueBinding(
        id=binding_id,
        channel="telegram",
        telegram_user_id="user-1",
        telegram_chat_id="chat-1",
        username="cook",
        venue_code=code,
        venue_name=f"Заведение {code}",
        spreadsheet_id=f"sheet-{code}",
        is_active=active,
        sync_status="synced",
    )


def test_venue_binding_model_preserves_migrated_index_names() -> None:
    """Проверяет имена индексов, уже созданных миграцией 0002."""
    assert {index.name for index in VenueBinding.__table__.indexes} == {
        "ix_venue_bindings_active",
        "ix_venue_bindings_chat",
        "ix_venue_bindings_code",
        "ix_venue_bindings_user",
    }


def test_session_repository_returns_empty_state_for_new_chat() -> None:
    """Возвращает начальное состояние для неизвестного чата."""
    db = MagicMock()
    db.scalar.return_value = None

    row, state = SessionRepository(db).get_for_update("chat-1")

    assert row is None
    assert state == ConversationState()


def test_session_repository_creates_and_updates_versioned_state() -> None:
    """Создаёт сессию и увеличивает версию при обновлении."""
    db = MagicMock()
    repository = SessionRepository(db)
    state = ConversationState(stage=SessionStage.REVIEW, status="review")

    created = repository.save("chat-1", state)

    db.add.assert_called_once_with(created)
    assert created.telegram_id == "chat-1"
    assert created.state_name == "review"
    assert created.version == 1

    state.stage = SessionStage.COLLECTING
    updated = repository.save("chat-1", state, created)

    assert updated is created
    assert updated.state_name == "collecting"
    assert updated.version == 2
    assert db.flush.call_count == 2


def test_session_repository_restores_saved_state() -> None:
    """Восстанавливает типизированное состояние из JSON записи."""
    db = MagicMock()
    stored = ConversationState(stage=SessionStage.REVIEW, venue_code="12345")
    row = BotSession(
        telegram_id="chat-1",
        state_name="review",
        data=stored.model_dump(mode="json"),
        version=3,
    )
    db.scalar.return_value = row

    loaded_row, loaded = SessionRepository(db).get_for_update("chat-1")

    assert loaded_row is row
    assert loaded.stage is SessionStage.REVIEW
    assert loaded.venue_code == "12345"


def test_update_repository_is_idempotent_on_integrity_error() -> None:
    """Откатывает транзакцию при повторном Telegram update."""
    db = MagicMock()
    repository = UpdateRepository(db)

    assert repository.enqueue_once(1, "chat-1", {"update_id": 1}) is True

    db.flush.side_effect = IntegrityError("insert", {}, RuntimeError("duplicate"))
    assert repository.enqueue_once(1, "chat-1", {"update_id": 1}) is False
    db.begin_nested.assert_called()
    db.rollback.assert_not_called()


def test_update_repository_duplicate_keeps_outer_transaction_usable() -> None:
    """Сохраняет возможность чтения статуса после отката вложенной точки сохранения."""
    db = MagicMock()
    repository = UpdateRepository(db)
    db.flush.side_effect = IntegrityError("insert", {}, RuntimeError("duplicate"))
    existing = TelegramUpdate(update_id=1, chat_id="chat-1", payload={}, status="queued")
    db.get.return_value = existing

    assert repository.enqueue_once(1, "chat-1", {"update_id": 1}) is False
    assert repository.get_status(1) == "queued"
    db.begin_nested.assert_called_once()
    db.rollback.assert_not_called()


def test_update_repository_returns_locked_update() -> None:
    """Возвращает найденное обновление Telegram."""
    db = MagicMock()
    update = TelegramUpdate(update_id=7, chat_id="chat-1", payload={})
    db.scalar.return_value = update

    assert UpdateRepository(db).get_for_update(7) is update


def test_update_repository_reads_existing_status_for_duplicate_redrive() -> None:
    """Читает статус существующего обновления для безопасного повторного запуска."""
    db = MagicMock()
    db.get.return_value = TelegramUpdate(update_id=7, chat_id="chat-1", payload={}, status="queued")

    assert UpdateRepository(db).get_status(7) == "queued"


def test_update_repository_detects_lower_unfinished_update() -> None:
    """Находит более раннее незавершённое обновление того же чата."""
    db = MagicMock()
    db.scalar.return_value = 101

    assert UpdateRepository(db).has_lower_unfinished("chat-1", 102) is True


def test_update_repository_accepts_terminal_predecessor() -> None:
    """Не блокирует новое обновление при отсутствии незавершённого предшественника."""
    db = MagicMock()
    db.scalar.return_value = None

    assert UpdateRepository(db).has_lower_unfinished("chat-1", 102) is False


def test_update_repository_redrive_chooses_oldest_recoverable_per_chat() -> None:
    """Выбирает только первое доступное для восстановления обновление каждого чата."""
    first = TelegramUpdate(
        update_id=101,
        chat_id="chat-a",
        payload={},
        status="queued",
    )
    second = TelegramUpdate(
        update_id=102,
        chat_id="chat-a",
        payload={},
        status="queued",
    )
    other = TelegramUpdate(
        update_id=201,
        chat_id="chat-b",
        payload={},
        status="processing",
        updated_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    db = MagicMock()
    db.scalars.side_effect = [
        SimpleNamespace(all=lambda: [first, second, other]),
        SimpleNamespace(all=lambda: [first, second, other]),
    ]

    result = UpdateRepository(db).recoverable_for_redrive(datetime.now(UTC) - timedelta(minutes=5))

    assert [row.update_id for row in result] == [101, 201]


def test_update_repository_does_not_redrive_fresh_processing_row() -> None:
    """Не ставит свежую обработку повторно и сохраняет порядок чата."""
    now = datetime.now(UTC)
    queued = TelegramUpdate(
        update_id=301,
        chat_id="chat-c",
        payload={},
        status="queued",
    )
    fresh = TelegramUpdate(
        update_id=302,
        chat_id="chat-c",
        payload={},
        status="processing",
        updated_at=now,
    )
    stale = TelegramUpdate(
        update_id=401,
        chat_id="chat-d",
        payload={},
        status="processing",
        updated_at=now - timedelta(minutes=10),
    )
    db = MagicMock()
    db.scalars.side_effect = [
        SimpleNamespace(all=lambda: [queued, fresh, stale]),
        SimpleNamespace(all=lambda: [queued, fresh, stale]),
    ]

    result = UpdateRepository(db).recoverable_for_redrive(now - timedelta(minutes=5))

    assert [row.update_id for row in result] == [301, 401]


def test_update_repository_defers_only_matching_processing_attempt() -> None:
    """Возвращает в очередь только указанную попытку обработки."""
    db = MagicMock()
    db.execute.return_value.rowcount = 1

    assert UpdateRepository(db).defer_if_current_attempt(42, 3) is True
    db.execute.assert_called_once()


def test_update_repository_reports_stale_attempt_without_mutation() -> None:
    """Не считает устаревшую попытку успешно возвращённой в очередь."""
    db = MagicMock()
    db.execute.return_value.rowcount = 0

    assert UpdateRepository(db).defer_if_current_attempt(42, 2) is False


def test_submission_repository_returns_existing_record() -> None:
    """Не создаёт повторную запись для существующего номера заявки."""
    db = MagicMock()
    pending = PendingSubmission(order_no="ORDER-1")
    existing = SubmissionRecord(order_no="ORDER-1", telegram_id="chat-1", payload={})
    db.scalar.return_value = existing

    result = SubmissionRepository(db).get_or_create("chat-1", pending)

    assert result is existing
    db.add.assert_not_called()


def test_submission_repository_creates_checkpoint_record() -> None:
    """Создаёт контрольную запись для новой отправки."""
    db = MagicMock()
    db.scalar.return_value = None
    pending = PendingSubmission(order_no="ORDER-2", venue_code="12345")

    result = SubmissionRepository(db).get_or_create("chat-1", pending)

    db.add.assert_called_once_with(result)
    db.flush.assert_called_once()
    assert result.order_no == "ORDER-2"
    assert result.payload["venue_code"] == "12345"


def test_submission_record_keeps_legacy_history_checkpoint_schema() -> None:
    """Сохраняет обязательное поле начальной миграции в ORM-модели."""
    column = SubmissionRecord.__table__.c.history_written

    assert column.nullable is False
    assert column.default is not None
    assert column.default.arg is False


def test_venue_repository_reads_active_bindings() -> None:
    """Возвращает активные привязки пользователя и чата."""
    db = MagicMock()
    binding = _binding("12345")
    db.scalar.return_value = binding
    repository = VenueBindingRepository(db)

    assert repository.get_active("user-1", "chat-1") is binding
    assert repository.get_active_by_chat("chat-1") is binding
    assert db.scalar.call_count == 2


def test_venue_repository_revokes_and_restores_access() -> None:
    """Сохраняет отзыв и последующее восстановление доступа."""
    db = MagicMock()
    binding = _binding("12345")
    db.get.return_value = binding
    repository = VenueBindingRepository(db)

    repository.set_access(1, active=False)

    assert binding.is_active is False
    assert binding.sync_status == "revoked"

    repository.set_access(1, active=True)

    assert binding.is_active is True
    assert binding.sync_status == "synced"
    assert db.flush.call_count == 2


def test_venue_repository_creates_first_binding() -> None:
    """Создаёт новую привязку со статусом ожидающей синхронизации."""
    db = MagicMock()
    db.scalars.return_value = []

    current, deactivated, created = VenueBindingRepository(db).bind(
        user_id="user-1",
        chat_id="chat-1",
        username="cook",
        venue_code="12345",
        venue_name="Кафе",
        legal_name="ООО Кафе",
        spreadsheet_id="sheet-1",
        spreadsheet_url="https://example.test/sheet-1",
    )

    assert created is True
    assert deactivated == []
    assert current.sync_status == "pending"
    assert current.is_active is True
    db.add.assert_called_once_with(current)
    db.flush.assert_called_once()


def test_venue_repository_switches_active_binding() -> None:
    """Обновляет выбранное заведение и отключает прежнюю привязку."""
    db = MagicMock()
    previous = _binding("OLD", binding_id=1)
    current = _binding("NEW", active=False, binding_id=2)
    db.scalars.return_value = [previous, current]

    selected, deactivated, created = VenueBindingRepository(db).bind(
        user_id="user-1",
        chat_id="chat-1",
        username="new-cook",
        venue_code="NEW",
        venue_name="Новое кафе",
        legal_name="",
        spreadsheet_id="new-sheet",
        spreadsheet_url="",
    )

    assert selected is current
    assert created is False
    assert deactivated == [previous]
    assert previous.is_active is False
    assert current.is_active is True
    assert current.sync_status == "pending"
    assert current.username == "new-cook"


def test_venue_repository_marks_sync_and_ignores_missing_binding() -> None:
    """Сохраняет результат синхронизации только для существующей записи."""
    db = MagicMock()
    binding = _binding("12345")
    db.get.side_effect = [binding, None]
    repository = VenueBindingRepository(db)

    repository.mark_sync(1, "failed", "offline")
    repository.mark_sync(2, "synced")

    assert binding.sync_status == "failed"
    assert binding.sync_error == "offline"
    db.flush.assert_called_once()


def test_venue_repository_restores_previous_binding_after_sync_failure() -> None:
    """Возвращает прежнее заведение при ошибке Google Sheets."""
    db = MagicMock()
    previous = _binding("OLD", active=False, binding_id=1)
    failed = _binding("NEW", binding_id=2)
    failed.sync_status = "pending"
    db.scalars.return_value = [previous, failed]

    VenueBindingRepository(db).restore_after_sync_failure(
        binding_id=2,
        user_id="user-1",
        chat_id="chat-1",
        previous_code="OLD",
        error="offline",
    )

    assert failed.is_active is False
    assert failed.sync_status == "failed"
    assert failed.sync_error == "offline"
    assert previous.is_active is True
    assert previous.sync_status == "synced"
    assert previous.sync_error is None
    db.flush.assert_called_once()
