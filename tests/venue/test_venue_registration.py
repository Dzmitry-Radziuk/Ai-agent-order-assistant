"""Проверяет поведение, связанное с модулем «test venue registration»."""

from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from restaurant_bot.domain.models import InputKind, TelegramEvent
from restaurant_bot.integrations.venue_access_registry import VenueAccessRegistry
from restaurant_bot.integrations.venue_directory import (
    VenueDirectory,
    VenueDirectoryError,
    extract_spreadsheet_id,
)
from restaurant_bot.presentation.telegram.venue_registration import not_bound_reply
from restaurant_bot.services import venue_registration as registration_module
from restaurant_bot.services.venue_registration import (
    VenueContext,
    VenueRegistrationService,
)
from restaurant_bot.venues.codes import valid_code
from restaurant_bot.venues.contracts import Venue


def _event(text: str = "", *, callback: str = "", chat_type: str = "private") -> TelegramEvent:
    """Создаёт тестовое событие Telegram."""
    return TelegramEvent(
        update_id=1,
        chat_id="77",
        telegram_user_id="77",
        telegram_username="cook",
        chat_type=chat_type,
        input_type=InputKind.CALLBACK if callback else InputKind.TEXT,
        text=text,
        callback_data=callback,
        callback_message_id=10 if callback else None,
    )


class _Directory:
    """Имитирует справочник заведений в тестах регистрации."""

    def __init__(self, matches: list[Venue]):
        """Инициализирует тестовый двойник зависимости."""
        self.matches = matches
        self.codes: list[str] = []

    def find(self, code: str) -> list[Venue]:
        """Возвращает тестовое заведение по коду."""
        self.codes.append(code)
        return self.matches


class _Service(VenueRegistrationService):
    """Имитирует сервис контекста заведения в тестах."""

    current: VenueContext | None = None

    def context_for(self, event: TelegramEvent) -> VenueContext | None:
        """Возвращает тестовый контекст активного заведения."""
        del event
        return self.current

    def denied_reply(self, event: TelegramEvent):  # type: ignore[no-untyped-def]
        """Возвращает ответ для пользователя без тестового обращения к БД."""
        del event
        return not_bound_reply()


def _service(settings, matches: list[Venue]) -> _Service:  # type: ignore[no-untyped-def]
    """Создаёт настроенный тестовый экземпляр сервиса."""
    return _Service(settings, MagicMock(), MagicMock(), directory=_Directory(matches))  # type: ignore[arg-type]


def _venue(code: str = "6461W6", name: str = "Качели") -> Venue:
    """Создаёт тестовую запись заведения."""
    return Venue(
        code=code,
        name=name,
        legal_name="ООО ЛИР",
        spreadsheet_id="sheet-venue-1",
        spreadsheet_url="https://docs.google.com/spreadsheets/d/sheet-venue-1/edit",
    )


def _access_row(
    *,
    active: str = "TRUE",
    company_type: str = "Заведение",
    user_id: str = "77",
    chat_id: str = "77",
    code: str = "6461W6",
) -> dict[str, str]:
    """Создаёт строку центрального реестра доступа."""
    return {
        "Тип компании": company_type,
        "Канал": "telegram",
        "Код": code,
        "Chat ID": chat_id,
        "User ID": user_id,
        "Активен": active,
    }


def _binding(*, active: bool = True, status: str = "synced") -> SimpleNamespace:
    """Создаёт сохранённую привязку пользователя."""
    return SimpleNamespace(
        id=5,
        channel="telegram",
        telegram_user_id="77",
        telegram_chat_id="77",
        venue_code="6461W6",
        venue_name="Качели",
        spreadsheet_id="sheet-venue-1",
        spreadsheet_url="https://docs.google.com/spreadsheets/d/sheet-venue-1/edit",
        is_active=active,
        sync_status=status,
    )


@pytest.mark.parametrize("active", ["TRUE", "ИСТИНА"])
def test_access_registry_allows_only_active_exact_binding(
    settings,
    active: str,
) -> None:  # type: ignore[no-untyped-def]
    """Разрешает доступ только точному активному пользователю и заведению."""
    redis = MagicMock()
    redis.get.return_value = None
    sheets = MagicMock()
    sheets.read_venue_registrations.return_value = [_access_row(active=active)]
    registry = VenueAccessRegistry(settings, redis, sheets)

    assert registry.decision("77", "77", "6461w6") is True
    assert registry.decision("88", "77", "6461W6") is False
    redis.setex.assert_called()


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [_access_row(active="FALSE")],
        [_access_row(company_type="Поставщик")],
        [_access_row(active="TRUE"), _access_row(active="FALSE")],
    ],
)
def test_access_registry_denies_missing_inactive_or_conflicting_rows(
    settings,
    rows: list[dict[str, str]],
) -> None:  # type: ignore[no-untyped-def]
    """Запрещает удалённую, неактивную или противоречивую привязку."""
    redis = MagicMock()
    redis.get.return_value = None
    sheets = MagicMock()
    sheets.read_venue_registrations.return_value = rows

    assert VenueAccessRegistry(settings, redis, sheets).decision("77", "77", "6461W6") is False


