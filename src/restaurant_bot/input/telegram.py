from __future__ import annotations

import re
from typing import Any

from restaurant_bot.application.conversation.contracts import ConversationInput
from restaurant_bot.domain.models import InputKind, TelegramEvent
from restaurant_bot.domain.text import clean_text


def _clean_message_text(value: Any) -> str:
    """Очищает текст сообщения, сохраняя границы строк товарного списка."""
    return "\n".join(
        cleaned for line in str(value or "").splitlines() if (cleaned := clean_text(line))
    )


def normalize_telegram_update(update: dict[str, Any]) -> TelegramEvent:
    """Преобразует обновление Telegram во внутреннее событие."""
    message = update.get("message") or update.get("edited_message") or {}
    callback = update.get("callback_query") or {}
    callback_message = callback.get("message") or {}
    sender = callback.get("from") or message.get("from") or {}
    chat = callback_message.get("chat") or message.get("chat") or {}

    text = _clean_message_text(message.get("text") or message.get("caption"))
    callback_data = clean_text(callback.get("data"))
    file_id = ""
    mime_type = ""
    input_type = InputKind.CALLBACK if callback_data else InputKind.TEXT

    if not callback_data and (voice := message.get("voice")):
        input_type = InputKind.VOICE
        file_id = clean_text(voice.get("file_id"))
        mime_type = clean_text(voice.get("mime_type")) or "audio/ogg"
    elif not callback_data and (photos := message.get("photo")):
        input_type = InputKind.PHOTO
        file_id = clean_text(photos[-1].get("file_id")) if photos else ""
        mime_type = "image/jpeg"
    elif not callback_data and (document := message.get("document")):
        document_mime = clean_text(document.get("mime_type"))
        if document_mime.startswith("image/"):
            input_type = InputKind.PHOTO
            file_id = clean_text(document.get("file_id"))
            mime_type = document_mime

    command_match = re.match(r"^/([a-z][a-z0-9_]*)(?:@[a-z0-9_]+)?(?:\s|$)", text, re.I)
    chat_id = clean_text(chat.get("id") or sender.get("id"))
    if not chat_id:
        input_type = InputKind.UNKNOWN

    return TelegramEvent(
        update_id=int(update.get("update_id") or 0),
        chat_id=chat_id,
        input_type=input_type,
        text=text,
        bot_command=(command_match.group(1).lower() if command_match else ""),
        callback_data=callback_data,
        callback_query_id=clean_text(callback.get("id")),
        callback_message_id=callback_message.get("message_id"),
        telegram_user_id=clean_text(sender.get("id")),
        telegram_username=clean_text(sender.get("username")).lstrip("@"),
        telegram_first_name=clean_text(sender.get("first_name")),
        telegram_last_name=clean_text(sender.get("last_name")),
        chat_type=clean_text(chat.get("type")),
        file_id=file_id,
        mime_type=mime_type,
        raw_update=update,
    )


def to_conversation_input(event: TelegramEvent) -> ConversationInput:
    """Преобразует Telegram-событие в нейтральный прикладной вход."""
    return ConversationInput(
        interaction_id=event.update_id,
        conversation_id=event.chat_id,
        actor_id=event.telegram_user_id or event.chat_id,
        channel="telegram",
        kind=event.input_type,
        text=event.text,
        action=event.callback_data,
        media_reference=event.file_id,
        metadata={
            "callback_query_id": event.callback_query_id,
            "callback_message_id": event.callback_message_id,
            "username": event.telegram_username,
            "first_name": event.telegram_first_name,
            "last_name": event.telegram_last_name,
            "chat_type": event.chat_type,
            "file_id": event.file_id,
            "mime_type": event.mime_type,
            "raw_update": event.raw_update,
        },
    )
