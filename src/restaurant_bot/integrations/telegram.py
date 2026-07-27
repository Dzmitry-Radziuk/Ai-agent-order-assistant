from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import perf_counter
from typing import Any

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from restaurant_bot.config import Settings
from restaurant_bot.domain.models import BotReply

logger = structlog.get_logger(__name__)


class TelegramAPIError(RuntimeError):
    """Сообщает о невосстановимой ошибке Telegram API."""

    pass


class TelegramRetryableError(TelegramAPIError):
    """Сообщает о временной ошибке Telegram API."""

    pass


TELEGRAM_CONNECT_ERRORS = (httpx.ConnectTimeout, httpx.ConnectError)
TELEGRAM_TRANSIENT_ERRORS = (*TELEGRAM_CONNECT_ERRORS, TelegramRetryableError)


@dataclass(slots=True)
class DownloadedFile:
    """Хранит загруженный из Telegram файл."""

    path: Path
    mime_type: str


class TelegramClient:
    """Безопасно вызывает методы Telegram Bot API."""

    MAX_MESSAGE_LENGTH = 4096

    def __init__(self, settings: Settings):
        """Инициализирует компонент."""
        self.base_url = (
            f"https://api.telegram.org/bot{settings.telegram_bot_token.get_secret_value()}"
        )
        self.file_url = (
            f"https://api.telegram.org/file/bot{settings.telegram_bot_token.get_secret_value()}"
        )
        self.client = httpx.Client(
            timeout=httpx.Timeout(
                settings.telegram_request_timeout_seconds,
                connect=settings.telegram_connect_timeout_seconds,
            )
        )

    @retry(
        # Retrying a read timeout can duplicate a Telegram message: the server
        # may have accepted it even though the response never reached us.
        # Connection failures are safe to repeat because no request was sent.
        retry=retry_if_exception_type(TELEGRAM_TRANSIENT_ERRORS),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=0.25, max=2.0),
        reraise=True,
    )
    def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Вызывает один метод Telegram Bot API."""
        started_at = perf_counter()
        try:
            response = self.client.post(f"{self.base_url}/{method}", json=payload)
        except httpx.HTTPError as exc:
            # Do not log the URL or payload: both can contain the bot token or
            # Telegram identifiers.  The exception class is enough to tell a
            # connection failure from an HTTP response failure.
            logger.warning(
                "telegram_api_transport_failed",
                method=method,
                error_type=type(exc).__name__,
            )
            raise
        finally:
            # Payloads and URLs are intentionally excluded: both may contain
            # Telegram identifiers or the bot token.  The duration identifies
            # slow callback cards without exposing either.
            logger.info(
                "telegram_api_call_finished",
                method=method,
                duration_ms=round((perf_counter() - started_at) * 1000),
            )
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code >= 500:
            raise TelegramRetryableError(self._error_text(method, response.status_code, data))
        if response.is_error:
            raise TelegramAPIError(self._error_text(method, response.status_code, data))
        if not data.get("ok"):
            raise TelegramAPIError(self._error_text(method, response.status_code, data))
        return data.get("result") or {}

    @staticmethod
    def _error_text(method: str, status_code: int, data: dict[str, Any]) -> str:
        """Извлекает безопасный текст ошибки Telegram API."""
        description = str(data.get("description") or "Telegram API request failed")
        return f"Telegram {method} failed ({status_code}): {description}"

    def answer_callback(self, callback_query_id: str) -> None:
        """Подтверждает обработку нажатия кнопки."""
        if not callback_query_id:
            return
        try:
            self._call("answerCallbackQuery", {"callback_query_id": callback_query_id})
        except TelegramAPIError as exc:
            # Telegram accepts callback answers for a short window only.  An
            # expired callback is already harmless and must not retry the
            # complete business action (clear cart, select product, etc.).
            description = str(exc).lower()
            if "query is too old" in description or "query id is invalid" in description:
                logger.info("telegram_callback_already_expired")
                return
            raise

    def send_reply(self, chat_id: str, reply: BotReply) -> int | None:
        # The n8n workflow deliberately uses visual symbols as card and action
        # anchors. Telegram must receive the exact rendered copy, not a
        # sanitised variant.
        """Отправляет ответ пользователю в Telegram."""
        chunks = self._split_text(reply.text)
        reply_markup = self._reply_markup(reply)
        message_id = reply.edit_message_id
        for index, text in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": reply.parse_mode,
                "disable_web_page_preview": True,
            }
            # n8n keeps the inline keyboard on the final visible card only.
            if index == len(chunks) - 1:
                if reply_markup:
                    payload["reply_markup"] = reply_markup
                elif reply.edit_message_id:
                    # n8n replaces a callback card with the progress card and
                    # explicitly removes the obsolete inline keyboard.
                    payload["reply_markup"] = {"inline_keyboard": []}
            if index == 0 and reply.edit_message_id:
                payload["message_id"] = reply.edit_message_id
                try:
                    result = self._call("editMessageText", payload)
                except TELEGRAM_CONNECT_ERRORS as exc:
                    # A connect failure happens before Telegram receives the
                    # request, so falling back to a new message cannot duplicate
                    # an already edited card. It also avoids losing a completed
                    # result merely because one old progress card was unreachable.
                    logger.warning(
                        "telegram_edit_fallback_to_send",
                        error_type=type(exc).__name__,
                    )
                    fallback_payload = dict(payload)
                    fallback_payload.pop("message_id", None)
                    result = self._call("sendMessage", fallback_payload)
                except TelegramAPIError as exc:
                    if "message is not modified" in str(exc).lower():
                        result = {"message_id": reply.edit_message_id}
                    else:
                        raise
            else:
                result = self._call("sendMessage", payload)
            raw_id = result.get("message_id")
            if isinstance(raw_id, (int, str)) and str(raw_id).isdigit():
                message_id = int(raw_id)
        return message_id

    @classmethod
    def _split_text(cls, text: str) -> list[str]:
        """Разбивает длинный текст без потери содержимого."""
        if len(text) <= cls.MAX_MESSAGE_LENGTH:
            return [text]
        chunks: list[str] = []
        remaining = text
        while len(remaining) > cls.MAX_MESSAGE_LENGTH:
            boundary = remaining.rfind("\n", 0, cls.MAX_MESSAGE_LENGTH + 1)
            if boundary < cls.MAX_MESSAGE_LENGTH // 2:
                boundary = cls.MAX_MESSAGE_LENGTH
            chunks.append(remaining[:boundary])
            remaining = remaining[boundary:].lstrip("\n")
        if remaining or not chunks:
            chunks.append(remaining)
        return chunks

    @classmethod
    def _reply_markup(cls, reply: BotReply) -> dict[str, Any] | None:
        """Формирует встроенную клавиатуру Telegram."""
        if not reply.rows:
            return None
        return {
            "inline_keyboard": [
                [{"text": button.text, "callback_data": button.callback_data} for button in row]
                for row in reply.rows
            ]
        }

    def disable_keyboard(self, chat_id: str, message_id: int | None) -> None:
        """Отключает устаревшую клавиатуру сообщения."""
        if not message_id:
            return
        try:
            self._call(
                "editMessageReplyMarkup",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reply_markup": {"inline_keyboard": []},
                },
            )
        except TelegramAPIError:
            return

    def delete_message(self, chat_id: str, message_id: int | None) -> None:
        """Удаляет устаревшую карточку Telegram без остановки сценария."""
        if not message_id:
            return
        try:
            self._call(
                "deleteMessage",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                },
            )
        except TelegramAPIError:
            logger.info("telegram_message_delete_skipped", message_id=message_id)

    def download_file(self, file_id: str, mime_type: str) -> DownloadedFile:
        """Загружает файл из Telegram."""
        if not file_id:
            raise TelegramAPIError("Telegram не передал бинарные данные файла.")
        file_info = self._call("getFile", {"file_id": file_id})
        remote_path = file_info.get("file_path")
        if not remote_path:
            raise TelegramAPIError("Telegram did not return file_path")
        suffix = ".ogg" if mime_type.startswith("audio/") else (Path(remote_path).suffix or ".jpg")
        response = self.client.get(f"{self.file_url}/{remote_path}")
        if response.is_error:
            raise TelegramAPIError(self._error_text("getFileContent", response.status_code, {}))
        with NamedTemporaryFile(delete=False, suffix=suffix) as target:
            target.write(response.content)
            return DownloadedFile(path=Path(target.name), mime_type=mime_type)

    def set_webhook(self, webhook_url: str, secret_token: str) -> None:
        """Устанавливает webhook Telegram."""
        self._call(
            "setWebhook",
            {
                "url": webhook_url,
                "secret_token": secret_token,
                "allowed_updates": ["message", "callback_query"],
                "drop_pending_updates": False,
            },
        )
