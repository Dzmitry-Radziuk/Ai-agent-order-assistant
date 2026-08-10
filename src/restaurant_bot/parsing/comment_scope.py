"""Распознаёт явно заданную область общего комментария."""

from __future__ import annotations

import re

from restaurant_bot.services.text import clean_text, normalize_text


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
