from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, cast

import structlog
from openai import APIConnectionError, APITimeoutError, OpenAI, RateLimitError
from pydantic import BaseModel, Field

from restaurant_bot.config import Settings
from restaurant_bot.domain.models import ExtractedItem, Intent, ParsedCommand
from restaurant_bot.observability import Tracer
from restaurant_bot.services.parser import (
    has_explicit_global_comment_scope,
    infer_intent,
    parse_product_lines,
    parse_quantity_unit,
)
from restaurant_bot.services.text import (
    UNIT_ALIASES,
    clean_text,
    normalize_text,
    normalize_unit,
    parse_number_words,
    remove_global_comment_overlap,
    to_float,
)

logger = structlog.get_logger(__name__)

_EMPTY_AI_VALUES = {
    "unknown",
    "none",
    "null",
    "n/a",
    "неизвестно",
    "неизвестный",
    "не указано",
    "не указан",
}


class ParsedInputSchema(BaseModel):
    """Проверяет структурированный результат извлечения товаров."""

    intent: Intent = Intent.UNKNOWN
    text: str = ""
    items: list[ExtractedItem] = Field(default_factory=list)
    target_query: str = ""
    target_queries: list[str] = Field(default_factory=list)
    selected_index: int | None = None
    selection_query: str = ""
    edit_quantity: float | None = None
    edit_unit: str = ""
    global_comment: str = ""
    document_type: str = ""


class ProductMatchDecision(BaseModel):
    """Проверяет решение ИИ о сопоставлении товара."""

    action: str = Field(pattern="^(select|ambiguous|not_found)$")
    selected_product_id: str = ""
    candidate_product_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0, ge=0, le=1)
    contradictions: list[str] = Field(default_factory=list)
    reason: str = ""


class VisibleActionDecision(BaseModel):
    """Проверяет выбор действия среди кнопок текущего экрана."""

    action_id: str = ""
    confidence: float = Field(default=0, ge=0, le=1)
    reason: str = ""


def _quantity_outside_packaged_query(
    product_query: str,
    source_line: str,
) -> tuple[float | None, str]:
    """Находит количество после названия товара с указанной фасовкой."""
    normalized_query = normalize_text(product_query)
    normalized_source = normalize_text(source_line)
    unit_pattern = "|".join(
        sorted((re.escape(unit) for unit in UNIT_ALIASES), key=len, reverse=True)
    )
    if not re.search(rf"\d+(?:[,.]\d+)?\s*(?:{unit_pattern})\b", normalized_query):
        return None, ""
    query_start = normalized_source.find(normalized_query)
    if query_start < 0:
        return None, ""

    before = normalized_source[:query_start].strip(" ,;:-—–")
    after = normalized_source[query_start + len(normalized_query) :].strip(" ,;:-—–")
    for outside_query in (after, before):
        quantity, unit = parse_quantity_unit(outside_query)
        if quantity is not None and unit:
            return quantity, unit
    return None, ""


def _quantities_with_units(source_line: str) -> list[tuple[float, str]]:
    """Извлекает все явно указанные количества вместе с единицами."""
    normalized = normalize_text(source_line).replace(",", ".")
    normalized = re.sub(r"(?<=\d)(?=[a-zа-я])", " ", normalized, flags=re.I)
    tokens = [token.strip(" .,:;—–-") for token in normalized.split()]
    found: list[tuple[float, str]] = []
    index = 0
    while index < len(tokens):
        parsed = parse_number_words(tokens, index)
        if parsed is None:
            index += 1
            continue
        quantity, end = parsed
        raw_unit = tokens[end] if end < len(tokens) else ""
        if raw_unit in UNIT_ALIASES:
            found.append((quantity, normalize_unit(raw_unit)))
            index = end + 1
            continue
        index += 1
    return found


def _clear_unknown_item_placeholders(items: list[dict[str, Any]]) -> None:
    """Очищает служебные заглушки ИИ в полях маршрутизации товара."""
    for item in items:
        for field in ("department", "supplier_hint", "source_department"):
            if normalize_text(item.get(field)) in _EMPTY_AI_VALUES:
                item[field] = ""


