from __future__ import annotations

import re
from collections.abc import Sequence

from restaurant_bot.domain.models import DialogueResponse, ExtractedItem, Intent, ParsedCommand
from restaurant_bot.parsing.comment_scope import _extract_global_comment
from restaurant_bot.parsing.comment_scope import (
    has_explicit_global_comment_scope as _has_explicit_global_comment_scope,
)
from restaurant_bot.parsing.products import parse_product_lines as _parse_product_lines
from restaurant_bot.parsing.quantities import parse_quantity_unit as _parse_quantity_unit
from restaurant_bot.services.text import (
    NUMBER_WORDS,
    UNIT_ALIASES,
    clean_text,
    normalize_text,
    normalize_unit,
)

has_explicit_global_comment_scope = _has_explicit_global_comment_scope
parse_product_lines = _parse_product_lines
parse_quantity_unit = _parse_quantity_unit

_COMMANDS: list[tuple[Intent, re.Pattern[str]]] = [
    (
        Intent.GREETING,
        re.compile(
            r"^(?:/start|старт|начать работу|начни работу|начать|начнём|начнем|"
            r"запускай|запусти(?: бота)?|привет|здравствуйте)$",
            re.I,
        ),
    ),
    (
        Intent.HELP,
        re.compile(
            r"^(?:/help|помощь|помоги|подскажи(?: что делать)?|покажи помощь|"
            r"что ты умеешь|как пользоваться|как использовать|как (?:сделать|оформить) "
            r"(?:заказ|заявку)|не (?:знаю|понимаю) что делать)$",
            re.I,
        ),
    ),
    (
        Intent.SHOW_CART,
        re.compile(
            r"^(?:/draft|черновик|корзина|покажи (?:корзину|черновик|заявку)|открой (?:корзину|черновик|заявку)|к черновику|моя заявка)$",
            re.I,
        ),
    ),
    (
        Intent.ORDER_STATUS,
        re.compile(
            r"^(?:/orders|мои заявки|покажи мои заявки|статус(?:ы)? заявок|проверь статус заявки|обновить статусы?|покажи заявки)$",
            re.I,
        ),
    ),
    (
        Intent.CLEAR_CART,
        re.compile(
            r"^(?:/reset|сброс|сбрось черновик|очисти(?:ть)? (?:заявку|корзину|черновик)|"
            r"(?:сброс(?:ить|ь)|очист(?:ить|и)|обнул(?:ить|и))(?: (?:заявку|корзину|черновик))? "
            r"и (?:начать|начни|начинаем) (?:заново|сначала)|"
            r"(?:удали|убери) (?:всё|все|всю|весь) и (?:начать|начни|начинаем) (?:заново|сначала))$",
            re.I,
        ),
    ),
    (
        Intent.START_NEW_ORDER,
        re.compile(
            r"^(?:новая заявка|новую заявку|новый заказ|новой заказ|новый черновик|"
            r"ещ[её] одна заявка|ещ[её] один заказ|начать заново|начни заново|"
            r"(?:заказ|заявка|черновик) заново)$",
            re.I,
        ),
    ),
    (
        Intent.SUBMIT_REQUEST,
        re.compile(
            r"^(?:/submit|отправь заявку|отправить заявку|отправить|отправь в корзину|отправить в корзину|добавь товары? в корзину|добавить товары? в корзину|перенеси в корзину|перенести в корзину|перейти к проверке заявки|готово отправляй|оформи заявку)$",
            re.I,
        ),
    ),
    (Intent.BACK, re.compile(r"^(?:/back|назад|вернуться)$", re.I)),
    (
        Intent.ADD_MORE,
        re.compile(
            r"^(?:(?:добавить(?:\s+ещ[её])?|добавь(?:\s+ещ[её])?|давай\s+добавим|хочу\s+добавить)\s+товары|"
            r"ещ[её]\s+товары|продолжим\s+добавлять|есть\s+ещ[её])$",
            re.I,
        ),
    ),
    (
        Intent.CONFIRM,
        re.compile(
            r"^(?:да|давай|да конечно|да (?:всё|все) верно|ага|ок|окей|верно|"
            r"всё верно|все верно|правильно|"
            r"согласен|согласна|подтверждаю|подтверждаю заявку)$",
            re.I,
        ),
    ),
    (
        Intent.CANCEL,
        re.compile(
            r"^(?:/cancel|нет|нет спасибо|нет не отправляй|не отправляй|не надо|не нужно|"
            r"отмена|отменить|"
            r"передумал|передумала|стоп|прекрати)$",
            re.I,
        ),
    ),
    (
        Intent.THANKS,
        re.compile(
            r"^(?:спасибо|благодарю|большое спасибо|спасибо большое|отлично|супер|класс)$",
            re.I,
        ),
    ),
    (Intent.SMALL_TALK, re.compile(r"^(?:как дела|кто ты|что нового)$", re.I)),
]

