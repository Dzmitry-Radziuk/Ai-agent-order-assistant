"""Сверяет структурированные наблюдения vision с геометрией фото и между собой."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from typing import cast

import structlog

from restaurant_bot.config import Settings
from restaurant_bot.domain.models import ExtractedItem, Intent, ParsedCommand
from restaurant_bot.domain.text import clean_text
from restaurant_bot.domain.units import normalize_unit
from restaurant_bot.input.photo_ingestion import (
    canonical_photo_identity,
    classify_photo_document,
    normalize_photo_observation,
    photo_row_department_quantities,
    photo_row_has_potential_order_evidence,
    photo_sheet_row_mapping_is_authoritative,
    uses_lettered_department_columns,
)
from restaurant_bot.parsing.ai.schemas import PhotoDocumentObservation, PhotoRowObservation

_LETTERED_DEPARTMENT_COLUMN_FIELDS = (
    ("n", "sheet_column_n_quantity"),
    ("o", "sheet_column_o_quantity"),
    ("p", "sheet_column_p_quantity"),
)


def _vision_image_detail(model: str) -> str:
    """Выбирает исходную детализацию только для моделей, которые её поддерживают."""
    match = re.match(r"gpt-(\d+)\.(\d+)", model.casefold())
    return "original" if match and (int(match[1]), int(match[2])) >= (5, 4) else "high"


def _vision_reasoning_effort(model: str) -> str:
    """Возвращает допустимый минимальный уровень рассуждения для семейства модели."""
    match = re.match(r"gpt-(\d+)\.(\d+)", model.casefold())
    return "none" if match and (int(match[1]), int(match[2])) >= (5, 4) else "minimal"


logger = structlog.get_logger(__name__)


def _photo_observation_matches_preprocessed_count(
    observation: PhotoDocumentObservation,
    expected_filled_order_row_count: int | None,
) -> bool:
    """Сверяет vision-строки с независимым пиксельным подсчётом order-строк."""
    if not expected_filled_order_row_count:
        # Ноль от пиксельного анализатора означает отсутствие уверенного
        # сигнала, но не доказывает, что в таблице нет чисел заказа.
        return True
    observed_order_row_count = sum(
        photo_row_has_potential_order_evidence(row) for row in observation.rows
    )
    return observed_order_row_count == expected_filled_order_row_count


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
    document_type = classify_photo_document(observation)
    observed_order_row_count = sum(
        photo_row_has_potential_order_evidence(row) for row in observation.rows
    )
    reported_count_mismatch = (
        observation.visible_filled_order_row_count is not None
        and observation.visible_filled_order_row_count != observed_order_row_count
    )
    preprocessed_count_mismatch = not _photo_observation_matches_preprocessed_count(
        observation,
        expected_filled_order_row_count,
    )
    if preprocessed_count_mismatch and expected_filled_order_row_count is not None:
        return True
    is_order_document = document_type in {
        "client_order_sheet",
        "order_table",
        "printed_order_form",
        "free_list",
    } or clean_text(observation.document_type_proposal).casefold() in {
        "client_order_sheet",
        "order_table",
        "printed_order_form",
        "free_list",
    }
    return is_order_document and (
        not observation.order_area_complete
        or observation.uncertain_order_row_count > 0
        or sheet_rows_incomplete
        or reported_count_mismatch
    )


def _photo_observation_is_complete(
    observation: PhotoDocumentObservation,
    *,
    require_sheet_row_numbers: bool = False,
    expected_filled_order_row_count: int | None = None,
) -> bool:
    """Проверяет, что повторное наблюдение не содержит неопределённых строк заказа."""
    sheet_rows_complete = not require_sheet_row_numbers or photo_sheet_row_mapping_is_authoritative(
        observation,
        require_sheet_row_numbers=require_sheet_row_numbers,
    )
    return (
        observation.order_area_complete
        and observation.uncertain_order_row_count == 0
        and _photo_observation_matches_preprocessed_count(
            observation,
            expected_filled_order_row_count,
        )
        and sheet_rows_complete
    )


def _lettered_department_reads_match(
    first: PhotoDocumentObservation,
    second: PhotoDocumentObservation,
) -> bool:
    """Подтверждает одинаковый порядок товаров и значения N/O/P в двух чтениях."""
    if not uses_lettered_department_columns(first) or not uses_lettered_department_columns(second):
        return False
    first_rows = [
        row
        for row in first.rows
        if photo_row_has_potential_order_evidence(row)
        or clean_text(row.sheet_column_q_comment)
        or clean_text(row.comment_text)
    ]
    second_rows = [
        row
        for row in second.rows
        if photo_row_has_potential_order_evidence(row)
        or clean_text(row.sheet_column_q_comment)
        or clean_text(row.comment_text)
    ]
    if not first_rows or len(first_rows) != len(second_rows):
        return False

    for first_row, second_row in zip(first_rows, second_rows, strict=True):
        if (
            not first_row.product_text
            or not second_row.product_text
            or canonical_photo_identity(first_row.product_text)
            != canonical_photo_identity(second_row.product_text)
        ):
            return False
        first_quantities = photo_row_department_quantities(first_row, lettered_columns=True)
        second_quantities = photo_row_department_quantities(second_row, lettered_columns=True)
        if first_quantities is None or first_quantities != second_quantities:
            return False
        if not _optional_quantities_match(
            first_row.explicit_order_quantity,
            second_row.explicit_order_quantity,
        ):
            return False
        if normalize_unit(first_row.explicit_order_unit) != normalize_unit(
            second_row.explicit_order_unit
        ):
            return False
        if canonical_photo_identity(first_row.sheet_column_q_comment) != canonical_photo_identity(
            second_row.sheet_column_q_comment
        ) or canonical_photo_identity(first_row.comment_text) != canonical_photo_identity(
            second_row.comment_text
        ):
            return False
    return (
        canonical_photo_identity(first.document_comment)
        == canonical_photo_identity(second.document_comment)
        and first.document_comment_scope == second.document_comment_scope
    )


def _optional_quantities_match(first: float | None, second: float | None) -> bool:
    """Сверяет пустую ячейку или числовое значение без допуска к сдвигу строки."""
    if first is None or second is None:
        return first is second
    return math.isclose(first, second, rel_tol=0, abs_tol=1e-9)


def _confirmed_partial_photo_command(
    first: PhotoDocumentObservation,
    second: PhotoDocumentObservation | None,
    settings: Settings,
    *,
    expected_filled_order_row_count: int | None = None,
) -> ParsedCommand:
    """Сохраняет только совпавшие в двух чтениях позиции с предупреждением о пропусках."""
    if second is None:
        return ParsedCommand(intent=Intent.ADD_ITEMS, photo_outcome="incomplete_photo_read")
    first_command = normalize_photo_observation(first, settings).command
    second_command = normalize_photo_observation(second, settings).command
    if first_command.global_comment != second_command.global_comment:
        return ParsedCommand(intent=Intent.ADD_ITEMS, photo_outcome="incomplete_photo_read")

    def identity(item: ExtractedItem) -> str:
        """Сравнивает факты заказа независимо от формы исходного OCR-текста."""
        return json.dumps(
            {
                "product": canonical_photo_identity(item.product_query),
                "quantity": item.quantity,
                "unit": normalize_unit(item.unit),
                "departments": item.department_quantities.model_dump(),
                "comment": clean_text(item.comment),
                "supplier": canonical_photo_identity(item.supplier_hint),
                "sheet_row": item.photo_sheet_row_number,
            },
            sort_keys=True,
        )

    remaining = Counter(identity(item) for item in second_command.items)
    items: list[ExtractedItem] = []
    for item in first_command.items:
        key = identity(item)
        if remaining[key] > 0:
            items.append(item)
            remaining[key] -= 1
    every_item_confirmed = second is not None and len(items) == len(first_command.items) == len(
        second_command.items
    )
    both_reads_complete = second is not None and all(
        _photo_observation_is_complete(
            observation,
            expected_filled_order_row_count=expected_filled_order_row_count,
        )
        and not _photo_observation_needs_second_pass(
            observation,
            expected_filled_order_row_count=expected_filled_order_row_count,
        )
        for observation in (first, second)
    )
    photo_outcome = (
        ""
        if every_item_confirmed and both_reads_complete
        else "partial_photo_read"
        if items
        else "incomplete_photo_read"
    )
    logger.info(
        "photo_reads_reconciled",
        first_item_count=len(first_command.items),
        second_item_count=len(second_command.items),
        confirmed_item_count=len(items),
        photo_outcome=photo_outcome or "complete_photo_read",
    )
    return ParsedCommand(
        intent=Intent.ADD_ITEMS,
        items=items,
        global_comment=first_command.global_comment if items else "",
        photo_outcome=photo_outcome,
    )


def _read_lettered_department_column_values(
    baseline: PhotoDocumentObservation,
    column_read: PhotoDocumentObservation | None,
    *,
    department_field: str,
    expected_quantity_cell_count: int | None,
) -> tuple[dict[int, float] | None, str | None]:
    """Связывает распознанные количества отдельной колонки со строками чернового чтения."""
    if column_read is None:
        return None, "missing_column_read"
    if expected_quantity_cell_count is None:
        return None, "missing_visual_quantity_count"
    target_rows = [row for row in column_read.rows if getattr(row, department_field) is not None]
    if len(target_rows) != expected_quantity_cell_count:
        return None, "quantity_cell_count_mismatch"
    if len(target_rows) > len(baseline.rows):
        return None, "too_many_rows"

    baseline_sheet_rows: dict[int, int] = {}
    baseline_products: dict[str, list[int]] = {}
    duplicate_sheet_rows: set[int] = set()
    for index, row in enumerate(baseline.rows):
        if row.sheet_row_number is not None and row.sheet_row_number_confidence >= 0.8:
            if row.sheet_row_number in baseline_sheet_rows:
                duplicate_sheet_rows.add(row.sheet_row_number)
            else:
                baseline_sheet_rows[row.sheet_row_number] = index
        identity = canonical_photo_identity(row.product_text)
        if identity:
            baseline_products.setdefault(identity, []).append(index)
    for row_number in duplicate_sheet_rows:
        baseline_sheet_rows.pop(row_number, None)
    aligned_rows = _complete_photo_row_alignment(baseline, column_read)

    values: dict[int, float] = {}
    for column_index, column_row in enumerate(column_read.rows):
        department_quantity = getattr(column_row, department_field)
        if department_quantity is None:
            continue
        if (
            not column_row.product_text
            or column_row.product_confidence < 0.5
            or column_row.row_alignment_confidence < 0.5
        ):
            return None, "low_row_confidence"
        if column_row.quantity_confidence < 0.5:
            return None, "low_quantity_confidence"
        if not math.isfinite(department_quantity) or department_quantity <= 0:
            return None, "invalid_quantity"

        sheet_row_number = column_row.sheet_row_number
        sheet_row_index = (
            baseline_sheet_rows.get(sheet_row_number)
            if sheet_row_number is not None and column_row.sheet_row_number_confidence >= 0.8
            else None
        )

        identity = canonical_photo_identity(column_row.product_text)
        product_indexes = baseline_products.get(identity, [])
        if sheet_row_index is not None:
            row_index = sheet_row_index
            if product_indexes and row_index not in product_indexes:
                return None, "conflicting_row_identity"
        elif len(product_indexes) == 1:
            row_index = product_indexes[0]
        elif aligned_rows is not None and column_index in aligned_rows:
            row_index = aligned_rows[column_index]
        else:
            return None, "ambiguous_row_identity"

        baseline_row = baseline.rows[row_index]
        if (
            not baseline_row.product_text
            or baseline_row.product_confidence < 0.5
            or baseline_row.row_alignment_confidence < 0.5
        ):
            return None, "low_baseline_row_confidence"
        if row_index in values:
            return None, "duplicate_row_identity"
        values[row_index] = department_quantity
    return values, None


def _complete_photo_row_alignment(
    baseline: PhotoDocumentObservation,
    candidate: PhotoDocumentObservation,
) -> dict[int, int] | None:
    """Выравнивает только полные списки строк с подтверждённым порядком изображения."""
    if not baseline.rows or len(baseline.rows) != len(candidate.rows):
        return None
    row_count = len(baseline.rows)
    baseline_visual_indexes = [row.visual_row_index for row in baseline.rows]
    candidate_visual_indexes = [row.visual_row_index for row in candidate.rows]
    expected_indexes = set(range(row_count))
    has_complete_visual_indexes = (
        all(index is not None for index in baseline_visual_indexes)
        and all(index is not None for index in candidate_visual_indexes)
        and set(cast(list[int], baseline_visual_indexes)) == expected_indexes
        and set(cast(list[int], candidate_visual_indexes)) == expected_indexes
    )
    if has_complete_visual_indexes:
        baseline_by_visual_index = {
            cast(int, row.visual_row_index): index for index, row in enumerate(baseline.rows)
        }
        candidate_by_visual_index = {
            cast(int, row.visual_row_index): index for index, row in enumerate(candidate.rows)
        }
        row_pairs = [
            (baseline_by_visual_index[index], candidate_by_visual_index[index])
            for index in range(row_count)
        ]
    else:
        row_pairs = [(index, index) for index in range(row_count)]

    aligned: dict[int, int] = {}
    for baseline_index, candidate_index in row_pairs:
        baseline_row = baseline.rows[baseline_index]
        candidate_row = candidate.rows[candidate_index]
        if (
            not baseline_row.product_text
            or not candidate_row.product_text
            or baseline_row.product_confidence < 0.5
            or candidate_row.product_confidence < 0.5
            or baseline_row.row_alignment_confidence < 0.5
            or candidate_row.row_alignment_confidence < 0.5
        ):
            return None
        same_sheet_row = (
            baseline_row.sheet_row_number is not None
            and candidate_row.sheet_row_number is not None
            and baseline_row.sheet_row_number_confidence >= 0.8
            and candidate_row.sheet_row_number_confidence >= 0.8
            and baseline_row.sheet_row_number == candidate_row.sheet_row_number
        )
        same_product = canonical_photo_identity(
            baseline_row.product_text
        ) == canonical_photo_identity(candidate_row.product_text)
        if (
            not same_sheet_row
            and not same_product
            and (
                not has_complete_visual_indexes
                or min(
                    baseline_row.row_alignment_confidence,
                    candidate_row.row_alignment_confidence,
                )
                < 0.9
            )
        ):
            return None
        if (
            baseline_row.sheet_row_number is not None
            and candidate_row.sheet_row_number is not None
            and baseline_row.sheet_row_number_confidence >= 0.8
            and candidate_row.sheet_row_number_confidence >= 0.8
            and baseline_row.sheet_row_number != candidate_row.sheet_row_number
        ):
            return None
        aligned[candidate_index] = baseline_index
    return aligned


def _reconcile_department_observations(
    baseline: PhotoDocumentObservation,
    candidate: PhotoDocumentObservation,
) -> PhotoDocumentObservation | None:
    """Сверяет общие строки и сохраняет полное чтение при пропуске строк в повторе."""
    if (
        classify_photo_document(baseline) != "client_order_sheet"
        or classify_photo_document(candidate) != "client_order_sheet"
    ):
        return None
    department_fields = (
        ("hall_quantity", "bar_quantity", "kitchen_quantity")
        if not uses_lettered_department_columns(baseline)
        else (
            "sheet_column_n_quantity",
            "sheet_column_o_quantity",
            "sheet_column_p_quantity",
        )
    )
    candidate_fields = (
        ("hall_quantity", "bar_quantity", "kitchen_quantity")
        if not uses_lettered_department_columns(candidate)
        else (
            "sheet_column_n_quantity",
            "sheet_column_o_quantity",
            "sheet_column_p_quantity",
        )
    )
    if len(baseline.rows) == len(candidate.rows):
        aligned_rows = _complete_photo_row_alignment(baseline, candidate)
        if aligned_rows is None:
            return None
        row_pairs = [
            (baseline.rows[baseline_index], candidate.rows[candidate_index])
            for candidate_index, baseline_index in aligned_rows.items()
        ]
        selected = baseline
    else:
        row_keys = _department_observation_row_keys(baseline, candidate)
        if row_keys is None:
            return None
        baseline_keys, candidate_keys = row_keys
        common_keys = baseline_keys.keys() & candidate_keys.keys()
        if not common_keys:
            return None
        if baseline_keys.keys() <= candidate_keys.keys():
            selected = candidate
        elif candidate_keys.keys() <= baseline_keys.keys():
            selected = baseline
        else:
            return None
        row_pairs = [(baseline_keys[key], candidate_keys[key]) for key in common_keys]

    for baseline_row, candidate_row in row_pairs:
        if canonical_photo_identity(baseline_row.product_text) != canonical_photo_identity(
            candidate_row.product_text
        ):
            return None
        for baseline_field, candidate_field in zip(
            department_fields,
            candidate_fields,
            strict=True,
        ):
            baseline_quantity = getattr(baseline_row, baseline_field)
            candidate_quantity = getattr(candidate_row, candidate_field)
            if baseline_quantity is None or candidate_quantity is None:
                if baseline_quantity is not candidate_quantity:
                    return None
            elif not math.isclose(baseline_quantity, candidate_quantity, rel_tol=0, abs_tol=1e-9):
                return None
    return selected


def _department_observation_row_keys(
    baseline: PhotoDocumentObservation,
    candidate: PhotoDocumentObservation,
) -> tuple[dict[tuple[str, int | str], PhotoRowObservation], ...] | None:
    """Строит однозначные ключи строк для двух чтений разной длины."""
    observations = (baseline, candidate)
    relevant_rows = [
        [
            row
            for row in observation.rows
            if clean_text(row.product_text) or photo_row_has_potential_order_evidence(row)
        ]
        for observation in observations
    ]
    if any(not rows for rows in relevant_rows):
        return None
    has_authoritative_numbers = all(
        all(
            row.sheet_row_number is not None and row.sheet_row_number_confidence >= 0.9
            for row in rows
        )
        and len({row.sheet_row_number for row in rows}) == len(rows)
        for rows in relevant_rows
    )
    keyed_observations: list[dict[tuple[str, int | str], PhotoRowObservation]] = []
    for rows in relevant_rows:
        keyed_rows: dict[tuple[str, int | str], PhotoRowObservation] = {}
        for row in rows:
            if not clean_text(row.product_text) or row.product_confidence < 0.5:
                return None
            key: tuple[str, int | str]
            if has_authoritative_numbers:
                sheet_row_number = row.sheet_row_number
                if sheet_row_number is None:
                    return None
                key = ("sheet_row", sheet_row_number)
            else:
                identity = canonical_photo_identity(row.product_text)
                if not identity or row.row_alignment_confidence < 0.5:
                    return None
                key = ("product", identity)
            if key in keyed_rows:
                return None
            keyed_rows[key] = row
        keyed_observations.append(keyed_rows)
    return keyed_observations[0], keyed_observations[1]


def _merge_lettered_department_column_reads(
    baseline: PhotoDocumentObservation,
    column_values: dict[str, dict[int, float]],
) -> PhotoDocumentObservation | None:
    """Собирает отделы только из трёх успешно сверенных чтений отдельных колонок."""
    if set(column_values) != {letter for letter, _ in _LETTERED_DEPARTMENT_COLUMN_FIELDS}:
        return None
    cleared_fields = {field_name: None for _, field_name in _LETTERED_DEPARTMENT_COLUMN_FIELDS}
    # Проверенные колонки заменяют предварительные значения отделов.
    cleared_fields.update(hall_quantity=None, bar_quantity=None, kitchen_quantity=None)
    merged_rows = [row.model_copy(update=cleared_fields) for row in baseline.rows]
    for letter, field_name in _LETTERED_DEPARTMENT_COLUMN_FIELDS:
        for index, value in column_values[letter].items():
            if not 0 <= index < len(merged_rows):
                return None
            if value is not None and (not math.isfinite(value) or value <= 0):
                return None
            merged_rows[index] = merged_rows[index].model_copy(update={field_name: value})
    return baseline.model_copy(update={"rows": merged_rows})
