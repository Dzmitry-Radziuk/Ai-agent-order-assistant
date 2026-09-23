"""Координирует read-only просмотр статусов заявок."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from restaurant_bot.domain.models import BotReply, Button, ConversationState
from restaurant_bot.integrations.cache import ChatLease
from restaurant_bot.integrations.telegram import TelegramAPIError
from restaurant_bot.presentation.telegram.formatting import escape, heading
from restaurant_bot.presentation.telegram.submission import (
    build_order_status_detail_reply,
    build_order_status_list_reply,
    build_order_status_text,
    group_order_status_rows,
    order_status_detail_page_count,
    submission_dispatch_still_uncertain_reply,
)

if TYPE_CHECKING:
    from restaurant_bot.services.submission import SubmissionService


class SubmissionStatusService:
    """Владеет read-only протоколом истории и статусов заявок."""

    ORDER_STATUS_PAGE_SIZE = 5

    def __init__(
        self,
        owner: SubmissionService,
        *,
        session_local: Any,
        session_repository: Any,
        chat_lock_factory: Any,
    ) -> None:
        """Подключает общий lease, доступ и транспорт владельца отправки."""
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

    @staticmethod
    def _history_order_number(row: dict[str, Any]) -> str:
        """Возвращает номер заявки из строки истории по поддержанным alias."""
        for key in ("Номер заявки", "№ Заявки", "ID заявки", "order_no"):
            value = str(row.get(key) or "").strip()
            if value:
                return value
        return ""

    def send_status(
        self,
        chat_id: str,
        *,
        page: int = 0,
        selected_index: int | None = None,
        order_number: str = "",
        detail_page: int = 0,
    ) -> None:
        """Показывает список заявок или подробности выбранной заявки."""
        with self._chat_lock(self.redis, chat_id) as lease:
            if lease is not None:
                lease.ensure_owned()
            with self._session_local.begin() as db:
                _, state = self._session_repository(db).get_for_update(chat_id)
            if lease is not None:
                lease.ensure_owned()
            if not self._has_current_access(
                chat_id,
                user_id=state.telegram_user_id or chat_id,
                bound_chat_id=state.telegram_chat_id or chat_id,
                venue_code=state.venue_code,
                spreadsheet_id=state.spreadsheet_id,
            ):
                if lease is not None:
                    lease.ensure_owned()
                self._send_access_disabled(chat_id)
                return
            if selected_index is not None or order_number:
                self._send_order_status_detail(
                    chat_id,
                    state,
                    selected_index=selected_index,
                    order_number=order_number,
                    detail_page=detail_page,
                    lease=lease,
                )
                return
            self._send_order_status_page(chat_id, state, page=max(0, page), lease=lease)

    def _send_order_status_page(
        self,
        chat_id: str,
        state: ConversationState,
        *,
        page: int,
        lease: ChatLease | None = None,
    ) -> None:
        """Показывает одну страницу реальных заявок текущего заведения."""
        spreadsheet_id = self._require_venue_spreadsheet_id(state.spreadsheet_id)
        if lease is not None:
            lease.ensure_owned()
        venue_name = state.venue_name or state.restaurant
        rows = self.sheets.read_recent_order_statuses(
            spreadsheet_id,
            venue_name,
            offset=page * self.ORDER_STATUS_PAGE_SIZE,
            limit=self.ORDER_STATUS_PAGE_SIZE + 1,
        )
        groups = group_order_status_rows(rows)
        if page > 0 and not groups:
            self._send_order_status_page(chat_id, state, page=0, lease=lease)
            return
        shown_groups = groups[: self.ORDER_STATUS_PAGE_SIZE]
        shown_rows = [row for _, order_rows in shown_groups for row in order_rows]
        order_numbers = [number for number, _ in shown_groups]
        self._persist_order_status_view(
            chat_id,
            page=page,
            order_numbers=order_numbers,
            lease=lease,
        )
        self._send_status_reply(
            chat_id,
            state,
            build_order_status_list_reply(
                shown_rows,
                page=page,
                has_more=len(groups) > self.ORDER_STATUS_PAGE_SIZE,
            ),
            lease=lease,
        )

    def _send_order_status_detail(
        self,
        chat_id: str,
        state: ConversationState,
        *,
        selected_index: int | None,
        order_number: str,
        detail_page: int = 0,
        lease: ChatLease | None = None,
    ) -> None:
        """Показывает поставщиков и товары выбранной реальной заявки."""
        stored_numbers = state.order_status_order_numbers
        selected = selected_index or 1
        target = order_number.strip()
        if not target and 1 <= selected <= len(stored_numbers):
            target = stored_numbers[selected - 1]
        if not target:
            self._send_order_status_page(
                chat_id,
                state,
                page=max(0, state.order_status_page),
                lease=lease,
            )
            return

        rows = self.sheets.read_order_statuses(
            [target],
            self._require_venue_spreadsheet_id(state.spreadsheet_id),
            state.venue_name or state.restaurant,
        )
        if not rows:
            if (
                state.status == "dispatch_uncertain"
                and state.pending_submission is not None
                and state.pending_submission.order_no == target
            ):
                self._send_status_reply(
                    chat_id,
                    state,
                    submission_dispatch_still_uncertain_reply(state, target),
                    lease=lease,
                )
                return
            self._send_status_reply(
                chat_id,
                state,
                BotReply(
                    text=(
                        f"🔸 {heading('Заявка не найдена')}\n\n"
                        f"Заявки {escape(target)} нет в «Истории» этого заведения."
                    ),
                    rows=[
                        [
                            Button(
                                text="← К списку заявок",
                                callback_data=f"v2:orderspage:{state.order_status_page}",
                            )
                        ]
                    ],
                ),
                lease=lease,
            )
            return

        preview_state = state.model_copy(deep=True)
        preview_state.last_order_no = target
        preview_state.submitted_order_numbers = []
        status_text = build_order_status_text(
            rows,
            preview_state,
            detail_page=max(0, detail_page),
            display_index=selected,
        )
        self._send_status_reply(
            chat_id,
            state,
            build_order_status_detail_reply(
                status_text,
                page=max(0, state.order_status_page),
                selected_index=selected,
                detail_page=max(0, detail_page),
                detail_page_count=order_status_detail_page_count(
                    rows,
                    preview_state,
                    display_index=selected,
                ),
            ),
            lease=lease,
        )

    def _send_status_reply(
        self,
        chat_id: str,
        state: ConversationState,
        reply: BotReply,
        *,
        lease: ChatLease | None = None,
    ) -> None:
        """Обновляет одну карточку статусов и не накапливает сообщения."""
        previous_message_id = state.ui_message_id
        if lease is not None:
            lease.ensure_owned()
        reply.edit_message_id = previous_message_id or None
        try:
            message_id = self.telegram.send_reply(chat_id, reply)
        except TelegramAPIError as exc:
            description = str(exc).lower()
            can_fallback = previous_message_id and any(
                marker in description
                for marker in (
                    "message to edit not found",
                    "message can't be edited",
                    "message is too old",
                )
            )
            if not can_fallback:
                raise
            reply.edit_message_id = None
            if lease is not None:
                lease.ensure_owned()
            message_id = self.telegram.send_reply(chat_id, reply)
        if lease is not None:
            lease.ensure_owned()
        if isinstance(message_id, int):
            self._persist_worker_ui(
                chat_id,
                state.ui_revision,
                message_id,
                reply.text,
                lease=lease,
            )

    def _persist_order_status_view(
        self,
        chat_id: str,
        *,
        page: int,
        order_numbers: list[str],
        lease: ChatLease | None = None,
    ) -> None:
        """Запоминает показанный список для голосового и кнопочного выбора."""
        with self._session_local.begin() as db:
            sessions = self._session_repository(db)
            row, state = sessions.get_for_update(chat_id)
            state.order_status_view_active = True
            state.order_status_page = page
            state.order_status_detail_page = 0
            state.order_status_detail_active = False
            state.order_status_selected_index = None
            state.order_status_selected_order_number = ""
            state.order_status_order_numbers = order_numbers
            if lease is not None:
                lease.ensure_owned()
            sessions.save(chat_id, state, row)
