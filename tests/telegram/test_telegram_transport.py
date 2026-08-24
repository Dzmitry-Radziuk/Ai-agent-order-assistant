"""Проверяет поведение, связанное с модулем «test telegram transport»."""

from unittest.mock import MagicMock

import httpx
import pytest

import restaurant_bot.integrations.telegram as telegram_module
from restaurant_bot.domain.models import BotReply, Button
from restaurant_bot.integrations.telegram import TelegramAPIError, TelegramClient


def test_long_reply_is_split_without_losing_text() -> None:
    """Проверяет, что long ответ является split без потери текст."""
    text = "текст" * 2500

    chunks = TelegramClient._split_text(text)

    assert len(chunks) > 1
    assert all(len(chunk) <= TelegramClient.MAX_MESSAGE_LENGTH for chunk in chunks)
    assert "".join(chunks) == text


def test_media_download_fails_clearly_without_telegram_file_id() -> None:
    """Проверяет, что медиа download завершается ошибкой clearly без Telegram файл идентификатор."""
    client = object.__new__(TelegramClient)

    with pytest.raises(TelegramAPIError, match="Telegram не передал бинарные данные файла"):
        client.download_file("", "audio/ogg")


def test_inline_keyboard_payload_preserves_callbacks_and_strips_emoji() -> None:
    """Проверяет, что inline keyboard payload сохраняет callback и strips emoji."""
    reply = BotReply(
        text="Черновик", rows=[[Button(text="Показать черновик", callback_data="v2:back:r3")]]
    )

    assert TelegramClient._reply_markup(reply) == {
        "inline_keyboard": [[{"text": "Показать черновик", "callback_data": "v2:back:r3"}]]
    }


def test_callback_progress_edit_clears_the_obsolete_keyboard() -> None:
    """Проверяет, что callback прогресс edit очищает устаревшая keyboard."""
    client = object.__new__(TelegramClient)
    calls: list[tuple[str, dict[str, object]]] = []

    def call(method: str, payload: dict[str, object]) -> dict[str, int]:
        """Сохраняет параметры тестового вызова Telegram API."""
        calls.append((method, payload))
        return {"message_id": 42}

    client._call = call  # type: ignore[method-assign]

    message_id = client.send_reply(
        "1",
        BotReply(text="Обновляю статус заявки...", edit_message_id=42),
    )

    assert message_id == 42
    assert calls == [
        (
            "editMessageText",
            {
                "chat_id": "1",
                "text": "Обновляю статус заявки...",
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "message_id": 42,
                "reply_markup": {"inline_keyboard": []},
            },
        )
    ]


def test_disable_keyboard_ignores_a_card_without_inline_keyboard() -> None:
    """Проверяет, что отключение клавиатуры игнорирует карточку без inline-клавиатуры."""
    client = object.__new__(TelegramClient)
    client._call = MagicMock(
        side_effect=TelegramAPIError("Telegram editMessageReplyMarkup failed (400)")
    )  # type: ignore[method-assign]

    client.disable_keyboard("1", 42)

    client._call.assert_called_once()


def test_delete_message_uses_exact_chat_and_message() -> None:
    """Удаляет только указанную устаревшую карточку."""
    client = object.__new__(TelegramClient)
    client._call = MagicMock(return_value=True)  # type: ignore[method-assign]

    client.delete_message("1", 42)

    client._call.assert_called_once_with(
        "deleteMessage",
        {"chat_id": "1", "message_id": 42},
    )


def test_delete_message_failure_does_not_stop_user_action() -> None:
    """Продолжает сценарий, если Telegram уже удалил старую карточку."""
    client = object.__new__(TelegramClient)
    client._call = MagicMock(side_effect=TelegramAPIError("message not found"))  # type: ignore[method-assign]

    client.delete_message("1", 42)

    client._call.assert_called_once()


@pytest.mark.parametrize(
    "description",
    [
        "Telegram answerCallbackQuery failed (400): Bad Request: query is too old",
        "Telegram answerCallbackQuery failed (400): Bad Request: query ID is invalid",
    ],
)
def test_expired_callback_does_not_retry_the_whole_action(description: str) -> None:
    """Проверяет, что устаревший callback выполняет не повтор whole действие."""
    client = object.__new__(TelegramClient)
    client._call = MagicMock(side_effect=TelegramAPIError(description))  # type: ignore[method-assign]

    client.answer_callback("expired")

    client._call.assert_called_once_with("answerCallbackQuery", {"callback_query_id": "expired"})


def test_telegram_api_error_never_includes_the_bot_url() -> None:
    """Проверяет, что Telegram API ошибка никогда не includes бота URL."""
    text = TelegramClient._error_text(
        "editMessageReplyMarkup",
        400,
        {"description": "Bad Request: message is not modified"},
    )

    assert "api.telegram.org" not in text
    assert "bot" not in text.lower()


def test_connect_timeout_is_retried_only_once() -> None:
    """Проверяет, что подключение тайм-аут является повторяется только один раз."""
    client = object.__new__(TelegramClient)
    client.base_url = "https://telegram.invalid"
    request = httpx.Request("POST", "https://telegram.invalid/sendMessage")
    success = httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})
    client.client = MagicMock()
    client.client.post.side_effect = [httpx.ConnectTimeout("timeout", request=request), success]

    assert client._call("sendMessage", {}) == {"message_id": 7}
    assert client.client.post.call_count == 2


