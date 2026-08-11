"""Разрешает ручной выбор кандидата без применения каталога."""

from __future__ import annotations

import re
from dataclasses import dataclass

from restaurant_bot.conversation.state.queries import item_index
from restaurant_bot.domain.models import (
    Candidate,
    CartItem,
    ConversationState,
    EngineResult,
    ItemStatus,
    ParsedCommand,
)
from restaurant_bot.services.replies import cart_reply, issue_reply
from restaurant_bot.services.text import normalize_text


@dataclass(frozen=True, slots=True)
class CandidateSelectionOutcome:
    """Возвращает выбранную пару либо готовый безопасный ответ."""

    item: CartItem | None = None
    candidate: Candidate | None = None
    result: EngineResult | None = None


class CandidateSelectionHandler:
    """Проверяет индекс и название выбранного варианта каталога."""

    def resolve(
        self,
        command: ParsedCommand,
        state: ConversationState,
    ) -> CandidateSelectionOutcome:
        """Находит ровно одного кандидата, не меняя товарную позицию."""
        item = state.current_item()
        if command.callback_target.isdigit():
            selected_item_index = int(command.callback_target)
            item = (
                state.cart[selected_item_index]
                if 0 <= selected_item_index < len(state.cart)
                else None
            )
        if item is None or item.status != ItemStatus.AMBIGUOUS:
            return CandidateSelectionOutcome(
                result=EngineResult(state=state, reply=cart_reply(state))
            )
        candidate_index = (command.selected_index or 0) - 1
        if command.selection_query:
            query = normalize_text(command.selection_query)
            scores = [
                self.contains_score(query, normalize_text(candidate.name))
                for candidate in item.candidates
            ]
            best_score = max(scores, default=0)
            candidate_index = (
                scores.index(best_score) if best_score > 0 and scores.count(best_score) == 1 else -1
            )
        if candidate_index < 0 or candidate_index >= len(item.candidates):
            return CandidateSelectionOutcome(
                result=EngineResult(
                    state=state,
                    reply=issue_reply(item, item_index(state, item)),
                )
            )
        return CandidateSelectionOutcome(
            item=item,
            candidate=item.candidates[candidate_index],
        )

    @staticmethod
    def contains_score(target: str, candidate: str) -> int:
        """Оценивает совпадение названий с учётом пунктуации и окончаний."""
        if not target or not candidate:
            return 0
        if target == candidate:
            return 100
        if target in candidate or candidate in target:
            return 80
        target_tokens = re.findall(r"[a-zа-яё0-9%]+", target, flags=re.I)
        candidate_tokens = re.findall(r"[a-zа-яё0-9%]+", candidate, flags=re.I)
        if not target_tokens or not candidate_tokens:
            return 0
        exact_matches = 0
        inflected_matches = 0
        for target_token in target_tokens:
            if target_token in candidate_tokens:
                exact_matches += 1
                continue
            if any(
                CandidateSelectionHandler.tokens_share_stem(target_token, candidate_token)
                for candidate_token in candidate_tokens
            ):
                inflected_matches += 1
        return exact_matches * 12 + inflected_matches * 10

    @staticmethod
    def tokens_share_stem(left: str, right: str) -> bool:
        """Сравнивает формы слова без агрессивного морфологического угадывания."""
        shorter_length = min(len(left), len(right))
        if shorter_length < 3:
            return False
        common_length = 0
        for left_char, right_char in zip(left, right, strict=False):
            if left_char != right_char:
                break
            common_length += 1
        if shorter_length <= 4:
            return common_length >= 3 and abs(len(left) - len(right)) <= 2
        return common_length >= 4
