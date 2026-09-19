"""Предоставляет интеграцию «openai client»."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, cast

import structlog
from openai import APIConnectionError, APITimeoutError, OpenAI, RateLimitError

from restaurant_bot.catalog.safety import (
    has_product_variant_qualifier,
    is_standalone_product_form_query,
)
from restaurant_bot.config import Settings
from restaurant_bot.domain.models import ExtractedItem, Intent, ParsedCommand
from restaurant_bot.domain.text import clean_text, normalize_text
from restaurant_bot.domain.units import UNIT_ALIASES, normalize_unit
from restaurant_bot.input.photo_ingestion import (
    classify_photo_document,
    normalize_photo_observation,
    photo_order_area_integrity,
    photo_sheet_row_mapping_is_authoritative,
)
from restaurant_bot.input.photo_views import PhotoImagePreparation, prepare_photo_views
from restaurant_bot.integrations.openai_prompts import (
    _COMMENT_SCOPE_SYSTEM,
    _MATCH_SYSTEM,
    _PHOTO_OBSERVATION_CONTRACT,
    _PHOTO_SYSTEM,
    _TEXT_SYSTEM,
    _VISIBLE_ACTION_SYSTEM,
)
from restaurant_bot.observability import Tracer
from restaurant_bot.parsing.ai.comment_reconciliation import (
    _CONVERSATIONAL_PRODUCT_LEADIN_RE,
    _comment_scope_has_explicit_anchor,
    _strip_conversational_product_leadin,
)
from restaurant_bot.parsing.ai.item_reconciliation import _repair_command_mixed_script_queries
from restaurant_bot.parsing.ai.quantity_reconciliation import (
    _quantities_with_units,
    _spoken_pair_quantity_for_item,
    restore_explicit_order_terms,
)
from restaurant_bot.parsing.ai.reconciliation import recover_omitted_explicit_items
from restaurant_bot.parsing.ai.schemas import (
    CommentBindingSchema,
    CommentScopeDecision,
    ParsedInputSchema,
    PhotoDocumentObservation,
    PhotoRowObservation,
    ProductMatchDecision,
    VisibleActionDecision,
)
from restaurant_bot.parsing.commands.api import infer_intent
from restaurant_bot.parsing.commands.dialogue import (
    dialogue_response_for,
    retry_requested_for,
)
from restaurant_bot.parsing.commands.item_commands import (
    explicit_add_item_target,
    has_explicit_add_items,
)
from restaurant_bot.parsing.comment_scope import has_explicit_global_comment_scope
from restaurant_bot.parsing.numeric import to_float
from restaurant_bot.parsing.numeric_ranges import numeric_range_spans
from restaurant_bot.parsing.semantic.measurements import (
    has_spoken_pair_marker,
    strip_authorized_spoken_pair_marker,
    strip_order_quantity_from_query,
)
from restaurant_bot.parsing.semantic_routing import (
    classify_bot_conversation,
    normalize_comment_proposal,
    protect_bot_conversation,
    protect_confirmed_command,
)

__all__ = [
    "CommentBindingSchema",
    "CommentScopeDecision",
    "OpenAIService",
    "ParsedInputSchema",
    "ProductMatchDecision",
    "VisibleActionDecision",
    "recover_omitted_explicit_items",
    "restore_explicit_order_terms",
]

_LARGE_ORDER_LIST_MIN_LINES = 10
_LARGE_ORDER_LIST_CHUNK_SIZE = 8

logger = structlog.get_logger(__name__)


def _photo_observation_matches_preprocessed_count(
    observation: PhotoDocumentObservation,
    expected_filled_order_row_count: int | None,
) -> bool:
    """Сверяет vision-строки с независимым пиксельным подсчётом order-строк."""
    if expected_filled_order_row_count is None:
        return True
    if len(observation.rows) != expected_filled_order_row_count:
        return False
    reported = observation.visible_filled_order_row_count
    return reported is None or reported == expected_filled_order_row_count


def _photo_observation_needs_second_pass(
    observation: PhotoDocumentObservation,
    *,
    require_sheet_row_numbers: bool = False,
    expected_filled_order_row_count: int | None = None,
) -> bool:
    """Определяет, нужен ли повторный vision-проход для неполной привязки заказа."""
    sheet_rows_incomplete = (
        require_sheet_row_numbers
        and not photo_sheet_row_mapping_is_authoritative(
            observation,
            require_sheet_row_numbers=require_sheet_row_numbers,
        )
    )
    integrity = photo_order_area_integrity(
        observation,
        classify_photo_document(observation),
    )
    preprocessed_count_mismatch = not _photo_observation_matches_preprocessed_count(
        observation,
        expected_filled_order_row_count,
    )
    if preprocessed_count_mismatch and expected_filled_order_row_count is not None:
        return True
    return (
        observation.document_type_proposal
        in {"client_order_sheet", "order_table", "printed_order_form"}
        and bool(observation.rows)
        and (
            not observation.order_area_complete
            or observation.uncertain_order_row_count > 0
            or sheet_rows_incomplete
            or integrity.reason == "filled_order_row_count_mismatch"
        )
    )


def _photo_observation_is_complete(
    observation: PhotoDocumentObservation,
    *,
    require_sheet_row_numbers: bool = False,
    expected_filled_order_row_count: int | None = None,
) -> bool:
    """Проверяет, что повторное наблюдение не содержит неопределённых строк заказа."""
    integrity = photo_order_area_integrity(
        observation,
        classify_photo_document(observation),
        require_sheet_row_numbers=require_sheet_row_numbers,
    )
    sheet_rows_complete = not require_sheet_row_numbers or photo_sheet_row_mapping_is_authoritative(
        observation,
        require_sheet_row_numbers=require_sheet_row_numbers,
    )
    return (
        observation.order_area_complete
        and observation.uncertain_order_row_count == 0
        and integrity.reason != "filled_order_row_count_mismatch"
        and _photo_observation_matches_preprocessed_count(
            observation,
            expected_filled_order_row_count,
        )
        and sheet_rows_complete
    )


class OpenAIService:
    """Выполняет распознавание, анализ фото и сопоставление товаров."""

    def __init__(self, settings: Settings):
        """Инициализирует компонент."""
        self.settings = settings
        self.tracer = Tracer(settings)
        self.client = OpenAI(
            api_key=settings.openai_api_key.get_secret_value(),
            timeout=settings.openai_text_timeout_seconds,
            max_retries=settings.openai_text_max_retries,
        )
        self.vision_client = OpenAI(
            api_key=settings.openai_api_key.get_secret_value(),
            timeout=settings.openai_vision_timeout_seconds,
            max_retries=0,
        )

    @staticmethod
    def _usage_details(response: Any) -> dict[str, Any] | None:
        """Возвращает непересекающиеся usage-бакеты для расчёта цены Langfuse."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        if isinstance(usage, dict):
            payload = dict(usage)
        elif hasattr(usage, "model_dump"):
            payload = usage.model_dump(exclude_none=True)
        else:
            payload = {
                key: value
                for key, value in vars(usage).items()
                if value is not None and not key.startswith("_")
            }
        input_total = int(payload.get("input_tokens") or payload.get("prompt_tokens") or 0)
        output_total = int(payload.get("output_tokens") or payload.get("completion_tokens") or 0)
        total = int(payload.get("total_tokens") or input_total + output_total)
        raw_input_details = (
            payload.get("input_tokens_details")
            or payload.get("input_token_details")
            or payload.get("prompt_tokens_details")
            or {}
        )
        if hasattr(raw_input_details, "model_dump"):
            raw_input_details = raw_input_details.model_dump(exclude_none=True)
        cached = int((raw_input_details or {}).get("cached_tokens") or 0)
        cached = min(max(cached, 0), input_total)

        normalized: dict[str, int] = {
            "input": input_total - cached,
            "output": output_total,
            "total": total,
        }
        if cached:
            normalized["input_cached_tokens"] = cached
        return normalized

    def parse_text(self, text: str) -> ParsedCommand:
        """Разбирает обычное сообщение или длинный список ограниченными вызовами AI."""
        chunks = self._large_order_list_chunks(text)
        if chunks:
            return self._parse_large_order_list(text, chunks)
        return self._parse_text_once(text)

    @staticmethod
    def _large_order_list_chunks(text: str) -> list[str]:
        """Выделяет только однозначный построчный список без догадки о количестве."""
        lines = [
            clean_text(line) for line in re.split(r"[\r\n;]+", str(text or "")) if clean_text(line)
        ]
        if len(lines) < _LARGE_ORDER_LIST_MIN_LINES:
            return []
        unit_pattern = "|".join(
            sorted((re.escape(unit) for unit in UNIT_ALIASES), key=len, reverse=True)
        )
        explicit_line = re.compile(
            rf"^.+?\s[-—–:]\s*\d+(?:[,.]\d+)?\s*(?:{unit_pattern})?\s*$",
            re.IGNORECASE,
        )
        comment_line = ""
        order_lines = lines
        if not all(explicit_line.fullmatch(line) for line in lines):
            if (
                lines
                and has_explicit_global_comment_scope(lines[-1])
                and all(explicit_line.fullmatch(line) for line in lines[:-1])
            ):
                comment_line = lines[-1]
                order_lines = lines[:-1]
            else:
                return []
        chunks = [
            "\n".join(order_lines[offset : offset + _LARGE_ORDER_LIST_CHUNK_SIZE])
            for offset in range(0, len(order_lines), _LARGE_ORDER_LIST_CHUNK_SIZE)
        ]
        if comment_line and chunks:
            chunks[-1] = f"{chunks[-1]}\n{comment_line}"
        return chunks

    def _parse_large_order_list(self, source_text: str, chunks: list[str]) -> ParsedCommand:
        """Разбирает части списка и отклоняет неполные или догаданные результаты."""
        commands: list[ParsedCommand] = []
        for index, chunk in enumerate(chunks, start=1):
            command = self._parse_text_once(chunk, force_ai=True)
            if command.intent != Intent.ADD_ITEMS or not command.items:
                logger.warning(
                    "large_order_list_chunk_unrecognized",
                    chunk_index=index,
                    chunk_count=len(chunks),
                    item_count=len(command.items),
                )
                return ParsedCommand(intent=Intent.UNKNOWN, text=source_text)
            commands.append(command)
        items = [item for command in commands for item in command.items]
        comments = [command.global_comment for command in commands if command.global_comment]
        confidence_values = [
            command.confidence for command in commands if command.confidence is not None
        ]
        result = ParsedCommand(
            intent=Intent.ADD_ITEMS,
            explicit_add_items=has_explicit_add_items(source_text, items),
            text=source_text,
            items=items,
            global_comment=comments[-1] if comments else "",
            confidence=min(confidence_values) if confidence_values else None,
        )
        logger.info(
            "large_order_list_parsed",
            chunk_count=len(chunks),
            item_count=len(items),
        )
        return _repair_command_mixed_script_queries(result)

    def _parse_text_once(self, text: str, *, force_ai: bool = False) -> ParsedCommand:
        """Извлекает структурированную команду из текста."""
        deterministic = infer_intent(text)
        conversation_intent = classify_bot_conversation(text)
        if not force_ai and self._looks_like_support_message(text):
            logger.info("text_support_message_detected", text=text)
            return ParsedCommand(intent=Intent.SMALL_TALK, text=text)
        if (
            not force_ai
            and conversation_intent is None
            and deterministic.intent not in {Intent.UNKNOWN, Intent.ADD_ITEMS}
        ):
            logger.info(
                "text_command_deterministic",
                text=text,
                intent=deterministic.intent.value,
                item_count=len(deterministic.items),
            )
            return _repair_command_mixed_script_queries(deterministic)
        if (
            not force_ai
            and conversation_intent is None
            and self._can_use_deterministic_product_list(text, deterministic)
        ):
            logger.info(
                "text_product_list_deterministic",
                text=text,
                intent=deterministic.intent.value,
                items=[self._item_log(item) for item in deterministic.items],
            )
            return _repair_command_mixed_script_queries(deterministic)
        if (
            not force_ai
            and conversation_intent is None
            and self._can_use_deterministic_single_product_with_quantity(text, deterministic)
        ):
            logger.info(
                "text_single_product_deterministic",
                text=text,
                intent=deterministic.intent.value,
                items=[self._item_log(item) for item in deterministic.items],
            )
            return _repair_command_mixed_script_queries(deterministic)
        if (
            not force_ai
            and conversation_intent is None
            and self._can_use_deterministic_short_product(text, deterministic)
        ):
            logger.info(
                "text_short_product_deterministic",
                text=text,
                intent=deterministic.intent.value,
                items=[self._item_log(item) for item in deterministic.items],
            )
            return _repair_command_mixed_script_queries(deterministic)
        if (
            not force_ai
            and conversation_intent is None
            and self._can_use_deterministic_packaged_product(text, deterministic)
        ):
            logger.info(
                "text_packaged_product_deterministic",
                text=text,
                intent=deterministic.intent.value,
                items=[self._item_log(item) for item in deterministic.items],
            )
            return _repair_command_mixed_script_queries(deterministic)
        if not force_ai and conversation_intent is None and self._should_skip_ai(text):
            logger.info(
                "text_ai_skipped",
                text=text,
                intent=deterministic.intent.value,
                item_count=len(deterministic.items),
            )
            return _repair_command_mixed_script_queries(deterministic)
        try:
            with self.tracer.generation(
                "openai.parse_text",
                model=self.settings.openai_text_model,
                input={"characters": len(text), "kind": "text"},
                metadata={"feature": "order_parser"},
            ) as generation:
                response = self.client.responses.parse(
                    model=self.settings.openai_text_model,
                    instructions=_TEXT_SYSTEM,
                    input=text,
                    text_format=ParsedInputSchema,
                )
                generation.update(
                    output={"parsed": response.output_parsed is not None},
                    usage_details=self._usage_details(response),
                )
        except (APIConnectionError, APITimeoutError, RateLimitError) as error:
            fallback = self._deterministic_transport_fallback(
                text, deterministic, force_ai=force_ai
            )
            logger.warning(
                "text_ai_transport_failed",
                text=text,
                error_type=type(error).__name__,
                deterministic_intent=deterministic.intent.value,
                deterministic_item_count=len(deterministic.items),
                deterministic_fallback_used=fallback is not None,
            )
            if fallback is not None:
                logger.warning(
                    "text_ai_transport_fallback",
                    text=text,
                    intent=fallback.intent.value,
                    item_count=len(fallback.items),
                )
                return fallback
            raise
        parsed = response.output_parsed
        if parsed is None:
            logger.warning("text_ai_empty_result", text=text)
            if conversation_intent is not None:
                return ParsedCommand(intent=conversation_intent, text=text)
            if force_ai:
                return ParsedCommand(intent=Intent.UNKNOWN, text=text)
            return _repair_command_mixed_script_queries(deterministic)
        logger.info(
            "text_ai_parsed",
            text=text,
            intent=parsed.intent.value,
            global_comment=parsed.global_comment,
            comment_bindings=[binding.model_dump() for binding in parsed.comment_bindings],
            items=[self._item_log(item) for item in parsed.items],
        )
        payload = parsed.model_dump()
        if parsed.history_query is not None:
            payload["intent"] = Intent.HISTORY_QUERY
            payload["items"] = []
        payload = recover_omitted_explicit_items(payload, text)
        for item in payload.get("items", []):
            if item.get("user_comment_to_supplier") and not item.get("comment"):
                item["comment"] = item["user_comment_to_supplier"]
        command = ParsedCommand.model_validate(payload)
        if command.history_query is not None:
            command = command.model_copy(
                update={
                    "intent": Intent.HISTORY_QUERY,
                    "items": [],
                    "explicit_add_items": False,
                }
            )
        command = command.model_copy(
            update={
                "explicit_add_items": has_explicit_add_items(text, payload.get("items", []))
                if command.intent is Intent.ADD_ITEMS
                else False,
                "retry_requested": retry_requested_for(text),
            }
        )
        command = normalize_comment_proposal(text, command)
        command = protect_bot_conversation(text, command)
        proven_deterministic_items = (
            deterministic.intent is Intent.ADD_ITEMS
            and deterministic.items
            and (
                deterministic.explicit_add_items
                or any(
                    item.quantity is not None and bool(item.unit) for item in deterministic.items
                )
            )
        )
        if proven_deterministic_items and conversation_intent is None:
            protected = protect_confirmed_command(deterministic, command)
            if protected is not command:
                logger.warning(
                    "text_ai_semantic_route_rejected",
                    text=text,
                    ai_intent=command.intent.value,
                    fallback_intent=deterministic.intent.value,
                    fallback_items=[self._item_log(item) for item in deterministic.items],
                )
                command = protected
        if (
            command.intent == Intent.ADD_MORE
            and deterministic.intent == Intent.ADD_ITEMS
            and deterministic.items
            and not force_ai
            and conversation_intent is None
        ):
            logger.warning(
                "text_ai_navigation_rejected",
                text=text,
                ai_intent=command.intent.value,
                fallback_intent=deterministic.intent.value,
                fallback_items=[self._item_log(item) for item in deterministic.items],
            )
            command = deterministic
        command = command.model_copy(
            update={
                "dialogue_response": dialogue_response_for(
                    text,
                    command.intent,
                    command.items,
                )
            }
        )
        logger.info(
            "text_command_normalized",
            intent=command.intent.value,
            items=[self._item_log(item) for item in command.items],
        )
        return _repair_command_mixed_script_queries(command)

    @staticmethod
    def _can_use_deterministic_product_list(text: str, command: ParsedCommand) -> bool:
        """Проверяет возможность разбора списка без вызова ИИ."""
        if command.intent != Intent.ADD_ITEMS or len(command.items) < 2:
            return False
        explicit_target = explicit_add_item_target(text)
        # Числовой диапазон может быть размером, фасовкой или кодом товара.
        # Оставляем такие строки на семантическом пути, чтобы признаки
        # поставщика и товара не слились в одно детерминированное название.
        if numeric_range_spans(text):
            return False
        if OpenAIService._has_conversational_product_leadin(text) and not explicit_target:
            return False

        raw = str(explicit_target or text or "").strip().lower().replace("ё", "е")
        segments = [
            clean_text(part)
            for part in re.split(r"\s*(?:;|\n|(?<!\d),(?!\d)|\s+и\s+)\s*", raw)
            if clean_text(part)
        ]
        if len(segments) != len(command.items):
            return False

        unit_pattern = "|".join(
            sorted((re.escape(unit) for unit in UNIT_ALIASES), key=len, reverse=True)
        )
        item_pattern = re.compile(
            rf"^(?P<name>.+?)\s*(?:[-—–:]\s*)?(?P<quantity>\d+(?:[,.]\d+)?)\s*"
            rf"(?P<unit>{unit_pattern})$",
            re.IGNORECASE,
        )
        suspicious_words = re.compile(
            r"\b(?:только|срочно|завтра|сегодня|охлажден\w*|заморож\w*|"
            r"без|пожалуйста|коммент\w*|достав\w*|позвон\w*)\b"
        )

        for segment, item in zip(segments, command.items, strict=True):
            match = item_pattern.fullmatch(segment)
            if match is None:
                return False
            name = normalize_text(match.group("name")).strip(" -:—–")
            if not name or re.search(r"\d", name) or suspicious_words.search(name):
                return False
            if has_product_variant_qualifier(name) and not is_standalone_product_form_query(name):
                # Оставляем варианты товара на семантическом пути с проверкой каталога.  # noqa: RUF003
                return False
            if normalize_text(item.product_query) != name:
                return False
            if item.quantity is None or not item.unit or clean_text(item.comment):
                return False
            if abs(item.quantity - float(match.group("quantity").replace(",", "."))) > 1e-9:
                return False
            if normalize_unit(match.group("unit")) != normalize_unit(item.unit):
                return False
        return True

    @staticmethod
    def _deterministic_transport_fallback(
        text: str,
        command: ParsedCommand,
        *,
        force_ai: bool,
    ) -> ParsedCommand | None:
        """Разрешает резервный ответ после сбоя AI только для одной однозначной позиции."""
        if force_ai or command.intent != Intent.ADD_ITEMS or len(command.items) != 1:
            return None
        if numeric_range_spans(text) or command.global_comment:
            return None
        item = command.items[0]
        pair_quantity = _spoken_pair_quantity_for_item(text, item.product_query)
        if pair_quantity is not None:
            quantity, unit = pair_quantity
            if clean_text(item.comment) or clean_text(item.user_comment_to_supplier):
                return None
        else:
            quantities = _quantities_with_units(text)
            if (
                item.quantity is None
                or item.quantity <= 0
                or not normalize_unit(item.unit)
                or clean_text(item.comment)
                or clean_text(item.user_comment_to_supplier)
                or len(quantities) != 1
            ):
                return None
            quantity, unit = quantities[0]
            if abs(float(item.quantity) - quantity) > 1e-9 or normalize_unit(item.unit) != unit:
                return None
        query = _strip_conversational_product_leadin(item.product_query)
        query = strip_order_quantity_from_query(query, text)
        if pair_quantity is not None:
            query = strip_authorized_spoken_pair_marker(query, text)
        query = re.sub(
            r"\s+\b(?:хочу|хотим|нужно|надо)\b(?=\s+\d|\s*$)",
            "",
            query,
            flags=re.IGNORECASE,
        ).strip(" .,;:-—–")
        if not query or not re.search(r"[a-zа-яё]", normalize_text(query), flags=re.IGNORECASE):
            return None
        item = item.model_copy(update={"quantity": quantity, "unit": unit, "product_query": query})
        return _repair_command_mixed_script_queries(command.model_copy(update={"items": [item]}))

    @staticmethod
    def _can_use_deterministic_single_product_with_quantity(
        text: str,
        command: ParsedCommand,
    ) -> bool:
        """Проверяет однозначный одиночный товар с количеством."""
        if command.intent != Intent.ADD_ITEMS or len(command.items) != 1:
            return False
        explicit_target = explicit_add_item_target(text)
        if OpenAIService._has_conversational_product_leadin(text) and not explicit_target:
            return False
        item = command.items[0]
        normalized_units = set(UNIT_ALIASES.values())
        raw_source = clean_text(explicit_target or text)
        source = raw_source.rstrip(" .!?")
        item_source = clean_text(item.source_line).rstrip(" .!?")
        if numeric_range_spans(source):
            # Не обходить семантический разбор: после числового диапазона здесь  # noqa: RUF003
            # могут находиться и название поставщика, и признаки товара.
            return False
        if has_product_variant_qualifier(
            item.product_query
        ) and not is_standalone_product_form_query(item.product_query):
            return False
        unit_pattern = "|".join(
            sorted((re.escape(unit) for unit in UNIT_ALIASES), key=len, reverse=True)
        )
        has_numeric_order_quantity = bool(
            re.search(
                rf"\d+(?:[,.]\d+)?\s*(?:{unit_pattern})\s*$",
                source,
                flags=re.I,
            )
        )
        return bool(
            source
            and raw_source.endswith((".", "!", "?"))
            and item_source == source
            and has_numeric_order_quantity
            and len(_quantities_with_units(source)) == 1
            and item.product_query
            and item.quantity is not None
            and item.quantity > 0
            and normalize_unit(item.unit) in normalized_units
            and not clean_text(item.comment)
            and not clean_text(item.user_comment_to_supplier)
            and not command.global_comment
        )

    @staticmethod
    def _can_use_deterministic_short_product(text: str, command: ParsedCommand) -> bool:
        """Проверяет короткое название товара без количества."""
        if command.intent != Intent.ADD_ITEMS or len(command.items) != 1:
            return False
        explicit_target = explicit_add_item_target(text)
        if OpenAIService._has_conversational_product_leadin(text) and not explicit_target:
            return False
        product_source = explicit_target or text
        if OpenAIService._looks_like_semantic_sentence(product_source):
            return False
        # Разговорное «пару/пара» требует AI-сверки: детерминированный короткий
        # путь не должен принять количество за часть названия товара.
        if has_spoken_pair_marker(product_source):
            return False
        item = command.items[0]
        query = clean_text(item.product_query)
        has_global_scope = bool(
            re.search(
                r"\b(?:все|всё|всем|всей|вся|весь|общий|общая|обоим|обеим|каждому)\b",
                normalize_text(text),
            )
        )
        return bool(
            query
            and len(query.split()) <= 4
            and not OpenAIService._looks_like_support_message(text)
            and not has_global_scope
            and item.quantity is None
            and not item.unit
            and not item.comment
            and not command.global_comment
            and normalize_text(query) == normalize_text(product_source)
            and (
                not has_product_variant_qualifier(query) or is_standalone_product_form_query(query)
            )
        )

    @staticmethod
    def _looks_like_semantic_sentence(text: str) -> bool:
        """Отправляет вопросительные и разговорные фразы на семантический разбор."""
        value = clean_text(text)
        normalized = normalize_text(value)
        if not normalized:
            return False
        if value.endswith(("?", "!")) or any(mark in value for mark in ",;:"):
            return True
        if re.search(r"\b(?:ты|тебе|тебя|тобой|вы|вам|мне|я|мы)\b", normalized):
            return True
        return bool(
            re.match(
                r"^(?:как|что|кто|где|когда|почему|зачем|можешь|можно|подскажи|"
                r"объясни|расскажи|помоги|скажи)\b",
                normalized,
            )
        )

    @staticmethod
    def _looks_like_support_message(text: str) -> bool:
        """Определяет жалобу на работу бота, которую должен разобрать ИИ."""
        normalized = normalize_text(text)
        return bool(
            re.search(
                r"(?:^|\s)(?:"
                r"не\s+(?:работает|срабатывает|получается|отвечает|добавляется|находит\w*)"
                r"|не\s+могу\s+(?:добавить|найти|удалить|отправить|оформить|проверить)"
                r"|ничего\s+не\s+(?:работает|происходит|добавляется|отправляется)"
                r"|бот\s+(?:не\s+)?(?:сломал\w*|завис\w*|глючит|молчит)"
                r"|ошибк\w*|сбой\w*|проблем\w*|сломал\w*|завис\w*"
                r")(?:\s|$|[.!?,])",
                normalized,
            )
        )

    @staticmethod
    def _has_conversational_product_leadin(text: str) -> bool:
        """Определяет разговорную вводную, которая не является частью названия товара."""
        return bool(_CONVERSATIONAL_PRODUCT_LEADIN_RE.match(normalize_text(text)))

    @staticmethod
    def _can_use_deterministic_packaged_product(text: str, command: ParsedCommand) -> bool:
        """Распознаёт одно точное название с компактной записью фасовки."""
        if command.intent != Intent.ADD_ITEMS or len(command.items) != 1:
            return False
        explicit_target = explicit_add_item_target(text)
        if OpenAIService._has_conversational_product_leadin(text) and not explicit_target:
            return False
        product_source = explicit_target or text
        if "\n" in str(product_source or "") or ";" in str(product_source or ""):
            return False
        unit_pattern = "|".join(
            sorted((re.escape(unit) for unit in UNIT_ALIASES), key=len, reverse=True)
        )
        return bool(
            re.search(
                rf"\d+(?:[,.]\d+)?\s*(?:{unit_pattern})\s*[*xх×]\s*"
                rf"\d+(?:[,.]\d+)?",
                product_source,
                flags=re.I,
            )
        )

    @staticmethod
    def _should_skip_ai(text: str) -> bool:
        """Проверяет возможность пропустить вызов ИИ."""
        value = clean_text(text).lower()
        return bool(
            not value
            or value.startswith("/")
            or re.fullmatch(
                r"\d+(?:[,.]\d+)?\s*(шт|штук|штуки|штука|кг|килограмм(?:а|ов)?|гр|г|л|литр(?:а|ов)?|мл|уп|упак|кор|ведро|пакет|бут|банка|банки)?",
                value,
            )
        )

    def _transcription_trace_output(self, text: str) -> dict[str, Any]:
        """Добавляет расшифровку в трассировку только при явном разрешении."""
        output: dict[str, Any] = {"transcript_characters": len(text)}
        if self.settings.log_user_content:
            output["transcript"] = text[: self.settings.log_content_max_length]
        return output

    def transcribe(self, path: Path, prompt: str = "", high_accuracy: bool = False) -> str:
        """Распознаёт голосовое сообщение Telegram."""
        request: dict[str, Any] = {
            "model": (
                self.settings.openai_transcribe_fallback_model
                if high_accuracy
                else self.settings.openai_transcribe_model
            ),
            "file": None,
            "response_format": "json",
            "language": "ru",
        }
        with path.open("rb") as audio:
            request["file"] = audio
            if prompt:
                request["prompt"] = prompt
            with self.tracer.generation(
                "openai.transcribe",
                model=str(request["model"]),
                input={
                    "audio_bytes": path.stat().st_size,
                    "prompt_characters": len(prompt),
                    "kind": "voice",
                },
                metadata={"feature": "voice_transcription", "fallback": False},
            ) as generation:
                result = self.client.audio.transcriptions.create(**request)
                text = clean_text(getattr(result, "text", ""))
                generation.update(
                    output=self._transcription_trace_output(text),
                    usage_details=self._usage_details(result),
                )
        if text:
            logger.info(
                "voice_transcription_completed",
                model=request["model"],
                transcript=text,
                fallback_used=False,
            )
            return text
        primary_model = str(request["model"])
        fallback_model = (
            self.settings.openai_transcribe_model
            if high_accuracy
            else self.settings.openai_transcribe_fallback_model
        )
        if (
            not fallback_model
            or primary_model.strip().casefold() == str(fallback_model).strip().casefold()
        ):
            logger.warning(
                "voice_transcription_fallback_skipped",
                reason="same_model",
                model=primary_model,
            )
            return ""
        with path.open("rb") as audio:
            request["model"] = fallback_model
            request["file"] = audio
            with self.tracer.generation(
                "openai.transcribe",
                model=str(request["model"]),
                input={
                    "audio_bytes": path.stat().st_size,
                    "prompt_characters": len(prompt),
                    "kind": "voice",
                },
                metadata={"feature": "voice_transcription", "fallback": True},
            ) as generation:
                fallback = self.client.audio.transcriptions.create(**request)
                fallback_text = clean_text(getattr(fallback, "text", ""))
                generation.update(
                    output=self._transcription_trace_output(fallback_text),
                    usage_details=self._usage_details(fallback),
                )
        logger.info(
            "voice_transcription_completed",
            model=request["model"],
            transcript=fallback_text,
            fallback_used=True,
        )
        return fallback_text

    def parse_photo(
        self,
        path: Path,
        mime_type: str,
        caption: str = "",
    ) -> ParsedCommand:
        """Извлекает товары из фотографии."""
        preparation = prepare_photo_views(path, mime_type)
        expected_filled_order_row_count = preparation.detected_filled_order_row_count
        table_focus_view_size = preparation.table_focus_view_size
        logger.info(
            "photo_image_views_prepared",
            original_width=preparation.original_width,
            original_height=preparation.original_height,
            view_count=len(preparation.views),
            table_focus_view_size=(list(table_focus_view_size) if table_focus_view_size else None),
            upscale_factor=preparation.upscale_factor,
            dense_table_views_used=preparation.dense_table_views_used,
            spreadsheet_layout_detected=preparation.spreadsheet_layout_detected,
            detected_filled_order_row_count=expected_filled_order_row_count,
        )
        input_text = caption or "Распознай заявку на фото"
        if preparation.dense_table_views_used:
            input_text = (
                f"{input_text}\n\n"
                "В запросе показан один канонический reading view исходного документа: он может "
                "содержать весь кадр или только безопасно выделенную область таблицы. "
                "Это может быть фотография бумажного документа, прямой скриншот таблицы или "
                "скриншот экрана/чата, внутри которого видна таблица. Игнорируй браузер, вкладки, "
                "панели чата, уменьшенные превью и сводные колонки; извлекай данные из самой "
                "крупной читаемой области документа. "
                "Используй таблицу как единственный источник геометрии: сначала установи полосы "
                "строк сверху вниз, не начинай с первой заполненной цифры, не переноси верхнюю "
                "заполненную ячейку на строку выше и не пропускай первую видимую товарную строку. "
                "Сопоставляй товар, количество и комментарий только внутри одной визуальной строки "
                "и соблюдай контракт PhotoDocumentObservation."
            )
        if preparation.spreadsheet_layout_detected:
            input_text = (
                f"{input_text}\n\n"
                "Детерминированная подготовка изображения обнаружила структуру электронной "
                "таблицы. Верни только строки с заполненным количеством заказа. Номера строк "
                "могут отсутствовать из-за обрезки изображения и не являются обязательными: "
                "связывай товар, количество и комментарий по одной визуальной строке."
            )
        observation, response_complete = self._request_photo_observation(
            path,
            mime_type,
            caption,
            preparation,
            input_text,
            pass_number=1,
        )
        if observation is None:
            logger.warning("photo_ai_empty_result", mime_type=mime_type)
            return ParsedCommand(intent=Intent.ADD_ITEMS, photo_outcome="incomplete_photo_read")
        if not response_complete:
            logger.warning("photo_ai_incomplete_result", mime_type=mime_type)
            return ParsedCommand(intent=Intent.ADD_ITEMS, photo_outcome="incomplete_photo_read")

        authoritative_sheet_rows = photo_sheet_row_mapping_is_authoritative(
            observation,
            require_sheet_row_numbers=True,
        )
        if _photo_observation_needs_second_pass(
            observation,
            expected_filled_order_row_count=expected_filled_order_row_count,
        ):
            preprocess_count_matches = _photo_observation_matches_preprocessed_count(
                observation,
                expected_filled_order_row_count,
            )
            logger.info(
                "photo_ai_second_pass_started",
                reason=(
                    "order_area_uncertain"
                    if preprocess_count_matches
                    else "preprocessed_filled_row_count_mismatch"
                ),
                row_count=len(observation.rows),
                uncertain_order_row_count=observation.uncertain_order_row_count,
                require_sheet_row_numbers=False,
            )
            retry_preparation = prepare_photo_views(
                path,
                mime_type,
                include_original=True,
                prefer_filled_order_rows=True,
            )
            retry_text = (
                f"{input_text}\n\n"
                "Повтори проверку по оригиналу и увеличенному виду. "
                "Считай неопределённость только для видимой заполненной ячейки, "
                "которую нельзя привязать к строке товара. Пустые ячейки не являются "
                "ошибкой и не требуют пометки неполной области. Если слева видны номера "
                "строк таблицы, прочитай номер каждой возвращаемой строки по изображению; "
                "не вычисляй его из позиции строки в массиве. Если номер строки и прочитанное "
                "название могли попасть из разных горизонтальных полос, заново прочитай всю "
                "строку слева направо и не переноси количество или комментарий между строками."
            )
            retry_observation, retry_complete = self._request_photo_observation(
                path,
                mime_type,
                caption,
                retry_preparation,
                retry_text,
                pass_number=2,
            )
            if (
                retry_complete
                and retry_observation is not None
                and _photo_observation_is_complete(
                    retry_observation,
                    expected_filled_order_row_count=expected_filled_order_row_count,
                )
            ):
                observation = retry_observation
                authoritative_sheet_rows = photo_sheet_row_mapping_is_authoritative(
                    observation,
                    require_sheet_row_numbers=True,
                )
                logger.info(
                    "photo_ai_second_pass_selected",
                    row_count=len(observation.rows),
                    uncertain_order_row_count=observation.uncertain_order_row_count,
                    order_area_complete=observation.order_area_complete,
                    sheet_row_numbers_visible=observation.sheet_row_numbers_visible,
                )
            else:
                logger.warning(
                    "photo_ai_second_pass_not_selected",
                    expected_filled_order_row_count=expected_filled_order_row_count,
                )
                if expected_filled_order_row_count is not None:
                    return ParsedCommand(
                        intent=Intent.ADD_ITEMS,
                        photo_outcome="incomplete_photo_read",
                    )

        logger.info(
            "photo_ai_parsed",
            proposed_document_type=observation.document_type_proposal,
            visible_product_row_count=observation.visible_product_row_count,
            returned_row_count=len(observation.rows),
            order_area_complete=observation.order_area_complete,
            uncertain_order_row_count=observation.uncertain_order_row_count,
            scan_complete=observation.scan_complete,
            sheet_row_numbers_visible=observation.sheet_row_numbers_visible,
            visible_filled_order_row_count=observation.visible_filled_order_row_count,
            require_sheet_row_numbers=False,
        )
        normalization = normalize_photo_observation(
            observation,
            self.settings,
            require_sheet_row_numbers=authoritative_sheet_rows,
        )
        normalized = normalization.command
        logger.info(
            "photo_command_normalized",
            document_type=normalization.document_type,
            input_row_count=len(observation.rows),
            output_item_count=len(normalized.items),
            dropped_row_count=normalization.dropped_rows,
            reason=normalization.reason,
        )
        return normalized

    def _request_photo_observation(
        self,
        path: Path,
        mime_type: str,
        caption: str,
        preparation: PhotoImagePreparation,
        input_text: str,
        *,
        pass_number: int,
    ) -> tuple[PhotoDocumentObservation | None, bool]:
        """Выполняет один structured vision-запрос и возвращает его полноту."""
        import base64

        started_at = time.perf_counter()
        content: list[dict[str, Any]] = [{"type": "input_text", "text": input_text}]
        for view in preparation.views:
            encoded = base64.b64encode(view.data).decode("ascii")
            content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{view.mime_type};base64,{encoded}",
                    "detail": "high",
                }
            )
        with self.tracer.generation(
            "openai.parse_photo",
            model=self.settings.openai_vision_model,
            input={
                "image_bytes": path.stat().st_size,
                "caption_characters": len(caption),
                "mime_type": mime_type,
                "kind": "photo",
                "pass_number": pass_number,
            },
            metadata={"feature": "photo_order_parser", "pass_number": pass_number},
        ) as generation:
            request: dict[str, Any] = {
                "model": self.settings.openai_vision_model,
                "instructions": f"{_PHOTO_SYSTEM}\n\n{_PHOTO_OBSERVATION_CONTRACT}",
                "input": cast(
                    Any,
                    [
                        {
                            "role": "user",
                            "content": content,
                        }
                    ],
                ),
                "text_format": PhotoDocumentObservation,
                "max_output_tokens": (6000 if preparation.spreadsheet_layout_detected else 8000),
            }
            if self.settings.openai_vision_model.casefold().startswith("gpt-5"):
                request["reasoning"] = {"effort": "minimal"}
                request["text"] = {"verbosity": "low"}
            response = self.vision_client.responses.parse(
                **cast(Any, request),
            )
            parsed_output = response.output_parsed
            observation = (
                PhotoDocumentObservation.model_validate(parsed_output.model_dump())
                if parsed_output is not None
                else None
            )
            generation.update(
                output={
                    "parsed": observation is not None,
                    "row_count": len(observation.rows) if observation else 0,
                    "visible_product_row_count": (
                        observation.visible_product_row_count if observation else None
                    ),
                    "order_area_complete": observation.order_area_complete
                    if observation
                    else False,
                    "uncertain_order_row_count": (
                        observation.uncertain_order_row_count if observation else 0
                    ),
                    "scan_complete": observation.scan_complete if observation else False,
                    "sheet_row_numbers_visible": observation.sheet_row_numbers_visible
                    if observation
                    else False,
                },
                usage_details=self._usage_details(response),
            )
        complete = not (
            getattr(response, "status", "") == "incomplete"
            or getattr(response, "incomplete_details", None)
        )
        logger.info(
            "photo_ai_pass_finished",
            pass_number=pass_number,
            duration_ms=round((time.perf_counter() - started_at) * 1000),
            row_count=len(observation.rows) if observation else 0,
            spreadsheet_layout_detected=preparation.spreadsheet_layout_detected,
            complete=complete,
        )
        return observation, complete

    def _normalise_photo_command(self, command: ParsedCommand, document_type: str) -> ParsedCommand:
        """Адаптирует старый тестовый контракт к единому photo observation normalizer."""
        columns: list[str]
        has_table = document_type not in {
            "free_list",
            "unknown",
            "unknown_document",
            "product_card",
        }
        if document_type == "client_order_sheet":
            columns = ["Зал", "Бар", "Кухня"]
        elif document_type == "printed_order_form":
            columns = ["Товар", "Фасовка", "Заказ"]
        elif document_type == "order_table":
            columns = ["Товар", "Количество"]
        else:
            columns = []
        rows: list[PhotoRowObservation] = []
        for index, item in enumerate(command.items):
            explicit_quantity = None
            handwritten_text = ""
            if document_type == "free_list" and item.quantity_source not in {
                "packaging",
                "printed_reference",
                "unit_weight",
                "price",
            }:
                explicit_quantity = item.quantity
                handwritten_text = item.source_line if item.quantity_source == "handwritten" else ""
            elif document_type == "unknown" and item.quantity_source not in {
                "packaging",
                "printed_reference",
                "unit_weight",
                "price",
            }:
                explicit_quantity = None
            elif document_type not in {"client_order_sheet", "product_card"}:
                explicit_quantity = to_float(item.order_entry_text)
            rows.append(
                PhotoRowObservation(
                    row_index=index,
                    row_text=item.source_line,
                    product_text=item.product_query,
                    supplier_hint=item.supplier_hint,
                    hall_quantity=item.department_quantities.hall,
                    bar_quantity=item.department_quantities.bar,
                    kitchen_quantity=item.department_quantities.kitchen,
                    explicit_order_quantity=explicit_quantity,
                    explicit_order_unit=item.unit,
                    order_entry_text=item.order_entry_text,
                    order_entry_type=item.order_entry_type,
                    printed_reference_text=item.printed_reference_text or item.packaging_text,
                    handwritten_quantity_text=handwritten_text,
                    comment_text=item.comment or item.user_comment_to_supplier,
                    comment_source="explicit_marker" if item.comment else "",
                )
            )
        proposal = "unknown" if document_type == "unknown_document" else document_type
        observation = PhotoDocumentObservation(
            document_type_proposal=proposal,
            detected_columns=columns,
            has_table_structure=has_table,
            rows=rows,
        )
        return normalize_photo_observation(observation, self.settings).command

    @staticmethod
    def _item_log(item: ExtractedItem) -> dict[str, Any]:
        """Формирует диагностический снимок извлечённой позиции."""
        return {
            "product_query": item.product_query,
            "quantity": item.quantity,
            "unit": item.unit,
            "department": item.department,
            "supplier_hint": item.supplier_hint,
            "comment": item.comment,
            "source_line": item.source_line,
            "source_span": item.source_span,
            "source_department": item.source_department,
            "department_quantities": item.department_quantities.model_dump(),
            "quantity_source": item.quantity_source,
            "printed_reference_text": item.printed_reference_text,
            "order_entry_text": item.order_entry_text,
            "order_entry_type": item.order_entry_type,
            "packaging_text": item.packaging_text,
            "packaging_role": item.packaging_role,
            "packaging_confidence": item.packaging_confidence,
        }

    def choose_catalog_candidate(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        product_context: str = "",
    ) -> ProductMatchDecision:
        """Выбирает кандидата только из заданного списка."""
        with self.tracer.generation(
            "openai.choose_catalog_candidate",
            model=self.settings.openai_match_model,
            input={
                "query_characters": len(query),
                "context_characters": len(product_context),
                "candidate_count": len(candidates),
                "kind": "catalog_match",
            },
            metadata={"feature": "catalog_matching"},
        ) as generation:
            response = self.client.responses.parse(
                model=self.settings.openai_match_model,
                instructions=_MATCH_SYSTEM,
                input=json.dumps(
                    {
                        "query": query,
                        "product_context": product_context,
                        "candidates": candidates,
                    },
                    ensure_ascii=False,
                ),
                text_format=ProductMatchDecision,
            )
            generation.update(
                output={"parsed": response.output_parsed is not None},
                usage_details=self._usage_details(response),
            )
        decision = response.output_parsed or ProductMatchDecision(action="ambiguous")
        logger.info(
            "catalog_candidate_ai_decision",
            target_query=query,
            candidate_count=len(candidates),
            candidate_product_ids=[
                str(candidate.get("product_id") or "") for candidate in candidates
            ],
            action=decision.action,
            selected_product_id=decision.selected_product_id,
            confidence=decision.confidence,
            contradictions=decision.contradictions,
            reason=decision.reason,
        )
        return decision

    def choose_visible_action(
        self,
        text: str,
        screen_text: str,
        actions: list[dict[str, str]],
    ) -> str:
        """Выбирает голосовое действие только среди кнопок текущего экрана."""
        allowed = {
            str(action.get("action_id") or ""): str(action.get("label") or "")
            for action in actions
            if action.get("action_id") and action.get("label")
        }
        if not allowed:
            return ""
        with self.tracer.generation(
            "openai.choose_visible_action",
            model=self.settings.openai_text_model,
            input={
                "text_characters": len(text),
                "screen_characters": min(len(screen_text), 2000),
                "action_count": len(allowed),
                "kind": "visible_action",
            },
            metadata={"feature": "voice_navigation"},
        ) as generation:
            response = self.client.responses.parse(
                model=self.settings.openai_text_model,
                instructions=_VISIBLE_ACTION_SYSTEM,
                input=json.dumps(
                    {
                        "user_text": text,
                        "screen_text": screen_text[:2000],
                        "actions": [
                            {"action_id": action_id, "label": label}
                            for action_id, label in allowed.items()
                        ],
                    },
                    ensure_ascii=False,
                ),
                text_format=VisibleActionDecision,
            )
            generation.update(
                output={"parsed": response.output_parsed is not None},
                usage_details=self._usage_details(response),
            )
        decision = response.output_parsed or VisibleActionDecision()
        selected = decision.action_id if decision.action_id in allowed else ""
        if decision.confidence < 0.9:
            selected = ""
        logger.info(
            "visible_action_ai_decision",
            text=text,
            action_count=len(allowed),
            selected_action_id=selected,
            confidence=decision.confidence,
            reason=decision.reason,
        )
        return selected

    def resolve_comment_scope(
        self,
        text: str,
        item_names: list[str],
    ) -> CommentScopeDecision:
        """Определяет область ожидающего комментария среди переданных товаров."""
        indexed_items = [
            {"index": index, "name": clean_text(name)[:200]}
            for index, name in enumerate(item_names)
        ]
        with self.tracer.generation(
            "openai.resolve_comment_scope",
            model=self.settings.openai_text_model,
            input={
                "text_characters": len(text),
                "item_count": len(indexed_items),
                "kind": "comment_scope",
            },
            metadata={"feature": "comment_scope_clarification"},
        ) as generation:
            response = self.client.responses.parse(
                model=self.settings.openai_text_model,
                instructions=_COMMENT_SCOPE_SYSTEM,
                input=json.dumps(
                    {"user_text": text, "items": indexed_items},
                    ensure_ascii=False,
                ),
                text_format=CommentScopeDecision,
            )
            generation.update(
                output={"parsed": response.output_parsed is not None},
                usage_details=self._usage_details(response),
            )
        decision = response.output_parsed or CommentScopeDecision()
        if decision.action == "items" and not _comment_scope_has_explicit_anchor(text, item_names):
            decision = CommentScopeDecision(
                action="ambiguous",
                confidence=min(decision.confidence, 0.5),
                reason="В ответе нет явного названия, номера или охвата товаров.",
            )
        logger.info(
            "comment_scope_ai_decision",
            text=text,
            item_count=len(indexed_items),
            action=decision.action,
            target_item_indexes=decision.target_item_indexes,
            confidence=decision.confidence,
            reason=decision.reason,
        )
        return decision
