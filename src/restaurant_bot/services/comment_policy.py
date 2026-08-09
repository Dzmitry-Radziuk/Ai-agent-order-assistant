"""Определяет безопасные границы комментария поставщику."""

from __future__ import annotations

import re

from restaurant_bot.services.text import clean_text, normalize_text

_COMMENT_LABEL_RE = re.compile(
    r"^(?:комментар(?:ий|ия)|примечани(?:е|я)|пожелани(?:е|я))"
    r"(?:\s+(?:поставщик\w*|к\s+товар\w*))?\s*[:—–-]?\s*",
    flags=re.I,
)
_STRONG_COMMENT_START_RE = re.compile(
    r"^(?:"
    r"желательно|обязательно|просьба|пожалуйста|срочно|только|главное|"
    r"по\s+возможности|если\s+можно|"
    r"на\s+завтра|сегодня|завтра|утром|вечером|до\s+\d|"
    r"нужн\w*\s+(?:фасов\w*|упаков\w*)|"
    r"без\s+замен\w*|не\s+замен\w*|не\s+смешив\w*|не\s+разморажив\w*|"
    r"(?:привез|достав|позвон|упаков|полож|выбер|выбрат|замен|смешив|"
    r"разморажив|нареж|нарез|натер|зачист|обреж|подреж)\w*"
    r")\b",
    flags=re.I,
)


def explicit_supplier_comment(value: str) -> str:
    """Возвращает пожелание только при явном языковом маркере."""
    comment = clean_text(value).strip(" .,;:-—–")
    if not comment:
        return ""
    labelled = _COMMENT_LABEL_RE.sub("", comment, count=1).strip(" .,;:-—–")
    if labelled != comment:
        return labelled
    if _STRONG_COMMENT_START_RE.search(comment):
        return comment
    return ""


def supplier_comment_start(words: list[str]) -> int | None:
    """Находит начало подтверждённой инструкции в остатке названия."""
    normalized = [normalize_text(word) for word in words if normalize_text(word)]
    for index in range(len(normalized)):
        if explicit_supplier_comment(" ".join(normalized[index:])):
            return index
    return None