# Natural voice forms from ``inferVoiceCommand`` in the active n8n workflow.
# These are deterministic navigation/actions. Product extraction is attempted
# only after none of these patterns matches, so a spoken command can never
# become a draft item named "товары" or "черновик".
_NATURAL_COMMANDS: list[tuple[Intent, re.Pattern[str]]] = [
    (
        Intent.GREETING,
        re.compile(
            r"^(?:старт|начать работу|начни работу|запусти бота|открой бота|привет|здравствуй|"
            r"здравствуйте|доброе утро|добрый день|добрый вечер|приветствую|хай)(?: (?:бот|ассистент))?$",
            re.I,
        ),
    ),
    (
        Intent.HELP,
        re.compile(
            r"^(?:помощь|помоги|покажи помощь|открой помощь|инструкция|что ты умеешь|"
            r"как пользоваться|как использовать|что делать)$",
            re.I,
        ),
    ),
    (
        Intent.SHOW_CART,
        re.compile(
            r"^(?:корзина|карзин(?:а|у)?|черновик|моя заявка|текущая заявка|к (?:корзине|черновику)|"
            r"верни (?:к|в) (?:корзину|черновик)|вернись (?:к|в) (?:корзине|корзину|черновику|черновик)|"
            r"(?:покажи|показать|открой|открыть) (?:корзину|черновик|заявку|текущую заявку)|"
            r"покажи текущую заявку)$",
            re.I,
        ),
    ),
    (
        Intent.ORDER_STATUS,
        re.compile(
            r"^(?:мои заявки|покажи мои заявки|покажи заказы|статус(?:ы)? (?:заявок|заказов)|"
            r"(?:проверь|проверить|покажи|показать|обнови|обновить) статус(?:ы)?"
            r"(?: (?:моей|моих|текущей|последней))?(?: (?:заявки|заявок|заказа|заказов))?|"
            r"обнови|обновить|(?:какой|какие) статус(?:ы)?(?: (?:заявки|заявок|заказа|заказов))?|"
            r"что (?:со|с) статусом(?: (?:заявки|заказа))?)$",
            re.I,
        ),
    ),
    (
        Intent.CLEAR_CART,
        re.compile(
            r"^(?:сброс|(?:сбрось|сбросить|очисти|очистить|обнули|обнулить|вычисти|вычистить)"
            r"(?: (?:всю|весь|все|полностью))? (?:заявку|корзину|черновик|список|заказ|товары|позиции)|"
            r"(?:удали|удалить|убери|убрать|сотри|стереть) (?:все|всё|всю|весь)(?: (?:из )?"
            r"(?:заявки|корзины|черновика|списка|заказа))?)$",
            re.I,
        ),
    ),
    (
        Intent.START_NEW_ORDER,
        re.compile(
            r"^(?:(?:(?:давай|давайте|хочу|хотим|нужно|надо|можно|можешь|можете|пора|попробуем|пожалуйста)\s+)?"
            r"(?:созда\w*|сдела\w*|нача\w*)\s+(?:заявк\w*|заказ\w*|черновик\w*)|"
            r"(?:давай|давайте|хочу|хотим|нужно|надо|можно|можешь|можете|пора)? ?"
            r"(?:начать|начни|начинаем|создать|создай|сделать|оформить|открыть)? ?"
            r"(?:новую|новая|другую|следующую|ещ[её] одну) (?:заявку|заявка)|"
            r"(?:давай|давайте|хочу|хотим|нужно|надо|можно|можешь|можете|пора)? ?"
            r"(?:начать|начни|начинаем|создать|создай|сделать|оформить|открыть)? ?"
            r"(?:новый|другой|следующий|ещ[её] один) (?:заказ|черновик)|"
            r"начать заново|начни заново|(?:заказ|заявка|черновик) заново)$",
            re.I,
        ),
    ),
    (
        Intent.ADD_MORE,
        re.compile(
            r"^(?:(?:давай )?(?:добавим|добавить|добавь)(?: мне)?"
            r"(?:(?: еще| ещё)(?: товары| товар| позиции| позиций| что-нибудь| что то)?|"
            r" (?:товары|товар|позиции|позиций|что-нибудь|что то))|"
            r"(?:хочу|нужно|надо|можно) (?:добавить )?(?:еще|ещё)(?: товары| позиции| что-нибудь| что то)?|"
            r"(?:еще|ещё) (?:товары|позиции)|"
            r"(?:продолжим|продолжить|давай продолжим|вернемся|вернуться)"
            r" (?:собирать|добавлять|к добавлению)(?: (?:заявку|товары|позиции))?|"
            r"(?:есть|будут) (?:еще|ещё)(?: (?:товары|позиции))?)$",
            re.I,
        ),
    ),
    (
        Intent.SUBMIT_AS_IS,
        re.compile(
            r"^(?:(?:давай )?(?:отправь|отправить|отправляй|запиши|записать)(?: заявку)?"
            r" (?:как есть|с предупреждением)|(?:все|всё) равно (?:отправь|отправляй|записывай)|"
            r"риск принимаю|не будем добирать|"
            r"(?:отправь|отправить|отправляй|передай|передать|передавай)(?: (?:заявку|заказ|товары))?"
            r" (?:поставщику|поставшику|поставщикам|снабженцу|в снабжение)(?: (?:заявку|заказ|товары))?)$",
            re.I,
        ),
    ),
    (
        Intent.SUBMIT_REQUEST,
        re.compile(
            r"^(?:(?:(?:да|ага|ладно|хорошо|ок|окей)\s*,?\s*|давай\s+)?"
            r"(?:отправь|отправить|отправляй|добавь|добавить|положи|положить|перенеси|перенести)"
            r"(?: (?:товары|товар|позиции|позицию|заявку|заказ))? в корзину|"
            r"(?:(?:да|ага|ладно|хорошо|ок|окей)\s*,?\s*|давай\s+)?"
            r"(?:отправь|отправить|отправляй|оформи|оформить|запиши|записать)(?: заявку| в таблицу заказа| в таблицу)?|"
            r"перейти к проверке заявки|проверь заявку перед отправкой|готово отправляй)$",
            re.I,
        ),
    ),
    (
        Intent.BACK,
        re.compile(r"^(?:назад|вернуться|вернись назад|давай назад|к предыдущему шагу)$", re.I),
    ),
    (
        Intent.CONTINUE_CURRENT,
        re.compile(
            r"^(?:продолжить|продолжай|продолжаем|давай продолжим|поехали|дальше|можно дальше)$",
            re.I,
        ),
    ),
    (
        Intent.CHECK_MIN_SUM,
        re.compile(
            r"^(?:(?:давай )?(?:проверь|проверим|проверить|посмотри|посмотрим|посмотреть|покажи|показать)"
            r"(?: (?:минимальную сумму|минималку|предупреждение|предупреждения|поставщиков))?|"
            r"(?:что|как) (?:с|по) (?:минималкой|минимальной суммой))$",
            re.I,
        ),
    ),
    (
        Intent.SHOW_FINAL_REVIEW,
        re.compile(
            r"^(?:(?:назад )?(?:к|на) финальн(?:ой|ую) проверк[еу]|"
            r"(?:покажи|верни|вернись)(?: к)? финальн(?:ую|ой) проверк[еу]|"
            r"(?:покажи|показать|открой|открыть) (?:итог|итоги|итоговую заявку)|"
            r"(?:проверь|проверить|посмотри|посмотреть) перед отправкой)$",
            re.I,
        ),
    ),
    (
        Intent.FIX_MULTIPLE,
        re.compile(
            r"^(?:(?:давай |давайте |хочу |нужно |надо )?"
            r"(?:исправь|исправить|исправим|исправьте|поправь|поправить|поправим|поправьте|"
            r"измени|изменить|изменим|измените|поменяй|поменять|поменяем|поменяйте|сделай|сделать|сделаем)"
            r"(?: (?:это|текущее))?(?: (?:количество|кол-во))?)$",
            re.I,
        ),
    ),
    (
        Intent.ENTER_OTHER_QUANTITY,
        re.compile(
            r"^(?:"
            r"(?:введу|ввести|скажу|сказать|назову|назвать|укажу|указать)(?: (?:другое|новое))? количество|"
            r"(?:хочу|давай) (?:ввести|указать|сказать) (?:другое|новое) количество|"
            r"(?:(?:я )?(?:хочу |буду |давай |давайте |нужно |надо )?)"
            r"(?:введу|ввести|введем|введём|скажем|сказать|назову|назвать|"
            r"укажу|указать|укажем)"
            r"(?: (?:другое|новое))?"
            r"(?: (?:количество|кол-во|вес|объем|объём))?"
            r" (?:в|по) "
            r"(?:кг|кило|килограмм\w*|шт\w*|штук\w*|грамм\w*|литр\w*|"
            r"миллилитр\w*|упаков\w*|короб\w*|пач\w*|банк\w*|бутыл\w*))$",
            re.I,
        ),
    ),
    (
        Intent.ACCEPT_SUGGESTED_QUANTITY,
        re.compile(
            r"^(?:(?:исправь|исправить|доведи|довести|сделай|поставь|замени)"
            r" (?:до (?:подходящего|рекомендованного|ближайшего)|как (?:нужно|положено)|"
            r"по (?:условиям|шагу)|правильно|рекомендованное|подходящее|ближайшее)(?: количество)?|"
            r"(?:округли|округлить)(?: количество)?(?: до (?:ближайшего|подходящего|рекомендованного))?|"
            r"(?:используй|поставь|выбери|возьми) (?:рекомендованное|подходящее|ближайшее) количество)$",
            re.I,
        ),
    ),
    (
        Intent.KEEP_CURRENT_QUANTITY,
        re.compile(
            r"^(?:(?:оставь|оставить|сохрани|сохранить)(?: (?:как есть|текущее количество|это количество))?|"
            r"не меняй|ничего не меняй)$",
            re.I,
        ),
    ),
    (
        Intent.USE_CATALOG_UNIT,
        re.compile(
            r"^(?:(?:используй|использовать|оставь|оставить|сделай)(?: единицу)?"
            r" (?:из|как в|по) каталог[еу]|(?:сделай|оставь|используй) как в каталоге)$",
            re.I,
        ),
    ),
    (
        Intent.CLARIFY_CURRENT,
        re.compile(
            r"^(?:(?:давай )?(?:уточним|уточнять|уточнить|проверим|проверить|разберем|разобрать)"
            r"(?: (?:товары|позиции|заявку|все|проблемные позиции|спорные позиции))?|"
            r"(?:давай )?(?:перейдем|переходим|вернемся) (?:к )?(?:уточнению|уточнениям|проверке позиций)|"
            r"(?:покажи|показать|открой|открыть) (?:что нужно уточнить|уточнения|проблемные позиции|спорные позиции)|"
            r"(?:что|какие позиции) (?:нужно|надо|осталось) уточнить)$",
            re.I,
        ),
    ),
    (
        Intent.MANUAL_CURRENT,
        re.compile(
            r"^(?:(?:давай )?(?:измени|изменить|исправь|исправить|поищи|искать|опишу|описать|опиши)"
            r"(?: (?:этот|эту|его|ее|товар|позицию))? (?:название|по-другому|иначе|вручную|текстом)|"
            r"(?:ни один|ничего) не подходит|(?:нужного|подходящего)(?: товара| варианта)? нет|"
            r"изменить название|другое название|другой товар|нужен другой товар|"
            r"(?:введу|ввести|напишу|написать|укажу|указать) название (?:сам|сама|вручную)|"
            r"поищи иначе|опишу вручную)$",
            re.I,
        ),
    ),
    (
        Intent.SKIP_CURRENT,
        re.compile(
            r"^(?:(?:давай )?(?:пропусти|пропустить|пропустим|скипни|скипнуть)"
            r"(?: (?:этот|эту|текущий|текущую|данный|данную))?(?: (?:товар|позицию|строку|пункт))?"
            r"(?: и? (?:перейдем|пойдем|идем) дальше)?|"
            r"(?:убери|удалить|удали|исключи|исключить) (?:эту|этот|текущую|текущий) "
            r"(?:позицию|товар|строку|пункт)|"
            r"(?:не добавляй|не добавлять|не нужен|не нужна|этот не нужен|эта не нужна)"
            r"(?: (?:этот|эту))?(?: (?:товар|позицию|строку))?|"
            r"следующий|следующая|дальше|идем дальше|пойдем дальше|перейдем дальше|давай дальше)$",
            re.I,
        ),
    ),
]


