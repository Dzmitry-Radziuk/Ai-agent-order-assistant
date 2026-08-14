"""Координирует чтение, matching, status policy и ответ по истории."""

from __future__ import annotations

from datetime import date, datetime
from time import perf_counter
from zoneinfo import ZoneInfo

import structlog

from restaurant_bot.application.history.contracts import HistoryReader
from restaurant_bot.domain.history import HistoryAnswer, HistoryQuery
from restaurant_bot.history.answers import build_history_answer
from restaurant_bot.history.matching import match_product_query
from restaurant_bot.history.product_list import parse_history_product_list
from restaurant_bot.history.status_policy import is_relevant_status
from restaurant_bot.parsing.history.dates import resolve_date_reference

logger = structlog.get_logger(__name__)


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
        target_date = resolve_date_reference(
            query.date_reference,
            today=self.today or datetime.now(ZoneInfo(self.timezone_name)).date(),
            explicit_date=query.explicit_date,
        )
        relevant_entries = [
            entry
            for entry in entries
            if is_relevant_status(entry.stage, query.question_type, query.temporal_scope)
        ]
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
