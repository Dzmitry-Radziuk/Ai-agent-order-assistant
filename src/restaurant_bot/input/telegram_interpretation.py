"""Владелец семантической интерпретации входных Telegram-сообщений."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from typing import Protocol

import structlog
from openai import APIConnectionError, APITimeoutError, RateLimitError

from restaurant_bot.conversation.comments import (
    comment_scope_items,
    has_pending_comment_scope,
    reconcile_comment_target,
    resolve_comment_scope_text,
)
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
    ItemStatus,
    ParsedCommand,
    SessionStage,
    TelegramEvent,
)
from restaurant_bot.domain.text import clean_text, normalize_text
from restaurant_bot.input.telegram_callbacks import parse_callback
from restaurant_bot.input.voice_policy import match_visible_action
from restaurant_bot.integrations.openai_client import CommentScopeDecision
from restaurant_bot.parsing.commands.api import enrich_command, infer_intent
from restaurant_bot.parsing.comment_scope import (
    has_explicit_global_comment_scope,
    has_explicit_order_comment_scope,
    strip_explicit_comment_scope_prefix,
)
from restaurant_bot.parsing.delivery_language import has_delivery_wish_shape
from restaurant_bot.parsing.history import parse_history_query, requires_history_context
from restaurant_bot.parsing.products import has_multiple_explicit_order_items
from restaurant_bot.parsing.semantic_routing import (
    normalize_comment_proposal,
    protect_confirmed_command,
)
from restaurant_bot.services.conversation_handlers.pending_quantity import (
    PendingQuantityHandler,
)

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
        *,
        timezone_name: str = "Europe/Minsk",
        today: date | None = None,
    ) -> None:
        """Сохраняет provider, media factory и каноническую state policy."""
        self.provider = provider
        self._recognizer_factory = recognizer_factory
        self.state_compatibility_policy = state_compatibility_policy
        self.timezone_name = timezone_name
        self.today = today

    def interpret(
        self,
        event: TelegramEvent,
        state: ConversationState,
        processing_message_id: int | None = None,
    ) -> ParsedCommand:
        """Выбирает семантический путь для нормализованного Telegram-события."""
        if event.input_type == InputKind.CALLBACK and event.callback_data.startswith("v2:review"):
            return enrich_command("", parse_callback(event.callback_data))
        if event.input_type == InputKind.CALLBACK and has_pending_comment_scope(state):
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
        quantity_reply = self._parse_quantity_modal_reply(text, state)
        if quantity_reply is not None:
            return quantity_reply
        if has_pending_comment_scope(state) and resolve_comment_scope_text(
            text, comment_scope_items(state)
        ):
            return self._parse_pending_comment_scope(text, state)
        if not has_pending_comment_scope(state):
            callback_data = self._match_visible_action(text, state)
            if callback_data:
                visible_command = enrich_command("", parse_callback(callback_data)).model_copy(
                    update={"text": text}
                )
                if self._candidate_command_is_authorized(visible_command, state):
                    return visible_command
        history_query = parse_history_query(
            text,
            context_product_queries=self._history_context_products(state),
            today=self.today,
            timezone_name=self.timezone_name,
        )
        if history_query is not None and not has_multiple_explicit_order_items(text):
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
        if requires_history_context(text):
            return ParsedCommand(intent=Intent.UNKNOWN, text=text)
        if has_explicit_global_comment_scope(text) and has_delivery_wish_shape(text):
            comment_text = strip_explicit_comment_scope_prefix(text)
            if comment_text:
                if any(item.status is not ItemStatus.SKIPPED for item in state.cart):
                    return ParsedCommand(
                        intent=Intent.EDIT_COMMENT,
                        text=text,
                        comment_action="add",
                        comment_scope="order",
                        comment_text=comment_text,
                    )
                deterministic_comment = self._normalize_explicit_comment(
                    text,
                    infer_intent(text),
                    state,
                )
                if deterministic_comment.intent is Intent.EDIT_COMMENT:
                    return deterministic_comment
                return ParsedCommand(intent=Intent.UNKNOWN, text=text)
        deterministic = self._normalize_explicit_comment(text, infer_intent(text), state)
        deterministic = self._authorize_candidate_command(deterministic, state, text)
        if deterministic.intent is Intent.ORDER_STATUS:
            return deterministic
        delivery_wish = has_delivery_wish_shape(text)
        proven_mutation = self._is_proven_mutation(deterministic)
        if deterministic.intent is Intent.EDIT_COMMENT and proven_mutation:
            return deterministic
        parsed = self.provider.parse_text(text)
        parsed = normalize_comment_proposal(text, parsed)
        parsed = self._normalize_explicit_comment(text, parsed, state)
        parsed = self._authorize_candidate_command(parsed, state, text)
        if proven_mutation:
            protected = protect_confirmed_command(deterministic, parsed)
            if protected is not parsed:
                return protected
        if delivery_wish and parsed.intent is Intent.HISTORY_QUERY:
            return self._safe_delivery_wish_command(text, state)
        if delivery_wish and not proven_mutation and parsed.intent is Intent.ADD_ITEMS:
            return self._safe_delivery_wish_command(text, state)
        review_command = self._parse_sheet_review_command(text, parsed, state)
        if review_command is not None:
            return review_command
        if has_pending_comment_scope(
            state
        ) and self.state_compatibility_policy.should_try_contextual_fallback(
            state,
            parsed,
            CompatibilityContext.COMMENT_SCOPE,
        ):
            return self._parse_pending_comment_scope(text, state)
        if (
            state.stage is SessionStage.AWAIT_ADD_MORE_CONFIRM
            and deterministic.intent is Intent.ADD_ITEMS
            and deterministic.items
            and (
                parsed.intent is Intent.UNKNOWN
                or (parsed.intent is Intent.ADD_ITEMS and not parsed.items)
            )
        ):
            return deterministic
        if parsed.intent is Intent.UNKNOWN or (
            parsed.intent is Intent.ADD_ITEMS and not parsed.items
        ):
            callback_data = self._match_visible_action(text, state)
            if callback_data:
                visible_command = enrich_command("", parse_callback(callback_data)).model_copy(
                    update={"text": text}
                )
                if self._candidate_command_is_authorized(visible_command, state):
                    return visible_command
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
            if self._is_proven_mutation(deterministic):
                return parsed
            return ParsedCommand(intent=Intent.UNKNOWN, text=text)
        if not selected:
            return parsed
        selected_command = enrich_command("", parse_callback(selected)).model_copy(
            update={"text": text}
        )
        return self._authorize_candidate_command(selected_command, state, text)

    @staticmethod
    def _history_context_products(state: ConversationState) -> list[str]:
        """Возвращает единственный товар, безопасно доступный по контексту черновика."""
        names = [
            (item.catalog_name or item.source_query).strip()
            for item in state.cart
            if (item.catalog_name or item.source_query).strip()
        ]
        normalized = {normalize_text(name): name for name in names}
        return list(normalized.values()) if len(normalized) == 1 else []

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

    @staticmethod
    def _normalize_explicit_comment(
        text: str,
        parsed: ParsedCommand,
        state: ConversationState,
    ) -> ParsedCommand:
        """Защищает явную цель комментария от ошибочной общей области ИИ."""
        deterministic = infer_intent(text)
        if deterministic.intent is not Intent.EDIT_COMMENT:
            return parsed
        if deterministic.comment_scope == "order":
            return deterministic
        if has_explicit_order_comment_scope(text):
            return deterministic.model_copy(
                update={"comment_target_query": "", "comment_scope": "order"}
            )
        resolution = reconcile_comment_target(
            state,
            deterministic.comment_target_query,
            deterministic.comment_text,
        )
        if resolution.ambiguous:
            return deterministic
        return deterministic.model_copy(
            update={
                "comment_target_query": resolution.target_query,
                "comment_text": resolution.comment_text,
            }
        )

    @staticmethod
    def _is_proven_mutation(command: ParsedCommand) -> bool:
        """Проверяет, содержит ли детерминированная команда доказанную мутацию заказа."""
        if command.intent is Intent.EDIT_COMMENT:
            return True
        if command.intent is not Intent.ADD_ITEMS:
            return False
        return command.explicit_add_items or any(
            item.quantity is not None and bool(item.unit) for item in command.items
        )

    @staticmethod
    def _safe_delivery_wish_command(text: str, state: ConversationState) -> ParsedCommand:
        """Не допускает историю и просит уточнить область пожелания о доставке."""
        if any(item.status is not ItemStatus.SKIPPED for item in state.cart):
            return ParsedCommand(
                intent=Intent.ADD_ITEMS,
                text=text,
                comment_clarification=text,
            )
        return ParsedCommand(intent=Intent.UNKNOWN, text=text)

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
            deterministic_scope = resolve_comment_scope_text(text, scope_items)
            if deterministic_scope is not None:
                action, target_indexes, confidence = deterministic_scope
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
    def _parse_quantity_modal_reply(
        text: str,
        state: ConversationState,
    ) -> ParsedCommand | None:
        """Распознаёт короткое количество только в открытом modal-количестве."""
        return PendingQuantityHandler.modal_command(text, state)

    def _candidate_command_is_authorized(
        self,
        command: ParsedCommand,
        state: ConversationState,
    ) -> bool:
        """Проверяет, открыта ли карточка, в которой разрешён выбор кандидата."""
        if command.intent is not Intent.SELECT_CANDIDATE:
            return True
        decision = self.state_compatibility_policy.evaluate(
            command,
            state,
            CompatibilityContext.CANDIDATE_SELECTION,
        )
        return decision.action is not CompatibilityAction.NOT_APPLICABLE

    def _authorize_candidate_command(
        self,
        command: ParsedCommand,
        state: ConversationState,
        text: str,
    ) -> ParsedCommand:
        """Не разрешает глобальному parser выбирать кандидата вне его карточки."""
        if self._candidate_command_is_authorized(command, state):
            return command
        return ParsedCommand(intent=Intent.UNKNOWN, text=text)

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
