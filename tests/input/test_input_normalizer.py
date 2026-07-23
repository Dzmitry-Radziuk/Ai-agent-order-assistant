from restaurant_bot.domain.models import InputKind
from restaurant_bot.services.input_normalizer import normalize_telegram_update


def test_normalizes_text_command_and_sender() -> None:
    event = normalize_telegram_update(
        {
            "update_id": 7,
            "message": {
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 9, "username": "cook"},
                "text": "/draft@order_bot",
            },
        }
    )
    assert event.input_type is InputKind.TEXT
    assert event.chat_id == "42"
    assert event.bot_command == "draft"
    assert event.telegram_username == "cook"
    assert event.chat_type == "private"


def test_normalizes_voice_message() -> None:
    event = normalize_telegram_update(
        {
            "update_id": 8,
            "message": {
                "chat": {"id": 42},
                "voice": {"file_id": "voice-id", "mime_type": "audio/ogg"},
            },
        }
    )
    assert event.input_type is InputKind.VOICE
    assert event.file_id == "voice-id"
    assert event.mime_type == "audio/ogg"