def normalize_command_text(value: str) -> str:
    """Нормализует пунктуацию и разговорные слова команды."""
    text = re.sub(r"[.,!?;:]+", " ", normalize_text(value))
    text = re.sub(r"\s+", " ", text).strip()
    for _ in range(3):
        cleaned = re.sub(r"^(?:ну|это|так|ладно|хорошо|ок|окей|ага)\s+", "", text, flags=re.I)
        if cleaned == text:
            break
        text = cleaned.strip()
    return re.sub(r"\s+пожалуйста$", "", text, flags=re.I).strip()


def _has_word_stem(words: list[str], *stems: str) -> bool:
    """Проверяет наличие слова с одним из заданных корней."""
    return any(word.startswith(stems) for word in words)


def has_negation(text: str) -> bool:
    """Определяет явное отрицание в пользовательской фразе."""
    words = re.findall(r"[a-zа-яё0-9-]+", normalize_text(text), flags=re.I)
    return any(word in {"не", "нет", "никогда", "никак"} for word in words)


def has_negated_action(text: str, *action_stems: str) -> bool:
    """Находит действие, отрицаемое в пределах короткой разговорной фразы."""
    words = re.findall(r"[a-zа-яё0-9-]+", normalize_text(text), flags=re.I)
    for index, word in enumerate(words):
        if word not in {"не", "нет", "никогда", "никак"}:
            continue
        # «не хочу сейчас добавлять», «не надо больше отправлять» и похожие
        # формы сохраняют отрицание, даже когда между частицей и глаголом
        # стоят модальные или вводные слова.
        window = words[index + 1 : index + 7]
        if any(candidate.startswith(action_stems) for candidate in window):
            return True
    return False


def is_explicit_item_rejection(text: str) -> bool:
    """Распознаёт однозначный отказ от текущей товарной позиции."""
    normalized = normalize_command_text(text)
    words = re.findall(r"[a-zа-яё0-9-]+", normalized, flags=re.I)
    if not words:
        return False

    has_current_marker = _has_word_stem(
        words,
        "этот",
        "эту",
        "это",
        "данн",
        "текущ",
    )
    has_item_target = _has_word_stem(words, "товар", "позици", "строк", "пункт")
    if _has_word_stem(words, "пропуст", "пропуск", "скип") or (
        _has_word_stem(words, "исключ", "вычерк", "откаж")
        and has_current_marker
        and has_item_target
    ):
        return True
    if _has_word_stem(words, "убер", "удал") and has_current_marker and has_item_target:
        return True
    if re.search(r"\bне\s+(?:нужен|нужна|нужно|нужны)\b", normalized) and (
        has_item_target or has_current_marker
    ):
        return True
    return bool(
        has_negated_action(normalized, "добав")
        and (
            has_current_marker
            or re.fullmatch(
                r"(?:не\s+)?добав\w*(?:\s+(?:товар|позицию|строку|пункт))?",
                normalized,
            )
        )
    )


