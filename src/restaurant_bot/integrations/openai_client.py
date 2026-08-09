from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

import structlog
from openai import APIConnectionError, APITimeoutError, OpenAI, RateLimitError

from restaurant_bot.config import Settings
from restaurant_bot.domain.models import ExtractedItem, Intent, ParsedCommand
from restaurant_bot.integrations.openai_parsing import (
    _CONVERSATIONAL_PRODUCT_LEADIN_RE,
    _LARGE_ORDER_LIST_CHUNK_SIZE,
    _LARGE_ORDER_LIST_MIN_LINES,
    CommentScopeDecision,
    ParsedInputSchema,
    ProductMatchDecision,
    VisibleActionDecision,
    _comment_scope_has_explicit_anchor,
    _quantities_with_units,
    _repair_command_mixed_script_queries,
    recover_omitted_explicit_items,
)
from restaurant_bot.integrations.openai_parsing import (
    CommentBindingSchema as CommentBindingSchema,
)
from restaurant_bot.integrations.openai_parsing import (
    restore_explicit_order_terms as restore_explicit_order_terms,
)
from restaurant_bot.integrations.openai_prompts import (
    _COMMENT_SCOPE_SYSTEM,
    _MATCH_SYSTEM,
    _PHOTO_SYSTEM,
    _TEXT_SYSTEM,
    _VISIBLE_ACTION_SYSTEM,
)
from restaurant_bot.observability import Tracer
from restaurant_bot.services.matching import has_product_variant_qualifier
from restaurant_bot.services.parser import (
    dialogue_response_for,
    has_explicit_add_items,
    has_explicit_global_comment_scope,
    infer_intent,
)
from restaurant_bot.services.text import (
    UNIT_ALIASES,
    clean_text,
    normalize_text,
    normalize_unit,
    numeric_range_spans,
    to_float,
)