def _query_is_already_represented(known_query: str, recovered_query: str) -> bool:
    """Сравнивает многословные названия с учётом разговорных окончаний."""
    if (
        known_query == recovered_query
        or known_query in recovered_query
        or recovered_query in known_query
    ):
        return True
    known_tokens = re.findall(r"[a-zа-я0-9%]+", known_query, flags=re.I)
    recovered_tokens = re.findall(r"[a-zа-я0-9%]+", recovered_query, flags=re.I)
    if len(known_tokens) < 2:
        return False
    return all(
        any(
            known == recovered
            or (len(known) >= 4 and len(recovered) >= 4 and known[:4] == recovered[:4])
            for recovered in recovered_tokens
        )
        for known in known_tokens
    )


def restore_explicit_order_terms(
    items: list[dict[str, Any]], source_text: str = ""
) -> list[dict[str, Any]]:
    """Восстанавливает явно указанное количество или единицу."""
    recovered_from_message = parse_product_lines(source_text)
    for index, item in enumerate(items):
        original_line = clean_text(item.get("source_line")) or clean_text(source_text)
        outside_quantity, outside_unit = _quantity_outside_packaged_query(
            clean_text(item.get("product_query")),
            original_line,
        )
        if outside_quantity is not None:
            item["source_line"] = original_line
            item["quantity"] = outside_quantity
            item["unit"] = outside_unit
            continue

        # A source can contain both packaging in the name and order quantity.
        # Keep the model value only when it is explicitly present in the source.
        explicit_terms = _quantities_with_units(original_line)
        model_quantity = to_float(item.get("quantity"))
        model_unit = normalize_unit(item.get("unit"))
        if (
            len(items) == 1
            and len(explicit_terms) >= 2
            and model_quantity is not None
            and model_unit
            and any(
                abs(quantity - model_quantity) <= 1e-9 and unit == model_unit
                for quantity, unit in explicit_terms
            )
        ):
            item["source_line"] = original_line
            item["quantity"] = model_quantity
            item["unit"] = model_unit
            continue

        # n8n recovers each explicit spoken order term even when the model
        # returned one shared source_line for a multi-product voice message.
        # Positional recovery from the complete user message is authoritative
        # when both enumerations have the same length. The model often copies
        # the whole message into every source_line; parsing that field first
        # would assign the final product's quantity to all preceding items.
        source = None
        if len(items) == len(recovered_from_message):
            source = recovered_from_message[index]
        if source is None:
            source_line = clean_text(item.get("source_line"))
            recovered = parse_product_lines(source_line)
            source = recovered[0] if len(recovered) == 1 else None
        if source is not None:
            if not clean_text(item.get("source_line")):
                item["source_line"] = source.source_line or clean_text(source_text)
            # The original spoken/source line is authoritative. In
            # particular, the model must not invent "one bottle" for a line
            # that contains no quantity at all.
            item["quantity"] = source.quantity
            item["unit"] = source.unit if source.quantity is not None else ""
            if source.quantity is not None:
                continue

        # Spoken "пару яблок" is an explicit quantity for this item, not a
        # continuation of the preceding product's number.
        query = normalize_text(item.get("product_query"))
        pair = re.search(
            r"\b(?:пару|пара)\s+([a-zа-я][a-zа-я0-9-]*)",
            normalize_text(source_text),
        )
        if pair and query and (pair.group(1) in query or query in pair.group(1)):
            item["quantity"] = 2.0
            item["unit"] = "шт"
    return items


def remove_unsupported_query_qualifiers(
    items: list[dict[str, Any]], source_text: str
) -> list[dict[str, Any]]:
    """Удаляет характеристики товара, которых не называл пользователь."""
    source_words = re.findall(r"[a-zа-я0-9]+", normalize_text(source_text), flags=re.I)
    if not source_words:
        return items

    for item in items:
        original_words = re.findall(
            r"[a-zа-я0-9]+", clean_text(item.get("product_query")), flags=re.I
        )
        normalized_words = [normalize_text(word) for word in original_words]
        supported: list[str] = []
        for original, word in zip(original_words, normalized_words, strict=True):
            if any(
                word == source_word
                or (
                    len(word) >= 4
                    and len(source_word) >= 4
                    and word[0] == source_word[0]
                    and SequenceMatcher(None, word, source_word).ratio() >= 0.72
                )
                for source_word in source_words
            ):
                supported.append(original)
        if supported and len(supported) < len(original_words):
            item["product_query"] = " ".join(supported)
    return items