def _infer_negated_command(normalized: str) -> Intent | None:
    """Блокирует мутации, если пользователь явно отрицает действие."""
    if not has_negation(normalized):
        return None
    if is_explicit_item_rejection(normalized):
        return Intent.SKIP_CURRENT
    words = re.findall(r"[a-zа-яё0-9-]+", normalized, flags=re.I)
    has_new_order_target = _has_word_stem(words, "заявк", "заказ", "черновик")
    has_new_order_marker = _has_word_stem(
        words,
        "нов",
        "друг",
        "следующ",
        "повторн",
        "занов",
    )
    if has_new_order_target and has_new_order_marker:
        return Intent.CANCEL
    if has_negated_action(normalized, "исправ", "поправ", "измен", "поменя") and (
        "колич" in normalized or "как есть" in normalized
    ):
        return Intent.KEEP_CURRENT_QUANTITY
    if any(
        has_negated_action(normalized, *stems)
        for stems in (
            ("отправ", "оформ", "подтверж", "переда", "запиш"),
            ("очист", "очищ", "почист", "сброс", "сбрасы", "обнул", "удал", "убер"),
            ("использ", "остав", "перевед", "конверт"),
            ("выбер", "выбир", "возьм", "бери"),
            ("объедин", "суммир", "прибав", "слож", "увелич"),
        )
    ):
        return Intent.CANCEL
    if has_negated_action(normalized, "добав", "внес", "докин", "попол", "продолж"):
        generic_targets = {
            "товары",
            "товаров",
            "позиции",
            "позиций",
            "продукты",
            "продуктов",
        }
        removal = _REMOVE_RE.match(normalized)
        if removal:
            target = normalize_text(removal.group(1))
            if target and not any(word in generic_targets for word in target.split()):
                return None
        return Intent.CANCEL
    return None


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


def _infer_free_form_navigation(normalized: str) -> Intent | None:
    """Распознаёт свободную разговорную навигацию независимо от порядка слов."""
    words = re.findall(r"[a-zа-яё0-9-]+", normalized, flags=re.I)
    if not words:
        return None

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


_DRAFT_CONTAINER_RE = r"(?:заявк\w*|заказ\w*|корзин\w*|черновик\w*|списк\w*)"
_DRAFT_MODIFIER_RE = r"(?:мо\w+|наш\w+|текущ\w+|эт\w+|данн\w+)"
_DRAFT_LOCATION_RE = rf"{_DRAFT_CONTAINER_RE}(?:\s+товар\w*)?"
_REMOVE_ACTION_RE = (
    r"(?:убери|убрать|уберем|уберём|убираем|удали|удалить|удаляем|"
    r"исключи|исключить|вычеркни|вычеркнуть|сними|снять|выкинь|выкинуть|"
    r"не\s+добавляй|не\s+добавлять|не\s+заказывай|не\s+заказывать)"
)
_REMOVE_RE = re.compile(
    rf"^(?:(?:пожалуйста\s+)?(?:(?:давай|можешь|можно|нужно|надо)\s+)?"
    rf"{_REMOVE_ACTION_RE}\s+(?:пожалуйста\s+)?(?:(?:мне|у\s+меня)\s+)?"
    rf"(?:(?:из|с)\s+(?:{_DRAFT_MODIFIER_RE}\s+)?{_DRAFT_LOCATION_RE}\s+)?|"
    r"(?:мне\s+)?не\s+(?:нужен|нужна|нужно|нужны)\s+)(.+?)"
    r"(?:\s*,?\s*пожалуйста)?\s*$",
    re.I,
)
_EDIT_ACTION = (
    r"(?:измени|изменить|поменяй|поменять|сделай|сделать|поставь|поставить|"
    r"исправь|исправить|обнови|обновить|замени|заменить)"
)
_NUMBER_START = (
    r"(?:\d+(?:[,.]\d+)?|"
    + "|".join(sorted((re.escape(word) for word in NUMBER_WORDS), key=len, reverse=True))
    + r")"
)
_EDIT_AMOUNT = rf"(?P<amount>{_NUMBER_START}(?:\s+[a-zа-яё.]+){{0,3}})"
_EDIT_PATTERNS = [
    re.compile(
        rf"^{_EDIT_ACTION}\s+у\s+(?P<target>.+?)\s+(?:количество|кол-во)\s+"
        rf"(?:на|до)\s+{_EDIT_AMOUNT}$",
        re.I,
    ),
    re.compile(
        rf"^{_EDIT_ACTION}\s+(?:количество|кол-во)\s+(?:у\s+)?(?P<target>.+?)\s+"
        rf"(?:на|до)\s+{_EDIT_AMOUNT}$",
        re.I,
    ),
    re.compile(
        rf"^для\s+(?P<target>.+?)\s+{_EDIT_ACTION}(?:\s+(?:количество|кол-во))?"
        rf"\s+(?:на\s+)?{_EDIT_AMOUNT}$",
        re.I,
    ),
    re.compile(
        rf"^(?P<target>.+?)\s+{_EDIT_ACTION}(?:\s+(?:количество|кол-во))?"
        rf"\s+(?:на|до)\s+{_EDIT_AMOUNT}$",
        re.I,
    ),
    re.compile(
        rf"^(?P<target>.+?)\s+{_EDIT_ACTION}(?:\s+(?:количество|кол-во))?"
        rf"\s+{_EDIT_AMOUNT}$",
        re.I,
    ),
    re.compile(
        rf"^{_EDIT_ACTION}\s+(?:у\s+)?(?P<target>.+?)\s+(?:на|до)\s+{_EDIT_AMOUNT}$",
        re.I,
    ),
    re.compile(
        rf"^{_EDIT_ACTION}(?:\s+(?:количество|кол-во))?\s+(?:на|до)\s+{_EDIT_AMOUNT}$",
        re.I,
    ),
    re.compile(
        rf"^{_EDIT_ACTION}\s+(?:у\s+)?(?P<target>.+)\s+{_EDIT_AMOUNT}$",
        re.I,
    ),
]
_SELECT_RE = re.compile(r"^(?:вариант|номер|выбери)?\s*([1-5])$", re.I)


def clean_command_target(value: str) -> str:
    """Убирает служебные слова вокруг названия товара в команде."""
    target = clean_text(value)
    target = re.sub(r"^(?:пожалуйста\s+)", "", target, flags=re.I)
    target = re.sub(r"(?:\s*,?\s*пожалуйста)$", "", target, flags=re.I)
    target = target.strip(" ,;:-—–.!?")
    target = re.sub(r"^для\s+", "", target, flags=re.I)
    target = re.sub(
        r"^(?:(?:этот|эту|это|данный|данную|текущий|текущую)\s+)?"
        r"(?:товар|позицию|строку|пункт)\s+",
        "",
        target,
        flags=re.I,
    )
    target = re.sub(
        rf"\s+(?:из|с|в|на)\s+(?:{_DRAFT_MODIFIER_RE}\s+)?{_DRAFT_LOCATION_RE}\s*$",
        "",
        target,
        flags=re.I,
    )
    return target.strip(" ,;:-—–.!?")


