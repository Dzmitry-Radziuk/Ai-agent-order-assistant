"""Содержит channel-neutral мутации отдельных позиций черновика."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from restaurant_bot.conversation.comments import prune_pending_comment_item_ids
from restaurant_bot.conversation.selection import contains_score, find_cart_item_candidates
from restaurant_bot.domain.models import (
    ConversationState,
    DepartmentQuantities,
    ItemStatus,
    ParsedCommand,
)
from restaurant_bot.domain.text import normalize_text
from restaurant_bot.domain.units import normalize_unit
from restaurant_bot.parsing.commands.item_commands import clean_command_target


class DraftActionOutcome(StrEnum):
    """Описывает семантический результат мутации черновика."""

    ADVANCE = "advance"
    REMOVED = "removed"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class DraftActionResult:
    """Возвращает результат мутации без Telegram-представления."""

    outcome: DraftActionOutcome
    candidate_names: tuple[str, ...] = ()


def skip_current_item(state: ConversationState) -> DraftActionResult:
    """Помечает текущую позицию пропущенной и очищает её issue-ссылку."""
    item = state.current_item()
    if item:
        item.status = ItemStatus.SKIPPED
        if state.pending_added_items_count:
            state.pending_added_items_count -= 1
    state.current_issue_item_id = ""
    return DraftActionResult(DraftActionOutcome.ADVANCE)


def confirm_duplicate_item(state: ConversationState) -> DraftActionResult:
    """Подтверждает только ожидающий дубликат с прежней проверкой единиц."""
    item = state.current_item()
    if item and item.status == ItemStatus.DUPLICATE_PENDING:
        existing = next((row for row in state.cart if row.id == item.issue_message), None)
        if (
            existing
            and item.catalog_unit
            and item.unit
            and normalize_unit(item.unit) != normalize_unit(item.catalog_unit)
        ):
            return DraftActionResult(DraftActionOutcome.ADVANCE)
        if existing:
            combined = DepartmentQuantities()
            assigned = existing.has_department_assignment() and item.has_department_assignment()
            if assigned:
                for field, department in (("hall", "Зал"), ("bar", "Бар"), ("kitchen", "Кухня")):
                    values = []
                    for source in (existing, item):
                        distribution = source.department_quantities.model_dump()
                        if any(distribution.values()) and not source.quantity_user_edited:
                            values.append(distribution[field] or 0)
                        else:
                            values.append(
                                (source.quantity or 0) if source.department == department else 0
                            )
                    setattr(combined, field, sum(values) or None)
            existing.quantity = (existing.quantity or 0) + (item.quantity or 0)
            existing.department_quantities = combined
            existing.department_confirmed = assigned
            existing.quantity_user_edited = False
            state.department_confirmation_required = True
            state.department_confirmed = False
            item.status = ItemStatus.SKIPPED
        state.current_issue_item_id = ""
    return DraftActionResult(DraftActionOutcome.ADVANCE)


def remove_item(command: ParsedCommand, state: ConversationState) -> DraftActionResult:
    """Удаляет позицию черновика или ожидающую область комментария."""
    target_query = clean_command_target(command.target_query)
    target = normalize_text(target_query)
    if not target and state.current_item():
        state.current_item().status = ItemStatus.SKIPPED  # type: ignore[union-attr]
        prune_pending_comment_item_ids(state)
        return DraftActionResult(DraftActionOutcome.ADVANCE)

    matches = find_cart_item_candidates(state, target_query)
    if len(matches) > 1:
        names = tuple(dict.fromkeys(item.catalog_name or item.source_query for item in matches))
        return DraftActionResult(DraftActionOutcome.AMBIGUOUS, candidate_names=names)
    item = matches[0] if matches else None
    if item is not None:
        was_current = item.id == state.current_issue_item_id
        item.status = ItemStatus.SKIPPED
        if was_current:
            state.current_issue_item_id = ""
            state.current_issue_kind = None
        prune_pending_comment_item_ids(state)
        return DraftActionResult(DraftActionOutcome.REMOVED)

    pending_matches = sorted(
        [
            (
                contains_score(target, normalize_text(pending.product_query)),
                pending,
            )
            for pending in state.pending_comment_items
        ],
        key=lambda pair: pair[0],
    )
    pending_item = None
    if pending_matches:
        best_score = pending_matches[-1][0]
        if best_score > 0 and sum(score == best_score for score, _ in pending_matches) == 1:
            pending_item = pending_matches[-1][1]
    if pending_item is not None:
        state.pending_comment_items.remove(pending_item)
        return DraftActionResult(DraftActionOutcome.REMOVED)
    return DraftActionResult(DraftActionOutcome.NOT_FOUND)