def collapse_comment_shadow_items(
    items: list[dict[str, Any]], global_comment: str = ""
) -> list[dict[str, Any]]:
    """Удаляет ложный товар, совпадающий с комментарием."""
    shadow_indexes: set[int] = set()
    normalized_global_comment = normalize_text(global_comment).strip(" .,;")
    if normalized_global_comment:
        shadow_indexes.update(
            index
            for index, item in enumerate(items)
            if normalize_text(item.get("product_query")).strip(" .,;") == normalized_global_comment
        )
    for owner_index, owner in enumerate(items):
        owner_comments = {
            normalize_text(owner.get(field)).strip(" .,;")
            for field in ("comment", "user_comment_to_supplier")
            if normalize_text(owner.get(field)).strip(" .,;")
        }
        if not owner_comments:
            continue
        for shadow_index, shadow in enumerate(items):
            if shadow_index == owner_index:
                continue
            shadow_query = normalize_text(shadow.get("product_query")).strip(" .,;")
            if not shadow_query:
                continue
            if shadow_query in owner_comments:
                if owner.get("quantity") is None and shadow.get("quantity") is not None:
                    owner["quantity"] = shadow.get("quantity")
                    owner["unit"] = shadow.get("unit") or owner.get("unit") or ""
                shadow_indexes.add(shadow_index)
                continue

            # The model can duplicate a boundary between two spoken items:
            # "желательно холодным. И говядина".  The first half is already
            # owned by the syrup comment and the second half is already the
            # next product, so the combined third item carries no new fact.
            for comment in owner_comments:
                if not shadow_query.startswith(comment):
                    continue
                remainder = shadow_query[len(comment) :].strip(" .,;")
                remainder = re.sub(r"^(?:и|а также|а)\s+", "", remainder)
                if not remainder:
                    continue
                if any(
                    target_index != shadow_index
                    and target_index != owner_index
                    and (
                        normalize_text(target.get("product_query")).startswith(remainder)
                        or remainder.startswith(normalize_text(target.get("product_query")))
                    )
                    for target_index, target in enumerate(items)
                    if normalize_text(target.get("product_query"))
                ):
                    shadow_indexes.add(shadow_index)
                    break
    return [item for index, item in enumerate(items) if index not in shadow_indexes]


def _strip_global_comment_scope(value: str) -> str:
    """Удаляет слова охвата из начала уже распознанного общего комментария."""
    comment = clean_text(value).strip(" .,;:-—–")
    if not comment:
        return ""
    comment = re.sub(
        r"^(?:(?:и|а)\s+)?(?:все|всё|всем|для всех)"
        r"(?:\s+(?:товар\w*|позици\w*|это(?:\s+дело)?))?"
        r"(?:\s*[,;:—–-]\s*|\s+)",
        "",
        comment,
        flags=re.I,
    )
    return clean_text(comment).strip(" .,;:-—–")


def _remove_matching_quantity(
    text: str,
    quantity: float | None,
    unit: str,
) -> tuple[str, bool]:
    """Удаляет из остатка строки указанное заказанное количество."""
    if quantity is None:
        return text, True
    words = list(re.finditer(r"\d+(?:[,.]\d+)?|[a-zа-яё]+", text, flags=re.I))
    tokens = [normalize_text(match.group()) for match in words]
    expected_unit = normalize_unit(unit)
    for index in range(len(tokens)):
        parsed = parse_number_words(tokens, index)
        if parsed is None:
            continue
        parsed_quantity, end = parsed
        if abs(parsed_quantity - quantity) > 1e-9:
            continue
        actual_unit = ""
        span_end = words[end - 1].end()
        if end < len(tokens) and tokens[end] in UNIT_ALIASES:
            actual_unit = normalize_unit(tokens[end])
            span_end = words[end].end()
        if expected_unit and actual_unit != expected_unit:
            continue
        return f"{text[: words[index].start()]} {text[span_end:]}", True
    return text, False