def _is_whole_draft_target(value: str) -> bool:
    """Отличает название всего черновика от названия отдельного товара."""
    return bool(
        re.fullmatch(
            rf"(?:{_DRAFT_MODIFIER_RE}\s+)?{_DRAFT_LOCATION_RE}",
            clean_text(value),
            flags=re.I,
        )
    )


def _parse_edit_quantity(text: str) -> ParsedCommand | None:
    """Разбирает изменение количества при разном порядке слов."""
    for pattern in _EDIT_PATTERNS:
        match = pattern.fullmatch(text)
        if match is None:
            continue
        quantity, unit = parse_quantity_unit(match.group("amount"))
        if quantity is None:
            continue
        target = clean_command_target(match.groupdict().get("target") or "")
        return ParsedCommand(
            intent=Intent.EDIT_QUANTITY,
            text=text,
            target_query=target,
            edit_quantity=quantity,
            edit_unit=unit,
        )
    return None


_COMMENT_NOUN_RE = r"(?:комментар\w*|примечан\w*)"
_COMMENT_ACTION_RE = r"(?:добав\w*|внес\w*|запиш\w*|укаж\w*|измени\w*|поправ\w*)"
_COMMENT_REMOVE_ACTION_RE = r"(?:убер\w*|удал\w*|сотр\w*|очист\w*)"
_COMMENT_GLOBAL_SCOPE_RE = (
    r"(?:для\s+всех\s+(?:товар\w*|позици\w*)|"
    r"всем\s+(?:товар\w*|позици\w*)|"
    r"ко?\s+всем\s+(?:товар\w*|позици\w*)|"
    r"у\s+всех\s+(?:товар\w*|позици\w*)|"
    r"для\s+всей\s+(?:заявк\w*|заказ\w*)|"
    r"ко?\s+всей\s+(?:заявк\w*|заказ\w*))"
)
_COMMENT_WISH_RE = re.compile(
    r"\b(?:желательн\w*|нужн\w*|обязательн\w*|только|именно|"
    r"пожалуйста|просьб\w*|привез\w*|достав\w*|полож\w*|упаков\w*|"
    r"не\s+(?:замен\w*|смешива\w*|размораж\w*))\b",
    re.I,
)


def _build_edit_comment(
    target: str,
    comment: str,
    source: str,
    *,
    action: str = "add",
    scope: str = "item",
    require_wish: bool = True,
) -> ParsedCommand | None:
    """Создаёт команду изменения комментария только с явным товаром и пожеланием."""
    target = clean_command_target(target)
    comment = clean_text(comment).strip(" ,;:-—–.!?")
    if scope == "item" and not target:
        return None
    if action == "add" and (not comment or (require_wish and not _COMMENT_WISH_RE.search(comment))):
        return None
    if action == "remove":
        comment = ""
    if scope not in {"item", "order"} or action not in {"add", "remove"}:
        return None
    return ParsedCommand(
        intent=Intent.EDIT_COMMENT,
        text=source,
        comment_target_query=target,
        comment_text=comment,
        comment_action=action,
        comment_scope=scope,
    )


def _parse_edit_comment(text: str) -> ParsedCommand | None:
    """Распознаёт изменение комментария существующего товара без создания позиции."""
    source = clean_text(text)
    normalized = normalize_command_text(source)
    if not normalized:
        return None

    remove_action = rf"{_COMMENT_REMOVE_ACTION_RE}\s+"
    noun = rf"{_COMMENT_NOUN_RE}\s*"
    global_scope = rf"{_COMMENT_GLOBAL_SCOPE_RE}"

    # Явное удаление общего комментария не должно становиться товаром.
    if re.fullmatch(
        rf"(?:{remove_action}общ\w*\s+{noun}(?:{global_scope})?|"
        rf"{remove_action}{noun}{global_scope})",
        normalized,
        re.I,
    ):
        return _build_edit_comment("", "", source, action="remove", scope="order")

    # Явное удаление комментария конкретной позиции.
    remove_patterns = (
        re.compile(
            rf"^{remove_action}{noun}(?:у|к|для)\s+(?P<target>.+)$",
            re.I,
        ),
        re.compile(
            rf"^{remove_action}(?:у|к|для)\s+(?P<target>.+?)\s+{noun}$",
            re.I,
        ),
    )
    for pattern in remove_patterns:
        match = pattern.fullmatch(normalized)
        if match is not None:
            return _build_edit_comment(
                match.group("target"), "", source, action="remove", scope="item"
            )

    # Явный общий комментарий: только при словах общего охвата.
    global_patterns = (
        re.compile(
            rf"^(?:{_COMMENT_ACTION_RE}\s+)?{noun}{global_scope}\s+(?P<comment>.+)$",
            re.I,
        ),
        re.compile(
            rf"^{_COMMENT_ACTION_RE}\s+общ\w*\s+{noun}(?::\s*|\s+)(?P<comment>.+)$",
            re.I,
        ),
    )
    for pattern in global_patterns:
        match = pattern.fullmatch(normalized)
        if match is not None:
            return _build_edit_comment(
                "", match.group("comment"), source, scope="order", require_wish=False
            )

    # Голос часто опускает слово «комментарий»: явная конструкция «к/для
    # товара + пожелание» всё равно безопасна, потому что ищет только черновик.
    wish_match = _COMMENT_WISH_RE.search(normalized)
    if wish_match is not None and not re.search(_COMMENT_NOUN_RE, normalized, re.I):
        prefix = normalized[: wish_match.start()].strip(" ,;:-—–")
        comment = normalized[wish_match.start() :]
        match = re.match(
            rf"^(?:{_COMMENT_ACTION_RE}\s+)?(?:к|для)\s+(?P<target>.+)$",
            prefix,
            re.I,
        )
        if match is not None:
            return _build_edit_comment(match.group("target"), comment, source)
        return None
    if not re.search(_COMMENT_NOUN_RE, normalized, re.I):
        return None

    action = rf"{_COMMENT_ACTION_RE}\s+"
    patterns = (
        re.compile(
            rf"^{action}{noun}(?:к|для)\s+(?P<target>.+?)(?:\s*[,;:—–-]\s*|\s+)(?P<comment>.+)$",
            re.I,
        ),
        re.compile(
            rf"^{action}(?:к|для)\s+(?P<target>.+?)\s+{noun}(?:[:—–-]\s*|\s+)(?P<comment>.+)$",
            re.I,
        ),
        re.compile(
            rf"^(?:к|для)\s+(?P<target>.+?)\s+{noun}(?:[:—–-]\s*|\s+)(?P<comment>.+)$",
            re.I,
        ),
    )
    for pattern in patterns:
        match = pattern.fullmatch(normalized)
        if match is not None:
            result = _build_edit_comment(
                match.group("target"),
                match.group("comment"),
                source,
                require_wish=False,
            )
            if result is not None:
                return result
    # Явный маркер комментария без понятной цели — это не удаление товара и
    # не новая товарная позиция. Оставляем команду безопасно нераспознанной.
    return ParsedCommand(intent=Intent.UNKNOWN, text=source)


