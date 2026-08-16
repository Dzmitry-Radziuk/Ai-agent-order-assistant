"""Распознаёт явно заданную область общего комментария."""

from __future__ import annotations

import re

from restaurant_bot.domain.text import clean_text, normalize_text


def parse_comment_scope_text(
    text: str,
    item_count: int,
) -> tuple[str, list[int], float] | None:
    """Распознаёт безопасный текстовый ответ на вопрос об области комментария."""
    normalized = normalize_text(text).strip(" .,;:!?—–-")
    if not normalized:
        return None
    if re.fullmatch(r"(?:не\s+добавля\w*|не\s+надо(?:\s+комментар\w*)?|отмен\w*)", normalized):
        return "cancel", [], 1.0
    if re.search(
        r"(?:для|на|ко?)\s+(?:всей|всю|всего|весь|всем?)\s+(?:заявк\w*|заказ\w*)",
        normalized,
    ):
        return "order", [], 1.0
    if re.fullmatch(
        r"(?:для\s+)?(?:всех(?:\s+этих)?\s+товар\w*|для\s+всех|ко\s+всем(?:\s+товар\w*)?|для\s+обоих|для\s+обеих|обоим|обеим)",
        normalized,
    ):
        return "items", list(range(item_count)), 1.0
    if re.fullmatch(r"(?:только\s+)?(?:к\s+)?последн\w*(?:\s+товар\w*)?", normalized):
        return "items", [item_count - 1] if item_count else [], 1.0
    if re.fullmatch(r"(?:к|для)\s+последн\w*(?:\s+товар\w*)?", normalized):
        return "items", [item_count - 1] if item_count else [], 1.0
    return None


def _extract_global_comment(text: str) -> tuple[str, str]:
    """Отделяет явно помеченный общий комментарий от списка товаров."""
    original = str(text or "")
    source = clean_text(original)
    match = re.fullmatch(
        r"(?P<products>.+?)"
        r"(?:[.!?;]\s*|\s*,?\s+(?:и|а)\s+)"
        r"(?:(?:все|всё|всем|для всех)"
        r"(?:\s+(?:товар\w*|позици\w*|это(?:\s+дело)?))?|"
        r"общ(?:ий|его)\s+комментар(?:ий|ия))"
        r"(?:\s*[,;:—–-]\s*|\s+)"
        r"(?P<comment>.+?)\s*[.!?]*",
        source,
        flags=re.I,
    )
    if match is None:
        return original, ""
    products = clean_text(match.group("products")).strip(" ,;:.!?—–-")
    comment = clean_text(match.group("comment")).strip(" ,;:.!?—–-")
    if not products or not comment:
        return original, ""
    return products, comment


def has_explicit_global_comment_scope(text: str) -> bool:
    """Проверяет, что пользователь явно распространил комментарий на всю заявку."""
    normalized = normalize_text(text)
    if not normalized:
        return False
    # «Всё по пять штук» — распределение количества, а не область комментария.  # noqa: RUF003
    if re.search(r"\b(?:все|всё|каждого|оба|обоих)\s+по\s+", normalized, re.I):
        return False
    if re.search(
        r"(?:^|\s)(?:все|всё|всем|для всех)"
        r"(?:\s+(?:товаров|товары|позиций|позиции))?(?:\s|$)",
        normalized,
        flags=re.I,
    ):
        return True
    if re.search(
        r"(?:для|ко|к|на)\s+(?:всей|всю|всего|весь)\s+(?:заявк\w*|заказ\w*)",
        normalized,
        flags=re.I,
    ):
        return True
    return "общ" in normalized and "комментар" in normalized