def test_access_registry_rejects_another_sheet_structure(settings) -> None:  # type: ignore[no-untyped-def]
    """Не принимает лист поставщика или другой лист за реестр заведений."""
    redis = MagicMock()
    redis.get.return_value = None
    sheets = MagicMock()
    sheets.read_venue_registrations.return_value = [
        {"Поставщик": "Раджабов", "Chat ID": "77", "User ID": "77"}
    ]

    assert VenueAccessRegistry(settings, redis, sheets).decision("77", "77", "6461W6") is None


def test_access_registry_failure_keeps_last_database_decision(settings) -> None:  # type: ignore[no-untyped-def]
    """Возвращает неопределённость вместо массовой блокировки при сбое Google."""
    redis = MagicMock()
    redis.get.return_value = None
    sheets = MagicMock()
    sheets.read_venue_registrations.side_effect = TimeoutError("offline")

    assert VenueAccessRegistry(settings, redis, sheets).decision("77", "77", "6461W6") is None


def test_access_registry_uses_short_redis_cache(settings) -> None:  # type: ignore[no-untyped-def]
    """Повторно использует снимок прав и не читает Google на каждом сообщении."""
    redis = MagicMock()
    redis.get.return_value = json.dumps(
        [
            {
                "channel": "telegram",
                "user_id": "77",
                "chat_id": "77",
                "venue_code": "6461W6",
                "active": True,
            }
        ]
    )
    sheets = MagicMock()

    assert VenueAccessRegistry(settings, redis, sheets).decision("77", "77", "6461W6") is True
    sheets.read_venue_registrations.assert_not_called()


def _mock_binding_repository(
    monkeypatch: pytest.MonkeyPatch,
    repository: MagicMock,
) -> None:
    """Подменяет транзакции и репозиторий привязок."""
    session_factory = MagicMock()
    session_factory.return_value = nullcontext(MagicMock())
    session_factory.begin.return_value = nullcontext(MagicMock())
    monkeypatch.setattr(registration_module, "SessionLocal", session_factory)
    monkeypatch.setattr(
        registration_module,
        "VenueBindingRepository",
        lambda _db: repository,
    )


