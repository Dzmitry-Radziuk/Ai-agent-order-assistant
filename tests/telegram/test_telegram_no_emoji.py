"""Проверяет поведение, связанное с модулем «test telegram no emoji»."""

import pytest

from restaurant_bot.domain.models import BotReply, Button
from restaurant_bot.integrations.telegram import TelegramClient


def test_outgoing_telegram_text_and_buttons_remove_decorative_button_emoji() -> None:
    """Удаляет декоративные emoji из кнопок и сохраняет их в тексте ответа."""
    reply = BotReply(
        text="✅ Черновик готов",
        rows=[[Button(text="📦 Показать черновик", callback_data="v2:back")]],
    )
    assert TelegramClient._split_text(reply.text) == ["✅ Черновик готов"]
    markup = TelegramClient._reply_markup(reply)
    assert markup == {
        "inline_keyboard": [[{"text": "Показать черновик", "callback_data": "v2:back"}]]
    }


@pytest.mark.parametrize("emoji", ["✅", "↩️", "🔄", "📦"])
def test_all_decorative_button_prefixes_are_removed_without_changing_callback(
    emoji: str,
) -> None:
    """Удаляет все согласованные декоративные префиксы без изменения callback."""
    reply = BotReply(rows=[[Button(text=f"{emoji} Действие", callback_data="v2:test")]], text="ok")

    markup = TelegramClient._reply_markup(reply)

    assert markup == {"inline_keyboard": [[{"text": "Действие", "callback_data": "v2:test"}]]}
