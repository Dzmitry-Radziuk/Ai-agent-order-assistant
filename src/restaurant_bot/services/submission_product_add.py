"""Координирует запись запросов снабженцу в Sheets."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from restaurant_bot.conversation.state.transitions import normalize_cart_page
from restaurant_bot.domain.models import BotReply, Button, ConversationState, SessionStage
from restaurant_bot.integrations.cache import ChatLease
from restaurant_bot.presentation.telegram.formatting import escape, heading
from restaurant_bot.presentation.telegram.pagination import CART_PAGE_SIZE
from restaurant_bot.presentation.telegram.replies import cart_reply
from restaurant_bot.presentation.telegram.submission import _callback_with_revision

if TYPE_CHECKING:
    from restaurant_bot.services.submission import SubmissionService


class SubmissionProductAddService:
    """Владеет отдельным протоколом записи нового товара."""

    def __init__(
        self,
        owner: SubmissionService,
        *,
        session_local: Any,
        session_repository: Any,
        chat_lock_factory: Any,
    ) -> None:
        """Подключает общий транспорт, доступ и checkpoint-зависимости."""
        self._owner = owner
        self._session_local = session_local
        self._session_repository = session_repository
        self._chat_lock = chat_lock_factory

    @property
    def redis(self) -> Any:
        """Возвращает Redis общего сервиса отправки."""
        return self._owner.redis

    @property
    def telegram(self) -> Any:
        """Возвращает Telegram-клиент общего сервиса отправки."""
        return self._owner.telegram

    @property
    def sheets(self) -> Any:
        """Возвращает Sheets-шлюз общего сервиса отправки."""
        return self._owner.sheets

    def _has_current_access(self, *args: Any, **kwargs: Any) -> bool:
        """Повторно проверяет доступ через единственного владельца правила."""
        return self._owner._has_current_access(*args, **kwargs)

    def _send_access_disabled(self, *args: Any, **kwargs: Any) -> None:
        """Передаёт отказ доступа каноническому владельцу отправки."""
        self._owner._send_access_disabled(*args, **kwargs)

    def _require_venue_spreadsheet_id(self, spreadsheet_id: str) -> str:
        """Проверяет наличие таблицы через единственного владельца правила."""
        return self._owner._require_venue_spreadsheet_id(spreadsheet_id)

    def _persist_worker_ui(self, *args: Any, **kwargs: Any) -> None:
        """Сохраняет интерфейс через общий checkpoint-адаптер."""
        self._owner._persist_worker_ui(*args, **kwargs)

    def submit_product_add(self, chat_id: str) -> None:
        """Отправляет запрос на добавление нового товара."""
        with self._chat_lock(self.redis, chat_id, timeout=300) as lease:
            if lease is not None:
                lease.ensure_owned()
            with self._session_local.begin() as db:
                _, access_state = self._session_repository(db).get_for_update(chat_id)
            if lease is not None:
                lease.ensure_owned()
            if not self._has_current_access(
                chat_id,
                user_id=access_state.telegram_user_id or chat_id,
                bound_chat_id=access_state.telegram_chat_id or chat_id,
                venue_code=access_state.venue_code,
                spreadsheet_id=access_state.spreadsheet_id,
            ):
                if lease is not None:
                    lease.ensure_owned()
                self._send_access_disabled(chat_id)
                return
            with self._session_local.begin() as db:
                sessions = self._session_repository(db)
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
                request["status"] = "write_uncertain"
                request["updated_at"] = datetime.now(UTC).isoformat()
                if lease is not None:
                    lease.ensure_owned()
                sessions.save(chat_id, state, row)

            try:
                if lease is not None:
                    lease.ensure_owned()
                self.sheets.append_product_request(
                    {"description": request["description"]},
                    self._require_venue_spreadsheet_id(state.spreadsheet_id),
                )
                if lease is not None:
                    lease.ensure_owned()
                status = "submitted"
                error = ""
            except Exception as exc:
                if lease is not None:
                    lease.ensure_owned()
                error = str(exc)[:1000]
                status = (
                    "write_uncertain" if self._uncertain_product_add_error(exc) else "write_failed"
                )

            with self._session_local.begin() as db:
                sessions = self._session_repository(db)
                row, state = sessions.get_for_update(chat_id)
                request = self._apply_product_add_write_outcome(state, request_id, status, error)
                if request is None:
                    return
                if lease is not None:
                    lease.ensure_owned()
                sessions.save(chat_id, state, row)

            if status == "submitted":
                if lease is not None:
                    lease.ensure_owned()
                if lease is None:
                    self._owner._send_product_add_success(chat_id, state, request)
                else:
                    self._owner._send_product_add_success(chat_id, state, request, lease=lease)
                return
            elif status == "write_failed":
                reply = BotReply(
                    text=f"{heading('Запрос не отправлен')}\n\nОн сохранён в черновике. Можно безопасно повторить отправку с тем же ID запроса.",
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
                    text=f"{heading('Не удалось подтвердить отправку')}\n\nЗапрос сохранён в черновике, но бот не уверен, что он попал в таблицу. Сообщите менеджеру по снабжению. Повторно отправлять запрос не нужно."
                )
            if lease is not None:
                lease.ensure_owned()
            self.telegram.send_reply(chat_id, reply)

    def _send_product_add_success_impl(
        self,
        chat_id: str,
        state: ConversationState,
        request: dict[str, Any],
        *,
        lease: ChatLease | None = None,
    ) -> None:
        """Показывает успешную запись запроса на новый товар."""
        confirmation = BotReply(
            text=f"<i>Запрос менеджеру отправлен</i>\n\n{escape(request['description'])}",
            edit_message_id=state.ui_message_id or None,
        )
        if lease is not None:
            lease.ensure_owned()
        self.telegram.send_reply(chat_id, confirmation)
        if lease is not None:
            lease.ensure_owned()

        state.ui_revision += 1
        normalize_cart_page(state, page_size=CART_PAGE_SIZE)
        draft = cart_reply(state)
        suffix = f":r{state.ui_revision}"
        for row in draft.rows:
            for button in row:
                if button.callback_data:
                    button.callback_data += suffix
        if lease is not None:
            lease.ensure_owned()
        draft_message_id = self.telegram.send_reply(chat_id, draft)
        if lease is not None:
            lease.ensure_owned()
        if lease is None:
            self._persist_worker_ui(chat_id, state.ui_revision, draft_message_id, draft.text)
        else:
            self._persist_worker_ui(
                chat_id,
                state.ui_revision,
                draft_message_id,
                draft.text,
                lease=lease,
            )

    def persist_worker_ui(
        self,
        chat_id: str,
        revision: int,
        message_id: int | None,
        message_text: str,
        *,
        lease: ChatLease | None = None,
    ) -> None:
        """Сохраняет состояние интерфейса фоновой задачи."""
        with self._session_local.begin() as db:
            sessions = self._session_repository(db)
            row, state = sessions.get_for_update(chat_id)
            state.ui_revision = revision
            state.ui_message_id = message_id
            state.ui_message_text = message_text
            if lease is not None:
                lease.ensure_owned()
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
