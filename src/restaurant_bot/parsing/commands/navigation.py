"""Распознаёт свободную навигацию и команды работы с заявками."""

from __future__ import annotations

import re

from restaurant_bot.domain.models import Intent, ParsedCommand
from restaurant_bot.domain.units import UNIT_ALIASES
from restaurant_bot.parsing.commands.normalization import _has_word_stem, has_negation
from restaurant_bot.parsing.number_words import NUMBER_WORDS


def _looks_like_generic_add_navigation(normalized: str, words: list[str]) -> bool:
    """Отличает переход к добавлению от названия конкретного товара."""
    has_action = _has_word_stem(
        words,
        "добав",
        "внес",
        "докин",
        "попол",
        "собир",
        "продолж",
        "перей",
        "верн",
        "пойд",
        "пошл",
        "набер",
        "закаж",
        "куп",
    )
    has_generic_target = _has_word_stem(
        words,
        "товар",
        "позици",
        "продукт",
        "покуп",
        "спис",
    ) or bool(re.search(r"\b(?:что[- ]?нибудь|что[- ]?то)\b", normalized))
    if not has_action or not has_generic_target:
        return False

    # Число или единица почти всегда означают реальную товарную строку:
    # «добавь товар сироп 5 кг» нельзя превращать в навигацию.
    if re.search(r"\d", normalized) or any(word in UNIT_ALIASES for word in words):
        return False

    filler_words = {
        "а",
        "в",
        "во",
        "да",
        "давай",
        "давайте",
        "для",
        "еще",
        "ещё",
        "и",
        "к",
        "ко",
        "бы",
        "мне",
        "может",
        "мы",
        "на",
        "нам",
        "наши",
        "нашу",
        "наш",
        "надо",
        "новые",
        "новых",
        "новый",
        "ну",
        "нужно",
        "пару",
        "пожалуйста",
        "потом",
        "сейчас",
        "текущую",
        "тогда",
        "хотелось",
        "хотим",
        "хочу",
        "я",
    }
    ignored_stems = (
        "добав",
        "внес",
        "докин",
        "попол",
        "собир",
        "продолж",
        "перей",
        "верн",
        "пойд",
        "пошл",
        "набер",
        "закаж",
        "куп",
        "товар",
        "позици",
        "продукт",
        "покуп",
        "спис",
        "заявк",
        "заказ",
        "что-нибудь",
        "что-то",
    )
    meaningful = [
        word for word in words if word not in filler_words and not word.startswith(ignored_stems)
    ]
    return not meaningful


def _infer_status_intent(normalized: str, words: list[str]) -> Intent | None:
    """Распознаёт команды проверки статуса заявок."""
    # Статусы имеют самый явный смысл и должны быть приоритетнее общих слов
    # «посмотреть», «заявка» и «заказ».
    if _has_word_stem(words, "статус"):
        return Intent.ORDER_STATUS
    if re.search(
        r"\b(?:что|как|где)(?:\s+там)?\s+(?:с|со)\s+"
        r"(?:моей|моим|моими|нашей|нашим|последней|последним|текущей|текущим)?\s*"
        r"(?:заявк|заказ)",
        normalized,
    ) or (
        _has_word_stem(words, "заявк", "заказ")
        and _has_word_stem(words, "ушл", "дошл", "отправлен", "принят", "обработ")
    ):
        return Intent.ORDER_STATUS
    if (
        _has_word_stem(words, "заявк", "заказ")
        and _has_word_stem(words, "как", "где", "посмотр", "покаж", "пров", "узна", "обнов")
        and _has_word_stem(words, "мо", "наш", "послед", "текущ")
    ):
        return Intent.ORDER_STATUS
    return None


def _infer_help_intent(normalized: str, words: list[str]) -> Intent | None:
    """Распознаёт свободные запросы помощи."""
    if _has_word_stem(words, "помощ", "инструкц", "подсказ"):
        return Intent.HELP
    if (_has_word_stem(words, "уме") and _has_word_stem(words, "что", "как")) or (
        _has_word_stem(words, "объясн", "расскаж")
        and _has_word_stem(words, "бот", "работ", "польз")
    ):
        return Intent.HELP
    if (
        _has_word_stem(words, "как")
        and _has_word_stem(words, "сдел", "созда", "оформ", "собра")
        and _has_word_stem(words, "заявк", "заказ")
    ):
        return Intent.HELP
    return None