def test_context_revokes_access_removed_from_central_table(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    """Отключает локальную привязку, отсутствующую в центральном реестре."""
    binding = _binding()
    repository = MagicMock()
    repository.get_active.return_value = binding
    repository.get_revoked.return_value = None
    _mock_binding_repository(monkeypatch, repository)
    service = VenueRegistrationService(settings, MagicMock(), MagicMock(), directory=_Directory([]))
    service.access_registry = MagicMock()
    service.access_registry.decision.return_value = False

    assert service.context_for_identity("77", "77") is None
    repository.set_access.assert_called_once_with(5, active=False)


def test_context_restores_access_reenabled_in_central_table(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    """Возвращает доступ после установки Активен=TRUE."""
    revoked = _binding(active=False, status="revoked")
    restored = _binding()
    repository = MagicMock()
    repository.get_active.return_value = None
    repository.get_revoked.return_value = revoked
    repository.set_access.return_value = restored
    _mock_binding_repository(monkeypatch, repository)
    service = VenueRegistrationService(settings, MagicMock(), MagicMock(), directory=_Directory([]))
    service.access_registry = MagicMock()
    service.access_registry.decision.return_value = True

    context = service.context_for_identity("77", "77")

    repository.set_access.assert_called_once_with(5, active=True)
    assert context is not None
    assert context.venue_code == "6461W6"


def test_context_keeps_active_binding_when_registry_is_temporarily_unavailable(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    """Не блокирует действующего пользователя из-за временного сбоя Google."""
    binding = _binding()
    repository = MagicMock()
    repository.get_active.return_value = binding
    repository.get_revoked.return_value = None
    _mock_binding_repository(monkeypatch, repository)
    service = VenueRegistrationService(settings, MagicMock(), MagicMock(), directory=_Directory([]))
    service.access_registry = MagicMock()
    service.access_registry.decision.return_value = None

    context = service.context_for_identity("77", "77")

    repository.set_access.assert_not_called()
    assert context is not None
    assert context.venue_code == "6461W6"


def test_revoked_user_cannot_reconnect_with_the_old_invite_code(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    """Не позволяет обойти блокировку повторным вводом кода заведения."""
    repository = MagicMock()
    repository.get_revoked.return_value = _binding(active=False, status="revoked")
    _mock_binding_repository(monkeypatch, repository)
    service = VenueRegistrationService(settings, MagicMock(), MagicMock(), directory=_Directory([]))
    service.access_registry = MagicMock()
    service.access_registry.decision.return_value = False

    result = service._bind(_event(), _venue(), None)

    assert result.reply is not None
    assert "Доступ к заведению отключён" in result.reply.text
    repository.bind.assert_not_called()
    service.sheets.upsert_venue_registration.assert_not_called()


@pytest.mark.parametrize(
    ("text", "expected_code"),
    [
        ("/start 6461W6", "6461W6"),
        ("/code 6461w6", "6461W6"),
        ("код 6461w6", "6461W6"),
        ("6461w6", "6461W6"),
    ],
)
def test_all_registration_entry_points_use_one_lookup(
    settings,
    text: str,
    expected_code: str,  # type: ignore[no-untyped-def]
) -> None:
    """Проверяет, что все регистрация запись points use один поиск."""
    service = _service(settings, [_venue()])

    result = service.handle(_event(text))

    assert result.handled is True
    assert service.directory.codes == [expected_code]  # type: ignore[attr-defined]
    assert result.reply is not None
    assert "Качели" in result.reply.text


def test_arbitrary_text_is_not_an_invite_code(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что произвольный текст не является пригласительным кодом."""
    result = _service(settings, []).handle(_event("добавить товары"))
    assert result.handled is False
    assert valid_code("добавить") is False


def test_start_without_code_explains_how_to_connect(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что start без кода объясняет порядок подключения."""
    result = _service(settings, []).handle(_event("/start"))
    assert result.handled is True
    assert result.reply is not None
    assert "ещё не подключено" in result.reply.text


def test_start_when_already_connected_shows_new_order_button(settings) -> None:  # type: ignore[no-untyped-def]
    """Показывает кнопку новой заявки для уже подключённого пользователя."""
    service = _service(settings, [])
    service.current = VenueContext(
        venue_code="6461W6",
        venue_name="Качели",
        spreadsheet_id="sheet-venue-1",
        spreadsheet_url="",
        telegram_user_id="77",
        telegram_chat_id="77",
    )

    result = service.handle(_event("/start"))

    assert result.reply is not None
    button = result.reply.rows[0][0]
    assert button.text == "Новая заявка"
    assert button.callback_data == "v2:new"


def test_registration_is_rejected_outside_private_chat(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что регистрация является rejected вне личный чат."""
    result = _service(settings, [_venue()]).handle(_event("/start 6461W6", chat_type="group"))
    assert result.handled is True
    assert result.reply is not None
    assert "только в личном чате" in result.reply.text


def test_confirmation_escapes_venue_html(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что подтверждение экранирует заведение html."""
    result = _service(settings, [_venue(name='<Кафе & "Бар">')]).handle(_event("/start 6461W6"))
    assert result.reply is not None
    assert '&lt;Кафе &amp; "Бар"&gt;' in result.reply.text
    assert result.reply.rows[0][0].callback_data == "venue_bind:6461W6:yes"


def test_callback_revision_is_ignored_and_code_is_looked_up_again(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что при старой ревизии callback игнорируется, а код запрашивается заново."""
    service = _service(settings, [_venue()])
    service._bind = MagicMock()  # type: ignore[method-assign]
    service._bind.return_value.handled = True

    service.handle(_event(callback="venue_bind:6461W6:yes:r9"))

    assert service.directory.codes == ["6461W6"]  # type: ignore[attr-defined]
    service._bind.assert_called_once()


def test_declining_confirmation_never_writes(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что отказ от подтверждение никогда не записывает."""
    service = _service(settings, [_venue()])
    service._bind = MagicMock()  # type: ignore[method-assign]

    result = service.handle(_event(callback="venue_bind:6461W6:no"))

    assert result.reply is not None and result.reply.text == "Подключение отменено."
    service._bind.assert_not_called()


def test_switch_requires_an_extra_confirmation(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что смена требует дополнительного подтверждения."""
    service = _service(settings, [_venue()])
    service.current = VenueContext(
        venue_code="OLD123",
        venue_name="Старое кафе",
        spreadsheet_id="old-sheet",
        spreadsheet_url="",
        telegram_user_id="77",
        telegram_chat_id="77",
    )

    result = service.handle(_event(callback="venue_bind:6461W6:yes"))

    assert result.reply is not None
    assert "Старое кафе" in result.reply.text
    assert "Качели" in result.reply.text
    assert result.reply.rows[0][0].callback_data == "venue_switch:6461W6:yes"


def test_missing_and_conflicting_codes_have_distinct_messages(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что отсутствующий и конфликтующие codes имеют различаются messages."""
    missing = _service(settings, []).handle(_event("6461W6"))
    conflict = _service(settings, [_venue(), _venue(name="Дубль")]).handle(_event("6461W6"))
    assert missing.reply is not None and "не найден" in missing.reply.text
    assert conflict.reply is not None and "неоднозначен" in conflict.reply.text


def test_gviz_parser_uses_headers_and_extracts_real_sheet_id() -> None:
    """Проверяет, что gviz парсер использует заголовки и extracts реальный таблица идентификатор."""
    payload = {
        "table": {
            "cols": [
                {"label": "Ссылка на таблицу"},
                {"label": "Код"},
                {"label": "Условное наз-ие заведения"},
                {"label": "Юр наз-ие компании"},
            ],
            "rows": [
                {
                    "c": [
                        {"v": "https://docs.google.com/spreadsheets/d/abc_DEF-123456789012/edit"},
                        {"v": "6461w6"},
                        {"v": "Качели"},
                        {"v": "ООО ЛИР"},
                    ]
                }
            ],
        }
    }
    text = f"/*O_o*/ google.visualization.Query.setResponse({json.dumps(payload)});"
    venues = VenueDirectory.parse_gviz(text)
    assert venues[0].code == "6461W6"
    assert venues[0].spreadsheet_id == "abc_DEF-123456789012"


def test_gviz_parser_rejects_missing_required_header() -> None:
    """Проверяет, что gviz парсер отклоняет отсутствующий required header."""
    text = 'google.visualization.Query.setResponse({"table":{"cols":[],"rows":[]}});'
    with pytest.raises(VenueDirectoryError, match="missing venue directory header"):
        VenueDirectory.parse_gviz(text)


def test_directory_does_not_cache_transport_errors(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что справочник выполняет не кэш транспорт ошибки."""
    redis = MagicMock()
    redis.get.return_value = None
    client = MagicMock()
    client.get.side_effect = httpx.ConnectError("offline")
    directory = VenueDirectory(settings, redis, client=client)
    with pytest.raises(VenueDirectoryError):
        directory.all()
    redis.setex.assert_not_called()


def test_directory_keeps_a_complete_gviz_url(settings) -> None:  # type: ignore[no-untyped-def]
    """Использует готовый адрес справочника без изменения."""
    url = "https://docs.google.com/spreadsheets/d/sheet-id/gviz/tq?tqx=out:json&gid=42"
    configured = settings.model_copy(update={"google_venue_directory_url": url})
    directory = VenueDirectory(configured, MagicMock(), client=MagicMock())

    assert directory._directory_url() == url


def test_directory_builds_gviz_url_from_spreadsheet_id(settings) -> None:  # type: ignore[no-untyped-def]
    """Строит адрес справочника, если передан только ID таблицы."""
    spreadsheet_id = "abc_DEF-12345678901234567890"
    configured = settings.model_copy(
        update={
            "google_venue_directory_url": spreadsheet_id,
            "google_registration_sheet": "Чаты заведений",
        }
    )
    directory = VenueDirectory(configured, MagicMock(), client=MagicMock())

    assert directory._directory_url() == (
        "https://docs.google.com/spreadsheets/d/"
        f"{spreadsheet_id}/gviz/tq?tqx=out%3Ajson&sheet=%D0%A7%D0%B0%D1%82%D1%8B+"
        "%D0%B7%D0%B0%D0%B2%D0%B5%D0%B4%D0%B5%D0%BD%D0%B8%D0%B9"
    )


def test_directory_falls_back_to_registration_spreadsheet_id(settings) -> None:  # type: ignore[no-untyped-def]
    """Использует центральную таблицу регистрации при пустом URL."""
    spreadsheet_id = "abc_DEF-12345678901234567890"
    configured = settings.model_copy(
        update={
            "google_venue_directory_url": "",
            "google_registration_spreadsheet_id": spreadsheet_id,
        }
    )
    directory = VenueDirectory(configured, MagicMock(), client=MagicMock())

    assert f"/d/{spreadsheet_id}/gviz/tq?" in directory._directory_url()


def test_directory_reports_missing_configuration_before_http_call(settings) -> None:  # type: ignore[no-untyped-def]
    """Возвращает контролируемую ошибку при отсутствии настроек."""
    client = MagicMock()
    directory = VenueDirectory(settings, MagicMock(), client=client)

    with pytest.raises(VenueDirectoryError, match="not configured"):
        directory._directory_url()

    client.get.assert_not_called()


def test_spreadsheet_id_parser_does_not_accept_arbitrary_url() -> None:
    """Проверяет, что таблица идентификатор парсер выполняет не accept произвольный URL."""
    assert extract_spreadsheet_id("https://example.test/not-a-sheet") == ""


def test_successful_binding_is_synced_before_success_is_shown(settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что успешная привязка синхронизируется до показа сообщения об успехе."""
    binding = SimpleNamespace(
        id=5,
        channel="telegram",
        telegram_user_id="77",
        telegram_chat_id="77",
        username="cook",
        venue_code="6461W6",
        venue_name="Качели",
        legal_name="ООО ЛИР",
        spreadsheet_id="sheet-venue-1",
        spreadsheet_url="https://docs.google.com/spreadsheets/d/sheet-venue-1/edit",
        created_at=datetime.now(UTC),
    )
    repository = MagicMock()
    repository.bind.return_value = (binding, [], True)
    monkeypatch.setattr(
        registration_module,
        "SessionLocal",
        SimpleNamespace(begin=lambda: nullcontext(MagicMock())),
    )
    monkeypatch.setattr(
        registration_module,
        "VenueBindingRepository",
        lambda _db: repository,
    )
    service = _service(settings, [_venue()])

    result = service._bind(_event(callback="venue_bind:6461W6:yes"), _venue(), None)

    service.sheets.upsert_venue_registration.assert_called_once()  # type: ignore[attr-defined]
    repository.mark_sync.assert_called_once_with(5, "synced")
    assert result.context is not None
    assert result.context.spreadsheet_id == "sheet-venue-1"
    assert result.reply is not None and "Вы подключены" in result.reply.text


def test_google_sync_failure_does_not_report_success(settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что Google синхронизация сбой выполняет не report успех."""
    binding = SimpleNamespace(
        id=5,
        channel="telegram",
        telegram_user_id="77",
        telegram_chat_id="77",
        username="cook",
        venue_code="6461W6",
        venue_name="Качели",
        legal_name="ООО ЛИР",
        spreadsheet_id="sheet-venue-1",
        spreadsheet_url="",
        created_at=datetime.now(UTC),
    )
    repository = MagicMock()
    repository.bind.return_value = (binding, [], True)
    monkeypatch.setattr(
        registration_module,
        "SessionLocal",
        SimpleNamespace(begin=lambda: nullcontext(MagicMock())),
    )
    monkeypatch.setattr(
        registration_module,
        "VenueBindingRepository",
        lambda _db: repository,
    )
    service = _service(settings, [_venue()])
    service.sheets.upsert_venue_registration.side_effect = TimeoutError("offline")  # type: ignore[attr-defined]

    result = service._bind(_event(callback="venue_bind:6461W6:yes"), _venue(), None)

    repository.restore_after_sync_failure.assert_called_once()
    repository.mark_sync.assert_not_called()
    assert result.context is None
    assert result.reply is not None and "Не удалось сохранить" in result.reply.text


def test_repeated_binding_reports_already_connected_without_duplicate(
    settings, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что повторный привязка сообщает уже connected без дубликат."""
    binding = SimpleNamespace(
        id=5,
        channel="telegram",
        telegram_user_id="77",
        telegram_chat_id="77",
        username="cook",
        venue_code="6461W6",
        venue_name="Качели",
        legal_name="ООО ЛИР",
        spreadsheet_id="sheet-venue-1",
        spreadsheet_url="",
        created_at=datetime.now(UTC),
    )
    repository = MagicMock()
    repository.bind.return_value = (binding, [], False)
    monkeypatch.setattr(
        registration_module,
        "SessionLocal",
        SimpleNamespace(begin=lambda: nullcontext(MagicMock())),
    )
    monkeypatch.setattr(
        registration_module,
        "VenueBindingRepository",
        lambda _db: repository,
    )
    service = _service(settings, [_venue()])
    previous = VenueContext(
        venue_code="6461W6",
        venue_name="Качели",
        spreadsheet_id="sheet-venue-1",
        spreadsheet_url="",
        telegram_user_id="77",
        telegram_chat_id="77",
    )

    result = service._bind(_event(callback="venue_bind:6461W6:yes"), _venue(), previous)

    assert result.reset_session is False
    assert result.reply is not None and "уже подключены" in result.reply.text