def test_two_connect_timeouts_are_retried_before_success() -> None:
    """Переживает два кратких сбоя TLS до успешного соединения."""
    client = object.__new__(TelegramClient)
    client.base_url = "https://telegram.invalid"
    request = httpx.Request("POST", "https://telegram.invalid/sendMessage")
    success = httpx.Response(200, json={"ok": True, "result": {"message_id": 8}})
    client.client = MagicMock()
    client.client.post.side_effect = [
        httpx.ConnectTimeout("timeout-1", request=request),
        httpx.ConnectTimeout("timeout-2", request=request),
        success,
    ]

    assert client._call("sendMessage", {}) == {"message_id": 8}
    assert client.client.post.call_count == 3


def test_edit_connect_failure_falls_back_to_new_message() -> None:
    """Доставляет сохранённый результат новой карточкой после сбоя edit."""
    client = object.__new__(TelegramClient)
    request = httpx.Request("POST", "https://telegram.invalid/editMessageText")
    client._call = MagicMock(  # type: ignore[method-assign]
        side_effect=[
            httpx.ConnectTimeout("edit unavailable", request=request),
            {"message_id": 99},
        ]
    )

    message_id = client.send_reply(
        "1",
        BotReply(text="Черновик обновлён", edit_message_id=42),
    )

    assert message_id == 99
    edit_method, edit_payload = client._call.call_args_list[0].args
    send_method, send_payload = client._call.call_args_list[1].args
    assert edit_method == "editMessageText"
    assert edit_payload["message_id"] == 42
    assert send_method == "sendMessage"
    assert "message_id" not in send_payload
    assert send_payload["text"] == "Черновик обновлён"


def test_read_timeout_is_not_retried_to_avoid_duplicate_message() -> None:
    """Проверяет, что чтение тайм-аут является не повторяется в avoid дубликат сообщение."""
    client = object.__new__(TelegramClient)
    client.base_url = "https://telegram.invalid"
    request = httpx.Request("POST", "https://telegram.invalid/sendMessage")
    client.client = MagicMock()
    client.client.post.side_effect = httpx.ReadTimeout("timeout", request=request)

    with pytest.raises(httpx.ReadTimeout):
        client._call("sendMessage", {})

    client.client.post.assert_called_once()


def test_media_download_retries_read_timeouts_without_repeating_a_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Повторяет безопасное чтение файла после временного тайм-аута Telegram."""
    client = object.__new__(TelegramClient)
    client.base_url = "https://telegram.invalid"
    client.file_url = "https://telegram.invalid/file"
    client.media_download_timeout_seconds = 15
    client.telegram_connect_timeout_seconds = 5
    request = httpx.Request("POST", "https://telegram.invalid/getFile")
    client.client = MagicMock()
    client.client.post.side_effect = [
        httpx.ReadTimeout("metadata timeout", request=request),
        httpx.Response(200, json={"ok": True, "result": {"file_path": "voice.ogg"}}),
    ]
    client.client.get.side_effect = [
        httpx.ReadTimeout(
            "content timeout",
            request=httpx.Request("GET", "https://telegram.invalid/file/voice.ogg"),
        ),
        httpx.Response(200, content=b"voice"),
    ]
    monkeypatch.setattr(telegram_module, "sleep", lambda _: None)

    downloaded = client.download_file("file-id", "audio/ogg")

    try:
        assert downloaded.path.read_bytes() == b"voice"
        assert client.client.post.call_count == 2
        assert client.client.get.call_count == 2
    finally:
        downloaded.path.unlink(missing_ok=True)


def test_media_download_deadline_prevents_a_retry_after_total_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Не начинает новую попытку после исчерпания общего лимита media-скачивания."""
    client = object.__new__(TelegramClient)
    request = httpx.Request("GET", "https://telegram.invalid/file/voice.ogg")
    moments = iter([0.0, 15.0])
    attempts: list[float] = []
    monkeypatch.setattr(telegram_module, "perf_counter", lambda: next(moments))

    def timeout(remaining_seconds: float) -> bytes:
        """Имитирует чтение, занявшее остаток общего лимита."""
        attempts.append(remaining_seconds)
        raise httpx.ReadTimeout("content timeout", request=request)

    with pytest.raises(TelegramAPIError, match="deadline exceeded"):
        client._retry_media_read(timeout, deadline=15.0)

    assert attempts == [15.0]


def test_edit_read_timeout_is_retried_without_sending_a_duplicate_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Повторяет одинаковое редактирование карточки после потери ответа Telegram."""
    client = object.__new__(TelegramClient)
    client.base_url = "https://telegram.invalid"
    request = httpx.Request("POST", "https://telegram.invalid/editMessageText")
    client.client = MagicMock()
    client.client.post.side_effect = [
        httpx.ReadTimeout("edit timeout", request=request),
        httpx.Response(200, json={"ok": True, "result": {"message_id": 42}}),
    ]
    monkeypatch.setattr(client._edit_message_text.retry, "wait", lambda _: 0)

    message_id = client.send_reply("1", BotReply(text="Готово", edit_message_id=42))

    assert message_id == 42
    assert client.client.post.call_count == 2
    assert all(
        call.args[0].endswith("/editMessageText") for call in client.client.post.call_args_list
    )