def _recover_missing_item_comments(
    items: list[dict[str, Any]],
    global_comment: str,
) -> None:
    """Восстанавливает пропущенный локальный комментарий из строки его товара."""
    source_counts: dict[str, int] = {}
    for item in items:
        source = normalize_text(item.get("source_line")).strip(" .,;:-—–")
        if source:
            source_counts[source] = source_counts.get(source, 0) + 1

    for item in items:
        if clean_text(item.get("comment")) or clean_text(item.get("user_comment_to_supplier")):
            continue
        source = normalize_text(item.get("source_line")).strip(" .,;:-—–")
        query = normalize_text(item.get("product_query")).strip(" .,;:-—–")
        if not source or not query or source_counts.get(source, 0) != 1:
            continue
        query_match = re.search(
            rf"(?<![a-zа-яё0-9]){re.escape(query)}(?![a-zа-яё0-9])",
            source,
            flags=re.I,
        )
        if query_match is None:
            continue

        remainder = f"{source[: query_match.start()]} {source[query_match.end() :]}"
        normalized_global = normalize_text(global_comment).strip(" .,;:-—–")
        if normalized_global:
            remainder = re.sub(
                re.escape(normalized_global),
                " ",
                remainder,
                count=1,
                flags=re.I,
            )
        remainder, quantity_removed = _remove_matching_quantity(
            remainder,
            to_float(item.get("quantity")),
            clean_text(item.get("unit")),
        )
        if not quantity_removed:
            continue
        remainder = re.sub(
            r"(?:\b(?:и|а)\s+)?(?:все|всё|всем|для всех)"
            r"(?:\s+(?:товаров|товары|позиций|позиции))?\s*$",
            " ",
            remainder,
            flags=re.I,
        )
        remainder = re.sub(r"^(?:и|а также|а)\s+", "", remainder, flags=re.I)
        remainder = re.sub(r"\s+(?:и|а также|а)$", "", remainder, flags=re.I)
        recovered = clean_text(remainder).strip(" .,;:-—–")
        if recovered:
            item["comment"] = recovered
            item["user_comment_to_supplier"] = recovered


def _append_local_item_comment(item: dict[str, Any], comment: str) -> None:
    """Добавляет локальный комментарий к позиции без повторения текста."""
    addition = clean_text(comment).strip(" .,;:-—–")
    if not addition:
        return
    existing = clean_text(item.get("comment") or item.get("user_comment_to_supplier")).strip(
        " .,;:-—–"
    )
    if normalize_text(addition) == normalize_text(existing):
        merged = existing
    elif existing:
        merged = f"{existing}; {addition}"
    else:
        merged = addition
    item["comment"] = merged
    item["user_comment_to_supplier"] = merged


def _remove_item_global_comment_overlaps(
    items: list[dict[str, Any]],
    global_comment: str,
) -> None:
    """Удаляет продублированный общий комментарий и слова его общего охвата."""
    if not clean_text(global_comment):
        return
    for item in items:
        local_comment = clean_text(item.get("comment") or item.get("user_comment_to_supplier"))
        cleaned = remove_global_comment_overlap(local_comment, global_comment)
        item["comment"] = cleaned
        item["user_comment_to_supplier"] = cleaned


def recover_omitted_explicit_items(payload: dict[str, Any], source_text: str) -> dict[str, Any]:
    """Восстанавливает пропущенные явно названные товары."""
    items = list(payload.get("items") or [])
    _clear_unknown_item_placeholders(items)
    intent = Intent(payload.get("intent", Intent.UNKNOWN))
    if intent not in {Intent.ADD_ITEMS, Intent.UNKNOWN}:
        # Навигация не может содержать товары. Иначе защитное восстановление
        # превращает фразы вроде «показать товары поставщика» в позицию заявки.
        payload["items"] = []
        return payload

    deterministic = parse_product_lines(source_text)
    if not items and deterministic:
        payload["intent"] = Intent.ADD_ITEMS
        payload["items"] = [item.model_dump() for item in deterministic]
        return payload

    items = remove_unsupported_query_qualifiers(items, source_text)
    known_queries = [normalize_text(item.get("product_query")) for item in items]
    for recovered in deterministic:
        recovered_query = normalize_text(recovered.product_query)
        if not recovered_query:
            continue
        # Do not replace a model-selected name (for example a corrected
        # spelling), but append an explicitly spoken product the model omitted.
        if any(
            _query_is_already_represented(known, recovered_query)
            for known in known_queries
            if known
        ):
            continue
        items.append(recovered.model_dump())
        known_queries.append(recovered_query)

    restored = restore_explicit_order_terms(items, source_text)
    global_comment = _strip_global_comment_scope(clean_text(payload.get("global_comment")))
    if global_comment and restored and not has_explicit_global_comment_scope(source_text):
        _append_local_item_comment(restored[-1], global_comment)
        global_comment = ""
    payload["global_comment"] = global_comment
    _remove_item_global_comment_overlaps(restored, global_comment)
    _recover_missing_item_comments(restored, global_comment)
    payload["items"] = collapse_comment_shadow_items(restored, global_comment)
    return payload


