"""Координирует чтение, matching, status policy и ответ по истории."""

from __future__ import annotations

from datetime import date, datetime
from time import perf_counter
from zoneinfo import ZoneInfo

import structlog

from restaurant_bot.application.history.contracts import HistoryReader
from restaurant_bot.domain.history import (
    HistoryAnswer,
    HistoryDeliveryDateRelation,
    HistoryMatch,
    HistoryProductEntry,
    HistoryQuery,
    HistoryQuestionType,
)
from restaurant_bot.history.answers import build_history_answer
from restaurant_bot.history.matching import match_product_query
from restaurant_bot.history.product_list import parse_history_product_list
from restaurant_bot.history.status_policy import is_relevant_status
from restaurant_bot.parsing.history.dates import resolve_date_reference

logger = structlog.get_logger(__name__)


def _date_relation(value: date | None, today: date) -> HistoryDeliveryDateRelation:
    """Определяет положение даты поставки относительно бизнес-даты."""
    if value is None:
        return HistoryDeliveryDateRelation.MISSING
    if value < today:
        return HistoryDeliveryDateRelation.PAST
    if value == today:
        return HistoryDeliveryDateRelation.TODAY
    return HistoryDeliveryDateRelation.FUTURE


def _annotate_date_relations(
    matches: list[HistoryMatch],
    today: date,
) -> list[HistoryMatch]:
    """Добавляет к совпадениям нейтральные сведения о свежести даты."""
    return [
        match.model_copy(
            update={"delivery_date_relation": _date_relation(match.entry.delivery_date, today)}
        )
        for match in matches
    ]


def _venue_entry_key(entry: HistoryProductEntry) -> tuple[object, ...]:
    """Возвращает консервативный ключ точного доказательства поставки."""
    return (
        entry.order_number,
        entry.product_name,
        entry.supplier,
        entry.delivery_date,
        entry.stage,
    )


def _venue_matches(entries: list[HistoryProductEntry], today: date) -> list[HistoryMatch]:
    """Создаёт упорядоченные совпадения для venue-level списка поставок."""
    unique: list[HistoryProductEntry] = []
    seen: set[tuple[object, ...]] = set()
    for entry in entries:
        key = _venue_entry_key(entry)
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    relation_order = {
        HistoryDeliveryDateRelation.TODAY: 0,
        HistoryDeliveryDateRelation.FUTURE: 1,
        HistoryDeliveryDateRelation.MISSING: 2,
        HistoryDeliveryDateRelation.PAST: 3,
    }
    unique.sort(
        key=lambda entry: (
            relation_order[_date_relation(entry.delivery_date, today)],
            entry.delivery_date or date.max,
            entry.product_name.casefold(),
        )
    )
    return [
        HistoryMatch(
            entry=entry,
            score=100,
            delivery_date_relation=_date_relation(entry.delivery_date, today),
        )
        for entry in unique
    ]


class HistoryQueryService:
    """Выполняет read-only запрос к истории текущего заведения."""

    def __init__(
        self,
        reader: HistoryReader,
        *,
        today: date | None = None,
        timezone_name: str = "Europe/Minsk",
    ) -> None:
        """Сохраняет reader и необязательные фиксированные часы для тестов."""
        self.reader = reader
        self.today = today
        self.timezone_name = timezone_name

    def execute(
        self,
        query: HistoryQuery,
        *,
        spreadsheet_id: str,
        venue_name: str = "",
    ) -> HistoryAnswer:
        """Читает «Историю» один раз и возвращает доказательный результат."""
        started = perf_counter()
        rows = self.reader.read_history(spreadsheet_id, venue_name)
        entries = [entry for row in rows for entry in parse_history_product_list(row)]
        business_today = self.today or datetime.now(ZoneInfo(self.timezone_name)).date()
        target_date = resolve_date_reference(
            query.date_reference,
            today=business_today,
            explicit_date=query.explicit_date,
        )
        relevant_entries = [
            entry
            for entry in entries
            if is_relevant_status(entry.stage, query.question_type, query.temporal_scope)
        ]
        if query.question_type is HistoryQuestionType.VENUE_DELIVERIES:
            venue_entries = relevant_entries
            if target_date is not None:
                venue_entries = [
                    entry for entry in venue_entries if entry.delivery_date == target_date
                ]
            matches = _venue_matches(venue_entries, business_today)
            answer = build_history_answer(
                query,
                matches,
                history_row_count=len(rows),
                expanded_product_count=len(entries),
                had_matching_products=bool(relevant_entries),
                target_date=target_date,
                active_without_delivery_date_count=sum(
                    entry.delivery_date is None for entry in relevant_entries
                ),
            )
            logger.info(
                "history_query_completed",
                question_type=query.question_type.value,
                has_date_filter=target_date is not None,
                history_row_count=len(rows),
                expanded_product_count=len(entries),
                candidate_count=len(matches),
                relevant_match_count=len(answer.matches),
                outcome=answer.kind.value,
                duration_ms=round((perf_counter() - started) * 1000),
            )
            return answer
        matching_entries = [
            entry
            for entry in entries
            if any(
                match_product_query(product_query, [entry])
                for product_query in query.product_queries
            )
        ]
        matches = []
        for product_query in query.product_queries:
            candidates = match_product_query(product_query, relevant_entries)
            if target_date is not None:
                dated = [
                    match
                    for match in candidates
                    if match.entry.delivery_date in {target_date, None}
                ]
                candidates = dated or candidates
            matches.extend(candidates)
        matches = _annotate_date_relations(matches, business_today)
        answer = build_history_answer(
            query,
            matches,
            history_row_count=len(rows),
            expanded_product_count=len(entries),
            had_matching_products=bool(matching_entries),
            target_date=target_date,
        )
        logger.info(
            "history_query_completed",
            question_type=query.question_type.value,
            has_date_filter=target_date is not None,
            history_row_count=len(rows),
            expanded_product_count=len(entries),
            candidate_count=len(matches),
            relevant_match_count=len(answer.matches),
            outcome=answer.kind.value,
            duration_ms=round((perf_counter() - started) * 1000),
        )
        return answer
