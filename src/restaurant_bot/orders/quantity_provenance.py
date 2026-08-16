"""Разрешает количество заказа после отделения признаков каталога."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from restaurant_bot.catalog.evidence import NumericEvidence, numeric_evidence
from restaurant_bot.domain.units import normalize_unit
from restaurant_bot.parsing.quantities import has_explicit_order_marker


class QuantityProvenance(StrEnum):
    """Описывает происхождение принятого или отклонённого количества."""

    ORDER = "order"
    CATALOG_IDENTITY = "catalog_identity"
    AMBIGUOUS = "ambiguous"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class QuantityAuthorization:
    """Возвращает разрешённое количество и его источник."""

    quantity: float | None
    unit: str
    provenance: QuantityProvenance


def _same_evidence(left: NumericEvidence, right: NumericEvidence) -> bool:
    """Сравнивает два числовых свидетельства после нормализации."""
    if abs(left.value - right.value) > 1e-9:
        return False
    if left.upper_value is None or right.upper_value is None:
        if left.upper_value is not None or right.upper_value is not None:
            return False
    elif abs(left.upper_value - right.upper_value) > 1e-9:
        return False
    return (
        not left.unit or not right.unit or normalize_unit(left.unit) == normalize_unit(right.unit)
    )


def _matches_proposal(
    evidence: NumericEvidence,
    quantity: float | None,
    unit: str,
) -> bool:
    """Проверяет соответствие свидетельства предложению parser-а."""
    if quantity is None or abs(evidence.value - quantity) > 1e-9:
        return False
    if evidence.upper_value is not None:
        return False
    normalized_unit = normalize_unit(unit)
    return (
        not normalized_unit or not evidence.unit or normalized_unit == normalize_unit(evidence.unit)
    )


def _catalog_identity_indexes(
    source: list[NumericEvidence],
    catalog: list[NumericEvidence],
) -> set[int]:
    """Отмечает ровно по одному источнику на каждую характеристику каталога."""
    consumed: set[int] = set()
    for catalog_entry in catalog:
        source_index = next(
            (
                index
                for index, source_entry in enumerate(source)
                if index not in consumed and _same_evidence(source_entry, catalog_entry)
            ),
            None,
        )
        if source_index is not None:
            consumed.add(source_index)
    return consumed


def reconcile_order_quantity_evidence(
    source_text: str,
    proposed_quantity: float | None,
    proposed_unit: str = "",
    *,
    quantity_source: str = "",
    catalog_name: str = "",
    packaging_role: str = "none",
) -> QuantityAuthorization:
    """Отделяет количество заказа от числовых признаков выбранного каталога."""
    normalized_unit = normalize_unit(proposed_unit)
    if proposed_quantity is None:
        return QuantityAuthorization(None, "", QuantityProvenance.NONE)
    if quantity_source == "packaging":
        return QuantityAuthorization(None, "", QuantityProvenance.CATALOG_IDENTITY)
    if quantity_source and quantity_source != "packaging":
        return QuantityAuthorization(proposed_quantity, normalized_unit, QuantityProvenance.ORDER)

    source = str(source_text or "").strip()
    if not source:
        return QuantityAuthorization(proposed_quantity, normalized_unit, QuantityProvenance.ORDER)

    source_entries = numeric_evidence(source)
    if not source_entries:
        return QuantityAuthorization(
            proposed_quantity,
            normalized_unit,
            QuantityProvenance.ORDER,
        )
    catalog_entries = numeric_evidence(catalog_name)
    consumed = _catalog_identity_indexes(source_entries, catalog_entries)
    residual = [entry for index, entry in enumerate(source_entries) if index not in consumed]
    proposed_source = [
        entry
        for entry in source_entries
        if _matches_proposal(entry, proposed_quantity, normalized_unit)
    ]
    proposed_residual = [
        entry
        for index, entry in enumerate(source_entries)
        if index not in consumed and _matches_proposal(entry, proposed_quantity, normalized_unit)
    ]

    if proposed_residual:
        return QuantityAuthorization(
            proposed_residual[-1].value,
            normalize_unit(proposed_residual[-1].unit or normalized_unit),
            QuantityProvenance.ORDER,
        )

    if has_explicit_order_marker(source) and proposed_source:
        return QuantityAuthorization(proposed_quantity, normalized_unit, QuantityProvenance.ORDER)

    if residual and (has_explicit_order_marker(source) or len(residual) == 1):
        selected = residual[-1]
        if selected.upper_value is None:
            return QuantityAuthorization(
                selected.value,
                normalize_unit(selected.unit),
                QuantityProvenance.ORDER,
            )

    if proposed_source:
        return QuantityAuthorization(None, "", QuantityProvenance.CATALOG_IDENTITY)
    if packaging_role == "catalog_attribute":
        return QuantityAuthorization(None, "", QuantityProvenance.CATALOG_IDENTITY)
    return QuantityAuthorization(None, "", QuantityProvenance.AMBIGUOUS)