def _infer_procurement_intent(normalized: str, words: list[str]) -> Intent | None:
    """Распознаёт навигацию по запросам снабжению."""
    has_procurement_request = _has_word_stem(words, "запрос")
    has_request_list_action = _has_word_stem(
        words, "покаж", "показ", "посмотр", "откр", "вывед", "спис", "пров"
    )
    has_procurement_target = _has_word_stem(words, "снабжен", "менеджер")
    has_request_send_action = _has_word_stem(words, "отправ", "созда", "оформ", "переда")
    if (
        has_procurement_request
        and not has_request_send_action
        and (has_request_list_action or has_procurement_target)
    ):
        return Intent.PRODUCT_ADD_LIST
    return None


def _infer_draft_intent(normalized: str, words: list[str]) -> Intent | None:
    """Распознаёт очистку, создание и переключение черновика."""
    # Сначала различаем очистку всей заявки и удаление одной позиции.
    clear_action = _has_word_stem(
        words,
        "очист",
        "сброс",
        "сбрасы",
        "обнул",
        "вычист",
        "стер",
        "сотри",
    )
    clear_target = _has_word_stem(words, "корзин", "черновик", "заявк", "заказ", "спис")
    remove_all = _has_word_stem(words, "удал", "убер") and _has_word_stem(
        words, "все", "всё", "всю", "весь", "полност", "целик"
    )
    restart_after_clear = _has_word_stem(words, "начн", "начат") and _has_word_stem(
        words, "занов", "сначал"
    )
    if not has_negation(normalized) and restart_after_clear and (clear_action or remove_all):
        return Intent.CLEAR_CART
    if (clear_action and clear_target) or remove_all:
        return Intent.CLEAR_CART
    if (
        not has_negation(normalized)
        and _has_word_stem(words, "начн", "начат", "созда")
        and _has_word_stem(words, "занов", "нов")
        and _has_word_stem(words, "заявк", "заказ", "черновик")
    ):
        return Intent.START_NEW_ORDER
    if (
        _has_word_stem(words, "удал", "убер", "отмен")
        and _has_word_stem(words, "текущ", "цел")
        and _has_word_stem(words, "заявк", "заказ", "черновик")
    ):
        return Intent.CLEAR_CART

    has_new_order_target = _has_word_stem(words, "заявк", "заказ", "черновик")
    has_new_order_marker = _has_word_stem(
        words,
        "нов",
        "друг",
        "следующ",
        "повторн",
        "занов",
    ) or (_has_word_stem(words, "еще", "ещё") and _has_word_stem(words, "одн"))
    has_new_order_action = _has_word_stem(
        words,
        "начн",
        "начат",
        "созда",
        "сдела",
        "оформ",
        "откр",
        "хоч",
        "нуж",
        "над",
        "давай",
    )
    if (
        not has_negation(normalized)
        and has_new_order_target
        and has_new_order_marker
        and has_new_order_action
        and not re.search(r"\d", normalized)
        and not any(word in UNIT_ALIASES for word in words)
    ):
        return Intent.START_NEW_ORDER
    return None


def _infer_submission_review_intent(normalized: str, words: list[str]) -> Intent | None:
    """Распознаёт подтверждение, отправку и финальную проверку."""
    if "как есть" in normalized and _has_word_stem(words, "отправ", "оформ", "переда", "запиш"):
        return Intent.SUBMIT_AS_IS
    if _has_word_stem(words, "риск") and _has_word_stem(words, "приним"):
        return Intent.SUBMIT_AS_IS

    if (
        _has_word_stem(words, "финальн")
        and _has_word_stem(words, "провер")
        and _has_word_stem(words, "покаж", "верн", "перей", "откр")
    ):
        return Intent.SHOW_FINAL_REVIEW
    if (
        _has_word_stem(words, "итог")
        and _has_word_stem(words, "покаж", "посмотр", "откр", "верн", "перей")
    ) or ("перед отправкой" in normalized and _has_word_stem(words, "пров", "посмотр", "покаж")):
        return Intent.SHOW_FINAL_REVIEW

    if _has_word_stem(words, "минимал", "минимальн") and _has_word_stem(
        words, "пров", "посмотр", "покаж", "что", "как", "верн", "перей", "назад"
    ):
        return Intent.CHECK_MIN_SUM

    if _has_word_stem(words, "повтор") and _has_word_stem(words, "отправ"):
        return Intent.SUBMIT_AS_IS
    return None