def is_product_add_request_phrase(text: str) -> bool:
    """Распознаёт команду отправки товара снабжению."""
    words = normalize_text(text).split()
    if not words:
        return False

    exact_short_commands = {
        "отправь",
        "отправить",
        "отправьте",
        "запрос",
        "запрос снабженцу",
    }
    normalized = " ".join(words)
    if normalized in exact_short_commands:
        return True

    has_action = any(
        word.startswith(("отправ", "переда", "созда", "оформ", "добав")) for word in words
    )
    has_procurement_target = any(
        word.startswith(("снабжен", "менеджер", "запрос", "заявк")) for word in words
    )
    has_product_target = any(word.startswith(("товар", "позици")) for word in words)
    return has_action and (has_procurement_target or has_product_target)


_EXPLICIT_ADD_ITEMS_RE = re.compile(
    r"^(?:(?:мне\s+нужно|мне\s+надо|я\s+хочу|хочу|давай(?:те)?|пожалуйста)\s+)?"
    r"(?:добав(?:ь|ить|им)|закаж(?:и|ем|ать)|полож(?:и|ить)|возьм(?:и|ем)|постав(?:ь|ить))\s+"
    r"(?P<target>.+)$",
    re.IGNORECASE,
)
_MIXED_ADD_ITEMS_RE = re.compile(
    r"^(?:да|нет)\s*(?:[,;:—–-]\s*)?"
    r"(?:добав(?:ь|ить|им)|закаж(?:и|ем|ать)|полож(?:и|ить)|"
    r"возьм(?:и|ем)|постав(?:ь|ить))\s+(?P<target>.+)$",
    re.IGNORECASE,
)
_AFFIRM_NEW_ORDER_RE = re.compile(
    r"^да\s*(?:[,;:—–-]\s*)?(?:начинай|начать|начн(?:ем|ём)|"
    r"сделай|создай|оформи)\s+(?:новую(?:\s+заявку)?|новый\s+заказ)$",
    re.IGNORECASE,
)
_NON_PRODUCT_ADD_TARGET_RE = re.compile(
    r"^(?:ещ[её]\s+)?(?:товар(?:ы|а|ов)?|позици(?:я|и|й)|продукт(?:ы|а|ов)?|"
    r"что(?:-нибудь|\s+нибудь)?)(?:\s+ещ[её])?$",
    re.IGNORECASE,
)

_STANDALONE_RETRY_RE = re.compile(
    r"(?:повтори|повторить|повтори отправку|повторить отправку|"
    r"отправь еще раз|отправь ещё раз|попробуй снова|отправляй)",
    re.IGNORECASE,
)


def retry_requested_for(text: str) -> bool:
    """Распознаёт только самостоятельную просьбу повторить отправку."""
    return bool(_STANDALONE_RETRY_RE.fullmatch(normalize_command_text(text)))


def has_explicit_add_items(text: str, items: Sequence[object] | None = None) -> bool:
    """Определяет явную команду добавления новой товарной позиции."""
    if items is not None:
        has_product = any(
            (
                item.get("product_query", "")
                if isinstance(item, dict)
                else getattr(item, "product_query", "")
            ).strip()
            for item in items
        )
        if not has_product:
            return False
    normalized = normalize_command_text(text)
    match = _EXPLICIT_ADD_ITEMS_RE.fullmatch(normalized)
    if match is None:
        return False
    target = clean_command_target(match.group("target"))
    if not target or _NON_PRODUCT_ADD_TARGET_RE.fullmatch(target):
        return False
    return not bool(re.fullmatch(r"(?:в|во)\s+(?:корзин\w*|заявк\w*)", target, re.IGNORECASE))


def _parse_mixed_add_items(text: str) -> ParsedCommand | None:
    """Сохраняет явное добавление после вводного да/нет-маркера."""
    match = _MIXED_ADD_ITEMS_RE.fullmatch(normalize_command_text(text))
    if match is None:
        return None
    target = clean_command_target(match.group("target"))
    if not target or _NON_PRODUCT_ADD_TARGET_RE.fullmatch(target):
        return None
    product_text, global_comment = _extract_global_comment(target)
    items = parse_product_lines(product_text)
    if not items:
        return None
    return ParsedCommand(
        intent=Intent.ADD_ITEMS,
        explicit_add_items=True,
        text=text,
        items=items,
        global_comment=global_comment,
    )


_QUANTITY_HINT_LEADINS = {
    "да",
    "давай",
    "ладно",
    "мне",
    "нужен",
    "нужна",
    "нужно",
    "ок",
    "окей",
    "поставь",
    "поставить",
    "пусть",
    "тогда",
    "хорошо",
    "возьми",
    "возьмем",
    "закажи",
    "заказать",
}


def _standalone_quantity_hint(text: str) -> tuple[float | None, str]:
    """Извлекает количество только из короткой standalone-фразы."""
    normalized = normalize_command_text(text)
    quantity, unit = parse_quantity_unit(normalized)
    if quantity is None:
        return None, ""
    tokens = normalized.replace(",", " ").split()
    allowed = set(UNIT_ALIASES) | set(NUMBER_WORDS) | _QUANTITY_HINT_LEADINS
    if not all(token in allowed or re.fullmatch(r"\d+(?:\.\d+)?", token) for token in tokens):
        return None, ""
    return quantity, normalize_unit(unit)