_TEXT_SYSTEM = """
Ты строгий маршрутизатор диалога и парсер текста для Telegram-бота, который собирает заявки на товары.
Верни только структуру по заданной схеме. Ты не ищешь товар в каталоге и не меняешь черновик.

Правила:
- Для списка используй intent=add_items; каждая отдельная позиция — отдельный item.
- Используй intent=add_items только когда пользователь назвал хотя бы один конкретный товар.
- Сообщение о работе бота, ошибке или проблеме не является товаром. Например,
  «не работает», «бот сломался» или «у меня ошибка» — это small_talk с пустым items.
- Если пользователь просит перейти к добавлению товаров, но не называет товар,
  верни intent=add_more и пустой items. Слова «товар», «товары», «товаров»,
  «позиция», «позиции» и «продукты» сами по себе не являются названием товара.
- Навигационная команда может быть длинной, разговорной, с вводными словами и любым
  порядком слов. Определи действие по смыслу, не превращай всю фразу в product_query.
- Явное отрицание всегда важнее глагола действия: «не добавлять» — skip_current,
  «не отправляй» — cancel, а не add_more/submit_request. Никогда не выполняй
  добавление, отправку, очистку, выбор или подтверждение, если пользователь их отрицает.
- Примеры навигации: «давай пойдём и добавим товары» → add_more;
  «давай посмотрим наши статусы» → order_status;
  «покажи вторую заявку», «открой второй заказ» → order_status с selected_index=2;
  «покажи следующие заявки», «вернись к списку заявок» → order_status;
  «можно вернуться и открыть черновик» → show_cart;
  «новый заказ», «хочу оформить ещё одну заявку» → start_new_order;
  «запросы снабженцу», «посмотреть запросы» → product_add_list.
- Поддерживай свободный порядок товара, количества, единицы и комментария, разную пунктуацию, запятые, тире, слеши и голосовые оговорки.
- Сохраняй исходную строку в source_line. Комментарий сохраняй в comment и не включай его в product_query.
- Комментарий может быть любым текстом пользователя. Порядок слов свободный; не используй закрытый словарь комментариев.
- Возвращай global_comment только при явном указании общего охвата: «всё», «всем товарам»,
  «для всех позиций», «для всей заявки», «ко всему заказу» или «общий комментарий».
- Пожелание после последнего товара без такого указания относится только к последнему товару,
  даже если это время доставки, дата, качество или способ упаковки. Сохрани его в item.comment.
- Никогда не создавай отдельный item из комментария или из фрагмента между двумя товарами.
- Слова о качестве, обработке, доставке, замене и пожеланиях пользователя сохраняй дословно в comment, а не в product_query.
- Незнакомое или написанное с опечаткой слово рядом с категорией товара может быть частью названия.
  Не переноси такое слово в comment только потому, что оно написано неправильно.
- Отделяй от возможного названия только явное пожелание о качестве, состоянии, обработке,
  доставке или замене. Само возможное название сохраняй дословно, без исправления.
- Пример: «говядина 10 кг мраморная без кожи» → product_query="говядина", quantity=10, unit="кг", comment="мраморная без кожи".
- Пример с опечаткой: «сироп рза холодным» → product_query="сироп рза", comment="холодным".
- Пример общего комментария: «всё привезти до 9 утра» → global_comment="привезти до 9 утра" и не создавай из него товар.
- Пример локального комментария последней позиции: «куриные лапы 15 кг. Желательно завтра с 9 до 14»
  → у куриных лап comment="Желательно завтра с 9 до 14", global_comment пустой.
- Пример локальных и общего комментариев: «сироп тархун в бутылках 10 шт, сироп роза 1 шт в банках, и всё желательно на завтра»
  → у тархуна comment="в бутылках", у розы comment="в банках", global_comment="желательно на завтра".
- Разговорная связка общего охвата не является локальным комментарием:
  «сироп роза 5 шт, главное быстро, и сироп тархун — всё это дело на завтра»
  → у розы comment="главное быстро", у тархуна comment="", global_comment="на завтра".
- Не выдумывай товар, количество, бренд, поставщика или характеристики.
- Заполняй supplier_hint только когда пользователь действительно назвал поставщика.
  Не считай неизвестное слово после товара поставщиком без явных оснований.
- Если количество без единицы, сохрани число, а unit оставь пустым.
- Число в фасовке или объёме внутри названия не является количеством заказа, если заказанное количество указано отдельно после тире или в конце.
- Неполное или неоднозначное название сохраняй как его сообщил пользователь. Не заменяй его своим вариантом.

Примеры:
«сироп роза 10 штук говядина 5 кг и джем 10 штук» — три позиции;
«говядина 10 кг, только охлаждённая» — товар говядина, количество 10 кг, комментарий только охлаждённая;
«Сироп Роза 1 л — 12» — название Сироп Роза 1 л, количество 12.

Поддерживаемые intent: add_items, remove_item, skip_current, clarify_current, manual_current,
clear_cart, confirm, cancel, show_cart, submit_request, edit_quantity, select_candidate, add_more,
greeting, help, thanks, small_talk, back, continue_current, check_min_sum, add_supplier_items,
submit_as_is, accept_suggested_quantity, keep_current_quantity, enter_other_quantity,
use_catalog_unit, show_final_review, order_status, product_add_list, start_new_order, unknown.
Для просмотра ранее созданных запросов снабженцу используй product_add_list.
""".strip()