def _infer_cart_intent(normalized: str, words: list[str]) -> Intent | None:
    """Распознаёт просмотр и дополнение текущего черновика."""
    show_action = _has_word_stem(words, "покаж", "показ", "посмотр", "откр", "вывед")
    product_target = _has_word_stem(words, "товар", "позиц", "продукт", "спис")
    is_issue_request = _has_word_stem(words, "уточн", "проблемн", "спорн")
    if (
        show_action
        and product_target
        and not is_issue_request
        and _has_word_stem(words, "поставщик")
    ):
        return Intent.CHECK_MIN_SUM
    if show_action and product_target and not is_issue_request:
        return Intent.SHOW_CART
    if (
        _has_word_stem(words, "заявк", "заказ", "корзин", "черновик")
        and (
            (
                _has_word_stem(words, "что", "какие")
                and ("у меня" in normalized or _has_word_stem(words, "мо", "наш", "текущ", "внутр"))
            )
            or _has_word_stem(words, "содерж", "добавлен", "леж")
        )
    ) or (
        _has_word_stem(words, "что", "какие")
        and _has_word_stem(words, "добавлен", "внесен", "внесён", "выбран")
    ):
        return Intent.SHOW_CART

    if _looks_like_generic_add_navigation(normalized, words):
        return Intent.ADD_MORE

    if _has_word_stem(words, "корзин", "черновик") and _has_word_stem(
        words, "покаж", "посмотр", "откр", "верн", "перей", "зайд", "пойд"
    ):
        return Intent.SHOW_CART
    if (
        _has_word_stem(words, "текущ")
        and _has_word_stem(words, "заявк", "заказ")
        and _has_word_stem(words, "покаж", "посмотр", "откр", "верн")
    ):
        return Intent.SHOW_CART
    return None


def _infer_workflow_intent(normalized: str, words: list[str]) -> Intent | None:
    """Распознаёт переходы рабочего процесса."""
    if _has_word_stem(words, "отправ", "оформ", "заверш", "запиш") and _has_word_stem(
        words, "заявк", "заказ", "корзин"
    ):
        return Intent.SUBMIT_REQUEST
    if _has_word_stem(words, "готов") and _has_word_stem(words, "отправ", "оформ", "заверш"):
        return Intent.SUBMIT_REQUEST
    if (
        (_has_word_stem(words, "готов") and _has_word_stem(words, "заявк", "заказ", "все", "всё"))
        or (
            _has_word_stem(words, "добав")
            and _has_word_stem(words, "все", "всё")
            and _has_word_stem(words, "я", "мы")
        )
        or (_has_word_stem(words, "можн") and _has_word_stem(words, "оформ", "отправ", "заверш"))
    ):
        return Intent.SUBMIT_REQUEST

    if _has_word_stem(words, "назад", "предыдущ") and _has_word_stem(
        words, "верн", "перей", "пойд", "шаг", "экран", "назад"
    ):
        return Intent.BACK
    if _has_word_stem(words, "продолж") and _has_word_stem(
        words, "работ", "дальш", "оформ", "заявк"
    ):
        return Intent.CONTINUE_CURRENT
    return None


def _infer_resolution_intent(normalized: str, words: list[str]) -> Intent | None:
    """Распознаёт команды уточнения и изменения количества."""
    has_issue_word = _has_word_stem(words, "уточн", "проблемн", "спорн", "ошиб") or (
        _has_word_stem(words, "не") and _has_word_stem(words, "найден", "нашл")
    )
    if has_issue_word and _has_word_stem(
        words, "покаж", "посмотр", "разбер", "перей", "провер", "что", "какие"
    ):
        return Intent.CLARIFY_CURRENT
    if _has_word_stem(words, "рекоменд", "ближайш", "округл") and _has_word_stem(
        words, "колич", "постав", "сдел", "возьм", "исправ"
    ):
        return Intent.ACCEPT_SUGGESTED_QUANTITY
    if (
        _has_word_stem(words, "колич")
        and _has_word_stem(
            words, "исправ", "поправ", "измен", "поменя", "выбр", "выбе", "выбира", "подобр"
        )
        and not _has_word_stem(words, "друг", "нов", "сво", "введ", "укаж", "скаж")
    ):
        return Intent.FIX_MULTIPLE
    if _has_word_stem(words, "каталог") and _has_word_stem(
        words, "единиц", "остав", "использ", "сдел"
    ):
        return Intent.USE_CATALOG_UNIT
    if _has_word_stem(words, "колич") and _has_word_stem(
        words, "друг", "нов", "измен", "введ", "укаж", "скаж"
    ):
        return Intent.ENTER_OTHER_QUANTITY
    if _has_word_stem(words, "остав", "сохран") and (
        (
            (re.search(r"\d", normalized) or any(word in NUMBER_WORDS for word in words))
            and any(word in UNIT_ALIASES for word in words)
        )
        or any(
            marker in normalized
            for marker in ("как есть", "как было", "как указано", "без изменений")
        )
    ):
        return Intent.KEEP_CURRENT_QUANTITY
    if (_has_word_stem(words, "не") and _has_word_stem(words, "меня")) or (
        _has_word_stem(words, "остав", "сохран")
        and (
            "как есть" in normalized
            or "как было" in normalized
            or "как указано" in normalized
            or "без изменений" in normalized
            or _has_word_stem(words, "текущ")
        )
    ):
        return Intent.KEEP_CURRENT_QUANTITY
    return None