def _infer_intent(text: str, callback_data: str = "") -> ParsedCommand:
    """Определяет намерение пользователя."""
    if callback_data:
        return parse_callback(callback_data)

    command_match = re.fullmatch(r"/([a-z][a-z0-9_]*)(?:@[a-z0-9_]+)?", clean_text(text), re.I)
    if command_match:
        command_intents = {
            "start": Intent.GREETING,
            "help": Intent.HELP,
            "draft": Intent.SHOW_CART,
            "reset": Intent.CLEAR_CART,
            "submit": Intent.SUBMIT_REQUEST,
            "orders": Intent.ORDER_STATUS,
            "cancel": Intent.CANCEL,
            "back": Intent.BACK,
        }
        intent = command_intents.get(command_match.group(1).lower())
        if intent:
            return ParsedCommand(intent=intent, text=text)

    # Speech recognition commonly returns a trailing full stop.  Commands are
    # complete phrases, so terminal punctuation must not turn "Добавить еще
    # товары." into a product named "товары".
    normalized = normalize_command_text(text)
    if status_command := _parse_order_status_navigation(normalized, text):
        return status_command
    if _AFFIRM_NEW_ORDER_RE.fullmatch(normalized):
        return ParsedCommand(intent=Intent.CONFIRM, text=text)
    for intent, pattern in (*_COMMANDS, *_NATURAL_COMMANDS):
        if pattern.fullmatch(normalized):
            return ParsedCommand(intent=intent, text=text)

    if is_explicit_item_rejection(normalized):
        return ParsedCommand(intent=Intent.SKIP_CURRENT, text=text)

    if mixed_add := _parse_mixed_add_items(text):
        return mixed_add

    if negated_intent := _infer_negated_command(normalized):
        return ParsedCommand(intent=negated_intent, text=text)

    if edit_command := _parse_edit_quantity(normalized):
        return edit_command.model_copy(update={"text": text})

    if comment_command := _parse_edit_comment(text):
        return comment_command

    if match := _REMOVE_RE.match(normalized):
        target = clean_command_target(match.group(1))
        if target and _is_whole_draft_target(target):
            return ParsedCommand(intent=Intent.CLEAR_CART, text=text)
        if target:
            return ParsedCommand(
                intent=Intent.REMOVE_ITEM, text=text, target_query=target, target_queries=[target]
            )

    if free_form_intent := _infer_free_form_navigation(normalized):
        return ParsedCommand(intent=free_form_intent, text=text)

    if match := _SELECT_RE.match(normalized):
        return ParsedCommand(
            intent=Intent.SELECT_CANDIDATE, text=text, selected_index=int(match.group(1))
        )

    ordinal_patterns = {
        1: r"(?:1|один|перв(?:ый|ого|ая|ую|ое))",
        2: r"(?:2|два|втор(?:ой|ого|ая|ую|ое))",
        3: r"(?:3|три|трет(?:ий|ьего|ья|ью|ье))",
        4: r"(?:4|четыре|четверт(?:ый|ого|ая|ую|ое))",
        5: r"(?:5|пять|пят(?:ый|ого|ая|ую|ое))",
    }
    explicit_choice = bool(
        re.search(
            r"(?:^|\s)(?:вариант|выбери|выбрать|выбираю|беру|возьми|взять|"
            r"подходит|подойдёт|подойдет|нужен|нужна|номер)(?:\s|$)",
            normalized,
        )
    )
    polite_choice = bool(re.match(r"^(?:давай|мне|хочу|нужен|нужна|возьми)\s+", normalized))
    contains_measurement = any(word in UNIT_ALIASES for word in normalized.split())
    for index, token in ordinal_patterns.items():
        bare = re.fullmatch(token, normalized)
        mentioned = re.search(rf"(?:^|\s){token}(?:\s|$)", normalized)
        if mentioned and not contains_measurement and (bare or explicit_choice or polite_choice):
            return ParsedCommand(
                intent=Intent.SELECT_CANDIDATE,
                text=text,
                selected_index=index,
            )

    ordinal_text = normalized.removesuffix(" вариант").strip()
    ordinal_matches = {"первый": 1, "второй": 2, "третий": 3, "четвертый": 4, "пятый": 5}
    if ordinal_text in ordinal_matches:
        return ParsedCommand(
            intent=Intent.SELECT_CANDIDATE,
            text=text,
            selected_index=ordinal_matches[ordinal_text],
        )

    if re.fullmatch(
        r"(?:пропусти|не нужен|не добавляй)(?: эту позицию| этот товар| товар)?", normalized
    ):
        return ParsedCommand(intent=Intent.SKIP_CURRENT, text=text)
    if re.fullmatch(r"(?:ни один|ничего не подходит|нужного нет|поищи иначе)", normalized):
        return ParsedCommand(intent=Intent.MANUAL_CURRENT, text=text)
    if re.fullmatch(r"(?:продолжить|продолжай|дальше|поехали)", normalized):
        return ParsedCommand(intent=Intent.CONTINUE_CURRENT, text=text)
    if re.fullmatch(
        r"(?:отправь как есть|отправить как есть|отправь поставщику|отправить поставщику|отправь заказ поставщику|отправить заказ поставщику|передай поставщикам|передать поставщикам|не будем добирать|риск принимаю)",
        normalized,
    ):
        return ParsedCommand(intent=Intent.SUBMIT_AS_IS, text=text)

    product_text, global_comment = _extract_global_comment(text)
    items = parse_product_lines(product_text)
    if items:
        return ParsedCommand(
            intent=Intent.ADD_ITEMS,
            explicit_add_items=has_explicit_add_items(text, items),
            text=text,
            items=items,
            global_comment=global_comment,
        )
    return ParsedCommand(intent=Intent.UNKNOWN, text=text)


def dialogue_response_for(
    text: str,
    intent: Intent,
    items: Sequence[ExtractedItem] | None = None,
) -> DialogueResponse:
    """Определяет общий короткий ответ без привязки к modal state."""
    normalized = normalize_command_text(text)
    if normalized in {"ну", "не знаю", "может быть", "ладно"}:
        return DialogueResponse.UNCERTAIN
    if normalized in {"хватит", "достаточно"} or re.fullmatch(
        r"нет(?:\s+.*)?(?:не\s+надо|не\s+нужно|хватит|достаточно)",
        normalized,
    ):
        return DialogueResponse.DECLINE
    if intent is Intent.CONFIRM and (
        not normalized or not re.search(r"\b(?:отправ|переда|оформ)\w*\b", normalized)
    ):
        return DialogueResponse.AFFIRM
    if intent is Intent.CANCEL:
        return DialogueResponse.DECLINE
    if intent is Intent.ADD_MORE and not normalized:
        return DialogueResponse.AFFIRM
    if intent is Intent.ADD_MORE and re.fullmatch(
        r"(?:да\s+)?(?:давай\s+)?(?:добавим|добавить|добавь)\s+ещ[её]"
        r"(?:\s+(?:товар\w*|позици\w*|что[- ]?нибудь))?",
        normalized,
        flags=re.IGNORECASE,
    ):
        return DialogueResponse.AFFIRM
    if intent is not Intent.ADD_ITEMS or not normalized:
        return DialogueResponse.NONE

    # These phrases reach product parsing as pseudo-items in the deterministic
    # fallback. Keep their meaning as data for state policy instead of teaching
    # each modal handler to inspect the raw text.
    if re.fullmatch(
        r"(?:да\s+)?(?:давай\s+)?(?:добавим|добавить|добавь)\s+ещ[её]",
        normalized,
        flags=re.IGNORECASE,
    ) or re.fullmatch(r"давай\s+ещ[её]", normalized, flags=re.IGNORECASE):
        return DialogueResponse.AFFIRM
    return DialogueResponse.NONE