_PHOTO_SYSTEM = """
Распознай фотографию заявки ресторана. Извлеки только реально видимые товарные строки.
Определи document_type:
- client_order_sheet — наша таблица с колонками «Зал», «Бар», «Кухня»;
- printed_order_form — бланк поставщика с печатной фасовкой и отдельной крайней правой ячейкой заказа;
- order_table — обычная таблица, где число является фактическим количеством заказа;
- free_list — обычный печатный или рукописный список;
- product_card или unknown — остальные изображения.

КРИТИЧЕСКОЕ ПРАВИЛО ОПРЕДЕЛЕНИЯ БЛАНКА ПОСТАВЩИКА:
- Таблица вида «название товара | печатная фасовка | узкая ячейка заказа» является printed_order_form.
- Это правило действует и для обрезанного фрагмента без заголовка страницы.
- Заголовок секции с именем поставщика, компанией или телефоном дополнительно подтверждает printed_order_form.
- Если одинаково оформленные печатные количества стоят почти в каждой строке средней колонки,
  а рукописные отметки встречаются только в отдельной правой колонке, средняя колонка — фасовка.
- Такой фрагмент запрещено классифицировать как order_table или free_list.

Печатные справочные значения, остатки, фасовку, цены, телефоны, номера строк и заголовки
не превращай в количество фактического заказа.
Комментарий может быть любым текстом. Порядок слов свободный; не используй закрытый словарь комментариев.
Если комментарий относится ко всем позициям, верни его в global_comment, индивидуальный — в comment.
Верни intent=add_items, document_type и items с product_query, quantity, unit, department, supplier_hint, comment, source_line,
source_department, department_quantities, quantity_source, printed_reference_text, order_entry_text, order_entry_type.
Если это наша таблица заявки с колонками «Зал», «Бар», «Кухня», верни все три значения в department_quantities
в полях hall, bar, kitchen соответственно. Количество может быть напечатано или вписано ручкой.
Для client_order_sheet всегда оставляй supplier_hint пустым. Поставщик будет определён по найденной строке
живого каталога. Не переноси поставщика из соседней строки таблицы на текущий товар.
Если печатное значение зачёркнуто и рядом указано новое, верни только новое рукописное значение.
Строку без положительного количества во всех трёх колонках полностью пропускай.
Не переноси значение из соседней строки: количество, подразделение и комментарий относятся только к товару
на той же горизонтальной строке. Если ячейки «Зал», «Бар» и «Кухня» текущего товара пусты, полностью пропусти именно эту строку,
даже когда строка ниже содержит количество. Значение в строке ниже относится только к товару в строке ниже.

КРИТИЧЕСКОЕ ПРАВИЛО ДЛЯ printed_order_form:
- Фактическим заказом является только отдельное разборчивое число в крайней правой ячейке строки.
- Фасовка в средней колонке не является заказом, даже если выглядит как «2 кг», «5 шт»,
  «8 кг», «10 кг», «12 л», «5 кг/5 кг» или «3/3 кг».
- Пустая правая ячейка, прочерк, минус, линия или неразборчивая отметка без цифры означает:
  товар не заказывают. Полностью пропусти такую строку.
- Просматривай правую ячейку каждой строки отдельно. Определи строку цифры по горизонтальным границам
  ячейки; не переноси рукописное число в строку выше или ниже.
- Мелкая, наклонная, синяя или чёрная рукописная цифра остаётся количеством, если она разборчива.
- Если печатное значение в правой ячейке зачёркнуто и рядом указано новое число ручкой,
  используй только новое число: рукописные исправления всегда заменяют старое значение.
- Перед ответом второй раз проверь: у каждого возвращённого item действительно заполнена
  отдельная правая ячейка именно в его строке.

Фактическое значение ячейки заказа верни в order_entry_text. Для напечатанного количества заказа используй
order_entry_type=typed_order_entry, для рукописного — handwritten, для рукописного вместо зачёркнутого —
handwritten_correction. Если старое значение зачёркнуто, итоговое рукописное значение его заменяет.
Никогда не суммируй зачёркнутое старое и новое значения.
Для free_list и order_table извлекай фактические количества из строки или колонки заказа.
Для order_table также запиши фактическое число в order_entry_text и укажи order_entry_type.
Добавляй товар только при наличии положительного количества заказа; строки без количества полностью пропускай.
Если число неразборчиво или непонятно, относится ли оно к строке, пропусти строку и ничего не угадывай.
Проверь каждую строку отдельно.
""".strip()

