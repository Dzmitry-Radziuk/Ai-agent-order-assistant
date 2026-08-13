from __future__ import annotations

from typing import Any

import structlog
from redis import Redis

from restaurant_bot.application.order_review.contracts import ReviewSnapshot
from restaurant_bot.application.order_review.snapshot import build_review_snapshot
from restaurant_bot.application.order_review.token import new_review_token
from restaurant_bot.config import Settings
from restaurant_bot.domain.models import SessionStage
from restaurant_bot.integrations.cache import ChatLeaseLostError, chat_lock
from restaurant_bot.integrations.google_sheets import GoogleSheetsGateway
from restaurant_bot.integrations.telegram import TelegramClient
from restaurant_bot.persistence.database import SessionLocal
from restaurant_bot.presentation.telegram.order_review import (
    preview_reply,
    submission_disabled_reply,
    submission_failure_reply,
    submission_success_reply,
)
from restaurant_bot.presentation.telegram.venue_registration import access_disabled_reply
from restaurant_bot.repositories.sessions import SessionRepository
from restaurant_bot.services.venue_registration import VenueContext, VenueRegistrationService

logger = structlog.get_logger(__name__)


class OrderReviewService:
    """Читает текущую заявку и безопасно обрабатывает её подтверждение."""

    def __init__(
        self,
        settings: Settings,
        redis: Redis[Any],
        telegram: TelegramClient,
        sheets: GoogleSheetsGateway,
    ) -> None:
        """Инициализирует сервис просмотра заявки."""
        self.settings = settings
        self.redis = redis
        self.telegram = telegram
        self.sheets = sheets
        self.registration = VenueRegistrationService(settings, redis, sheets)

    def snapshot(self, context: VenueContext) -> ReviewSnapshot:
        """Читает позиции с ненулевым количеством из листа каталога-заявки."""
        return build_review_snapshot(
            self.sheets.load_catalog(context.spreadsheet_id),
            venue_code=context.venue_code,
            venue_name=context.venue_name,
            spreadsheet_id=context.spreadsheet_id,
        )

    def submit(self, chat_id: str, token: str) -> None:
        """Повторно проверяет заявку и отправляет её через существующий Web App."""
        with chat_lock(self.redis, chat_id, timeout=300) as lease, SessionLocal.begin() as db:
            if lease is not None:
                lease.ensure_owned()
            sessions = SessionRepository(db)
            row, state = sessions.get_for_update(chat_id)
            if state.review_token != token:
                return
            context = self.registration.context_for_identity(
                state.telegram_user_id or chat_id,
                state.telegram_chat_id or chat_id,
                force_refresh=True,
            )
            if context is None or context.venue_code != state.review_venue_code:
                if lease is not None:
                    lease.ensure_owned()
                state.review_submission_in_progress = False
                state.review_mode = "cart"
                sessions.save(chat_id, state, row)
                if lease is not None:
                    lease.ensure_owned()
                self.telegram.send_reply(
                    chat_id,
                    access_disabled_reply(),
                )
                return
            current = self.snapshot(context)
            if current.fingerprint != state.review_snapshot_hash:
                token = new_review_token()
                state.review_token = token
                state.review_snapshot_hash = current.fingerprint
                state.review_submission_in_progress = False
                state.stage = SessionStage.REVIEW
                state.status = "review"
                if lease is not None:
                    lease.ensure_owned()
                sessions.save(chat_id, state, row)
                if lease is not None:
                    lease.ensure_owned()
                self.telegram.send_reply(
                    chat_id,
                    preview_reply(
                        current,
                        token,
                        changed=True,
                        edit_message_id=state.ui_message_id,
                    ),
                )
                return
            if not self.settings.google_order_submission_enabled:
                state.review_submission_in_progress = False
                state.stage = SessionStage.REVIEW
                state.status = "review"
                if lease is not None:
                    lease.ensure_owned()
                sessions.save(chat_id, state, row)
                if lease is not None:
                    lease.ensure_owned()
                self.telegram.send_reply(
                    chat_id,
                    submission_disabled_reply(
                        state.review_venue_code,
                        edit_message_id=state.ui_message_id,
                    ),
                )
                return
            try:
                if lease is not None:
                    lease.ensure_owned()
                prepared = self.sheets.prepare_order_submission(
                    current.spreadsheet_id,
                    f"review:{token}",
                )
                result = self.sheets.send_order_submission(prepared)
                if lease is not None:
                    lease.ensure_owned()
            except ChatLeaseLostError:
                raise
            except Exception:
                logger.exception("review_order_submission_failed", chat_id=chat_id)
                state.review_submission_in_progress = False
                state.stage = SessionStage.REVIEW
                state.status = "review"
                if lease is not None:
                    lease.ensure_owned()
                sessions.save(chat_id, state, row)
                if lease is not None:
                    lease.ensure_owned()
                self.telegram.send_reply(
                    chat_id,
                    submission_failure_reply(token, edit_message_id=state.ui_message_id),
                )
                return
            state.review_token = ""
            state.review_snapshot_hash = ""
            state.review_venue_code = ""
            state.review_submission_in_progress = False
            state.review_mode = "cart"
            state.stage = SessionStage.SUBMITTED
            state.status = "submitted"
            if lease is not None:
                lease.ensure_owned()
            sessions.save(chat_id, state, row)
            if lease is not None:
                lease.ensure_owned()
            self.telegram.send_reply(
                chat_id,
                submission_success_reply(
                    result.order_number,
                    result.base_rows,
                    edit_message_id=state.ui_message_id,
                ),
            )
