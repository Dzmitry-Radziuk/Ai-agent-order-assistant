"""Координирует сервис «venue registration»."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from redis import Redis

from restaurant_bot.application.venue_registration.contracts import (
    RegistrationResult,
    VenueContext,
)
from restaurant_bot.config import Settings
from restaurant_bot.domain.models import BotReply, TelegramEvent
from restaurant_bot.domain.text import clean_text, normalize_text
from restaurant_bot.input.telegram_venue_registration import registration_input
from restaurant_bot.integrations.google_sheets import GoogleSheetsGateway
from restaurant_bot.integrations.venue_access_registry import (
    VenueAccessRegistry as _VenueAccessRegistry,
)
from restaurant_bot.integrations.venue_directory import (
    VenueDirectory as _VenueDirectory,
)
from restaurant_bot.integrations.venue_directory import (
    VenueDirectoryError as _VenueDirectoryError,
)
from restaurant_bot.persistence.database import SessionLocal
from restaurant_bot.persistence.models import VenueBinding
from restaurant_bot.presentation.telegram.venue_registration import (
    access_disabled_reply,
    already_connected_reply,
    binding_success_reply,
    code_conflict_reply,
    code_not_found_reply,
    confirmation,
    directory_error_reply,
    not_bound_reply,
    private_chat_required_reply,
    rejected_reply,
    switch_confirmation,
    sync_failure_reply,
)
from restaurant_bot.repositories.venue_bindings import VenueBindingRepository
from restaurant_bot.venues.codes import normalize_code, valid_code
from restaurant_bot.venues.contracts import Venue as _Venue

logger = structlog.get_logger(__name__)

__all__ = ["RegistrationResult", "VenueContext", "VenueRegistrationService"]


class VenueRegistrationService:
    """Проверяет и сохраняет привязку к заведению."""

    def __init__(
        self,
        settings: Settings,
        redis: Redis[Any],
        sheets: GoogleSheetsGateway,
        directory: _VenueDirectory | None = None,
    ):
        """Инициализирует компонент."""
        self.settings = settings
        self.redis = redis
        self.sheets = sheets
        self.directory = directory or _VenueDirectory(settings, redis)
        self.access_registry = _VenueAccessRegistry(settings, redis, sheets)

    def context_for(self, event: TelegramEvent) -> VenueContext | None:
        """Возвращает активный контекст заведения пользователя."""
        if event.chat_type != "private" or not event.telegram_user_id:
            return None
        return self.context_for_identity(event.telegram_user_id, event.chat_id)

    def context_for_identity(
        self,
        user_id: str,
        chat_id: str,
        *,
        force_refresh: bool = False,
    ) -> VenueContext | None:
        """Синхронизирует право пользователя и возвращает доступное заведение."""
        with SessionLocal() as db:
            repository = VenueBindingRepository(db)
            row = repository.get_active(user_id, chat_id) or repository.get_revoked(
                user_id,
                chat_id,
            )
        if row is None:
            return None

        decision = self.access_registry.decision(
            user_id,
            chat_id,
            row.venue_code,
            force_refresh=force_refresh,
        )
        if decision is None:
            return self._context(row) if row.is_active and row.sync_status == "synced" else None
        if decision:
            if not row.is_active or row.sync_status != "synced":
                with SessionLocal.begin() as db:
                    restored = VenueBindingRepository(db).set_access(row.id, active=True)
                row = restored or row
                logger.info(
                    "venue_access_restored",
                    telegram_user_id=user_id,
                    venue_code=row.venue_code,
                )
            return self._context(row)

        if row.is_active or row.sync_status != "revoked":
            with SessionLocal.begin() as db:
                VenueBindingRepository(db).set_access(row.id, active=False)
            logger.info(
                "venue_access_revoked",
                telegram_user_id=user_id,
                venue_code=row.venue_code,
            )
        return None

    def bootstrap_existing_bindings(self) -> int:
        """Импортирует действующие привязки из старого реестра."""
        venues_by_code: dict[str, list[_Venue]] = {}
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
        action, code = registration_input(event)
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
                reply=private_chat_required_reply(),
            )
        if action in {"reject", "switch_reject"}:
            logger.info("venue_binding_rejected", telegram_user_id=event.telegram_user_id)
            return RegistrationResult(handled=True, reply=rejected_reply())
        if action == "start" and not code:
            context = self.context_for(event)
            if context:
                return RegistrationResult(
                    handled=True,
                    reply=already_connected_reply(context.venue_name),
                    context=context,
                )
            return RegistrationResult(handled=True, reply=self.denied_reply(event))
        if not code or not valid_code(code):
            return RegistrationResult(handled=True, reply=not_bound_reply())

        venue_result = self._lookup(code, event)
        if isinstance(venue_result, BotReply):
            return RegistrationResult(handled=True, reply=venue_result)
        venue = venue_result
        current = self.context_for(event)
        if action == "confirm" and current and current.venue_code != venue.code:
            return RegistrationResult(
                handled=True,
                reply=switch_confirmation(current.venue_name, venue),
            )
        if action in {"confirm", "switch_confirm"}:
            return self._bind(event, venue, current)
        logger.info("venue_confirmation_shown", venue_code=venue.code)
        return RegistrationResult(handled=True, reply=confirmation(venue))

    def _lookup(self, code: str, event: TelegramEvent) -> _Venue | BotReply:
        """Находит единственное заведение по коду приглашения."""
        normalized_code = normalize_code(code)
        try:
            matches = self.directory.find(normalized_code)
        except _VenueDirectoryError:
            logger.exception("venue_directory_read_failed", chat_id=event.chat_id)
            return directory_error_reply()
        if not matches:
            logger.info("venue_code_not_found", venue_code=normalized_code)
            return code_not_found_reply()
        if len(matches) > 1:
            logger.warning("venue_code_conflict", venue_code=normalized_code)
            return code_conflict_reply()
        return matches[0]

    def _bind(
        self, event: TelegramEvent, venue: _Venue, previous: VenueContext | None
    ) -> RegistrationResult:
        """Создаёт или обновляет привязку к заведению."""
        if self._revoked_for_venue(event, venue.code):
            decision = self.access_registry.decision(
                event.telegram_user_id,
                event.chat_id,
                venue.code,
                force_refresh=True,
            )
            if decision is not True:
                return RegistrationResult(
                    handled=True,
                    reply=access_disabled_reply(),
                )
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
            self.access_registry.invalidate()
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
                reply=sync_failure_reply(),
            )
        self.access_registry.invalidate()
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
        return RegistrationResult(
            handled=True,
            reply=binding_success_reply(venue, already_same=already_same),
            context=context,
            reset_session=previous is None or previous.venue_code != venue.code,
        )

    def denied_reply(self, event: TelegramEvent) -> BotReply:
        """Объясняет отзыв доступа либо предлагает первоначальное подключение."""
        if event.chat_type == "private" and event.telegram_user_id:
            with SessionLocal.begin() as db:
                revoked = VenueBindingRepository(db).get_revoked(
                    event.telegram_user_id,
                    event.chat_id,
                )
            if revoked is not None:
                return access_disabled_reply()
        return not_bound_reply()

    @staticmethod
    def _revoked_for_venue(event: TelegramEvent, venue_code: str) -> bool:
        """Проверяет сохранённый отзыв доступа для выбранного заведения."""
        with SessionLocal.begin() as db:
            row = VenueBindingRepository(db).get_revoked(
                event.telegram_user_id,
                event.chat_id,
            )
        return row is not None and row.venue_code == normalize_code(venue_code)

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
