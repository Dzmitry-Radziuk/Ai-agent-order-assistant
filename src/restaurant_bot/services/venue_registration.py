from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar, cast

import httpx
import structlog
from redis import Redis

from restaurant_bot.config import Settings
from restaurant_bot.db import SessionLocal
from restaurant_bot.db_models import VenueBinding
from restaurant_bot.domain.models import BotReply, Button, TelegramEvent
from restaurant_bot.integrations.google_sheets import GoogleSheetsGateway
from restaurant_bot.repositories.venue_bindings import VenueBindingRepository
from restaurant_bot.services.text import clean_text, escape, normalize_text

logger = structlog.get_logger(__name__)


class VenueDirectoryError(RuntimeError):
    """Сообщает об ошибке справочника заведений."""

    pass


@dataclass(slots=True, frozen=True)
class Venue:
    """Описывает заведение из центрального справочника."""

    code: str
    name: str
    legal_name: str
    spreadsheet_id: str
    spreadsheet_url: str


@dataclass(slots=True, frozen=True)
class VenueContext:
    """Передаёт контекст активного заведения при обработке."""

    venue_code: str
    venue_name: str
    spreadsheet_id: str
    spreadsheet_url: str
    telegram_user_id: str
    telegram_chat_id: str


@dataclass(slots=True)
class RegistrationResult:
    """Описывает результат привязки пользователя к заведению."""

    handled: bool
    reply: BotReply | None = None
    context: VenueContext | None = None
    reset_session: bool = False


class VenueDirectory:
    """Находит заведение по коду приглашения."""

    CACHE_KEY = "restaurant-bot:venue-directory:v1"
    HEADER_ALIASES: ClassVar[dict[str, tuple[str, ...]]] = {
        "code": ("Код", "Код заведения", "Invite код"),
        "name": (
            "Условное наз-ие заведения",
            "Условное название заведения",
            "Название заведения",
        ),
        "legal_name": (
            "Юр наз-ие компании",
            "Юр. Название компании",
            "Юридическое название",
        ),
        "spreadsheet": (
            "Ссылка на таблицу",
            "ID таблицы заведения",
            "Spreadsheet ID",
        ),
    }

    def __init__(self, settings: Settings, redis: Redis[Any], client: httpx.Client | None = None):
        """Инициализирует компонент."""
        self.settings = settings
        self.redis = redis
        self.client = client or httpx.Client(timeout=httpx.Timeout(10.0, connect=2.0))

    def find(self, code: str) -> list[Venue]:
        """Находит заведение по нормализованному коду."""
        wanted = normalize_code(code)
        return [venue for venue in self.all() if venue.code == wanted]

    def all(self, force_refresh: bool = False) -> list[Venue]:
        """Возвращает все заведения из кэша или справочника."""
        if not force_refresh and (cached := self.redis.get(self.CACHE_KEY)):
            payload = json.loads(cast(str | bytes | bytearray, cached))
            return [Venue(**item) for item in payload]
        try:
            response = self.client.get(self.settings.google_venue_directory_url)
            response.raise_for_status()
            venues = self.parse_gviz(response.text)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, VenueDirectoryError) as exc:
            raise VenueDirectoryError("venue directory is unavailable") from exc
        self.redis.setex(
            self.CACHE_KEY,
            self.settings.venue_directory_cache_ttl_seconds,
            json.dumps([asdict(venue) for venue in venues], ensure_ascii=False),
        )
        return venues

    @classmethod
    def parse_gviz(cls, text: str) -> list[Venue]:
        """Разбирает ответ публичного интерфейса GViz."""
        match = re.search(r"setResponse\((\{.*\})\);?\s*$", text.strip(), re.S)
        if not match:
            raise VenueDirectoryError("invalid GViz response")
        payload = json.loads(match.group(1))
        table = payload.get("table") or {}
        columns = table.get("cols") or []
        headers = [clean_text(column.get("label")) for column in columns]
        indexes: dict[str, int] = {}
        normalized_headers = {normalize_text(header): index for index, header in enumerate(headers)}
        for field, aliases in cls.HEADER_ALIASES.items():
            index = next(
                (
                    normalized_headers[normalize_text(alias)]
                    for alias in aliases
                    if normalize_text(alias) in normalized_headers
                ),
                None,
            )
            if index is None and field in {"code", "name", "spreadsheet"}:
                raise VenueDirectoryError(f"missing venue directory header: {aliases[0]}")
            if index is not None:
                indexes[field] = index

        venues: list[Venue] = []
        for raw_row in table.get("rows") or []:
            cells = raw_row.get("c") or []

            def value(field: str, row_cells: list[dict[str, Any] | None] = cells) -> str:
                """Возвращает нормализованное значение ячейки GViz."""
                index = indexes.get(field)
                if index is None or index >= len(row_cells) or not row_cells[index]:
                    return ""
                cell = row_cells[index]
                assert cell is not None
                return clean_text(cell.get("f") or cell.get("v"))

            code = normalize_code(value("code"))
            name = value("name")
            spreadsheet_url = value("spreadsheet")
            spreadsheet_id = extract_spreadsheet_id(spreadsheet_url)
            if not code or not name or not spreadsheet_id:
                continue
            venues.append(
                Venue(
                    code=code,
                    name=name,
                    legal_name=value("legal_name"),
                    spreadsheet_id=spreadsheet_id,
                    spreadsheet_url=spreadsheet_url,
                )
            )
        return venues


