from restaurant_bot.domain.models import BotReply, Button
from restaurant_bot.integrations.telegram import TelegramClient


def test_outgoing_telegram_text_and_buttons_preserve_workflow_emoji() -> None:
    reply = BotReply(
        text="✅ Черновик готов",
        rows=[[Button(text="📦 Показать черновик", callback_data="v2:back")]],
    )
    assert TelegramClient._split_text(reply.text) == ["✅ Черновик готов"]
    markup = TelegramClient._reply_markup(reply)
    assert markup == {
        "inline_keyboard": [[{"text": "📦 Показать черновик", "callback_data": "v2:back"}]]
    }
