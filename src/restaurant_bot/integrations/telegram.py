"""Предоставляет интеграцию «telegram»."""

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
        # Повтор после тайм-аута чтения может продублировать сообщение Telegram:
        # сервер мог принять его, хотя ответ до нас не дошёл.  # noqa: RUF003
        # Ошибку соединения безопасно повторить, потому что запрос не отправлялся.
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
            # Не записываем URL и payload: в них могут быть токен бота или  # noqa: RUF003
            # идентификаторы Telegram. Класса исключения достаточно, чтобы
            # отличить ошибку соединения от ошибки HTTP-ответа.
            logger.warning(
                "telegram_api_transport_failed",
                method=method,
                error_type=type(exc).__name__,
            )
            raise
        finally:
            # Payload и URL намеренно не записываем: в них могут быть
            # идентификаторы Telegram или токен бота. Длительность показывает
            # медленные карточки callback, не раскрывая эти данные.
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
            # Telegram принимает ответы на callback только короткое время.
            # Просроченный callback уже безопасен и не должен повторно
            # complete business action (clear cart, select product, etc.).
            description = str(exc).lower()
            if "query is too old" in description or "query id is invalid" in description:
                logger.info("telegram_callback_already_expired")
                return
            raise

    def send_reply(self, chat_id: str, reply: BotReply) -> int | None:
        # В workflow n8n визуальные символы намеренно используются как якоря  # noqa: RUF003
        # карточек и действий. Telegram должен получить точный текст, а не  # noqa: RUF003
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
            # n8n оставляет inline-клавиатуру только на последней видимой карточке.
            if index == len(chunks) - 1:
                if reply_markup:
                    payload["reply_markup"] = reply_markup
                elif reply.edit_message_id:
                    # n8n заменяет карточку callback карточкой прогресса и явно
                    # удаляет устаревшую inline-клавиатуру.
                    payload["reply_markup"] = {"inline_keyboard": []}
            if index == 0 and reply.edit_message_id:
                payload["message_id"] = reply.edit_message_id
                try:
                    result = self._call("editMessageText", payload)
                except TELEGRAM_CONNECT_ERRORS as exc:
                    # Ошибка соединения возникает до получения запроса Telegram, поэтому
                    # переход к новому сообщению не может продублировать
                    # an already edited card. It also avoids losing a completed
                    # результат только из-за недоступности старой карточки прогресса.
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