logger = structlog.get_logger(__name__)


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
        """Parse a normal message or a long explicit list in bounded AI calls."""
        chunks = self._large_order_list_chunks(text)
        if chunks:
            return self._parse_large_order_list(text, chunks)
        return self._parse_text_once(text)

    @staticmethod
    def _large_order_list_chunks(text: str) -> list[str]:
        """Split only unambiguous line-based lists; never infer a quantity from a name."""
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
        """Parse every list chunk with AI and reject partial/guessed results."""
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
        if not force_ai and self._looks_like_support_message(text):
            logger.info("text_support_message_detected", text=text)
            return ParsedCommand(intent=Intent.SMALL_TALK, text=text)
        if not force_ai and deterministic.intent not in {Intent.UNKNOWN, Intent.ADD_ITEMS}:
            logger.info(
                "text_command_deterministic",
                text=text,
                intent=deterministic.intent.value,
                item_count=len(deterministic.items),
            )
            return _repair_command_mixed_script_queries(deterministic)
        if not force_ai and self._can_use_deterministic_product_list(text, deterministic):
            logger.info(
                "text_product_list_deterministic",
                text=text,
                intent=deterministic.intent.value,
                items=[self._item_log(item) for item in deterministic.items],
            )
            return _repair_command_mixed_script_queries(deterministic)
        if not force_ai and self._can_use_deterministic_single_product_with_quantity(
            text, deterministic
        ):
            logger.info(
                "text_single_product_deterministic",
                text=text,
                intent=deterministic.intent.value,
                items=[self._item_log(item) for item in deterministic.items],
            )
            return _repair_command_mixed_script_queries(deterministic)
        if not force_ai and self._can_use_deterministic_short_product(text, deterministic):
            logger.info(
                "text_short_product_deterministic",
                text=text,
                intent=deterministic.intent.value,
                items=[self._item_log(item) for item in deterministic.items],
            )
            return _repair_command_mixed_script_queries(deterministic)
        if not force_ai and self._can_use_deterministic_packaged_product(text, deterministic):
            logger.info(
                "text_packaged_product_deterministic",
                text=text,
                intent=deterministic.intent.value,
                items=[self._item_log(item) for item in deterministic.items],
            )
            return _repair_command_mixed_script_queries(deterministic)
        if not force_ai and self._should_skip_ai(text):
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
            logger.warning(
                "text_ai_transport_failed",
                text=text,
                error_type=type(error).__name__,
                deterministic_intent=deterministic.intent.value,
                deterministic_item_count=len(deterministic.items),
                deterministic_fallback_used=False,
            )
            raise
        parsed = response.output_parsed
        if parsed is None:
            logger.warning("text_ai_empty_result", text=text)
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
        payload = recover_omitted_explicit_items(parsed.model_dump(), text)
        for item in payload.get("items", []):
            if item.get("user_comment_to_supplier") and not item.get("comment"):
                item["comment"] = item["user_comment_to_supplier"]
        command = ParsedCommand.model_validate(payload)
        command = command.model_copy(
            update={
                "explicit_add_items": has_explicit_add_items(text, payload.get("items", []))
                if command.intent is Intent.ADD_ITEMS
                else False,
            }
        )
        if command.global_comment and not command.items:
            # A standalone request such as «добавь общий комментарий:
            # желательно на завтра» modifies the current draft.  It is not a
            # request to add products, even if the deterministic fallback
            # split the words «комментарий» and «общий» into fake items.
            command = command.model_copy(update={"intent": Intent.ADD_ITEMS})
        if (
            command.intent == Intent.ADD_MORE
            and deterministic.intent == Intent.ADD_ITEMS
            and deterministic.items
            and not force_ai
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
        # A numeric range may be a size, package, or product code. Keep these
        # lines on the semantic path so supplier and product qualifiers are
        # not swallowed into one deterministic product name.
        if numeric_range_spans(text):
            return False
        if OpenAIService._has_conversational_product_leadin(text):
            return False

        raw = str(text or "").strip().lower().replace("ё", "е")
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
            if has_product_variant_qualifier(name):
                # Keep product variants on the catalog-verified semantic path.
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
    def _can_use_deterministic_single_product_with_quantity(
        text: str,
        command: ParsedCommand,
    ) -> bool:
        """Проверяет однозначный одиночный товар с количеством."""
        if command.intent != Intent.ADD_ITEMS or len(command.items) != 1:
            return False
        if OpenAIService._has_conversational_product_leadin(text):
            return False
        item = command.items[0]
        normalized_units = set(UNIT_ALIASES.values())
        raw_source = clean_text(text)
        source = raw_source.rstrip(" .!?")
        item_source = clean_text(item.source_line).rstrip(" .!?")
        if numeric_range_spans(source):
            # Do not bypass semantic parsing: this form can contain both a
            # supplier name and product qualifiers after the numeric range.
            return False
        if has_product_variant_qualifier(item.product_query):
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
        if OpenAIService._has_conversational_product_leadin(text):
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
            and normalize_text(query) == normalize_text(text)
            and not has_product_variant_qualifier(query)
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
        if OpenAIService._has_conversational_product_leadin(text):
            return False
        if "\n" in str(text or "") or ";" in str(text or ""):
            return False
        unit_pattern = "|".join(
            sorted((re.escape(unit) for unit in UNIT_ALIASES), key=len, reverse=True)
        )
        return bool(
            re.search(
                rf"\d+(?:[,.]\d+)?\s*(?:{unit_pattern})\s*[*xх×]\s*"
                rf"\d+(?:[,.]\d+)?",
                text,
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

    def parse_photo(self, path: Path, mime_type: str, caption: str = "") -> ParsedCommand:
        """Извлекает товары из фотографии."""
        import base64

        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        with self.tracer.generation(
            "openai.parse_photo",
            model=self.settings.openai_vision_model,
            input={
                "image_bytes": path.stat().st_size,
                "caption_characters": len(caption),
                "mime_type": mime_type,
                "kind": "photo",
            },
            metadata={"feature": "photo_order_parser"},
        ) as generation:
            response = self.vision_client.responses.parse(
                model=self.settings.openai_vision_model,
                instructions=_PHOTO_SYSTEM,
                input=cast(
                    Any,
                    [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": caption or "Распознай заявку на фото",
                                },
                                {
                                    "type": "input_image",
                                    "image_url": f"data:{mime_type};base64,{encoded}",
                                    "detail": "high",
                                },
                            ],
                        }
                    ],
                ),
                text_format=ParsedInputSchema,
                max_output_tokens=5000,
            )
            parsed_output = response.output_parsed
            generation.update(
                output={
                    "parsed": parsed_output is not None,
                    "item_count": len(parsed_output.items) if parsed_output else 0,
                },
                usage_details=self._usage_details(response),
            )
        parsed = response.output_parsed
        if parsed is None:
            logger.warning("photo_ai_empty_result", mime_type=mime_type)
            return ParsedCommand(intent=Intent.UNKNOWN)
        logger.info(
            "photo_ai_parsed",
            document_type=parsed.document_type,
            global_comment=parsed.global_comment,
            item_count=len(parsed.items),
            items=[self._item_log(item) for item in parsed.items],
        )
        command = ParsedCommand.model_validate(parsed.model_dump()).model_copy(
            update={"explicit_add_items": False}
        )
        document_type = clean_text(parsed.document_type).lower()
        normalized = self._normalise_photo_command(command, document_type)
        logger.info(
            "photo_command_normalized",
            document_type=document_type,
            input_item_count=len(command.items),
            output_item_count=len(normalized.items),
            dropped_item_count=len(command.items) - len(normalized.items),
            items=[self._item_log(item) for item in normalized.items],
        )
        return normalized

    def _normalise_photo_command(self, command: ParsedCommand, document_type: str) -> ParsedCommand:
        """Применяет защитные правила к результату OCR."""
        items: list[ExtractedItem] = []
        for item in command.items:
            copy = item.model_copy(deep=True)
            if document_type == "client_order_sheet":
                # Исходная таблица уже содержит справочное поле поставщика из каталога.
                # Распознавание может перенести значение из соседней строки, поэтому оно не
                # должно ограничивать поиск товара по актуальному каталогу.
                copy.supplier_hint = ""
                department_values = (
                    copy.department_quantities.hall,
                    copy.department_quantities.bar,
                    copy.department_quantities.kitchen,
                )
                positive_values = [
                    value for value in department_values if value is not None and value > 0
                ]
                if not positive_values:
                    continue
                copy.quantity = sum(positive_values)
                copy.department = self.settings.default_department
                if copy.quantity_source not in {"handwritten", "handwritten_correction"}:
                    copy.quantity_source = "department_columns"
            if document_type in {"printed_order_form", "supplier_form", "order_table"}:
                entry_quantity = to_float(copy.order_entry_text)
                if entry_quantity is not None and copy.order_entry_type in {
                    "typed",
                    "typed_order_entry",
                    "handwritten",
                    "handwritten_correction",
                }:
                    copy.quantity = entry_quantity
                else:
                    # Фасовка в бланке поставщика может выглядеть как количество.
                    # Пустая отдельная ячейка заказа означает, что товар не заказывают.
                    continue
            if (
                document_type in {"unknown", "unknown_document", "product_card"}
                and copy.quantity_source == "printed_order_column"
                and to_float(copy.order_entry_text) is None
            ):
                continue
            if copy.quantity_source in {
                "printed_reference",
                "packaging",
                "unit_weight",
                "price",
            }:
                continue
            if copy.quantity is None or copy.quantity <= 0:
                continue
            if not copy.quantity_source:
                copy.quantity_source = copy.order_entry_type or "photo_order_entry"
            items.append(copy)
        return command.model_copy(update={"items": items})

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