def normalize_code(value: str) -> str:
    """Нормализует код приглашения."""
    return clean_text(value).upper()


def valid_code(value: str) -> bool:
    """Проверяет формат кода приглашения."""
    code = normalize_code(value)
    return bool(re.fullmatch(r"[A-ZА-ЯЁ0-9]{4,32}", code) and re.search(r"\d", code))


def extract_spreadsheet_id(value: str) -> str:
    """Извлекает идентификатор Google-таблицы."""
    raw = clean_text(value)
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", raw)
    if match:
        return match.group(1)
    return raw if re.fullmatch(r"[A-Za-z0-9_-]{20,}", raw) else ""


class VenueRegistrationService:
    """Проверяет и сохраняет привязку к заведению."""

    def __init__(
        self,
        settings: Settings,
        redis: Redis[Any],
        sheets: GoogleSheetsGateway,
        directory: VenueDirectory | None = None,
    ):
        """Инициализирует компонент."""
        self.settings = settings
        self.redis = redis
        self.sheets = sheets
        self.directory = directory or VenueDirectory(settings, redis)

    def context_for(self, event: TelegramEvent) -> VenueContext | None:
        """Возвращает активный контекст заведения пользователя."""
        if event.chat_type != "private" or not event.telegram_user_id:
            return None
        with SessionLocal() as db:
            row = VenueBindingRepository(db).get_active(event.telegram_user_id, event.chat_id)
            return self._context(row)

    def bootstrap_existing_bindings(self) -> int:
        """Импортирует действующие привязки из старого реестра."""
        venues_by_code: dict[str, list[Venue]] = {}
        for venue in self.directory.all(force_refresh=True):
            venues_by_code.setdefault(venue.code, []).append(venue)
        imported = 0
        for row in self.sheets.read_venue_registrations():
            channel = normalize_text(row.get("Канал"))
            active = normalize_text(row.get("Активен")) in {"true", "да", "1", "активен"}
            code = normalize_code(clean_text(row.get("Код")))
            user_id = clean_text(row.get("User ID"))
            chat_id = clean_text(row.get("Chat ID"))
            matches = venues_by_code.get(code, [])
            if (
                channel != "telegram"
                or not active
                or not user_id
                or not chat_id
                or len(matches) != 1
            ):
                continue
            venue = matches[0]
            with SessionLocal.begin() as db:
                repository = VenueBindingRepository(db)
                binding, _, _ = repository.bind(
                    user_id=user_id,
                    chat_id=chat_id,
                    username=clean_text(row.get("Username")),
                    venue_code=venue.code,
                    venue_name=venue.name,
                    legal_name=venue.legal_name,
                    spreadsheet_id=venue.spreadsheet_id,
                    spreadsheet_url=venue.spreadsheet_url,
                )
                repository.mark_sync(binding.id, "synced")
            imported += 1
        return imported

    def handle(self, event: TelegramEvent) -> RegistrationResult:
        """Обрабатывает входные данные текущего компонента."""
        action, code = self._registration_input(event)
        if not action:
            return RegistrationResult(handled=False)
        logger.info(
            "venue_registration_started",
            telegram_user_id=event.telegram_user_id,
            chat_id=event.chat_id,
            has_code=bool(code),
        )
        if event.chat_type != "private" or not event.telegram_user_id:
            return RegistrationResult(
                handled=True,
                reply=BotReply(text="Подключить заведение можно только в личном чате с ботом."),
            )
        if action in {"reject", "switch_reject"}:
            logger.info("venue_binding_rejected", telegram_user_id=event.telegram_user_id)
            return RegistrationResult(handled=True, reply=BotReply(text="Подключение отменено."))
        if action == "start" and not code:
            context = self.context_for(event)
            if context:
                return RegistrationResult(
                    handled=True,
                    reply=BotReply(
                        text=(f"Вы уже подключены к заведению:\n«{escape(context.venue_name)}»")
                    ),
                    context=context,
                )
            return RegistrationResult(handled=True, reply=self.not_bound_reply())
        if not code or not valid_code(code):
            return RegistrationResult(handled=True, reply=self.not_bound_reply())

        venue_result = self._lookup(code, event)
        if isinstance(venue_result, BotReply):
            return RegistrationResult(handled=True, reply=venue_result)
        venue = venue_result
        current = self.context_for(event)
        if action == "confirm" and current and current.venue_code != venue.code:
            return RegistrationResult(
                handled=True,
                reply=self._switch_confirmation(current, venue),
            )
        if action in {"confirm", "switch_confirm"}:
            return self._bind(event, venue, current)
        logger.info("venue_confirmation_shown", venue_code=venue.code)
        return RegistrationResult(handled=True, reply=self._confirmation(venue))

    def _lookup(self, code: str, event: TelegramEvent) -> Venue | BotReply:
        """Находит единственное заведение по коду приглашения."""
        normalized_code = normalize_code(code)
        try:
            matches = self.directory.find(normalized_code)
        except VenueDirectoryError:
            logger.exception("venue_directory_read_failed", chat_id=event.chat_id)
            return BotReply(
                text="Не удалось проверить код заведения.\n\nПопробуйте ещё раз через несколько секунд."
            )
        if not matches:
            logger.info("venue_code_not_found", venue_code=normalized_code)
            return BotReply(
                text=(
                    "Код заведения не найден.\n\n"
                    "Проверьте код или запросите новую invite-ссылку у менеджера АвтоСнаб."
                )
            )
        if len(matches) > 1:
            logger.warning("venue_code_conflict", venue_code=normalized_code)
            return BotReply(
                text=(
                    "Код привязки неоднозначен.\n\n"
                    "Обратитесь к менеджеру АвтоСнаб за новой invite-ссылкой."
                )
            )
        return matches[0]

    def _bind(
        self, event: TelegramEvent, venue: Venue, previous: VenueContext | None
    ) -> RegistrationResult:
        """Создаёт или обновляет привязку к заведению."""
        with SessionLocal.begin() as db:
            binding, deactivated, created = VenueBindingRepository(db).bind(
                user_id=event.telegram_user_id,
                chat_id=event.chat_id,
                username=event.telegram_username,
                venue_code=venue.code,
                venue_name=venue.name,
                legal_name=venue.legal_name,
                spreadsheet_id=venue.spreadsheet_id,
                spreadsheet_url=venue.spreadsheet_url,
            )
            binding_id = binding.id
            deactivated_payloads = [self._registry_values(row, active=False) for row in deactivated]
            binding_values = self._registry_values(binding, active=True)
        try:
            for payload in deactivated_payloads:
                self.sheets.upsert_venue_registration(payload)
            self.sheets.upsert_venue_registration(binding_values)
        except Exception as exc:
            with SessionLocal.begin() as db:
                VenueBindingRepository(db).restore_after_sync_failure(
                    binding_id=binding_id,
                    user_id=event.telegram_user_id,
                    chat_id=event.chat_id,
                    previous_code=previous.venue_code if previous else "",
                    error=str(exc)[:1000],
                )
            logger.exception(
                "venue_binding_google_sync_failed",
                telegram_user_id=event.telegram_user_id,
                venue_code=venue.code,
            )
            return RegistrationResult(
                handled=True,
                reply=BotReply(
                    text=(
                        "Не удалось сохранить подключение.\n\n"
                        "Попробуйте ещё раз или обратитесь к менеджеру АвтоСнаб."
                    )
                ),
            )
        with SessionLocal.begin() as db:
            VenueBindingRepository(db).mark_sync(binding_id, "synced")
        context = VenueContext(
            venue_code=venue.code,
            venue_name=venue.name,
            spreadsheet_id=venue.spreadsheet_id,
            spreadsheet_url=venue.spreadsheet_url,
            telegram_user_id=event.telegram_user_id,
            telegram_chat_id=event.chat_id,
        )
        logger.info(
            "venue_binding_created" if created else "venue_binding_updated",
            telegram_user_id=event.telegram_user_id,
            venue_code=venue.code,
        )
        already_same = previous is not None and previous.venue_code == venue.code
        text = (
            f"Вы уже подключены к заведению:\n«{escape(venue.name)}»"
            if already_same
            else (
                "✅ Вы подключены к заведению:\n"
                f"«{escape(venue.name)}»\n\nТеперь Вы можете создавать заявки в этом чате."
            )
        )
        return RegistrationResult(
            handled=True,
            reply=BotReply(text=text),
            context=context,
            reset_session=previous is None or previous.venue_code != venue.code,
        )

    @staticmethod
    def _registry_values(binding: VenueBinding, *, active: bool) -> dict[str, Any]:
        """Формирует строку центрального реестра привязок."""
        now = datetime.now(UTC).strftime("%d.%m.%Y %H:%M:%S")
        bound_at = (
            binding.created_at.astimezone(UTC).strftime("%d.%m.%Y %H:%M:%S")
            if binding.created_at
            else now
        )
        return {
            "company_type": "Заведение",
            "channel": "telegram",
            "code": binding.venue_code,
            "legal_name": binding.legal_name or "",
            "chat_id": binding.telegram_chat_id,
            "user_id": binding.telegram_user_id,
            "username": binding.username,
            "active": "TRUE" if active else "FALSE",
            "bound_at": bound_at,
            "comment": "Подключено через Telegram-бот заведений",
            "venue_name": binding.venue_name,
            "spreadsheet_id": binding.spreadsheet_id,
            "spreadsheet_url": binding.spreadsheet_url or "",
            "updated_at": now,
        }

    @staticmethod
    def _registration_input(event: TelegramEvent) -> tuple[str, str]:
        """Разбирает команду регистрации и код приглашения."""
        callback = re.sub(r":r\d+$", "", event.callback_data, flags=re.I)
        match = re.fullmatch(r"venue_bind:([A-ZА-ЯЁ0-9]{4,32}):(yes|no)", callback, re.I)
        if match:
            return ("confirm" if match.group(2).lower() == "yes" else "reject"), match.group(1)
        match = re.fullmatch(r"venue_switch:([A-ZА-ЯЁ0-9]{4,32}):(yes|no)", callback, re.I)
        if match:
            return (
                "switch_confirm" if match.group(2).lower() == "yes" else "switch_reject",
                match.group(1),
            )
        text = clean_text(event.text)
        match = re.fullmatch(r"/start(?:@[A-Za-z0-9_]+)?(?:\s+(.+))?", text, re.I)
        if match:
            return "start", clean_text(match.group(1))
        match = re.fullmatch(r"/code(?:@[A-Za-z0-9_]+)?(?:\s+(.+))?", text, re.I)
        if match:
            return "code", clean_text(match.group(1))
        match = re.fullmatch(r"код\s+(.+)", text, re.I)
        if match:
            return "code", clean_text(match.group(1))
        if valid_code(text):
            return "code", text
        return "", ""

    @staticmethod
    def _confirmation(venue: Venue) -> BotReply:
        """Формирует карточку подтверждения заведения."""
        return BotReply(
            text=(f"Вы хотите подключиться к заведению:\n«{escape(venue.name)}»?"),
            rows=[
                [Button(text="Да, подключить", callback_data=f"venue_bind:{venue.code}:yes")],
                [Button(text="Нет", callback_data=f"venue_bind:{venue.code}:no")],
            ],
        )

    @staticmethod
    def _switch_confirmation(current: VenueContext, venue: Venue) -> BotReply:
        """Формирует подтверждение смены заведения."""
        return BotReply(
            text=(
                "Сейчас Вы подключены к заведению:\n"
                f"«{escape(current.venue_name)}».\n\n"
                "Подключиться вместо него к заведению:\n"
                f"«{escape(venue.name)}»?"
            ),
            rows=[
                [Button(text="Да, изменить", callback_data=f"venue_switch:{venue.code}:yes")],
                [Button(text="Отмена", callback_data=f"venue_switch:{venue.code}:no")],
            ],
        )

    @staticmethod
    def not_bound_reply() -> BotReply:
        """Формирует инструкцию для непривязанного пользователя."""
        return BotReply(
            text=(
                "Ваше заведение ещё не подключено.\n\n"
                "Отправьте код заведения или перейдите по invite-ссылке, "
                "полученной от менеджера АвтоСнаб."
            )
        )

    @staticmethod
    def _context(row: VenueBinding | None) -> VenueContext | None:
        """Преобразует запись привязки в контекст заведения."""
        if row is None:
            return None
        return VenueContext(
            venue_code=row.venue_code,
            venue_name=row.venue_name,
            spreadsheet_id=row.spreadsheet_id,
            spreadsheet_url=row.spreadsheet_url or "",
            telegram_user_id=row.telegram_user_id,
            telegram_chat_id=row.telegram_chat_id,
        )
