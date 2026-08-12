from restaurant_bot.domain.models import InputKind
from restaurant_bot.input.telegram import normalize_telegram_update


def test_telegram_photo_uses_largest_available_size() -> None:
    """Проверяет, что Telegram photo использует наибольший доступно размер."""
    event = normalize_telegram_update(
        {
            "update_id": 1,
            "message": {"chat": {"id": 7}, "photo": [{"file_id": "small"}, {"file_id": "large"}]},
        }
    )

    assert event.input_type is InputKind.PHOTO
    assert event.file_id == "large"
    assert event.mime_type == "image/jpeg"


def test_image_document_is_routed_as_photo() -> None:
    """Проверяет, что изображение документ является маршрутизируется как photo."""
    event = normalize_telegram_update(
        {
            "update_id": 2,
            "message": {
                "chat": {"id": 7},
                "document": {"file_id": "scan", "mime_type": "image/png"},
            },
        }
    )

    assert event.input_type is InputKind.PHOTO
    assert event.file_id == "scan"
    assert event.mime_type == "image/png"
