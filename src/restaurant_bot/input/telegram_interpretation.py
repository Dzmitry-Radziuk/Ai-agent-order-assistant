"""Владелец семантической интерпретации входных Telegram-сообщений."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Protocol

import structlog
from openai import APIConnectionError, APITimeoutError, RateLimitError

from restaurant_bot.conversation.comments import comment_scope_items
from restaurant_bot.conversation.routing.contracts import (
    CompatibilityAction,
    CompatibilityContext,
)
from restaurant_bot.conversation.routing.state_compatibility import (
    StateCompatibilityPolicy,
)
from restaurant_bot.domain.models import (
    ConversationState,
    InputKind,
    Intent,
    ParsedCommand,
    SessionStage,
    TelegramEvent,
)
from restaurant_bot.domain.text import clean_text, normalize_text
from restaurant_bot.input.telegram_callbacks import parse_callback
from restaurant_bot.input.voice_policy import match_visible_action
from restaurant_bot.integrations.openai_client import CommentScopeDecision
from restaurant_bot.parsing.commands.api import enrich_command
from restaurant_bot.parsing.history import parse_history_query

logger = structlog.get_logger(__name__)
_OPENAI_TRANSIENT_ERRORS = (APIConnectionError, APITimeoutError, RateLimitError)
_SHEET_REVIEW_MODE = "sheet_link"


class InputProvider(Protocol):
    """Описывает только вызовы провайдера, нужные для интерпретации входа."""

    def parse_text(self, text: str) -> ParsedCommand:
        """Разбирает обычный текст пользователя."""

    def resolve_comment_scope(
        self,
        text: str,
        item_names: list[str],
    ) -> CommentScopeDecision:
        """Определяет область ожидаемого комментария."""

    def choose_visible_action(
        self,
        text: str,
        screen_text: str,
        visible_actions: list[dict[str, str]],
    ) -> str:
        """Выбирает видимое действие по свободной фразе."""


class MediaRecognizer(Protocol):
    """Описывает распознавание голоса и фотографии без зависимости от service-слоя."""

    def recognize_media(
        self,
        event: TelegramEvent,
        state: ConversationState,
        parse_text: Callable[[str, ConversationState], ParsedCommand],
        processing_message_id: int | None = None,
    ) -> ParsedCommand:
        """Распознаёт media и передаёт транскрипт в общий text parser."""


class TelegramInputInterpreter:
    """Интерпретирует callback, text, voice и photo в ParsedCommand."""

    def __init__(
        self,
        provider: InputProvider,
        recognizer_factory: Callable[[], MediaRecognizer],
        state_compatibility_policy: StateCompatibilityPolicy,
    ) -> None:
        """Сохраняет provider, media factory и каноническую state policy."""
        self.provider = provider
        self._recognizer_factory = recognizer_factory
        self.state_compatibility_policy = state_compatibility_policy

    def interpret(
        self,
        event: TelegramEvent,
        state: ConversationState,
        processing_message_id: int | None = None,
    ) -> ParsedCommand:
        """Выбирает семантический путь для нормализованного Telegram-события."""
        if event.input_type == InputKind.CALLBACK and event.callback_data.startswith("v2:review"):
            return enrich_command("", parse_callback(event.callback_data))
        if event.input_type == InputKind.CALLBACK and state.pending_comment_items:
            return self._parse_pending_comment_scope(event.callback_data, state)
        if event.input_type == InputKind.CALLBACK:
            return enrich_command("", parse_callback(event.callback_data))
        if event.input_type == InputKind.TEXT:
            return self.interpret_text(event.text, state)
        if event.input_type in {InputKind.VOICE, InputKind.PHOTO}:
            return self._recognizer_factory().recognize_media(
                event,
                state,
                self.interpret_text,
                processing_message_id,
            )
        return ParsedCommand(intent=Intent.UNKNOWN, text=event.text)

    def interpret_text(self, text: str, state: ConversationState) -> ParsedCommand:
        """Интерпретирует текст после обычного глобального разбора."""
        review_match = re.fullmatch(
            r"/start(?:@[A-Za-z0-9_]+)?\s+review_([A-Za-zА-Яа-яЁё0-9]{4,32})",
            clean_text(text),
            flags=re.IGNORECASE,
        )
        if review_match:
            return ParsedCommand(
                intent=Intent.REVIEW_ORDER,
                text=text,
                callback_target=review_match.group(1),
            )
        if not state.pending_comment_items:
            callback_data = self._match_visible_action(text, state)
            if callback_data:
                return enrich_command("", parse_callback(callback_data)).model_copy(
                    update={"text": text}
                )
        history_query = parse_history_query(text)
        if history_query is not None:
            logger.info(
                "history_query_parsed",
                question_type=history_query.question_type.value,
                product_count=len(history_query.product_queries),
                has_date_filter=history_query.date_reference.value != "none",
            )
            return ParsedCommand(
                intent=Intent.HISTORY_QUERY,
                text=text,
                history_query=history_query,
            )
        parsed = self.provider.parse_text(text)
        review_command = self._parse_sheet_review_command(text, parsed, state)
        if review_command is not None:
            return review_command
        if (
            state.pending_comment_items
            and self.state_compatibility_policy.should_try_contextual_fallback(
                state,
                parsed,
                CompatibilityContext.COMMENT_SCOPE,
            )
        ):
            return self._parse_pending_comment_scope(text, state)
        if parsed.intent is Intent.UNKNOWN or (
            parsed.intent is Intent.ADD_ITEMS and not parsed.items
        ):
            callback_data = self._match_visible_action(text, state)
            if callback_data:
                return enrich_command("", parse_callback(callback_data)).model_copy(
                    update={"text": text}
                )
        if not self._needs_visible_action_ai(text, parsed, state):
            return parsed
        try:
            selected = self.provider.choose_visible_action(
                text,
                state.ui_message_text,
                state.visible_actions,
            )
        except _OPENAI_TRANSIENT_ERRORS as error:
            logger.warning(
                "visible_action_transport_failed",
                error_type=type(error).__name__,
            )
            return ParsedCommand(intent=Intent.UNKNOWN, text=text)
        if not selected:
            if parsed.intent is Intent.ADD_ITEMS and parsed.items:
                return ParsedCommand(intent=Intent.UNKNOWN, text=text)
            return parsed
        return enrich_command("", parse_callback(selected)).model_copy(update={"text": text})

    def _parse_sheet_review_command(
        self,
        text: str,
        parsed: ParsedCommand,
        state: ConversationState,
    ) -> ParsedCommand | None:
        """Нормализует только подтверждённую команду sheet-review."""
        if state.stage != SessionStage.REVIEW or state.review_mode != _SHEET_REVIEW_MODE:
            return None
        decision = self.state_compatibility_policy.evaluate(
            parsed,
            state,
            CompatibilityContext.SHEET_REVIEW,
        )
        if decision.action is CompatibilityAction.INTERRUPT:
            return parsed
        if decision.action is CompatibilityAction.AMBIGUOUS:
            return ParsedCommand(intent=Intent.UNKNOWN, text=text)
        if decision.action is not CompatibilityAction.CONTINUE:
            return parsed.model_copy(update={"text": text})
        if parsed.intent in {Intent.REVIEW_SUBMIT, Intent.REVIEW_CANCEL}:
            return parsed.model_copy(update={"callback_target": state.review_token, "text": text})
        if parsed.intent in {Intent.SUBMIT_REQUEST, Intent.SUBMIT_AS_IS, Intent.CONFIRM} or (
            parsed.dialogue_response.value == "affirm"
        ):
            return parsed.model_copy(
                update={
                    "intent": Intent.REVIEW_SUBMIT,
                    "callback_target": state.review_token,
                    "text": text,
                }
            )
        if parsed.intent in {Intent.CANCEL, Intent.BACK} or (
            parsed.dialogue_response.value == "decline"
        ):
            return parsed.model_copy(
                update={
                    "intent": Intent.REVIEW_CANCEL,
                    "callback_target": state.review_token,
                    "text": text,
                }
            )
        return parsed.model_copy(update={"text": text})

    def _parse_pending_comment_scope(
        self,
        text: str,
        state: ConversationState,
    ) -> ParsedCommand:
        """Разбирает ответ на вопрос об области ожидаемого комментария."""
        callback = clean_text(text).casefold()
        scope_items = comment_scope_items(state)
        item_count = len(scope_items)
        action = ""
        target_indexes: list[int] = []
        confidence = 0.0
        if callback.startswith("v2:comment:"):
            target = callback.removeprefix("v2:comment:").split(":r", maxsplit=1)[0]
            confidence = 1.0
            if target == "all":
                action = "items"
                target_indexes = list(range(item_count))
            elif target == "last" and item_count:
                action = "items"
                target_indexes = [item_count - 1]
            elif target == "order":
                action = "order"
            elif target == "cancel":
                action = "cancel"
        else:
            try:
                decision = self.provider.resolve_comment_scope(
                    text,
                    [item.product_query for item in scope_items],
                )
            except _OPENAI_TRANSIENT_ERRORS as error:
                logger.warning(
                    "comment_scope_transport_failed",
                    error_type=type(error).__name__,
                )
                action = "ambiguous"
            else:
                action = decision.action
                target_indexes = decision.target_item_indexes
                confidence = decision.confidence
        carries_items = action in {"items", "order"}
        intent = (
            Intent.ADD_ITEMS
            if carries_items
            else Intent.CANCEL
            if action == "cancel"
            else Intent.CLARIFY_CURRENT
        )
        return ParsedCommand(
            intent=intent,
            text=text,
            items=(
                [item.model_copy(deep=True) for item in state.pending_comment_items]
                if carries_items
                else []
            ),
            comment_scope_action=action or "ambiguous",
            comment_target_indexes=target_indexes,
            confidence=confidence,
        )

    @staticmethod
    def _match_visible_action(text: str, state: ConversationState) -> str:
        """Находит явно названную кнопку текущего Telegram-экрана."""
        return match_visible_action(text, state)

    @staticmethod
    def _needs_visible_action_ai(
        text: str,
        parsed: ParsedCommand,
        state: ConversationState,
    ) -> bool:
        """Определяет необходимость смыслового выбора видимой кнопки."""
        if state.stage == SessionStage.REVIEW and state.review_mode == _SHEET_REVIEW_MODE:
            return False
        if not state.visible_actions or parsed.intent not in {Intent.UNKNOWN, Intent.ADD_ITEMS}:
            return False
        if (
            parsed.intent == Intent.ADD_ITEMS
            and parsed.items
            and (
                parsed.explicit_add_items
                or any(item.quantity is not None or item.unit for item in parsed.items)
            )
        ):
            return False
        words = normalize_text(text).split()
        action_stems = (
            "выбр",
            "остав",
            "введ",
            "укаж",
            "исправ",
            "измен",
            "поменя",
            "покаж",
            "посмотр",
            "откр",
            "перей",
            "верн",
            "отправ",
            "добав",
            "убер",
            "удал",
            "очист",
            "начн",
            "повтор",
            "пропуст",
            "пров",
            "прав",
        )
        return state.stage not in {SessionStage.COLLECTING, SessionStage.REVIEW} or any(
            word.startswith(action_stems) for word in words
        )
