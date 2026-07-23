from unittest.mock import MagicMock

import httpx
import pytest

from restaurant_bot.domain.models import BotReply, Button
from restaurant_bot.integrations.telegram import TelegramAPIError, TelegramClient


def test_long_reply_is_split_without_losing_text() -> None:
    text = "текст" * 2500

    chunks = TelegramClient._split_text(text)

    assert len(chunks) > 1
    assert all(len(chunk) <= TelegramClient.MAX_MESSAGE_LENGTH for chunk in chunks)
    assert "".join(chunks) == text


def test_media_download_fails_clearly_without_telegram_file_id() -> None:
    client = object.__new__(TelegramClient)

    with pytest.raises(TelegramAPIError, match="Telegram не передал бинарные данные файла"):
        client.download_file("", "audio/ogg")


def test_inline_keyboard_payload_preserves_callbacks_and_strips_emoji() -> None:
    reply = BotReply(
        text="Черновик", rows=[[Button(text="Показать черновик", callback_data="v2:back:r3")]]
    )

    assert TelegramClient._reply_markup(reply) == {
        "inline_keyboard": [[{"text": "Показать черновик", "callback_data": "v2:back:r3"}]]
    }


def test_callback_progress_edit_clears_the_obsolete_keyboard() -> None:
    client = object.__new__(TelegramClient)
    calls: list[tuple[str, dict[str, object]]] = []

    def call(method: str, payload: dict[str, object]) -> dict[str, int]:
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
    client = object.__new__(TelegramClient)
    client._call = MagicMock(side_effect=TelegramAPIError(description))  # type: ignore[method-assign]

    client.answer_callback("expired")

    client._call.assert_called_once_with("answerCallbackQuery", {"callback_query_id": "expired"})


def test_telegram_api_error_never_includes_the_bot_url() -> None:
    text = TelegramClient._error_text(
        "editMessageReplyMarkup",
        400,
        {"description": "Bad Request: message is not modified"},
    )

    assert "api.telegram.org" not in text
    assert "bot" not in text.lower()


def test_connect_timeout_is_retried_only_once() -> None:
    client = object.__new__(TelegramClient)
    client.base_url = "https://telegram.invalid"
    request = httpx.Request("POST", "https://telegram.invalid/sendMessage")
    success = httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})
    client.client = MagicMock()
    client.client.post.side_effect = [httpx.ConnectTimeout("timeout", request=request), success]

    assert client._call("sendMessage", {}) == {"message_id": 7}
    assert client.client.post.call_count == 2


def test_read_timeout_is_not_retried_to_avoid_duplicate_message() -> None:
    client = object.__new__(TelegramClient)
    client.base_url = "https://telegram.invalid"
    request = httpx.Request("POST", "https://telegram.invalid/sendMessage")
    client.client = MagicMock()
    client.client.post.side_effect = httpx.ReadTimeout("timeout", request=request)

    with pytest.raises(httpx.ReadTimeout):
        client._call("sendMessage", {})

    client.client.post.assert_called_once()
