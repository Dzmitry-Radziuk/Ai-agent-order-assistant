"""Нормализует наблюдение фотографии в авторизованные строки заказа."""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass

import structlog

from restaurant_bot.config import Settings
from restaurant_bot.domain.models import (
    CommentSource,
    DepartmentQuantities,
    ExtractedItem,
    Intent,
    ParsedCommand,
)
from restaurant_bot.domain.text import clean_text
from restaurant_bot.domain.units import UNIT_ALIASES, normalize_unit
from restaurant_bot.parsing.ai.schemas import PhotoDocumentObservation, PhotoRowObservation

logger = structlog.get_logger(__name__)

_DEPARTMENT_HEADERS = {
    "hall": {"зал", "hall"},
    "bar": {"бар", "bar"},
    "kitchen": {"кухня", "kitchen"},
}
_ORDER_HEADERS = {
    "количество",
    "заказ",
    "заказано",
    "фактический заказ",
    "количество заказа",
    "actual_order_quantity",
    "actual order quantity",
    "order_quantity",
    "order quantity",
    "ordered_quantity",
    "quantity_decimal",
}
_COMMENT_HEADERS = {"комментарий", "комментарии", "comment", "comments", "примечание"}
_REFERENCE_HEADERS = {
    "фасовка",
    "упаковка",
    "характеристики",
    "артикул",
    "reference",
    "package",
    "packaging",
}
_CARD_HEADERS = {"цена", "остаток", "price", "stock", "фасовка", "упаковка", "package", "packaging"}
_NUMBER_RE = re.compile(r"(?<![\w-])\d+(?:[,.]\d+)?(?![\w-])")
_UNIT_RE = re.compile(
    r"(?P<unit>"
    + "|".join(re.escape(alias) for alias in sorted(UNIT_ALIASES, key=len, reverse=True))
    + r")\b",
    flags=re.IGNORECASE,
)
_GLOBAL_COMMENT_RE = re.compile(
    r"(?:комментар\w*\s+(?:ко\s+всей\s+заявк\w*|для\s+всех\s+позиц\w*)|для\s+всех\s+позиц\w*)",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class PhotoNormalizationResult:
    """Возвращает авторизованные строки и детерминированную диагностику фото."""

    command: ParsedCommand
    document_type: str
    admitted_rows: int
    dropped_rows: int
    reason: str = ""


@dataclass(frozen=True, slots=True)
class PhotoOrderAreaIntegrity:
    """Описывает полноту именно значимых для заказа данных фотографии."""

    decision: str
    reason: str
    row_count_mismatch: bool


def canonical_photo_identity(value: str) -> str:
    """Канонизирует только безопасные пробелы, кавычки, пунктуацию и единицы."""
    text = unicodedata.normalize("NFKC", clean_text(value)).casefold().replace("ё", "е")
    text = (
        text.replace("«", "")
        .replace("»", "")
        .replace("“", "")
        .replace("”", "")
        .replace('"', "")
        .replace("'", "")
    )
    text = re.sub(r"\s*([,;:/()\-])\s*", r"\1", text)

    def replace_unit(match: re.Match[str]) -> str:
        """Канонизирует единицу измерения внутри полного product identity."""
        return f" {normalize_unit(match.group('unit'))}"

    text = re.sub(r"(?<=\d)\s*" + _UNIT_RE.pattern, replace_unit, text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def classify_photo_document(observation: PhotoDocumentObservation) -> str:
    """Классифицирует документ по структурным признакам, а не только по proposal модели."""
    if observation.extraction_confidence < 0.5:
        return "unknown"
    columns = {
        canonical_photo_identity(column)
        for column in observation.detected_columns
        if clean_text(column)
    }
    has_departments = all(
        any(header in columns for header in aliases) for aliases in _DEPARTMENT_HEADERS.values()
    )
    has_order_column = any(column in _ORDER_HEADERS for column in columns)
    has_comment_column = bool(columns & _COMMENT_HEADERS)
    has_reference_column = bool(columns & _REFERENCE_HEADERS)
    has_card_evidence = bool(columns & _CARD_HEADERS)
    rows = [row for row in observation.rows if clean_text(row.product_text)]
    proposal = canonical_photo_identity(observation.document_type_proposal)
    has_aligned_explicit_order = any(
        _row_has_order_evidence(row)
        and row.row_alignment_confidence >= 0.9
        and row.quantity_confidence >= 0.9
        for row in rows
    )

    if (
        observation.has_table_structure
        and has_departments
        and (
            rows
            or (
                not observation.rows
                and observation.visible_product_row_count
                and not observation.order_area_complete
            )
        )
    ):
        return "client_order_sheet"
    if observation.has_table_structure and has_order_column and rows:
        if has_reference_column and any(_row_has_order_evidence(row) for row in rows):
            return "printed_order_form"
        return "order_table"
    if (
        observation.has_table_structure
        and has_comment_column
        and any(_row_has_order_evidence(row) for row in rows)
    ):
        return "order_table"
    if (
        observation.has_table_structure
        and rows
        and has_aligned_explicit_order
        and proposal in {"client_order_sheet", "order_table", "printed_order_form"}
        and not has_card_evidence
    ):
        return "order_table"
    if has_card_evidence and not has_order_column and not has_departments:
        return "product_card"
    if (
        canonical_photo_identity(observation.document_type_proposal) == "product_card"
        and not has_order_column
        and not has_departments
    ):
        return "product_card"
    if not observation.has_table_structure and any(_row_has_order_evidence(row) for row in rows):
        return "free_list"
    if canonical_photo_identity(observation.document_type_proposal) == "product_card":
        return "product_card"
    return "unknown"


def normalize_photo_observation(
    observation: PhotoDocumentObservation,
    settings: Settings,
    *,
    require_sheet_row_numbers: bool = False,
) -> PhotoNormalizationResult:
    """Проверяет строки фотографии и превращает только разрешённые строки в ParsedCommand."""
    document_type = classify_photo_document(observation)
    integrity = photo_order_area_integrity(
        observation,
        document_type,
        require_sheet_row_numbers=require_sheet_row_numbers,
    )
    authoritative_sheet_rows = photo_sheet_row_mapping_is_authoritative(
        observation,
        require_sheet_row_numbers=require_sheet_row_numbers,
    )
    logger.info(
        "photo_scan_integrity",
        visible_product_row_count=observation.visible_product_row_count,
        returned_row_count=len(observation.rows),
        row_count_mismatch=integrity.row_count_mismatch,
        order_area_complete=observation.order_area_complete,
        uncertain_order_row_count=observation.uncertain_order_row_count,
        sheet_row_numbers_visible=observation.sheet_row_numbers_visible,
        require_sheet_row_numbers=require_sheet_row_numbers,
        authoritative_sheet_rows=authoritative_sheet_rows,
        decision=integrity.decision,
        reason=integrity.reason,
    )
    if integrity.decision == "incomplete_order_evidence":
        logger.warning(
            "photo_scan_integrity_rejected",
            reason=integrity.reason,
        )
        return PhotoNormalizationResult(
            command=ParsedCommand(
                intent=Intent.ADD_ITEMS,
                photo_outcome="incomplete_photo_read",
            ),
            document_type="incomplete",
            admitted_rows=0,
            dropped_rows=len(observation.rows),
            reason=integrity.reason,
        )
    if document_type == "client_order_sheet":
        unresolved_rows = [
            row for row in observation.rows if _client_sheet_quantity_without_department(row)
        ]
        if unresolved_rows:
            logger.warning(
                "photo_client_sheet_quantity_without_department",
                row_indexes=[row.row_index for row in unresolved_rows],
                row_count=len(unresolved_rows),
            )
            return PhotoNormalizationResult(
                command=ParsedCommand(
                    intent=Intent.ADD_ITEMS,
                    photo_outcome="incomplete_photo_read",
                ),
                document_type="incomplete",
                admitted_rows=0,
                dropped_rows=len(observation.rows),
                reason="client_sheet_quantity_without_department",
            )
    logger.info(
        "photo_document_classified",
        proposed_type=canonical_photo_identity(observation.document_type_proposal),
        final_type=document_type,
        detected_columns=[
            canonical_photo_identity(column) for column in observation.detected_columns[:20]
        ],
        row_count=len(observation.rows),
        reason="structural_evidence"
        if document_type != "unknown"
        else "insufficient_structural_evidence",
    )
    if document_type in {"unknown", "product_card"}:
        return PhotoNormalizationResult(
            command=ParsedCommand(
                intent=Intent.ADD_ITEMS,
                photo_outcome=(
                    "unsupported_photo" if document_type in {"unknown", "product_card"} else ""
                ),
                global_comment=_authorized_document_comment(observation),
            ),
            document_type=document_type,
            admitted_rows=0,
            dropped_rows=len(observation.rows),
            reason="no_authorized_order_contract",
        )

    items: list[ExtractedItem] = []
    dropped = 0
    for row in sorted(observation.rows, key=_visual_row_sort_key):
        item = _authorize_row(
            row,
            document_type,
            settings.default_department,
            sheet_row_number_authoritative=authoritative_sheet_rows,
        )
        decision = "admitted" if item is not None else "dropped"
        logger.info(
            "photo_row_admission_decision",
            row_index=row.row_index,
            visual_row_index=row.visual_row_index,
            sheet_row_number=row.sheet_row_number
            if row.sheet_row_number_confidence >= 0.9
            else None,
            document_type=document_type,
            has_product=bool(clean_text(row.product_text)),
            hall=row.hall_quantity,
            bar=row.bar_quantity,
            kitchen=row.kitchen_quantity,
            order_entry_present=bool(
                clean_text(row.order_entry_text) or row.explicit_order_quantity is not None
            ),
            quantity_source=item.quantity_source if item is not None else "",
            decision=decision,
            reason="same_row_positive_quantity"
            if item is not None
            else "missing_or_uncertain_same_row_quantity",
        )
        if item is None:
            dropped += 1
        else:
            items.append(item)

    global_comment = _authorized_document_comment(observation)
    return PhotoNormalizationResult(
        command=ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=items,
            photo_outcome="no_order_quantities" if not items else "",
            global_comment=global_comment,
        ),
        document_type=document_type,
        admitted_rows=len(items),
        dropped_rows=dropped,
        reason="rows_authorized" if items else "no_filled_quantities_found",
    )


def photo_order_area_integrity(
    observation: PhotoDocumentObservation,
    document_type: str,
    *,
    require_sheet_row_numbers: bool = False,
) -> PhotoOrderAreaIntegrity:
    """Определяет, есть ли риск пропуска значимой строки заказа."""
    row_count_mismatch = (
        observation.visible_product_row_count is not None
        and len(observation.rows) != observation.visible_product_row_count
    )
    if document_type in {"unknown", "product_card"}:
        return PhotoOrderAreaIntegrity(
            decision="no_order_evidence",
            reason="order_area_not_applicable",
            row_count_mismatch=row_count_mismatch,
        )
    if not observation.order_area_complete:
        return PhotoOrderAreaIntegrity(
            decision="incomplete_order_evidence",
            reason="potential_order_row_unreadable",
            row_count_mismatch=row_count_mismatch,
        )
    if observation.uncertain_order_row_count:
        return PhotoOrderAreaIntegrity(
            decision="incomplete_order_evidence",
            reason="uncertain_potential_order_row",
            row_count_mismatch=row_count_mismatch,
        )
    has_order_evidence = any(_row_has_potential_order_evidence(row) for row in observation.rows)
    sheet_rows_required = require_sheet_row_numbers
    if (
        sheet_rows_required
        and has_order_evidence
        and not photo_sheet_row_mapping_is_authoritative(
            observation,
            require_sheet_row_numbers=require_sheet_row_numbers,
        )
    ):
        return PhotoOrderAreaIntegrity(
            decision="incomplete_order_evidence",
            reason="sheet_row_identity_unreadable",
            row_count_mismatch=row_count_mismatch,
        )
    return PhotoOrderAreaIntegrity(
        decision="complete" if has_order_evidence else "no_order_evidence",
        reason=(
            "blank_row_count_mismatch_irrelevant"
            if row_count_mismatch
            else "order_area_evidence_complete"
            if has_order_evidence
            else "no_order_evidence"
        ),
        row_count_mismatch=row_count_mismatch,
    )


def photo_sheet_row_mapping_is_authoritative(
    observation: PhotoDocumentObservation,
    *,
    require_sheet_row_numbers: bool = False,
) -> bool:
    """Подтверждает уникальные номера строк для всех фактически заполненных строк заказа."""
    if not require_sheet_row_numbers:
        return False
    ordered_rows = [
        row
        for row in sorted(observation.rows, key=_visual_row_sort_key)
        if _row_has_potential_order_evidence(row)
    ]
    if not ordered_rows:
        return False
    row_numbers: list[int] = []
    for row in ordered_rows:
        if row.sheet_row_number is None or row.sheet_row_number_confidence < 0.9:
            return False
        row_numbers.append(row.sheet_row_number)
    return row_numbers == sorted(set(row_numbers))


def _visual_row_sort_key(row: PhotoRowObservation) -> tuple[int, int]:
    """Сортирует строки по локальному visual index с совместимостью старого row_index."""
    return (
        row.visual_row_index if row.visual_row_index is not None else row.row_index,
        row.row_index,
    )


def _authorize_row(
    row: PhotoRowObservation,
    document_type: str,
    default_department: str,
    *,
    sheet_row_number_authoritative: bool = False,
) -> ExtractedItem | None:
    """Авторизует одну строку без использования соседних строк или fuzzy-данных."""
    product_text = clean_text(row.product_text)
    if not product_text or row.product_confidence < 0.5 or row.row_alignment_confidence < 0.5:
        return None

    if document_type == "client_order_sheet":
        quantities = DepartmentQuantities(
            hall=_positive_or_none(row.hall_quantity),
            bar=_positive_or_none(row.bar_quantity),
            kitchen=_positive_or_none(row.kitchen_quantity),
        )
        positive = [
            value for value in (quantities.hall, quantities.bar, quantities.kitchen) if value
        ]
        if not positive or row.quantity_confidence < 0.5:
            return None
        quantity = sum(positive)
        quantity_source = (
            "handwritten_correction"
            if row.order_entry_type == "handwritten_correction"
            else "department_columns"
        )
        unit = _row_unit(row)
        exact_provenance = "venue_table_exact_candidate"
    else:
        if row.quantity_confidence < 0.5:
            return None
        resolved = _resolve_row_quantity(row)
        if resolved is None:
            return None
        quantity, unit, quantity_source = resolved
        quantities = DepartmentQuantities()
        exact_provenance = ""

    comment = (
        clean_text(row.comment_text)
        if row.comment_source in {"explicit_marker", "user_note"}
        else ""
    )
    return ExtractedItem(
        product_query=product_text,
        quantity=quantity,
        unit=unit,
        department=default_department,
        supplier_hint=""
        if document_type == "client_order_sheet"
        else clean_text(row.supplier_hint),
        comment=comment,
        comment_source=CommentSource.EXPLICIT_MARKER if comment else CommentSource.NONE,
        source_line=clean_text(row.row_text) or product_text,
        source_span=clean_text(row.row_text) or product_text,
        department_quantities=quantities,
        quantity_source=quantity_source,
        printed_reference_text=clean_text(row.printed_reference_text),
        order_entry_text=clean_text(row.order_entry_text),
        order_entry_type=row.order_entry_type,
        packaging_text=clean_text(row.printed_reference_text),
        packaging_role="catalog_attribute" if row.printed_reference_text else "none",
        catalog_identity_provenance=exact_provenance,
        photo_sheet_row_number=(row.sheet_row_number if sheet_row_number_authoritative else None),
        photo_sheet_row_number_confidence=(
            row.sheet_row_number_confidence if sheet_row_number_authoritative else 0
        ),
        photo_sheet_row_number_authoritative=sheet_row_number_authoritative,
    )


def _resolve_row_quantity(row: PhotoRowObservation) -> tuple[float, str, str] | None:
    """Разрешает quantity только из текущей строки и применяет правило исправления."""
    crossed = _single_number(row.crossed_out_quantity_text)
    corrected = _single_number(row.corrected_quantity_text)
    if crossed is not None:
        if corrected is None:
            return None
        return corrected, _row_unit(row), "handwritten_correction"
    if corrected is not None:
        return corrected, _row_unit(row), "handwritten_correction"
    if len(row.active_quantity_texts) > 1:
        return None
    if len(_numbers(row.order_entry_text)) > 1:
        return None
    if len(_numbers(row.handwritten_quantity_text)) > 1:
        return None
    quantity = row.explicit_order_quantity
    if quantity is None:
        quantity = _single_number(row.order_entry_text) or _single_number(
            row.handwritten_quantity_text
        )
    if quantity is None or not math.isfinite(quantity) or quantity <= 0:
        return None
    source = row.order_entry_type or (
        "handwritten" if row.handwritten_quantity_text else "photo_order_entry"
    )
    return quantity, _row_unit(row), source


def _row_has_order_evidence(row: PhotoRowObservation) -> bool:
    """Проверяет положительное evidence заказа в самой строке."""
    return (
        row.explicit_order_quantity is not None
        or bool(clean_text(row.order_entry_text))
        or bool(clean_text(row.handwritten_quantity_text))
        or bool(clean_text(row.corrected_quantity_text))
    )


def _row_has_potential_order_evidence(row: PhotoRowObservation) -> bool:
    """Проверяет department и обычные order evidence одной визуальной строки."""
    return any(
        _positive_or_none(value) is not None
        for value in (row.hall_quantity, row.bar_quantity, row.kitchen_quantity)
    ) or _row_has_order_evidence(row)


def _client_sheet_quantity_without_department(row: PhotoRowObservation) -> bool:
    """Находит quantity evidence клиентского листа без подтверждённой колонки отдела."""
    has_department_quantity = any(
        _positive_or_none(value) is not None
        for value in (row.hall_quantity, row.bar_quantity, row.kitchen_quantity)
    )
    return _row_has_order_evidence(row) and not has_department_quantity


def _authorized_document_comment(observation: PhotoDocumentObservation) -> str:
    """Возвращает только явно обозначенный комментарий ко всему документу."""
    comment = clean_text(observation.document_comment)
    if observation.document_comment_scope != "order" or not comment:
        return ""
    marker = _GLOBAL_COMMENT_RE.search(comment)
    if marker is None:
        return ""
    return clean_text(comment[marker.end() :].lstrip(" :-—–")) or comment


def _positive_or_none(value: float | None) -> float | None:
    """Оставляет только конечное положительное число из ячейки отдела."""
    return value if value is not None and math.isfinite(value) and value > 0 else None


def _numbers(value: str) -> list[float]:
    """Извлекает числа только для проверки неоднозначности одной ячейки."""
    result: list[float] = []
    for raw in _NUMBER_RE.findall(value):
        try:
            number = float(raw.replace(",", "."))
        except ValueError:
            continue
        if math.isfinite(number):
            result.append(number)
    return result


def _single_number(value: str) -> float | None:
    """Возвращает число только из однозначного текстового evidence."""
    numbers = _numbers(value)
    return numbers[0] if len(numbers) == 1 else None


def _row_unit(row: PhotoRowObservation) -> str:
    """Канонизирует единицу из той же order-ячейки или handwritten evidence."""
    if clean_text(row.explicit_order_unit):
        return normalize_unit(row.explicit_order_unit)
    for text in (row.corrected_quantity_text, row.order_entry_text, row.handwritten_quantity_text):
        match = _UNIT_RE.search(text)
        if match:
            return normalize_unit(match.group("unit"))
    return ""
