"""Защищает подтверждённую семантику команды от небезопасного ответа ИИ."""

from __future__ import annotations

import re

from restaurant_bot.domain.models import Intent, ParsedCommand
from restaurant_bot.domain.text import normalize_text
from restaurant_bot.domain.units import UNIT_ALIASES
from restaurant_bot.parsing.comment_scope import has_explicit_order_comment_scope

_BOT_REFERENCE_RE = re.compile(r"\b(?:ты|тебе|тебя|тобой|вы|вам|бот(?:а|у|ом|е|ы|ов|ам|ами|ах)?)\b")
_FIRST_PERSON_META_RE = re.compile(r"\b(?:я|мне|мой|моя|мои|мы|наш\w*)\b")
_EXPLICIT_ACTION_RE = re.compile(
    r"\b(?:добав\w*|закаж\w*|куп\w*|внес\w*|полож\w*|удал\w*|убер\w*|отправ\w*|оформ\w*)\b"
)
_QUANTITY_RE = re.compile(r"\b\d+(?:[,.]\d+)?\s*(?:кг|г|л|мл|шт|штук|упак|упаков\w*)\b")
_CONFUSION_RE = re.compile(
    r"\b(?:не\s+понима\w*|не\s+понят\w*|непонят\w*|не\s+зна\w*\s+что\s+делать)\b"
)
_HELP_SHAPE_RE = re.compile(
    r"\b(?:что\s+(?:мне\s+)?делать|что\s+(?:мне\s+)?дальше|"
    r"как\s+дальше|с\s+чего\s+начать|как\s+пользова\w*|"
    r"как\s+с\s+тобой\s+работа\w*)\b"
)
_SMALL_TALK_RE = re.compile(
    r"\b(?:флуд\w*|болта\w*|поболта\w*|поговор\w*|шут\w*|анекдот\w*|"
    r"как\s+дела|расскаж\w*\s+(?:анекдот|шутк\w*))\b"
)

_META_STEMS = (
    "бот",
    "работ",
    "уме",
    "помощ",
    "подсказ",
    "инструк",
    "объясн",
    "расскаж",
    "понима",
    "понят",
    "отвеч",
    "сказ",
    "скаж",
    "говор",
    "запрос",
    "истор",
    "правил",
    "правиль",
    "сообщ",
    "вопрос",
    "можн",
    "должн",
    "дальш",
    "буд",
    "идт",
    "флуд",
    "болта",
    "шут",
    "поболта",
    "прост",
    "люб",
    "котор",
    "они",
    "дела",
    "нача",
    "пользова",
)
_HELP_STEMS = (
    "помощ",
    "подсказ",
    "инструк",
    "объясн",
    "расскаж",
    "уме",
    "работ",
    "пользова",
    "понима",
    "правиль",
    "правил",
    "запрос",
    "истор",
    "должн",
    "дальш",
)
_FLOOD_STEMS = ("флуд", "болта", "шут", "поболта")
_FILLER_WORDS = {
    "а",
    "и",
    "или",
    "но",
    "ведь",
    "если",
    "просто",
    "вообще",
    "что",
    "как",
    "кто",
    "почему",
    "зачем",
    "когда",
    "где",
    "там",
    "тут",
    "это",
    "так",
    "на",
    "по",
    "с",
    "со",
    "про",
    "у",
    "в",
    "из",
    "для",
    "же",
    "ли",
    "не",
    "ну",
    "да",
    "нет",
    "ничего",
    "ничто",
    "мне",
    "я",
    "мы",
    "ты",
    "тебе",
    "тебя",
    "тобой",
    "вы",
    "вам",
    "мой",
    "моя",
    "мои",
    "наш",
    "наша",
    "наши",
    "уже",
    "буду",
    "будет",
    "будут",
    "могу",
    "можешь",
    "можно",
    "скажи",
    "сказать",
    "говорю",
    "говорят",
    "должны",
    "должен",
    "идти",
    "идут",
    "приедет",
    "приехала",
    "привезли",
    "настоящему",
    "правильно",
}


_HYPOTHETICAL_CHAT_RE = re.compile(
    r"(?:\bесли\b.*\bя\b.*\b(?:буду\w*|будем\w*|скажу\w*|говор\w*|разговар\w*)\b.*(?:"
    r"\bсвободн\w*\b|\bфлуд\w*\b|\bболта\w*\b|\bчто\s+ты\s+скаж\w*\b)"
    r"|\bесли\b.*\bя\b.*\bчто[-\s]?(?:нибудь|то)\b.*\b(?:скажу\w*|говор\w*|разговар\w*)\b.*\bчто\s+будет\b)",
    flags=re.IGNORECASE,
)
_BOT_ACTION_QUESTION_RE = re.compile(
    r"\b(?:что|как)\s+ты\s+(?:будешь|можешь|умеешь|делаешь|делать)\b",
    flags=re.IGNORECASE,
)
_SPEECH_HYPOTHETICAL_RE = re.compile(
    r"\bесли\b.*\bя\b.*\b(?:буд\w*|скажу\w*|говор\w*|разговар\w*)\b",
    flags=re.IGNORECASE,
)
_NON_PRODUCT_ACTOR_STEMS = (
    "машин",
    "автомобил",
    "поезд",
    "самолёт",
    "самолет",
    "люд",
    "человек",
    "коллег",
    "курьер",
    "водител",
)