_MATCH_SYSTEM = """
Ты проверяешь сопоставление пользовательского товара с кандидатами каталога.
Разрешено выбрать только product_id из переданного списка. Единственный кандидат не означает, что он подходит.

Верни select только когда кандидат обозначает тот же самый товар и confidence не ниже 0.90.
Все явно названные пользователем свойства должны быть совместимы: вид продукта, часть туши, наличие или
отсутствие костей, кожи и хрящей, обработка, форма, сорт, вкус и фасовка. Отрицания пользователя обязательны.
В product_context может быть комментарий к этой позиции. Учитывай из него только признаки самого товара
(например, «без костей» или «спелая»), но не считай свойствами товара пожелания о доставке или сроке
вроде «на завтра» и «привезти отдельно».
Если кандидат лишь относится к похожей категории, но не является тем же товаром, верни ambiguous — бот
покажет его только как возможную подсказку. Если кандидат является другим продуктом или противоречит
явным требованиям, верни not_found и перечисли противоречия в contradictions.

Примеры:
- «кукуруза спелая» и «крупа кукурузная» — ambiguous: продукты связаны, но это не один товар;
- «свинина без костей, без шкуры, без хрящей» и «сало свиное» — not_found: другой продукт;
- небольшая ошибка распознавания в названии при совпадении самого товара — select.

Если подходят несколько — ambiguous. Если ни один — not_found. Не используй внешние знания для
выдумывания позиций. confidence показывает уверенность именно в выбранном action.
""".strip()

