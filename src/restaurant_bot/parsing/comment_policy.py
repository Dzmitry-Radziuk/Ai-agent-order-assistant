"""Определяет безопасные границы комментария поставщику."""

from __future__ import annotations

import re

from restaurant_bot.domain.text import clean_text, normalize_text

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
    r"(?:привез|достав|позвон|упаков|полож|выбер|выбрат|замен|смешив|срез|"
    r"разморажив|нареж|нарез|натер|зачист|обреж|подреж)\w*"
    r")\b",
    flags=re.I,
)
_COMMENT_POLITENESS_PREFIX_RE = re.compile(
    r"^(?:желательно|обязательно|пожалуйста|просьба|главное)\s+",
    flags=re.I,
)
_COMMENT_ACTION_RE = re.compile(
    r"\b(?:добав\w*|покаж\w*|откр\w*|убер\w*|удал\w*|измен\w*|"
    r"исправ\w*|выбер\w*|перей\w*|сброс\w*|отправ\w*|проверь\w*|"
    r"сдела\w*|созда\w*|оформ\w*|начн\w*)\b",
    flags=re.I,
)
_COMMENT_OBJECT_RE = re.compile(
    r"\b(?:комментар\w*|пожелан\w*|товар\w*|заявк\w*|заказ\w*|"
    r"черновик\w*|корзин\w*|итог\w*|количеств\w*|единиц\w*)\b",
    flags=re.I,
)
_COMMENT_STANDALONE_ACTION_RE = re.compile(
    r"^(?:пожалуйста\s+)?(?:добав\w*|покаж\w*|откр\w*|убер\w*|удал\w*|"
    r"измен\w*|исправ\w*|выбер\w*|перей\w*|сброс\w*|отправ\w*|проверь\w*)$",
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


def strip_comment_label(value: str) -> str:
    """Удаляет только служебную метку комментария, сохраняя текст пожелания."""
    edge_punctuation = r"^[\s.,;:!?—–-]+|[\s.,;:!?—–-]+$"
    comment = re.sub(edge_punctuation, "", clean_text(value))
    if not comment:
        return ""
    return re.sub(edge_punctuation, "", _COMMENT_LABEL_RE.sub("", comment, count=1))


def supplier_comment_start(words: list[str]) -> int | None:
    """Находит начало подтверждённой инструкции в остатке названия."""
    normalized = [normalize_text(word) for word in words if normalize_text(word)]
    for index in range(len(normalized)):
        if explicit_supplier_comment(" ".join(normalized[index:])):
            return index
    return None


def comment_semantic_key(value: str) -> str:
    """Возвращает ключ для дедупликации одной инструкции поставщику."""
    normalized = normalize_text(value).strip(" .,;:-—–")
    while True:
        stripped = _COMMENT_POLITENESS_PREFIX_RE.sub("", normalized, count=1).strip()
        if stripped == normalized:
            return normalized
        normalized = stripped


def _same_normalized_product_word(left: str, right: str) -> bool:
    """Сопоставляет формы одного слова товара без агрессивного угадывания."""
    if left == right:
        return True
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
    return common_length >= 4 and abs(len(left) - len(right)) <= 2


def is_product_name_fragment(value: str, product_query: str) -> bool:
    """Проверяет, входит ли фрагмент в название товара после нормализации."""
    value_tokens = re.findall(r"[a-zа-яё0-9]+", normalize_text(value), flags=re.I)
    query_tokens = re.findall(r"[a-zа-яё0-9]+", normalize_text(product_query), flags=re.I)
    if not value_tokens or len(value_tokens) > len(query_tokens):
        return False
    return any(
        all(
            _same_normalized_product_word(value_token, query_token)
            for value_token, query_token in zip(
                value_tokens,
                query_tokens[start : start + len(value_tokens)],
                strict=True,
            )
        )
        for start in range(len(query_tokens) - len(value_tokens) + 1)
    )


def is_comment_control_text(value: str) -> bool:
    """Проверяет, является ли текст командой, а не пожеланием поставщику."""
    comment = clean_text(value).strip(" .,;:-—–")
    if not comment:
        return False
    normalized = normalize_text(comment)
    if re.fullmatch(r"(?:комментар\w*|пожелан\w*)", normalized):
        return True
    if _COMMENT_STANDALONE_ACTION_RE.fullmatch(normalized):
        return True
    return bool(_COMMENT_ACTION_RE.search(comment) and _COMMENT_OBJECT_RE.search(comment))