def _stem_matches(word: str, stems: tuple[str, ...]) -> bool:
    """Проверяет, начинается ли слово с одного из смысловых корней."""
    return any(word.startswith(stem) for stem in stems)


def _has_product_anchor(normalized: str, words: list[str]) -> bool:
    """Находит явный товарный или командный признак в свободной фразе."""
    if _EXPLICIT_ACTION_RE.search(normalized) or _QUANTITY_RE.search(normalized):
        return True
    if any(word in UNIT_ALIASES for word in words):
        return True
    return any(
        len(word) >= 4 and word not in _FILLER_WORDS and not _stem_matches(word, _META_STEMS)
        for word in words
    )


def is_conversational_hypothetical(text: str) -> bool:
    """Отличает гипотетический разговор от вопроса о поставке."""
    normalized = normalize_text(text)
    if not normalized or _EXPLICIT_ACTION_RE.search(normalized) or _QUANTITY_RE.search(normalized):
        return False
    return bool(
        _HYPOTHETICAL_CHAT_RE.search(normalized)
        or _SPEECH_HYPOTHETICAL_RE.search(normalized)
        or _BOT_ACTION_QUESTION_RE.search(normalized)
    )


def is_conversational_non_history(text: str) -> bool:
    """Отличает разговорный вопрос с объектом-участником от вопроса о товарной истории."""
    normalized = normalize_text(text)
    if not normalized or _EXPLICIT_ACTION_RE.search(normalized) or _QUANTITY_RE.search(normalized):
        return False
    if is_conversational_hypothetical(normalized):
        return True
    return bool(
        re.search(
            r"\bкогда\s+(?:это\s+)?(?:"
            + "|".join(re.escape(stem) + r"\w*" for stem in _NON_PRODUCT_ACTOR_STEMS)
            + r")\s+(?:приед\w*|приех\w*|прилет\w*|приедут\w*)\b",
            normalized,
        )
    )


def classify_bot_conversation(text: str) -> Intent | None:
    """Распознаёт разговор о боте, помощь или флуд без выдумывания товара."""
    normalized = normalize_text(text)
    if not normalized:
        return None
    words = normalized.split()
    if _SMALL_TALK_RE.search(normalized):
        return Intent.SMALL_TALK
    if is_conversational_non_history(normalized):
        if re.search(r"\b(?:что|как)\s+ты\s+(?:умеешь|можешь)\b", normalized):
            return Intent.HELP
        return Intent.SMALL_TALK
    if _has_product_anchor(normalized, words):
        return None
    has_bot_reference = bool(_BOT_REFERENCE_RE.search(normalized))
    has_first_person_meta = bool(_FIRST_PERSON_META_RE.search(normalized)) and any(
        _stem_matches(word, _META_STEMS) for word in words
    )
    has_meta = any(_stem_matches(word, _META_STEMS) for word in words)
    has_question_shape = bool(
        re.search(
            r"\b(?:что|как|кто|почему|зачем|можешь|уме\w*|работа\w*|помоги)\b",
            normalized,
        )
        or _HELP_SHAPE_RE.search(normalized)
        or _CONFUSION_RE.search(normalized)
    )
    if (
        not ((has_bot_reference and (has_meta or has_question_shape)) or has_first_person_meta)
        and not any(_stem_matches(word, _FLOOD_STEMS) for word in words)
        and not _CONFUSION_RE.search(normalized)
        and not _HELP_SHAPE_RE.search(normalized)
    ):
        return None
    if any(_stem_matches(word, _FLOOD_STEMS) for word in words):
        return Intent.SMALL_TALK
    if any(_stem_matches(word, _HELP_STEMS) for word in words) or has_question_shape:
        return Intent.HELP
    return Intent.UNKNOWN


def protect_bot_conversation(source_text: str, command: ParsedCommand) -> ParsedCommand:
    """Не позволяет ответу ИИ превратить разговор о боте в товар или историю."""
    intent = classify_bot_conversation(source_text)
    if intent is None:
        return command
    if intent in {Intent.HELP, Intent.SMALL_TALK, Intent.UNKNOWN}:
        return ParsedCommand(intent=intent, text=source_text)
    return command


def normalize_comment_proposal(source_text: str, command: ParsedCommand) -> ParsedCommand:
    """Превращает явный общий комментарий без товаров в безопасную мутацию черновика."""
    if (
        command.global_comment
        and not command.items
        and has_explicit_order_comment_scope(source_text)
    ):
        return command.model_copy(
            update={
                "intent": Intent.EDIT_COMMENT,
                "comment_action": "add",
                "comment_scope": "order",
                "comment_text": command.global_comment,
                "global_comment": "",
                "explicit_add_items": False,
            }
        )
    return command


def protect_confirmed_command(
    deterministic: ParsedCommand,
    proposed: ParsedCommand,
) -> ParsedCommand:
    """Сохраняет доказанную команду, если ИИ потерял её товарную структуру."""
    if deterministic.intent is Intent.EDIT_COMMENT:
        return deterministic
    if (
        deterministic.intent is Intent.ADD_ITEMS
        and deterministic.items
        and (
            proposed.intent is Intent.HISTORY_QUERY
            or proposed.intent is not Intent.ADD_ITEMS
            or not proposed.items
        )
    ):
        return deterministic
    return proposed