_VISIBLE_ACTION_SYSTEM = """
Ты определяешь, хочет ли пользователь выполнить одно из действий, доступных на текущем экране
Telegram-бота. Выбирай только action_id из переданного списка actions.

Пользователь может говорить разговорно, менять порядок слов, использовать синонимы, называть
номер кнопки, часть её текста или смысл действия. Если фраза является названием товара,
комментарием, количеством или не относится ни к одной кнопке, верни пустой action_id.
Отрицание действия запрещает выбирать противоположную положительную кнопку.
Не выдумывай действие. confidence >= 0.9 ставь только при однозначном соответствии.
""".strip()


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
        """Извлекает структурированную команду из текста."""
        deterministic = infer_intent(text)
        if deterministic.intent not in {Intent.UNKNOWN, Intent.ADD_ITEMS}:
            logger.info(
                "text_command_deterministic",
                text=text,
                intent=deterministic.intent.value,
                item_count=len(deterministic.items),
            )
            return deterministic
        if self._can_use_deterministic_product_list(text, deterministic):
            logger.info(
                "text_product_list_deterministic",
                text=text,
                intent=deterministic.intent.value,
                items=[self._item_log(item) for item in deterministic.items],
            )
            return deterministic
        if self._can_use_deterministic_single_product_with_quantity(text, deterministic):
            logger.info(
                "text_single_product_deterministic",
                text=text,
                intent=deterministic.intent.value,
                items=[self._item_log(item) for item in deterministic.items],
            )
            return deterministic
        if self._can_use_deterministic_short_product(text, deterministic):
            logger.info(
                "text_short_product_deterministic",
                text=text,
                intent=deterministic.intent.value,
                items=[self._item_log(item) for item in deterministic.items],
            )
            return deterministic
        if self._can_use_deterministic_packaged_product(text, deterministic):
            logger.info(
                "text_packaged_product_deterministic",
                text=text,
                intent=deterministic.intent.value,
                items=[self._item_log(item) for item in deterministic.items],
            )
            return deterministic
        if self._should_skip_ai(text):
            logger.info(
                "text_ai_skipped",
                text=text,
                intent=deterministic.intent.value,
                item_count=len(deterministic.items),
            )
            return deterministic
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
            return deterministic
        logger.info(
            "text_ai_parsed",
            text=text,
            intent=parsed.intent.value,
            global_comment=parsed.global_comment,
            items=[self._item_log(item) for item in parsed.items],
        )
        payload = recover_omitted_explicit_items(parsed.model_dump(), text)
        for item in payload.get("items", []):
            if item.get("user_comment_to_supplier") and not item.get("comment"):
                item["comment"] = item["user_comment_to_supplier"]
        command = ParsedCommand.model_validate(payload)
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
        ):
            logger.warning(
                "text_ai_navigation_rejected",
                text=text,
                ai_intent=command.intent.value,
                fallback_intent=deterministic.intent.value,
                fallback_items=[self._item_log(item) for item in deterministic.items],
            )
            command = deterministic
        logger.info(
            "text_command_normalized",
            intent=command.intent.value,
            items=[self._item_log(item) for item in command.items],
        )
        return command

    @staticmethod
    def _can_use_deterministic_product_list(text: str, command: ParsedCommand) -> bool:
        """Проверяет возможность разбора списка без вызова ИИ."""
        if command.intent != Intent.ADD_ITEMS or len(command.items) < 2:
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
        item = command.items[0]
        normalized_units = set(UNIT_ALIASES.values())
        raw_source = clean_text(text)
        source = raw_source.rstrip(" .!?")
        item_source = clean_text(item.source_line).rstrip(" .!?")
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
        )

    @staticmethod
    def _looks_like_support_message(text: str) -> bool:
        """Определяет жалобу на работу бота, которую должен разобрать ИИ."""
        normalized = normalize_text(text)
        return bool(
            re.search(
                r"(?:^|\s)(?:не\s+(?:работает|срабатывает|получается|отвечает)|"
                r"ошибк\w*|сбой\w*|проблем\w*|сломал\w*|завис\w*)(?:\s|$)",
                normalized,
            )
        )

    @staticmethod
    def _can_use_deterministic_packaged_product(text: str, command: ParsedCommand) -> bool:
        """Распознаёт одно точное название с компактной записью фасовки."""
        if command.intent != Intent.ADD_ITEMS or len(command.items) != 1:
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
                    output={"transcript_characters": len(text)},
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
        with path.open("rb") as audio:
            request["model"] = (
                self.settings.openai_transcribe_model
                if high_accuracy
                else self.settings.openai_transcribe_fallback_model
            )
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
                    output={"transcript_characters": len(fallback_text)},
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
        command = ParsedCommand.model_validate(parsed.model_dump())
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