def infer_intent(text: str, callback_data: str = "") -> ParsedCommand:
    """Определяет intent и нормализует общий короткий ответ пользователя."""
    command = _infer_intent(text, callback_data)
    quantity_hint, quantity_hint_unit = _standalone_quantity_hint(text)
    return command.model_copy(
        update={
            "quantity_hint": quantity_hint,
            "quantity_hint_unit": quantity_hint_unit,
            "retry_requested": retry_requested_for(text),
            "dialogue_response": dialogue_response_for(
                text or command.text,
                command.intent,
                command.items,
            ),
        }
    )


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


def parse_callback(data: str) -> ParsedCommand:
    """Разбирает данные нажатой кнопки."""
    parts = data.split(":")
    if parts[0] == "v2":
        parts = parts[1:]
    revision = None
    if parts and re.fullmatch(r"r\d+", parts[-1], re.I):
        revision = int(parts.pop()[1:])
    action = parts[0] if parts else ""
    rest = parts[1:]
    mapping = {
        # The labels are intentionally not inferred from their names.  These
        # values are the callback contract of Engine v2.1 Prepare in n8n.
        # `cart` opens final checking, while `back` renders the draft.
        "cart": Intent.SHOW_FINAL_REVIEW,
        "new": Intent.START_NEW_ORDER,
        "submit": Intent.SUBMIT_AS_IS,
        "clear": Intent.CLEAR_CART,
        "cancel": Intent.CANCEL,
        "back": Intent.BACK,
        "skip": Intent.SKIP_CURRENT,
        "rename": Intent.MANUAL_CURRENT,
        "manual": Intent.MANUAL_CURRENT,
        "orders": Intent.ORDER_STATUS,
        "help": Intent.HELP,
        "check_min": Intent.CHECK_MIN_SUM,
        "resolve": Intent.CONTINUE_CURRENT,
        "final_review": Intent.SHOW_FINAL_REVIEW,
        "review": Intent.REVIEW_REFRESH,
        "review_submit": Intent.REVIEW_SUBMIT,
        "review_cancel": Intent.REVIEW_CANCEL,
        "add": Intent.ADD_MORE,
        "addreq": Intent.PRODUCT_ADD,
        "addreqlist": Intent.PRODUCT_ADD_LIST,
        "addreqretry": Intent.PRODUCT_ADD_RETRY,
        "addreqskip": Intent.PRODUCT_ADD_SKIP,
        "searchall": Intent.SEARCH_ALL_SUPPLIERS,
        "switchsupplier": Intent.SWITCH_SUPPLIER,
        "keepmul": Intent.KEEP_MULTIPLE,
        "keepwarn": Intent.KEEP_MULTIPLE,
        "mulone": Intent.FIX_MULTIPLE,
        "minsum": Intent.CHECK_MIN_SUM,
        "minsumadd": Intent.ADD_SUPPLIER_ITEMS,
        "minsumchoose": Intent.CHOOSE_SUPPLIER_WARNING,
        "fixmul": Intent.FIX_MULTIPLE,
        "editmul": Intent.EDIT_MULTIPLE,
        "unitedit": Intent.UNIT_EDIT,
        "unitok": Intent.UNIT_OK,
        "use_catalog_unit": Intent.USE_CATALOG_UNIT,
        "confirm": Intent.CONFIRM,
        "keep_current": Intent.KEEP_CURRENT_QUANTITY,
        "accept_multiple": Intent.ACCEPT_SUGGESTED_QUANTITY,
        "enter_quantity": Intent.ENTER_OTHER_QUANTITY,
        "dupmerge": Intent.MERGE_DUPLICATE,
    }
    if action in {"select", "sel"} and rest:
        try:
            selected = int(rest[-1])
            if action == "sel":
                selected += 1
            return ParsedCommand(
                intent=Intent.SELECT_CANDIDATE,
                selected_index=selected,
                callback_revision=revision,
                callback_target=rest[0],
            )
        except ValueError:
            return ParsedCommand(
                intent=Intent.SELECT_CANDIDATE,
                selection_query=rest[-1],
                callback_revision=revision,
                callback_target=rest[0],
            )
    if action == "remove" and rest:
        return ParsedCommand(
            intent=Intent.REMOVE_ITEM,
            target_query=rest[-1],
            callback_revision=revision,
            callback_target=rest[0],
        )
    if action == "qty" and len(rest) >= 2:
        try:
            quantity = float(rest[-1])
        except ValueError:
            quantity = None
        return ParsedCommand(
            intent=Intent.EDIT_QUANTITY,
            target_query=rest[0],
            edit_quantity=quantity,
            callback_revision=revision,
        )
    if action == "order" and rest:
        try:
            selected_index = int(rest[-1])
        except ValueError:
            selected_index = None
        return ParsedCommand(
            intent=Intent.ORDER_STATUS,
            text=data,
            selected_index=selected_index,
            selection_query="" if selected_index is not None else rest[-1],
            callback_revision=revision,
        )
    if action == "orderitems" and len(rest) >= 2:
        try:
            selected_index = int(rest[0])
        except ValueError:
            selected_index = None
        try:
            detail_page = max(0, int(rest[1]))
        except ValueError:
            detail_page = 0
        return ParsedCommand(
            intent=Intent.ORDER_STATUS,
            text=data,
            selected_index=selected_index,
            selection_query="" if selected_index is not None else rest[0],
            callback_target=f"detail:{detail_page}",
            order_status_detail_page=detail_page,
            callback_revision=revision,
        )
    if action == "orderspage" and rest:
        try:
            page = max(0, int(rest[-1]))
        except ValueError:
            page = 0
        return ParsedCommand(
            intent=Intent.ORDER_STATUS,
            text=data,
            callback_target=f"page:{page}",
            callback_revision=revision,
        )
    if action == "cartpage" and rest:
        try:
            page = max(0, int(rest[0]))
        except ValueError:
            page = 0
        return ParsedCommand(
            intent=Intent.SHOW_CART,
            text=data,
            callback_target=f"page:{page}",
            callback_revision=revision,
        )
    if action == "finalpage" and rest:
        try:
            page = max(0, int(rest[0]))
        except ValueError:
            page = 0
        return ParsedCommand(
            intent=Intent.SHOW_FINAL_REVIEW,
            text=data,
            callback_target=f"page:{page}",
            callback_revision=revision,
        )
    return ParsedCommand(
        intent=mapping.get(action, Intent.UNKNOWN),
        text=data,
        callback_revision=revision,
        callback_target=rest[0] if rest else "",
    )
