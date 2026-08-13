"""Содержит операции диалога «contextual commands»."""

from __future__ import annotations

import re
from collections.abc import Callable

from restaurant_bot.conversation.quantity_resolution import (
    is_multiple_warning,
    multiple_warnings,
)
from restaurant_bot.conversation.routing.item_resolution import has_named_product_items
from restaurant_bot.conversation.state.queries import item_index as state_item_index
from restaurant_bot.domain.models import (
    CartItem,
    ConversationState,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    SessionStage,
)
from restaurant_bot.domain.text import normalize_text
from restaurant_bot.domain.units import UNIT_ALIASES, normalize_unit
from restaurant_bot.orders.supplier_minimums import supplier_minimum_warnings
from restaurant_bot.parsing.commands.normalization import (
    has_negated_action,
    has_negation,
    is_explicit_item_rejection,
    normalize_command_text,
)
from restaurant_bot.parsing.number_words import NUMBER_WORDS


class ContextualCommandPolicy:
    """Выполняет контекстное переосмысление уже разобранной команды."""

    def __init__(
        self,
        spoken_quantity_parser: Callable[[str], tuple[float | None, str]],
    ) -> None:
        """Сохраняет детерминированный разбор короткого количества."""
        self._spoken_quantity_parser = spoken_quantity_parser

    def _spoken_quantity(self, text: str) -> tuple[float | None, str]:
        """Извлекает явно произнесённое количество через существующий parser."""
        return self._spoken_quantity_parser(text)

    def normalize_pre_modal_voice(
        self,
        command: ParsedCommand,
        *,
        input_kind: InputKind,
        raw_text: str,
        state: ConversationState,
    ) -> ParsedCommand:
        """Нормализует голосовую команду до проверки modal-состояния."""
        return self._contextual_voice_command(command, input_kind, raw_text, state)

    def reinterpret_contextual_command(
        self,
        command: ParsedCommand,
        *,
        input_kind: InputKind,
        raw_text: str,
        state: ConversationState,
        available_cart_pages: set[int] | None = None,
        available_final_review_pages: set[int] | None = None,
    ) -> ParsedCommand:
        """Применяет contextual transforms в утверждённом порядке."""
        command = self._contextual_negative_command(command, input_kind, raw_text, state)
        command = self._contextual_quantity_command(command, input_kind, raw_text, state)
        command = self._contextual_cart_pagination_command(
            command,
            input_kind,
            raw_text,
            state,
            available_cart_pages or set(),
        )
        command = self._contextual_final_review_pagination_command(
            command,
            input_kind,
            raw_text,
            state,
            available_final_review_pages or set(),
        )
        command = self._contextual_order_status_command(command, input_kind, raw_text, state)
        return self._contextual_voice_command(command, input_kind, raw_text, state)

    @staticmethod
    def is_generic_show_products_command(text: str) -> bool:
        """Проверяет, является ли фраза общим запросом списка товаров."""
        return ContextualCommandPolicy._is_generic_show_products_command(text)

    @staticmethod
    def _mentions_expected_unit(text: str, expected_unit: str) -> bool:
        """Проверяет, упомянул ли пользователь единицу открытой позиции."""
        normalized_expected = normalize_unit(expected_unit)
        if not normalized_expected:
            return False
        words = re.findall(r"[a-zа-яё]+", normalize_text(text), flags=re.I)
        return any(
            word in UNIT_ALIASES and normalize_unit(word) == normalized_expected for word in words
        )

    @staticmethod
    def _has_named_product_items(command: ParsedCommand, text: str) -> bool:
        """Отличает товарный запрос от короткого ответа количеством."""
        return has_named_product_items(command, text)

    @staticmethod
    def _item_index(state: ConversationState, item: CartItem | None) -> int:
        """Возвращает индекс позиции через общий state-query."""
        return state_item_index(state, item) if item is not None else 0

    def _contextual_quantity_command(
        self,
        command: ParsedCommand,
        input_kind: InputKind,
        raw_text: str,
        state: ConversationState,
    ) -> ParsedCommand:
        """Обрабатывает команду количества с учётом текущего экрана."""
        if input_kind is InputKind.CALLBACK:
            return command
        current = state.current_item()
        if current is None:
            return command

        phrase = normalize_text(raw_text or command.text)
        if not phrase:
            return command
        words = set(phrase.split())
        has_product_word = any(
            word.startswith(("товар", "позици", "продукт", "покуп")) for word in words
        )
        quantity, unit = self._spoken_quantity(phrase)
        correction = any(word.startswith(("исправ", "поправ", "измен", "поменя")) for word in words)
        recommended = (
            "как нужно" in phrase
            or "рекоменд" in phrase
            or "округл" in phrase
            or "ближайш" in phrase
        )
        contextual_add = bool(
            re.fullmatch(
                r"(?:(?:добавь|добавить)(?: еще)?(?: недостающее)?|докинь|дополни)",
                phrase,
            )
        )
        add_to_current = (
            command.intent == Intent.ADD_MORE or contextual_add
        ) and not has_product_word
        increment_current = quantity is not None and bool(
            re.search(
                r"(?:^|\s)(?:добавь|добавить|докинь|докинуть|прибавь|прибавить|дополни)"
                r"(?:\s|$)",
                phrase,
            )
        )

        has_multiple_warning = is_multiple_warning(current)
        if has_multiple_warning:
            if quantity is not None:
                return command.model_copy(
                    update={
                        "intent": Intent.EDIT_QUANTITY,
                        "edit_quantity": (current.quantity or 0) + quantity
                        if increment_current
                        else quantity,
                        "edit_unit": unit,
                        "target_query": "",
                    }
                )
            if recommended or add_to_current:
                return command.model_copy(update={"intent": Intent.ACCEPT_SUGGESTED_QUANTITY})
            if correction:
                wants_manual_quantity = any(
                    word.startswith(("друг", "нов", "сво")) for word in words
                )
                return command.model_copy(
                    update={
                        "intent": (
                            Intent.ENTER_OTHER_QUANTITY
                            if wants_manual_quantity
                            else Intent.FIX_MULTIPLE
                        )
                    }
                )

        if current.status == ItemStatus.UNIT_MISMATCH:
            rejects_quantity_change = has_negated_action(
                phrase,
                "добав",
                "введ",
                "укаж",
                "постав",
                "закаж",
                "возьм",
                "измен",
                "поменя",
                "перевед",
                "конверт",
            )
            if rejects_quantity_change:
                return command
            if quantity is not None:
                return command.model_copy(
                    update={
                        "intent": Intent.EDIT_QUANTITY,
                        "edit_quantity": quantity,
                        "edit_unit": unit,
                        "target_query": "",
                    }
                )
            if add_to_current or "перевед" in phrase or "конверт" in phrase:
                return command.model_copy(update={"intent": Intent.USE_CATALOG_UNIT})
            if correction or self._mentions_expected_unit(phrase, current.catalog_unit):
                return command.model_copy(update={"intent": Intent.ENTER_OTHER_QUANTITY})

        if (
            current.status == ItemStatus.MISSING_QTY
            and quantity is not None
            and (
                not self._has_named_product_items(command, phrase)
                # Контекстное правило разбора команды.
                or (
                    command.intent == Intent.SELECT_CANDIDATE
                    and self._is_quantity_only_phrase(phrase)
                )
            )
        ):
            return command.model_copy(
                update={
                    "intent": Intent.EDIT_QUANTITY,
                    "edit_quantity": quantity,
                    "edit_unit": unit,
                    "target_query": "",
                }
            )
        return command

    @staticmethod
    def _is_quantity_only_phrase(phrase: str) -> bool:
        """Проверяет, что короткая фраза содержит только число и единицу.

        Это контекстная проверка для карточки «Укажите количество». Она не
        меняет обычный выбор кандидата: фразы «первый вариант» и «вариант
        один» не считаются количеством.
        """
        normalized = normalize_text(phrase)
        if not normalized:
            return False
        words = normalized.replace(",", ".").split()
        if words and words[0] in {
            "первый",
            "первая",
            "первое",
            "второй",
            "вторая",
            "третья",
            "вариант",
        }:
            return False
        allowed = set(UNIT_ALIASES) | set(NUMBER_WORDS)
        for word in words:
            if re.fullmatch(r"\d+(?:\.\d+)?", word):
                continue
            if word not in allowed:
                return False
        return any(re.fullmatch(r"\d+(?:\.\d+)?", word) or word in NUMBER_WORDS for word in words)

    def _contextual_cart_pagination_command(
        self,
        command: ParsedCommand,
        input_kind: InputKind,
        raw_text: str,
        state: ConversationState,
        available_pages: set[int],
    ) -> ParsedCommand:
        """Оставляет голосовую навигацию в черновике, если он разбит на страницы."""
        if not available_pages:
            return command

        raw = raw_text or command.text
        phrase = normalize_command_text(raw)
        navigation_target = self._status_navigation_target(phrase)
        if navigation_target not in {"next", "previous"}:
            return command

        target_page = state.cart_page + (1 if navigation_target == "next" else -1)
        if target_page not in available_pages:
            target_page = state.cart_page
        return ParsedCommand(
            intent=Intent.SHOW_CART,
            text=raw,
            callback_target=f"page:{max(0, target_page)}",
        )

    def _contextual_order_status_command(
        self,
        command: ParsedCommand,
        input_kind: InputKind,
        raw_text: str,
        state: ConversationState,
    ) -> ParsedCommand:
        """Связывает короткую фразу с последним показанным списком заявок."""
        if not state.order_status_view_active:
            return command
        raw = raw_text or command.text
        phrase = normalize_command_text(raw)
        if not phrase:
            return command
        navigation_target = self._status_navigation_target(phrase)
        if navigation_target:
            if state.order_status_detail_active:
                navigation_target = f"detail_{navigation_target}"
            selected_index = (
                state.order_status_selected_index if state.order_status_detail_active else None
            )
            order_number = (
                state.order_status_selected_order_number if state.order_status_detail_active else ""
            )
            return ParsedCommand(
                intent=Intent.ORDER_STATUS,
                text=raw,
                selected_index=selected_index,
                selection_query=order_number,
                callback_target=navigation_target,
            )
        if command.intent == Intent.ORDER_STATUS and (
            command.selected_index is not None or command.selection_query or command.callback_target
        ):
            return command
        if command.intent == Intent.SELECT_CANDIDATE and command.selected_index is not None:
            return ParsedCommand(
                intent=Intent.ORDER_STATUS,
                text=raw,
                selected_index=command.selected_index,
            )
        selection_phrase = re.sub(
            r"^(?:(?:покаж\w*|открой\w*|выбер\w*|посмотр\w*|давай)\s+)+",
            "",
            phrase,
        )
        selection_phrase = re.sub(
            r"^(?:(?:заявк\w*|заказ\w*)\s+)?(?:номер\s+)?",
            "",
            selection_phrase,
        )
        spoken_index = self._spoken_choice_index(selection_phrase)
        if spoken_index is not None and spoken_index <= len(state.order_status_order_numbers):
            return ParsedCommand(
                intent=Intent.ORDER_STATUS,
                text=raw,
                selected_index=spoken_index,
            )
        if re.fullmatch(
            r"(?:назад(?:\s+к списку)?|к списку|вернись назад|вернись к списку)"
            r"(?:\s+(?:заявок|заказов))?",
            phrase,
        ):
            return ParsedCommand(
                intent=Intent.ORDER_STATUS,
                text=raw,
                callback_target="current",
            )
        return command

    def _contextual_final_review_pagination_command(
        self,
        command: ParsedCommand,
        input_kind: InputKind,
        raw_text: str,
        state: ConversationState,
        available_pages: set[int],
    ) -> ParsedCommand:
        """Оставляет голосовую навигацию в постраничной финальной проверке."""
        if not available_pages:
            return command

        raw = raw_text or command.text
        phrase = normalize_command_text(raw)
        navigation_target = self._status_navigation_target(phrase)
        if navigation_target not in {"next", "previous"}:
            return command

        target_page = state.final_review_page + (1 if navigation_target == "next" else -1)
        if target_page not in available_pages:
            target_page = state.final_review_page
        return ParsedCommand(
            intent=Intent.SHOW_FINAL_REVIEW,
            text=raw,
            callback_target=f"page:{max(0, target_page)}",
        )

    @staticmethod
    def _status_navigation_target(phrase: str) -> str:
        """Распознаёт разговорную навигацию по страницам истории."""
        cleaned = re.sub(
            r"^(?:(?:ну|покаж\w*|открой\w*|перей\w*|верн\w*|листай\w*|"
            r"перелист\w*|пролист\w*|поехал\w*|давай\w*|мне|можешь|можно|пожалуйста)\s+)+",
            "",
            phrase,
        ).strip()
        cleaned = re.sub(r"^(?:на|в|к)\s+", "", cleaned)
        if "список" in cleaned or "списку" in cleaned:
            return ""
        if re.fullmatch(
            r"(?:следующ\w*|дальше|ещ[её]|(?:более\s+)?стар\w*|впер[её]д)"
            r"(?:\s+(?:страниц\w*|заявк\w*|заказ\w*))?",
            cleaned,
        ):
            return "next"
        if re.fullmatch(
            r"(?:предыдущ\w*|новее|назад|более\s+нов\w*)"
            r"(?:\s+(?:страниц\w*|заявк\w*|заказ\w*))?",
            cleaned,
        ):
            return "previous"
        return ""

    def _contextual_voice_command(
        self,
        command: ParsedCommand,
        input_kind: InputKind,
        raw_text: str,
        state: ConversationState,
    ) -> ParsedCommand:
        """Обрабатывает голосовую команду с учётом текущего экрана."""
        if input_kind is not InputKind.VOICE:
            return command
        if command.intent == Intent.EDIT_QUANTITY and command.edit_quantity is not None:
            return command

        raw = raw_text or command.text
        phrase = normalize_text(raw)
        if not phrase:
            return command
        current = state.current_item()

        # Контекстное правило разбора команды.
        if (
            current is None
            and command.intent == Intent.ADD_ITEMS
            and len(command.items) == 1
            and command.items[0].quantity is None
            and normalize_text(command.items[0].product_query)
            in {"новая", "новую", "новый", "новое"}
        ):
            return command.model_copy(update={"intent": Intent.START_NEW_ORDER, "items": []})

        if (
            current
            and current.status == ItemStatus.DUPLICATE_PENDING
            and command.intent == Intent.ADD_MORE
            and self._is_duplicate_merge_confirmation(phrase)
        ):
            return command.model_copy(update={"intent": Intent.MERGE_DUPLICATE})

        if state.stage == SessionStage.AWAIT_ADD_MORE_CONFIRM:
            if not has_negated_action(
                phrase,
                "отправ",
                "оформ",
                "запиш",
                "переда",
            ) and self._has_any_prefix(
                phrase,
                "отправ",
                "оформ",
                "запиш",
                "переда",
            ):
                return command.model_copy(update={"intent": Intent.SUBMIT_REQUEST})
            if command.intent == Intent.ADD_MORE or self._is_explicit_yes(phrase):
                return command.model_copy(update={"intent": Intent.ADD_MORE})
            if command.intent in {Intent.BACK, Intent.CANCEL, Intent.SHOW_CART}:
                return command.model_copy(update={"intent": Intent.BACK})
            if self._has_any_prefix(
                phrase, "нет", "хват", "достат", "готов", "больше не", "не надо"
            ):
                return command.model_copy(update={"intent": Intent.BACK})

        # Контекстное правило разбора команды.
        if command.intent in {
            Intent.ADD_MORE,
            Intent.BACK,
            Intent.CHECK_MIN_SUM,
            Intent.EDIT_COMMENT,
            Intent.CLARIFY_CURRENT,
            Intent.CLEAR_CART,
            Intent.START_NEW_ORDER,
            Intent.GREETING,
            Intent.HELP,
            Intent.ORDER_STATUS,
            Intent.PRODUCT_ADD_LIST,
            Intent.SHOW_CART,
            Intent.SHOW_FINAL_REVIEW,
            Intent.SMALL_TALK,
            Intent.THANKS,
        }:
            return command

        if current and current.status == ItemStatus.AMBIGUOUS:
            spoken_index = self._spoken_choice_index(phrase)
            if spoken_index is not None:
                return ParsedCommand(
                    intent=Intent.SELECT_CANDIDATE,
                    text=raw,
                    selected_index=spoken_index,
                    callback_target=str(self._item_index(state, current)),
                )
            if self._has_any_prefix(phrase, "не добав", "пропуст", "не нужен", "откаж"):
                return command.model_copy(update={"intent": Intent.SKIP_CURRENT})
            if self._has_any_prefix(phrase, "измен", "другое назв", "название товар"):
                return command.model_copy(
                    update={
                        "intent": Intent.MANUAL_CURRENT,
                        "callback_target": str(self._item_index(state, current)),
                    }
                )

        if current and current.status == ItemStatus.NOT_FOUND:
            if current.rename_attempted and (
                self._is_explicit_yes(phrase) or self._has_any_prefix(phrase, "отправ")
            ):
                return command.model_copy(
                    update={
                        "intent": Intent.PRODUCT_ADD,
                        "callback_target": str(self._item_index(state, current)),
                    }
                )
            if current.rename_attempted and self._has_any_prefix(
                phrase, "нет", "не добав", "отмен"
            ):
                return command.model_copy(
                    update={
                        "intent": Intent.PRODUCT_ADD_SKIP,
                        "callback_target": str(self._item_index(state, current)),
                    }
                )
            if self._has_any_prefix(phrase, "не добав", "пропуст", "не нужен"):
                return command.model_copy(update={"intent": Intent.SKIP_CURRENT})
            if self._has_any_prefix(phrase, "измен", "другое назв", "название товар"):
                return command.model_copy(
                    update={
                        "intent": Intent.MANUAL_CURRENT,
                        "callback_target": str(self._item_index(state, current)),
                    }
                )
            if "всех поставщик" in phrase or "у других поставщик" in phrase:
                return command.model_copy(
                    update={
                        "intent": Intent.SEARCH_ALL_SUPPLIERS,
                        "callback_target": str(self._item_index(state, current)),
                    }
                )
            if "другого поставщик" in phrase:
                return command.model_copy(
                    update={
                        "intent": Intent.SWITCH_SUPPLIER,
                        "callback_target": str(self._item_index(state, current)),
                    }
                )

        if current and current.status == ItemStatus.DUPLICATE_PENDING:
            if self._is_duplicate_skip_confirmation(phrase):
                return command.model_copy(update={"intent": Intent.SKIP_CURRENT})
            if self._is_explicit_yes(phrase) or self._is_duplicate_merge_confirmation(phrase):
                return command.model_copy(update={"intent": Intent.MERGE_DUPLICATE})

        if current and current.status == ItemStatus.UNIT_MISMATCH:
            if command.intent == Intent.SKIP_CURRENT:
                return command
            if not has_negation(phrase) and (
                self._is_explicit_yes(phrase) or self._has_any_prefix(phrase, "остав", "добав")
            ):
                return command.model_copy(update={"intent": Intent.USE_CATALOG_UNIT})
            if self._has_any_prefix(phrase, "нет", "измен", "другое колич"):
                return command.model_copy(update={"intent": Intent.ENTER_OTHER_QUANTITY})

        warnings = multiple_warnings(state)
        if warnings:
            if self._has_any_prefix(phrase, "остав", "как указ", "не исправ"):
                return command.model_copy(update={"intent": Intent.KEEP_MULTIPLE})
            if self._has_any_prefix(phrase, "рекоменд", "ближайш", "округл"):
                return command.model_copy(update={"intent": Intent.ACCEPT_SUGGESTED_QUANTITY})

            current_warning = state.current_item()
            if current_warning in warnings:
                spoken_index = self._spoken_choice_index(phrase)
                if spoken_index is None and command.intent == Intent.SELECT_CANDIDATE:
                    spoken_index = command.selected_index
                if spoken_index == 1:
                    return command.model_copy(update={"intent": Intent.ACCEPT_SUGGESTED_QUANTITY})
                if spoken_index == 2:
                    return command.model_copy(update={"intent": Intent.ENTER_OTHER_QUANTITY})
                if spoken_index == 3:
                    return command.model_copy(update={"intent": Intent.KEEP_CURRENT_QUANTITY})
            if self._spoken_quantity(phrase)[0] is None and self._has_any_prefix(
                phrase,
                "исправ",
                "поправ",
                "измен",
                "поменя",
                "выб",
                "подоб",
            ):
                return command.model_copy(update={"intent": Intent.FIX_MULTIPLE})

        supplier_index = self._spoken_supplier_warning_index(phrase, state)
        if supplier_index is not None:
            return command.model_copy(
                update={"intent": Intent.ADD_SUPPLIER_ITEMS, "callback_target": str(supplier_index)}
            )

        warning_count = self._supplier_warning_count(state)
        if (
            warning_count > 1
            and self._has_any_prefix(phrase, "выбер", "выбрат", "выбор")
            and "поставщик" in phrase
        ):
            return command.model_copy(update={"intent": Intent.CHOOSE_SUPPLIER_WARNING})

        if re.fullmatch(
            r"(?:(?:давай )?(?:добавим|добавить|доберем|добрать)(?: еще)? "
            r"(?:товары|позиции|сумму)(?: (?:этого|у этого|по этому) поставщик\w*)?|"
            r"(?:давай )?(?:добрать|доберем) до (?:минималки|минимальной суммы))",
            phrase,
        ):
            if warning_count == 1:
                return command.model_copy(
                    update={"intent": Intent.ADD_SUPPLIER_ITEMS, "callback_target": "0"}
                )
            if warning_count > 1:
                return command.model_copy(update={"intent": Intent.CHOOSE_SUPPLIER_WARNING})

        requests = [
            request
            for request in state.product_add_requests
            if request.get("description") and request.get("status") != "cancelled"
        ]
        if (
            not has_negation(phrase)
            and "запрос" in phrase
            and any(
                stem in phrase
                for stem in (
                    "снабжен",
                    "покаж",
                    "показ",
                    "посмотр",
                    "откр",
                    "вывед",
                    "спис",
                    "пров",
                    "повтор",
                    "еще раз",
                )
            )
        ):
            if self._has_any_prefix(phrase, "повтор", "еще раз"):
                index = self._spoken_choice_index(phrase) or 1
                retryable = [row for row in requests if row.get("status") == "write_failed"]
                if 0 < index <= len(retryable):
                    return command.model_copy(
                        update={
                            "intent": Intent.PRODUCT_ADD_RETRY,
                            "callback_target": str(retryable[index - 1].get("request_id") or ""),
                        }
                    )
            return command.model_copy(update={"intent": Intent.PRODUCT_ADD_LIST})
        return command

    def _contextual_negative_command(
        self,
        command: ParsedCommand,
        input_kind: InputKind,
        raw_text: str,
        state: ConversationState,
    ) -> ParsedCommand:
        """Применяет отрицание до любых подтверждающих и изменяющих маршрутов."""
        if input_kind is InputKind.CALLBACK:
            return command
        raw = raw_text or command.text
        phrase = normalize_text(raw)
        if not phrase:
            return command

        current = state.current_item()
        rejects_item = is_explicit_item_rejection(phrase)

        if state.stage == SessionStage.AWAIT_PRODUCT_ADD_DETAILS and (
            rejects_item
            or command.intent == Intent.CANCEL
            or has_negated_action(
                phrase,
                "отправ",
                "созда",
                "оформ",
                "добав",
                "подтверж",
            )
        ):
            return command.model_copy(
                update={
                    "intent": Intent.PRODUCT_ADD_SKIP,
                    "callback_target": str(state.pending_product_add_item_index or 0),
                }
            )

        if state.stage == SessionStage.AWAIT_ADD_MORE_CONFIRM and (
            rejects_item
            or command.intent == Intent.CANCEL
            or has_negated_action(phrase, "добав", "продолж", "внес", "докин")
        ):
            return command.model_copy(update={"intent": Intent.BACK})

        if state.stage == SessionStage.AWAIT_SUBMIT_CONFIRM and (
            command.intent == Intent.CANCEL
            or has_negated_action(phrase, "отправ", "подтверж", "оформ", "переда", "запиш")
            or self._has_any_prefix(phrase, "передум", "отмен")
        ):
            return command.model_copy(update={"intent": Intent.BACK})

        if current is None:
            return command
        if rejects_item:
            return command.model_copy(update={"intent": Intent.SKIP_CURRENT})
        if current.status == ItemStatus.DUPLICATE_PENDING and (
            command.intent == Intent.CANCEL
            or has_negated_action(
                phrase,
                "объедин",
                "добав",
                "суммир",
                "прибав",
                "слож",
                "увелич",
            )
        ):
            return command.model_copy(update={"intent": Intent.SKIP_CURRENT})
        if current.status == ItemStatus.UNIT_MISMATCH and (
            command.intent == Intent.CANCEL
            or has_negated_action(
                phrase,
                "использ",
                "остав",
                "добав",
                "перевед",
                "конверт",
                "подтверж",
            )
        ):
            return command.model_copy(update={"intent": Intent.ENTER_OTHER_QUANTITY})
        if current.status == ItemStatus.AMBIGUOUS and (
            command.intent == Intent.CANCEL
            or has_negated_action(phrase, "выбер", "выбир", "возьм", "бери")
            or (has_negation(phrase) and self._spoken_choice_index(phrase) is not None)
        ):
            return command.model_copy(update={"intent": Intent.CONTINUE_CURRENT})
        if is_multiple_warning(current) and (
            command.intent == Intent.KEEP_CURRENT_QUANTITY
            or has_negated_action(
                phrase,
                "исправ",
                "измен",
                "округл",
                "рекоменд",
                "замен",
            )
        ):
            return command.model_copy(update={"intent": Intent.KEEP_CURRENT_QUANTITY})
        return command

    @staticmethod
    def _has_any_prefix(phrase: str, *stems: str) -> bool:
        """Проверяет наличие одного из допустимых префиксов."""
        return any(re.search(rf"(?:^|\s){re.escape(stem)}", phrase) for stem in stems)

    @staticmethod
    def _is_explicit_yes(phrase: str) -> bool:
        """Отличает подтверждение «да» от разговорного слова «давай»."""
        if has_negation(phrase):
            return False
        words = re.findall(r"[a-zа-яё0-9-]+", phrase, flags=re.I)
        return any(
            word
            in {
                "да",
                "ага",
                "ок",
                "окей",
                "конечно",
                "верно",
                "правильно",
                "согласен",
                "согласна",
                "подтверждаю",
            }
            for word in words
        )

    @staticmethod
    def _is_duplicate_merge_confirmation(phrase: str) -> bool:
        """Распознаёт разговорное подтверждение объединения повторного товара."""
        words = re.findall(r"[a-zа-яё0-9-]+", phrase, flags=re.I)
        has_negation = any(
            word in {"не", "нет", "отмена", "отменить", "пропусти", "пропустить"} for word in words
        )
        has_merge_action = any(
            word.startswith(
                (
                    "добав",
                    "объедин",
                    "суммир",
                    "прибав",
                    "слож",
                    "плюс",
                    "увелич",
                    "вмест",
                )
            )
            for word in words
        )
        has_new_items = any(
            word.startswith(("товар", "позиц", "продукт", "ещ", "нов", "друг")) for word in words
        )
        return has_merge_action and not has_new_items and not has_negation

    @staticmethod
    def _is_duplicate_skip_confirmation(phrase: str) -> bool:
        """Распознаёт отказ от повторного добавления товара."""
        return bool(
            re.search(
                r"(?:^|\s)(?:нет|не\s+(?:надо|добав\w*)|пропуст\w*|"
                r"отмен\w*|остав\w*\s+как\s+есть)",
                phrase,
            )
        )

    @staticmethod
    def _is_generic_show_products_command(text: str) -> bool:
        """Определяет просьбу показать товары без упоминания черновика."""
        words = normalize_text(text).split()
        has_show_action = any(
            word.startswith(("покаж", "показ", "посмотр", "откр", "вывед")) for word in words
        )
        has_product_target = any(
            word.startswith(("товар", "позиц", "продукт", "спис")) for word in words
        )
        has_draft_target = any(
            word.startswith(("черновик", "корзин", "заявк", "заказ")) for word in words
        )
        return has_show_action and has_product_target and not has_draft_target

    @staticmethod
    def _spoken_choice_index(phrase: str) -> int | None:
        """Определяет номер варианта из живой речи."""
        if has_negation(phrase):
            return None
        words = {
            1: r"(?:1|один|перв\w*)",
            2: r"(?:2|два|втор\w*)",
            3: r"(?:3|три|трет\w*)",
            4: r"(?:4|четыр\w*)",
            5: r"(?:5|пят\w*)",
        }
        for index, token in words.items():
            if re.search(
                rf"(?:^|\s)(?:(?:давай|выбираю|беру|нужен|хочу)\s+)?(?:вариант\s+)?{token}(?:\s|$)",
                phrase,
            ):
                return index
        return None

    @staticmethod
    def _spoken_supplier_warning_index(phrase: str, state: ConversationState) -> int | None:
        """Определяет поставщика из голосовой команды."""
        options = [warning.supplier for warning in supplier_minimum_warnings(state)]
        compact_phrase = ContextualCommandPolicy._spoken_acronym(phrase)
        for index, supplier in enumerate(options):
            normalized_supplier = normalize_text(supplier)
            compact_supplier = normalized_supplier.replace(" ", "")
            if normalized_supplier and (
                normalized_supplier in phrase or compact_supplier == compact_phrase.replace(" ", "")
            ):
                return index
        return None

    @staticmethod
    def _supplier_warning_count(state: ConversationState) -> int:
        """Считает предупреждения по поставщикам."""
        return len(supplier_minimum_warnings(state))

    @staticmethod
    def _spoken_acronym(value: str) -> str:
        """Нормализует произнесённую аббревиатуру поставщика."""
        letters = {
            "а": "а",
            "бэ": "б",
            "вэ": "в",
            "гэ": "г",
            "дэ": "д",
            "е": "е",
            "ё": "ё",
            "жэ": "ж",
            "зэ": "з",
            "и": "и",
            "ка": "к",
            "эл": "л",
            "эм": "м",
            "эн": "н",
            "о": "о",
            "пэ": "п",
            "эр": "р",
            "эс": "с",
            "тэ": "т",
            "у": "у",
            "эф": "ф",
            "ха": "х",
            "це": "ц",
            "че": "ч",
            "ша": "ш",
            "ща": "щ",
            "ы": "ы",
            "э": "э",
            "ю": "ю",
            "я": "я",
        }
        return "".join(letters.get(word, word) for word in value.split())
