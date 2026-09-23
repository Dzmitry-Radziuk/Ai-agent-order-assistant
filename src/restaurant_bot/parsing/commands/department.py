"""Разбирает текстовые команды выбора подразделения заявки."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache

from restaurant_bot.domain.departments import DEPARTMENT_ALIASES, normalize_department
from restaurant_bot.domain.models import Intent, ParsedCommand
from restaurant_bot.domain.text import clean_text, normalize_text
from restaurant_bot.domain.units import UNIT_ALIASES
from restaurant_bot.parsing.commands.normalization import normalize_command_text
from restaurant_bot.parsing.products import parse_assigned_product, parse_product_lines

_DEPARTMENT_ALIASES_RE = "|".join(
    sorted((re.escape(alias) for alias in DEPARTMENT_ALIASES), key=len, reverse=True)
)
_DEPARTMENT_ACTION_RE = r"(?:выбер\w*|укаж\w*|назнач\w*|отнес\w*|добав\w*|постав\w*)"
_DEPARTMENT_ITEM_RE = r"(?:товар\w*|позици\w*|заказ\w*|заявк\w*|подразделени\w*|отдел\w*)"
_DEPARTMENT_FILLER_RE = r"(?:(?:пожалуйста|давай\w*|можно|нужно|надо)\s+)*"
_DEPARTMENT_SCOPE_RE = r"(?:подразделени\w*|отдел\w*|отделени\w*)"
_DEPARTMENT_PREPOSITION_RE = r"(?:на|в|во|для|к)"
_UNIT_ALIASES_RE = "|".join(
    sorted((re.escape(alias) for alias in UNIT_ALIASES), key=len, reverse=True)
)
_COMPACT_DEPARTMENT_RE = re.compile(
    rf"(?P<quantity>\d+(?:[,.]\d+)?\s*(?:{_UNIT_ALIASES_RE}))\s*"
    rf"(?P<preposition>{_DEPARTMENT_PREPOSITION_RE})\s*"
    rf"(?P<department>{_DEPARTMENT_ALIASES_RE})(?!\w)",
    flags=re.IGNORECASE,
)
_COMPACT_MEASURE_COUNT_DEPARTMENT_RE = re.compile(
    rf"(?P<measure>\d+(?:[,.]\d+)?\s*(?:{_UNIT_ALIASES_RE}))\s*"
    rf"(?P<quantity>\d+(?:[,.]\d+)?)\s*"
    rf"(?P<quantity_unit>(?:{_UNIT_ALIASES_RE}))?\s*"
    rf"(?P<preposition>{_DEPARTMENT_PREPOSITION_RE})?\s*"
    rf"(?P<department>{_DEPARTMENT_ALIASES_RE})(?!\w)",
    flags=re.IGNORECASE,
)
_COMPACT_PREFIX_DEPARTMENT_RE = re.compile(
    rf"^(?P<preposition>{_DEPARTMENT_PREPOSITION_RE})\s*"
    rf"(?P<scope>{_DEPARTMENT_SCOPE_RE})?\s*"
    rf"(?P<department>{_DEPARTMENT_ALIASES_RE})(?=(?-i:[A-ZА-ЯЁ]))",
    flags=re.IGNORECASE,
)
_COMPACT_SEPARATOR_PREFIX_DEPARTMENT_RE = re.compile(
    rf"(?P<separator>[,;])\s*(?P<preposition>{_DEPARTMENT_PREPOSITION_RE})\s*"
    rf"(?P<scope>{_DEPARTMENT_SCOPE_RE})?\s*"
    rf"(?P<department>{_DEPARTMENT_ALIASES_RE})(?=(?-i:[A-ZА-ЯЁ]))",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class InlineDepartment:
    """Хранит товарную часть строки и явно указанный в ней отдел."""

    product_text: str
    department: str
    has_scope_word: bool
    preposition: str


def extract_inline_department(text: str) -> InlineDepartment | None:
    """Извлекает один отдел из строки товара, не принимая признак товара за отдел."""
    departments = extract_inline_departments(text)
    return departments[0] if len(departments) == 1 else None


def extract_inline_departments(text: str) -> tuple[InlineDepartment, ...]:
    """Разделяет только доказанные товарные границы, сохраняя запятые в названии."""
    result: list[InlineDepartment] = []
    for raw_line in re.split(r"[\n;]+", text):
        phrase = _normalize_compact_department_markers(raw_line).strip(" .,;:!?-—–")
        phrase = re.sub(r"\s*,?\s+пожалуйста$", "", phrase, flags=re.IGNORECASE)
        if not phrase:
            continue
        parsed = _split_department_items(phrase)
        if not parsed:
            return ()
        result.extend(parsed)
    return tuple(result) if any(item.department for item in result) else ()


def _split_department_items(phrase: str) -> tuple[InlineDepartment, ...]:
    """Принимает разделитель только между самостоятельными товарными фрагментами."""
    # Десятичная запятая не разделитель; запятая внутри каталожного имени
    # допустима, пока фрагменты по сторонам не доказаны как самостоятельные позиции.
    separators = [
        (match.start(), match.end())
        for match in re.finditer(r"(?<!\d),|,(?!\d)|\s+(?:и|а также|а)\s+", phrase, re.I)
    ]
    # При пропущенной запятой отдел завершает позицию только тогда, когда
    # далее явно указан ещё один отдел для самостоятельной товарной части.
    department_matches = _department_matches(phrase)
    for match in department_matches[:-1]:
        if not (
            match.group("scope")
            or normalize_text(match.group("preposition")) in {"на", "в", "во", "к"}
        ):
            continue
        gap = re.match(r"\s+", phrase[match.end() :])
        if gap:
            separators.append((match.end(), match.end() + gap.end()))
    separators.sort()

    @lru_cache(maxsize=256)
    def parse_from(start: int, allow_bare_department: bool = False) -> tuple[InlineDepartment, ...]:
        """Проверяет оставшийся суффикс без повторного перебора уже разобранных границ."""
        for separator_start, separator_end in separators:
            if separator_start <= start:
                continue
            left = _extract_inline_department_clause(phrase[start:separator_start])
            if not left:
                continue
            # Префикс отдела не заканчивает позицию; границу подтверждает
            # самостоятельное количество вместо первой запятой внутри названия.
            if not _ends_assignment(phrase[start:separator_start]):
                items = parse_assigned_product(left[0].product_text)
                if len(items) != 1 or items[0].quantity is None:
                    continue
            explicit_left_department = bool(
                left[0].department
                and (left[0].has_scope_word or left[0].preposition in {"на", "в", "во", "к"})
            )
            right = parse_from(separator_end, explicit_left_department)
            if right:
                return (*left, *right)
        return _extract_inline_department_clause(
            phrase[start:], allow_bare_department=allow_bare_department
        )

    return parse_from(0)


def _department_matches(phrase: str) -> list[re.Match[str]]:
    """Находит отдел с необязательным предлогом и словом области."""
    return list(
        re.finditer(
            rf"(?<!\w)(?:(?P<preposition>{_DEPARTMENT_PREPOSITION_RE})\s+)?"
            rf"(?:(?P<scope>{_DEPARTMENT_SCOPE_RE})\s*[:\-]?\s+)?"
            rf"(?P<department>{_DEPARTMENT_ALIASES_RE})(?!\w)",
            phrase,
            re.I,
        )
    )


def confirm_inline_department(source_span: str, proposed_department: str) -> str:
    """Подтверждает предложенный отдел только явным маркером локальной фразы."""
    proposed = _canonical_department(proposed_department)
    matches = _department_matches(source_span)
    if not proposed or len(matches) != 1:
        return ""
    marker = matches[0]
    preposition = normalize_text(marker.group("preposition"))
    if not (marker.group("scope") or preposition in {"на", "в", "во", "к"}):
        return ""
    return proposed if _canonical_department(marker.group("department")) == proposed else ""


def _ends_assignment(phrase: str) -> bool:
    """Отличает конец назначения отдела от запятой в его товарной части."""
    matches = _department_matches(phrase)
    return bool(matches and not phrase[matches[-1].end() :].strip(" .,;:!?-—–"))


def _extract_inline_department_clause(
    phrase: str, *, allow_bare_department: bool = False
) -> tuple[InlineDepartment, ...]:
    """Разбирает один фрагмент с отделом до или после товарной части."""

    phrase = clean_text(phrase).strip(" .,;:!?-—–")
    matches = _department_matches(phrase)
    if not matches:
        items = parse_product_lines(phrase)
        if len(items) == 1 and items[0].quantity is not None:
            return (InlineDepartment(phrase, "", False, ""),)
        return ()
    if len(matches) != 1:
        return ()
    match = matches[0]
    before = phrase[: match.start()].strip(" .,;:!?-—–")
    after = phrase[match.end() :].strip(" .,;:!?-—–")
    scope_word = bool(match.group("scope"))
    preposition = normalize_text(match.group("preposition"))
    # «Для кухни» внутри названия не является назначением отдела даже
    # при последующем количестве. Явное «в отдел кухни» имеет другую семантику.
    if (
        before
        and preposition == "для"
        and not scope_word
        and (not _has_order_quantity(before) or after)
    ):
        return ()
    if (
        before
        and after
        and not re.fullmatch(rf"\d+(?:[,.]\d+)?\s*(?:{_UNIT_ALIASES_RE})?", after, re.I)
    ):
        return ()
    product_text = clean_text(f"{before} {after}")
    items = parse_assigned_product(product_text)
    if not items or (
        not allow_bare_department
        and not scope_word
        and not preposition
        and not _has_order_quantity(product_text)
    ):
        return ()
    department = _canonical_department(match.group("department"))
    if department is None:
        return ()
    return (InlineDepartment(product_text, department, scope_word, preposition),)


def _normalize_compact_department_markers(text: str) -> str:
    """Добавляет только структурные пробелы вокруг слитного количества и отдела."""
    phrase = clean_text(text)
    phrase = re.sub(
        rf"\b(?P<prefix>{_DEPARTMENT_PREPOSITION_RE}\s+(?:{_DEPARTMENT_SCOPE_RE}\s+)?)"
        r"(?P<word>[а-яё]{4,8})(?!\w)",
        _repair_explicit_department_typo,
        phrase,
        flags=re.I,
    )
    phrase = _COMPACT_MEASURE_COUNT_DEPARTMENT_RE.sub(
        _separate_measure_and_order_count,
        phrase,
    )
    phrase = _COMPACT_DEPARTMENT_RE.sub(
        r"\g<quantity> \g<preposition> \g<department>",
        phrase,
    )
    phrase = _COMPACT_PREFIX_DEPARTMENT_RE.sub(_expand_compact_department, phrase, count=1)
    return _COMPACT_SEPARATOR_PREFIX_DEPARTMENT_RE.sub(_expand_compact_department, phrase)


def _separate_measure_and_order_count(match: re.Match[str]) -> str:
    """Разделяет фасовочный размер, заказанное количество и название отдела."""
    parts = [
        match.group("measure"),
        match.group("quantity"),
        match.group("quantity_unit"),
        match.group("preposition"),
        match.group("department"),
    ]
    return " ".join(part for part in parts if part)


def _expand_compact_department(match: re.Match[str]) -> str:
    """Разворачивает слитный префикс отдела, сохраняя разделитель списка."""
    prefix = match.groupdict().get("separator") or ""
    parts = [match.group("preposition"), match.group("scope"), match.group("department")]
    return f"{prefix}{' '.join(part for part in parts if part)} "


def _has_order_quantity(text: str) -> bool:
    """Требует разобранное количество, а не любое число из каталожного имени."""
    return any(item.quantity is not None for item in parse_assigned_product(text))


def _repair_explicit_department_typo(match: re.Match[str]) -> str:
    """Исправляет одну опечатку только в явно обозначенном и однозначном отделе."""
    word = normalize_text(match.group("word"))
    if word in DEPARTMENT_ALIASES:
        return match.group(0)
    candidates = {
        department
        for alias, department in DEPARTMENT_ALIASES.items()
        if len(alias) >= 5
        and sum(
            max(end_a - start_a, end_b - start_b)
            for tag, start_a, end_a, start_b, end_b in SequenceMatcher(
                None, word, alias
            ).get_opcodes()
            if tag != "equal"
        )
        == 1
    }
    if len(candidates) == 1:
        return f"{match.group('prefix')}{candidates.pop()}"
    return match.group(0)


def parse_department_command(text: str) -> ParsedCommand | None:
    """Распознаёт выбор Зала, Бара или Кухни без обращения к каталогу."""
    phrase = normalize_command_text(text)
    if not phrase:
        return None

    department = _match_department(phrase)
    if department is None:
        return None
    callback_target = {"Зал": "hall", "Бар": "bar", "Кухня": "kitchen"}[department]
    return ParsedCommand(
        intent=Intent.SELECT_DEPARTMENT,
        text=text,
        callback_target=callback_target,
    )


def _match_department(phrase: str) -> str | None:
    """Извлекает подразделение из короткой команды без товарной части."""
    exact = _canonical_department(phrase)
    if exact:
        return exact

    department = rf"(?P<department>{_DEPARTMENT_ALIASES_RE})"
    scope = rf"(?:{_DEPARTMENT_SCOPE_RE}\s+)?"
    patterns = (
        rf"^{_DEPARTMENT_FILLER_RE}(?:на|в|во|для|к)\s+{scope}{department}$",
        rf"^{_DEPARTMENT_FILLER_RE}(?:подразделени\w*|отдел\w*|отделени\w*)"
        rf"\s*[:\-]?\s*{department}$",
        rf"^{_DEPARTMENT_FILLER_RE}(?:{_DEPARTMENT_ACTION_RE})"
        rf"(?:\s+{_DEPARTMENT_ITEM_RE})?\s+(?:на|в|во|для|к)\s+{scope}{department}$",
        rf"^{_DEPARTMENT_FILLER_RE}(?:{_DEPARTMENT_ACTION_RE})\s+"
        rf"(?:{_DEPARTMENT_SCOPE_RE}\s+)?{department}$",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, phrase, flags=re.IGNORECASE)
        if match:
            return _canonical_department(match.group("department"))
    return None


def _canonical_department(value: str) -> str | None:
    """Возвращает каноническое название подразделения для известного алиаса."""
    normalized = normalize_department(value)
    return normalized if normalized in {"Зал", "Бар", "Кухня"} else None
