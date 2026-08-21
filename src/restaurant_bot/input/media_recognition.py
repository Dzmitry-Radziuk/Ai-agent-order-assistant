"""Преобразует входные данные «media recognition»."""

from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from time import perf_counter

import structlog
from openai import APIConnectionError, APITimeoutError, RateLimitError

from restaurant_bot.domain.models import (
    BotReply,
    ConversationState,
    InputKind,
    Intent,
    ParsedCommand,
    TelegramEvent,
)
from restaurant_bot.input.voice_policy import (
    match_visible_action,
    requires_high_accuracy_transcription,
    voice_transcription_prompt,
)
from restaurant_bot.input.voice_transcript_policy import (
    has_supported_voice_letters,
    select_transcription_result,
)
from restaurant_bot.integrations.openai_client import OpenAIService
from restaurant_bot.integrations.openai_transcription_policy import has_distinct_models
from restaurant_bot.integrations.telegram import TelegramClient

logger = structlog.get_logger(__name__)
_OPENAI_TRANSIENT_ERRORS = (APIConnectionError, APITimeoutError, RateLimitError)


class InputRecognitionService:
    """Распознаёт голосовые и фотографические входные данные."""

    def __init__(self, telegram: TelegramClient, openai_service: OpenAIService) -> None:
        """Сохраняет внешние клиенты распознавания."""
        self.telegram = telegram
        self.openai = openai_service

    def recognize_media(
        self,
        event: TelegramEvent,
        state: ConversationState,
        parse_text: Callable[[str, ConversationState], ParsedCommand],
        processing_message_id: int | None = None,
    ) -> ParsedCommand:
        """Скачивает и распознаёт голос или фотографию."""
        stage_started = perf_counter()
        downloaded = self.telegram.download_file(event.file_id, event.mime_type)
        logger.info(
            "telegram_media_download_finished",
            input_type=event.input_type.value,
            duration_ms=round((perf_counter() - stage_started) * 1000),
        )
        try:
            if event.input_type == InputKind.VOICE:
                return self._recognize_voice(downloaded.path, state, parse_text)
            return self._recognize_photo(
                event, downloaded.path, downloaded.mime_type, processing_message_id
            )
        finally:
            with suppress(OSError):
                os.unlink(downloaded.path)

    def _recognize_voice(
        self,
        path: Path,
        state: ConversationState,
        parse_text: Callable[[str, ConversationState], ParsedCommand],
    ) -> ParsedCommand:
        """Распознаёт голос и безопасно разбирает полученный текст."""
        stage_started = perf_counter()
        try:
            transcript = self.openai.transcribe(
                path,
                prompt=self.voice_transcription_prompt(state),
            )
        except _OPENAI_TRANSIENT_ERRORS as error:
            logger.warning(
                "voice_transcription_transport_failed",
                phase="primary",
                error_type=type(error).__name__,
            )
            return ParsedCommand(intent=Intent.UNKNOWN, text="")
        high_accuracy_retry = False
        if (
            self.requires_high_accuracy_transcription(
                transcript,
                state,
            )
            and self.has_distinct_transcription_fallback()
        ):
            high_accuracy_retry = True
            primary_transcript = transcript
            try:
                retry_transcript = self.openai.transcribe(
                    path,
                    prompt=self.voice_transcription_prompt(state),
                    high_accuracy=True,
                )
                transcript = select_transcription_result(
                    primary_transcript,
                    retry_transcript,
                )
            except _OPENAI_TRANSIENT_ERRORS as error:
                logger.warning(
                    "voice_transcription_transport_failed",
                    phase="high_accuracy",
                    error_type=type(error).__name__,
                )
                transcript = primary_transcript
        logger.info(
            "voice_transcription_finished",
            duration_ms=round((perf_counter() - stage_started) * 1000),
            high_accuracy_retry=high_accuracy_retry,
        )
        if not has_supported_voice_letters(transcript):
            return ParsedCommand(intent=Intent.UNKNOWN, text="")
        stage_started = perf_counter()
        parsed = parse_text(transcript, state)
        logger.info(
            "voice_text_parse_finished",
            duration_ms=round((perf_counter() - stage_started) * 1000),
            intent=parsed.intent.value,
            item_count=len(parsed.items),
        )
        return parsed.model_copy(update={"text": transcript})

    def _recognize_photo(
        self,
        event: TelegramEvent,
        path: Path,
        mime_type: str,
        processing_message_id: int | None,
    ) -> ParsedCommand:
        """Распознаёт список товаров на фотографии."""
        self.update_processing(
            event.chat_id,
            processing_message_id,
            "🔎 <b>Распознаю товары на фото…</b>\n\n"
            "Для большого списка это может занять до нескольких минут.",
        )
        stage_started = perf_counter()
        parsed = self.openai.parse_photo(path, mime_type, event.text)
        logger.info(
            "photo_parse_finished",
            duration_ms=round((perf_counter() - stage_started) * 1000),
            item_count=len(parsed.items),
        )
        progress_text = (
            "📋 <b>Фото прочитано частично</b>\n\n"
            "Проверяю, можно ли надёжно использовать распознанные строки…"
            if parsed.photo_outcome == "incomplete_photo_read"
            else "📋 <b>Фото распознано</b>\n\nСверяю товары с каталогом…"
        )
        self.update_processing(event.chat_id, processing_message_id, progress_text)
        return parsed

    def update_processing(self, chat_id: str, message_id: int | None, text: str) -> None:
        """Обновляет карточку прогресса без остановки обработки."""
        if not message_id:
            return
        try:
            self.telegram.send_reply(
                chat_id,
                BotReply(text=text, edit_message_id=message_id),
            )
        except Exception:
            logger.warning("telegram_processing_update_failed", exc_info=True)

    @staticmethod
    def match_visible_action(text: str, state: ConversationState) -> str:
        """Находит явно названную кнопку текущего экрана."""
        return match_visible_action(text, state)

    @staticmethod
    def voice_transcription_prompt(state: ConversationState) -> str:
        """Формирует контекст для распознавания голоса."""
        return voice_transcription_prompt(state)

    @classmethod
    def requires_high_accuracy_transcription(
        cls,
        transcript: str,
        state: ConversationState,
    ) -> bool:
        """Проверяет необходимость повторного распознавания речи."""
        return requires_high_accuracy_transcription(transcript, state)

    def has_distinct_transcription_fallback(self) -> bool:
        """Разрешает повтор только при реально отличающейся модели."""
        return has_distinct_models(self.openai)