def _infer_free_form_navigation(normalized: str) -> Intent | None:
    """Распознаёт свободную навигацию через упорядоченные тематические этапы."""
    words = re.findall(r"[a-zа-яё0-9-]+", normalized, flags=re.I)
    if not words:
        return None
    resolvers = (
        _infer_status_intent,
        _infer_help_intent,
        _infer_procurement_intent,
        _infer_draft_intent,
        _infer_submission_review_intent,
        _infer_cart_intent,
        _infer_workflow_intent,
        _infer_resolution_intent,
    )
    for resolver in resolvers:
        intent = resolver(normalized, words)
        if intent is not None:
            return intent
    return None


def _parse_order_status_navigation(normalized: str, source_text: str) -> ParsedCommand | None:
    """Разбирает выбор заявки и навигацию по истории естественной фразой."""
    if has_negation(normalized):
        return None
    has_history_target = bool(re.search(r"\b(?:заявк\w*|заказ\w*|страниц\w*)\b", normalized))
    if not has_history_target:
        return None
    has_navigation_action = bool(
        re.search(r"\b(?:покаж\w*|откро\w*|открой\w*|посмотр\w*|перейд\w*)\b", normalized)
    )
    has_next_page_wording = bool(
        re.search(
            r"\b(?:следующ\w*|дальше|стар\w*|впер[её]д)\b",
            normalized,
        )
    )
    creates_order = bool(re.search(r"\b(?:созда\w*|оформ\w*|нача\w*|сдела\w*)\b", normalized))
    if (
        has_next_page_wording or (has_navigation_action and "следующ" in normalized)
    ) and not creates_order:
        return ParsedCommand(
            intent=Intent.ORDER_STATUS,
            text=source_text,
            callback_target="next",
        )
    if re.search(
        r"\b(?:предыдущ\w*|новее|назад|более\s+нов\w*)\b",
        normalized,
    ):
        return ParsedCommand(
            intent=Intent.ORDER_STATUS,
            text=source_text,
            callback_target="previous",
        )
    if match := re.search(
        r"\b(?:заявк\w*|заказ\w*)\s*(?:номер|№)\s*([a-zа-яё0-9#№._/-]+)",
        normalized,
        re.I,
    ):
        selected_index = {
            "1": 1,
            "один": 1,
            "первый": 1,
            "2": 2,
            "два": 2,
            "второй": 2,
            "3": 3,
            "три": 3,
            "третий": 3,
            "4": 4,
            "четыре": 4,
            "четвертый": 4,
            "5": 5,
            "пять": 5,
            "пятый": 5,
        }.get(match.group(1))
        return ParsedCommand(
            intent=Intent.ORDER_STATUS,
            text=source_text,
            selected_index=selected_index,
            selection_query="" if selected_index is not None else match.group(1),
        )

    ordinals = {
        1: r"(?:1|один|перв\w*|последн\w*|свеж\w*)",
        2: r"(?:2|два|втор\w*)",
        3: r"(?:3|три|трет\w*)",
        4: r"(?:4|четыр\w*)",
        5: r"(?:5|пять|пят\w*)",
    }
    for index, ordinal in ordinals.items():
        if re.search(
            rf"(?:^|\s){ordinal}(?:\s+(?:заявк\w*|заказ\w*)|$)",
            normalized,
        ):
            return ParsedCommand(
                intent=Intent.ORDER_STATUS,
                text=source_text,
                selected_index=index,
            )
    return None
