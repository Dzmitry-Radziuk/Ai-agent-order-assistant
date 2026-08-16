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
    if re.fullmatch(
        r"(?:только\s+)?(?:(?:к|для)\s+)?последн\w*(?:\s+товар\w*)?",
        normalized,
    ):
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
    """Проверяет общий маркер комментария в исходной фразе."""
    normalized = normalize_text(text)
    if not normalized:
        return False
    if re.search(r"\b(?:все|всё|каждого|оба|обоих)\s+по\s+", normalized, re.I):
        return False
    return bool(
        re.search(
            r"(?:^|\s)(?:все|всё|всем|для всех)"
            r"(?:\s+(?:товаров|товары|позиций|позиции|это\s+дело))?(?:\s|$)",
            normalized,
            flags=re.I,
        )
        or has_explicit_order_comment_scope(normalized)
        or ("общ" in normalized and "комментар" in normalized)
    )


def has_explicit_group_comment_scope(text: str) -> bool:
    """Проверяет явную область комментария для группы позиций."""
    normalized = normalize_text(text)
    if not normalized:
        return False
    # «Всё по пять штук» — распределение количества, а не область комментария.  # noqa: RUF003
    if re.search(r"\b(?:все|всё|каждого|оба|обоих)\s+по\s+", normalized, re.I):
        return False
    return bool(
        re.search(
            r"(?:^|\s)(?:для\s+всех(?:\s+этих)?\s+(?:товар\w*|позиц\w*)|"
            r"для\s+всех|ко\s+всем(?:\s+(?:товар\w*|позиц\w*))?|"
            r"всем\s+(?:товар\w*|позиц\w*)|"
            r"для\s+обоих|для\s+обеих|обоим|обеим|"
            r"оба|обе|обоих|обеих|"
            r"(?:данные|перечисленные|указанные|эти)\s+(?:товар\w*|позиц\w*)|"
            r"(?:к\s+)?каждому\s+из\s+(?:них|этих))(?:\s|$)",
            normalized,
            flags=re.I,
        )
    )


def has_explicit_order_comment_scope(text: str) -> bool:
    """Проверяет явную область комментария для всей заявки."""
    normalized = normalize_text(text)
    if not normalized:
        return False
    # «Всё по пять штук» — распределение количества, а не область комментария.  # noqa: RUF003
    if re.search(r"\b(?:все|всё|каждого|оба|обоих)\s+по\s+", normalized, re.I):
        return False
    if re.search(
        r"(?:для|ко|к|на)\s+(?:всей|всю|всего|весь)\s+(?:заявк\w*|заказ\w*)",
        normalized,
        flags=re.I,
    ):
        return True
    return "общ" in normalized and "комментар" in normalized


def strip_comment_scope_suffix(value: str, scope: str) -> str:
    """Удаляет из текста комментария один явно указанный суффикс области."""
    comment = clean_text(value).strip(" .,;:-—–")
    if scope == "items":
        pattern = (
            r"\s+(?:для\s+всех(?:\s+этих)?\s+(?:товар\w*|позиц\w*)|"
            r"для\s+всех|ко\s+всем(?:\s+(?:товар\w*|позиц\w*))?|"
            r"всем\s+(?:товар\w*|позиц\w*)|для\s+обоих|для\s+обеих|"
            r"обоим|обеим)\s*$"
        )
    elif scope == "order":
        pattern = r"\s+(?:для|ко?|на)\s+(?:всей|всю|всего|весь|всем?)\s+(?:заявк\w*|заказ\w*)\s*$"
    else:
        return comment
    return re.sub(pattern, "", comment, flags=re.I).strip(" .,;:-—–")
