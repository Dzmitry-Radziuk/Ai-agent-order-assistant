from __future__ import annotations

from datetime import UTC, datetime
from time import perf_counter
from typing import Any

import structlog
from redis import Redis

from restaurant_bot.config import Settings
from restaurant_bot.db import SessionLocal
from restaurant_bot.db_models import SubmissionRecord
from restaurant_bot.domain.models import (
    BotReply,
    Button,
    ConversationState,
    PendingSubmission,
    SessionStage,
)
from restaurant_bot.integrations.cache import CatalogCache, chat_lock
from restaurant_bot.integrations.google_sheets import GoogleSheetsError, GoogleSheetsGateway
from restaurant_bot.integrations.telegram import TelegramClient
from restaurant_bot.repositories.sessions import SessionRepository
from restaurant_bot.repositories.submissions import SubmissionRepository
from restaurant_bot.services.replies import cart_reply
from restaurant_bot.services.submission_presenter import (
    _callback_with_revision,
    build_order_status_text,
    submission_failure_reply,
    submission_success_reply,
)
from restaurant_bot.services.text import escape

logger = structlog.get_logger(__name__)


class SubmissionService:
    """Управляет надёжной отправкой заявок."""

    def __init__(
        self,
        settings: Settings,
        redis: Redis[Any],
        telegram: TelegramClient,
        sheets: GoogleSheetsGateway,
    ):
        """Инициализирует компонент."""
        self.settings = settings
        self.redis = redis
        self.telegram = telegram
        self.sheets = sheets
        self.catalog_cache = CatalogCache(settings, redis, sheets)

    def submit(self, chat_id: str, *, report_failure: bool = True) -> None:
        """Выполняет надёжную отправку текущей заявки."""
        started_at = perf_counter()
        logger.info("submission_started", chat_id=chat_id, report_failure=report_failure)
        with chat_lock(self.redis, chat_id, timeout=300):
            pending = self._load_pending(chat_id)
            if pending is None:
                logger.info("submission_without_pending_order", chat_id=chat_id)
                completion = self._load_unnotified_completion(chat_id)
                if completion is not None:
                    order_no, state = completion
                    self._send_completion(chat_id, state, order_no)
                return
            finalized = False
            try:
                record = self._record(chat_id, pending)
                spreadsheet_id = self._require_venue_spreadsheet_id(pending.spreadsheet_id)
                if not record.history_written:
                    stage_started = perf_counter()
                    with self.redis.lock(
                        "lock:google-history-write",
                        timeout=120,
                        blocking_timeout=120,
                    ):
                        self.sheets.append_history(pending.rows, spreadsheet_id)
                    self._checkpoint(pending.order_no, "history_written")
                    logger.info(
                        "submission_history_written",
                        chat_id=chat_id,
                        order_no=pending.order_no,
                        row_count=len(pending.rows),
                        duration_ms=round((perf_counter() - stage_started) * 1000),
                    )
                record = self._get_record(pending.order_no)
                if not record.catalog_updated:
                    stage_started = perf_counter()
                    self.sheets.increment_catalog_quantities(pending.rows, spreadsheet_id)
                    self._checkpoint(pending.order_no, "catalog_updated")
                    self.catalog_cache.invalidate(spreadsheet_id)
                    logger.info(
                        "submission_catalog_updated",
                        chat_id=chat_id,
                        order_no=pending.order_no,
                        duration_ms=round((perf_counter() - stage_started) * 1000),
                    )
                record = self._get_record(pending.order_no)
                if not record.recalc_done:
                    stage_started = perf_counter()
                    self.sheets.trigger_recalculation(pending.order_no, spreadsheet_id)
                    self._checkpoint(pending.order_no, "recalc_done")
                    logger.info(
                        "submission_recalculation_completed",
                        chat_id=chat_id,
                        order_no=pending.order_no,
                        duration_ms=round((perf_counter() - stage_started) * 1000),
                    )
                final_state = self._finalize(chat_id, pending.order_no)
                finalized = True
                self._send_completion(chat_id, final_state, pending.order_no)
                logger.info(
                    "submission_completed",
                    chat_id=chat_id,
                    order_no=pending.order_no,
                    total_ms=round((perf_counter() - started_at) * 1000),
                )
            except Exception as exc:
                logger.exception("submission_failed", chat_id=chat_id, order_no=pending.order_no)
                if finalized:
                    self._remember_transient_error(pending.order_no, str(exc))
                elif report_failure:
                    failed_state = self._fail(chat_id, pending.order_no, str(exc))
                    self.telegram.send_reply(
                        chat_id,
                        submission_failure_reply(failed_state, pending.order_no),
                    )
                else:
                    self._remember_transient_error(pending.order_no, str(exc))
                raise

    def _send_completion(self, chat_id: str, state: ConversationState, order_no: str) -> None:
        """Отправляет подтверждение завершённой заявки."""
        self.telegram.send_reply(chat_id, submission_success_reply(state, order_no))
        self._checkpoint(order_no, "completion_notified")

    @staticmethod
    def _load_unnotified_completion(
        chat_id: str,
    ) -> tuple[str, ConversationState] | None:
        """Загружает завершённую заявку без уведомления."""
        from sqlalchemy import select

        from restaurant_bot.db_models import SubmissionRecord

        with SessionLocal() as db:
            record = db.scalar(
                select(SubmissionRecord)
                .where(
                    SubmissionRecord.telegram_id == chat_id,
                    SubmissionRecord.finalized.is_(True),
                    SubmissionRecord.completion_notified.is_(False),
                )
                .order_by(SubmissionRecord.created_at.desc())
                .limit(1)
            )
            if record is None:
                return None
            _, state = SessionRepository(db).get_for_update(chat_id)
            return record.order_no, state

    def send_status(self, chat_id: str) -> None:
        """Отправляет пользователю статусы его заявок."""
        with chat_lock(self.redis, chat_id):
            with SessionLocal() as db:
                _, state = SessionRepository(db).get_for_update(chat_id)
            order_numbers = list(
                dict.fromkeys([state.last_order_no, *state.submitted_order_numbers])
            )[:10]
            if not order_numbers:
                self.telegram.send_reply(
                    chat_id, BotReply(text="У вас пока нет отправленных заявок.")
                )
                return
            rows = self.sheets.read_order_statuses(
                order_numbers,
                self._require_venue_spreadsheet_id(state.spreadsheet_id),
            )
            self.telegram.send_reply(chat_id, BotReply(text=build_order_status_text(rows, state)))

    def submit_product_add(self, chat_id: str) -> None:
        """Отправляет запрос на добавление нового товара."""
        with chat_lock(self.redis, chat_id, timeout=300):
            with SessionLocal.begin() as db:
                sessions = SessionRepository(db)
                row, state = sessions.get_for_update(chat_id)
                request_id = state.pending_product_add_request_id
                request = next(
                    (
                        item
                        for item in state.product_add_requests
                        if item.get("request_id") == request_id
                    ),
                    None,
                )
                if not request or request.get("status") not in {
                    "pending_write",
                    "retry_pending",
                    "write_failed",
                }:
                    return
                request["status"] = "pending_write"
                request["updated_at"] = datetime.now(UTC).isoformat()
                sessions.save(chat_id, state, row)

            try:
                self.sheets.append_product_request(
                    {"description": request["description"]},
                    self._require_venue_spreadsheet_id(state.spreadsheet_id),
                )
                status = "submitted"
                error = ""
            except Exception as exc:
                error = str(exc)[:1000]
                status = (
                    "write_uncertain" if self._uncertain_product_add_error(exc) else "write_failed"
                )

            with SessionLocal.begin() as db:
                sessions = SessionRepository(db)
                row, state = sessions.get_for_update(chat_id)
                request = self._apply_product_add_write_outcome(state, request_id, status, error)
                if request is None:
                    return
                sessions.save(chat_id, state, row)

            if status == "submitted":
                self._send_product_add_success(chat_id, state, request)
                return
            elif status == "write_failed":
                reply = BotReply(
                    text="<b>Запрос не отправлен</b>\n\nОн сохранён в черновике. Можно безопасно повторить отправку с тем же ID запроса.",
                    rows=[
                        [
                            Button(
                                text="Повторить отправку",
                                callback_data=_callback_with_revision(
                                    state, f"v2:addreqretry:{request_id}"
                                ),
                            )
                        ]
                    ],
                )
            else:
                reply = BotReply(
                    text="<b>Не удалось подтвердить отправку</b>\n\nЗапрос сохранён в черновике, но бот не уверен, что он попал в таблицу. Сообщите менеджеру по снабжению. Повторно отправлять запрос не нужно."
                )
            self.telegram.send_reply(chat_id, reply)

    def _send_product_add_success(
        self, chat_id: str, state: ConversationState, request: dict[str, Any]
    ) -> None:
        """Показывает успешную запись запроса на новый товар."""
        confirmation = BotReply(
            text=f"<b>Запрос менеджеру отправлен</b>\n\n{escape(request['description'])}",
            edit_message_id=state.ui_message_id or None,
        )
        self.telegram.send_reply(chat_id, confirmation)

        state.ui_revision += 1
        draft = cart_reply(state)
        suffix = f":r{state.ui_revision}"
        for row in draft.rows:
            for button in row:
                if button.callback_data:
                    button.callback_data += suffix
        draft_message_id = self.telegram.send_reply(chat_id, draft)
        self._persist_worker_ui(chat_id, state.ui_revision, draft_message_id, draft.text)

    @staticmethod
    def _persist_worker_ui(
        chat_id: str, revision: int, message_id: int | None, message_text: str
    ) -> None:
        """Сохраняет состояние интерфейса фоновой задачи."""
        with SessionLocal.begin() as db:
            sessions = SessionRepository(db)
            row, state = sessions.get_for_update(chat_id)
            state.ui_revision = revision
            state.ui_message_id = message_id
            state.ui_message_text = message_text
            sessions.save(chat_id, state, row)

    @staticmethod
    def _uncertain_product_add_error(exc: Exception) -> bool:
        """Формирует ошибку неопределённой записи товара."""
        text = str(exc).lower()
        return not text or any(
            token in text
            for token in (
                "timeout",
                "timed out",
                "etimedout",
                "network",
                "connection",
                "socket",
                "502",
                "503",
                "504",
                "500",
            )
        )

    @staticmethod
    def _apply_product_add_write_outcome(
        state: ConversationState,
        request_id: str,
        status: str,
        error: str = "",
    ) -> dict[str, Any] | None:
        """Сохраняет результат записи запроса на новый товар."""
        request = next(
            (item for item in state.product_add_requests if item.get("request_id") == request_id),
            None,
        )
        if request is None:
            return None
        now = datetime.now(UTC).isoformat()
        request["status"] = status
        request["updated_at"] = now
        request["sheets_error"] = error
        if status == "submitted":
            request["submitted_at"] = now
        state.pending_product_add_request_id = ""
        state.product_add_write_in_progress = False
        state.stage = SessionStage.REVIEW
        state.status = "review"
        return request

    def _load_pending(self, chat_id: str) -> PendingSubmission | None:
        """Загружает снимок ожидающей отправки заявки."""
        with SessionLocal() as db:
            _, state = SessionRepository(db).get_for_update(chat_id)
            return state.pending_submission

    @staticmethod
    def _require_venue_spreadsheet_id(spreadsheet_id: str) -> str:
        """Останавливает запись без таблицы активного заведения."""
        target_id = spreadsheet_id.strip()
        if not target_id:
            logger.error("venue_spreadsheet_id_missing")
            raise GoogleSheetsError("Venue spreadsheet ID is required")
        return target_id

    def _record(self, chat_id: str, pending: PendingSubmission) -> SubmissionRecord:
        """Создаёт или загружает запись отправки."""
        with SessionLocal.begin() as db:
            return SubmissionRepository(db).get_or_create(chat_id, pending)

    def _get_record(self, order_no: str) -> SubmissionRecord:
        """Возвращает запись отправки по номеру заявки."""
        from sqlalchemy import select

        with SessionLocal() as db:
            record = db.scalar(
                select(SubmissionRecord).where(SubmissionRecord.order_no == order_no)
            )
            if record is None:
                raise RuntimeError(f"Submission record {order_no} not found")
            db.expunge(record)
            return record

    def _checkpoint(self, order_no: str, field: str) -> None:
        """Отмечает завершение этапа отправки заявки."""
        from sqlalchemy import select

        with SessionLocal.begin() as db:
            record = db.scalar(
                select(SubmissionRecord)
                .where(SubmissionRecord.order_no == order_no)
                .with_for_update()
            )
            if record is None:
                raise RuntimeError(f"Submission record {order_no} not found")
            setattr(record, field, True)
            record.last_error = None

    def _finalize(self, chat_id: str, order_no: str) -> ConversationState:
        """Финализирует успешно отправленную заявку."""
        from sqlalchemy import select

        with SessionLocal.begin() as db:
            sessions = SessionRepository(db)
            row, state = sessions.get_for_update(chat_id)
            self._apply_successful_submission_state(state, order_no)
            sessions.save(chat_id, state, row)
            record = db.scalar(
                select(SubmissionRecord)
                .where(SubmissionRecord.order_no == order_no)
                .with_for_update()
            )
            if record:
                record.finalized = True
                record.last_error = None
            return state

    @staticmethod
    def _apply_successful_submission_state(state: ConversationState, order_no: str) -> None:
        """Применяет успешное завершение отправки заявки."""
        if order_no not in state.submitted_order_numbers:
            state.submitted_order_numbers.append(order_no)
        state.last_order_no = order_no
        state.pending_submission = None
        state.cart = []
        state.current_issue_item_id = ""
        state.stage = SessionStage.SUBMITTED
        state.status = "submitted"

    def _fail(self, chat_id: str, order_no: str, error: str) -> ConversationState:
        """Сохраняет неуспешную отправку заявки."""
        from sqlalchemy import select

        with SessionLocal.begin() as db:
            sessions = SessionRepository(db)
            row, state = sessions.get_for_update(chat_id)
            state.stage = SessionStage.SUBMISSION_FAILED
            if state.pending_submission:
                state.pending_submission.last_error = error[:1000]
            sessions.save(chat_id, state, row)
            record = db.scalar(
                select(SubmissionRecord)
                .where(SubmissionRecord.order_no == order_no)
                .with_for_update()
            )
            if record:
                record.last_error = error[:4000]
            return state

    def _remember_transient_error(self, order_no: str, error: str) -> None:
        """Сохраняет временную ошибку для повторной попытки."""
        from sqlalchemy import select

        with SessionLocal.begin() as db:
            record = db.scalar(
                select(SubmissionRecord)
                .where(SubmissionRecord.order_no == order_no)
                .with_for_update()
            )
            if record:
                record.last_error = error[:4000]
