"""Владеет общей проверкой доступа и загрузкой снимка отправки."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import structlog

from restaurant_bot.domain.models import PendingSubmission
from restaurant_bot.integrations.google_sheets import GoogleSheetsError
from restaurant_bot.presentation.telegram.venue_registration import access_disabled_reply

if TYPE_CHECKING:
    from restaurant_bot.services.submission import SubmissionService

logger = structlog.get_logger(__name__)


class SubmissionAccessService:
    """Владеет общей access-проверкой перед операциями заявки."""

    def __init__(
        self,
        owner: SubmissionService,
        *,
        session_local: Any,
        session_repository: Any,
    ) -> None:
        """Подключает регистрацию, Telegram и репозитории владельца."""
        self._owner = owner
        self._session_local = session_local
        self._session_repository = session_repository

    @property
    def registration(self) -> Any:
        """Возвращает реестр доступа общего сервиса отправки."""
        return self._owner.registration

    @property
    def telegram(self) -> Any:
        """Возвращает Telegram-клиент общего сервиса отправки."""
        return self._owner.telegram

    def _has_current_access(
        self,
        chat_id: str,
        *,
        user_id: str,
        bound_chat_id: str,
        venue_code: str,
        spreadsheet_id: str,
    ) -> bool:
        """Повторно проверяет доступ перед чтением или записью Google Sheets."""
        registration = getattr(self._owner, "registration", None)
        if registration is None:
            return True
        context = registration.context_for_identity(
            user_id,
            bound_chat_id,
            force_refresh=True,
        )
        allowed = bool(
            context
            and (not venue_code or context.venue_code == venue_code)
            and (not spreadsheet_id or context.spreadsheet_id == spreadsheet_id)
        )
        if not allowed:
            logger.info(
                "venue_access_blocked_before_sheet_operation",
                chat_id=chat_id,
                telegram_user_id=user_id,
                venue_code=venue_code,
            )
        return allowed

    def _send_access_disabled(self, chat_id: str) -> None:
        """Отправляет единый ответ об отзыве доступа."""
        self.telegram.send_reply(
            chat_id,
            access_disabled_reply(),
        )

    def _load_pending(self, chat_id: str) -> PendingSubmission | None:
        """Загружает снимок ожидающей отправки заявки."""
        with self._session_local() as db:
            _, state = self._session_repository(db).get_for_update(chat_id)
            return cast(PendingSubmission | None, state.pending_submission)

    @staticmethod
    def _require_venue_spreadsheet_id(spreadsheet_id: str) -> str:
        """Останавливает запись без таблицы активного заведения."""
        target_id = spreadsheet_id.strip()
        if not target_id:
            logger.error("venue_spreadsheet_id_missing")
            raise GoogleSheetsError("Venue spreadsheet ID is required")
        return target_id
