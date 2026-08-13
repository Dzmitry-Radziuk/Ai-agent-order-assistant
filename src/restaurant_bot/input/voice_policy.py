"""Содержит политику контекстного распознавания голосовых команд."""

from __future__ import annotations

import re

from restaurant_bot.conversation.selection import contains_score
from restaurant_bot.domain.models import ConversationState, Intent, ItemStatus, SessionStage
from restaurant_bot.domain.text import normalize_text
from restaurant_bot.domain.units import normalize_unit
from restaurant_bot.parsing.commands.api import infer_intent
from restaurant_bot.parsing.quantities import parse_quantity_unit


def match_visible_action(text: str, state: ConversationState) -> str:
    """Находит явно названную кнопку текущего экрана."""
    phrase = normalize_text(text)
    if not phrase:
        return ""
    filler_stems = (
        "давай",
        "давайте",
        "пожалуй",
        "можно",
        "хочу",
        "хотел",
        "нужно",
        "надо",
        "пойд",
        "перейд",
    )
    phrase_words = [
        word
        for word in re.findall(r"[a-zа-яё0-9]+", phrase, flags=re.I)
        if word not in {"я", "мы", "мне", "нам", "бы", "сейчас"}
        and not any(word.startswith(stem) for stem in filler_stems)
    ]
    phrase_tokens = set(phrase_words)
    best: tuple[float, str] = (0.0, "")
    for action in state.visible_actions:
        label = normalize_text(action.get("label"))
        callback_data = str(action.get("action_id") or "")
        if not label or not callback_data:
            continue
        label_words = re.findall(r"[a-zа-яё0-9]+", label, flags=re.I)
        if " ".join(label_words) == " ".join(phrase_words):
            return callback_data
        label_tokens = set(label_words)
        if len(phrase_tokens) >= 2 and phrase_tokens <= label_tokens and label_tokens:
            score = len(phrase_tokens) / len(label_tokens)
            if score > best[0]:
                best = (score, callback_data)
    return best[1] if best[0] >= 0.72 else ""


def voice_transcription_prompt(state: ConversationState) -> str:
    """Формирует контекст для распознавания голоса."""
    current = state.current_item()
    if current and current.status == ItemStatus.AMBIGUOUS:
        names = "; ".join(candidate.name for candidate in current.candidates[:5])
        return (
            "Русская речь. Пользователь выбирает один из вариантов товара: "
            f"{names}. Он может сказать номер, например «первый» или «вариант два», "
            "либо полное или частичное название. Верни только произнесённый русский текст."
        )
    if current and state.stage == SessionStage.AWAIT_UNIT_QUANTITY and current.catalog_unit:
        expected_unit = normalize_unit(current.catalog_unit)
        product_name = current.catalog_name or current.source_query
        return (
            "Русская речь сотрудника кафе. Пользователь отвечает на просьбу указать "
            f"количество товара «{product_name}» в {expected_unit}. Точно сохрани "
            "произнесённое число и полное название единицы измерения. Особенно не "
            "путай «грамм» и «килограмм». Ничего не заменяй и не придумывай. "
            "Верни только произнесённый русский текст."
        )
    if current and current.status == ItemStatus.MISSING_QTY and current.catalog_unit:
        expected_unit = normalize_unit(current.catalog_unit)
        product_name = current.catalog_name or current.source_query
        return (
            "Русская речь сотрудника кафе. Пользователь отвечает на просьбу указать "
            f"количество товара «{product_name}» в {expected_unit}. Он может сказать "
            "только число («пять», «5») или число с единицей («пять штук», «5 кг»). "
            "Обязательно сохрани произнесённое число и единицу измерения. Короткий "
            "ответ с числом — это количество, а не команда «не добавлять». Не заменяй "
            "число командой и не придумывай текст. Верни только произнесённый русский текст."
        )
    visible_actions = getattr(state, "visible_actions", [])
    visible = "; ".join(
        action.get("label", "") for action in visible_actions[:10] if action.get("label")
    )
    screen_hint = f" На текущем экране есть кнопки: {visible}." if visible else ""
    return (
        "Русская речь сотрудника кафе. Верни только произнесённый русский текст, "
        "ничего не заменяй и не придумывай. Возможные команды: «добавить товары», "
        "«добавь товары», «покажи черновик», «отправить заявку», «очистить черновик», "
        "«не добавлять», «не отправлять», «пропускаем», «первый вариант», "
        "«второй вариант». Сохраняй частицы «не» и «нет» дословно: они меняют "
        "действие на противоположное. Команда не является названием товара."
        f"{screen_hint}"
    )


def requires_high_accuracy_transcription(
    transcript: str,
    state: ConversationState,
) -> bool:
    """Проверяет необходимость повторного распознавания речи."""
    normalized = normalize_text(transcript)
    if normalized in {"тестовый товар", "тест товар", "test product"}:
        return True
    if match_visible_action(transcript, state):
        return False
    transcript_words = set(re.findall(r"[a-zа-яё0-9]+", normalized, flags=re.I))
    for action in getattr(state, "visible_actions", []):
        label_words = set(
            re.findall(
                r"[a-zа-яё0-9]+",
                normalize_text(action.get("label")),
                flags=re.I,
            )
        )
        if transcript_words & label_words and transcript_words != label_words:
            return True
    current = state.current_item()
    if current and state.stage == SessionStage.AWAIT_UNIT_QUANTITY and current.catalog_unit:
        quantity, spoken_unit = parse_quantity_unit(re.sub(r"[.!?]+$", "", transcript).strip())
        if (
            quantity is not None
            and spoken_unit
            and normalize_unit(spoken_unit) != normalize_unit(current.catalog_unit)
        ):
            return True
    if current is None or current.status != ItemStatus.AMBIGUOUS:
        return False
    command = infer_intent(transcript)
    if command.intent not in {Intent.UNKNOWN, Intent.ADD_ITEMS}:
        return False
    scores = [
        contains_score(normalized, normalize_text(candidate.name))
        for candidate in current.candidates
    ]
    return max(scores, default=0) < 2
