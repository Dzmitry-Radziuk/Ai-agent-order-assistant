"""Проверяет естественные вопросы и безопасное чтение истории заявок."""

from __future__ import annotations

from datetime import date
from unittest.mock import Mock

from restaurant_bot.application.history.query_service import HistoryQueryService
from restaurant_bot.domain.history import (
    HistoryAnswerKind,
    HistoryDeliveryDateRelation,
    HistoryQuestionType,
    HistoryRow,
    HistoryStatusClass,
)
from restaurant_bot.history.product_list import parse_history_product_list
from restaurant_bot.history.status_policy import classify_status
from restaurant_bot.parsing.history import parse_history_query
from restaurant_bot.parsing.history.normalization import history_stems
from restaurant_bot.presentation.telegram.history import history_reply
from restaurant_bot.repositories.history import GoogleHistoryRepository


def test_delivery_phrases_have_one_structured_meaning() -> None:
    """Сводит разные вопросы о сроке к одному типу HistoryQuery."""
    phrases = (
        "Когда приедет говядина?",
        "Когда будет говядина?",
        "Когда привезут говядину?",
        "Когда поставка говядины?",
        "На когда говядина?",
        "Когда ждать говядину?",
        "По говядине когда поставка?",
    )

    queries = [parse_history_query(phrase) for phrase in phrases]

    assert all(query is not None for query in queries)
    assert {query.question_type for query in queries if query} == {
        HistoryQuestionType.DELIVERY_DATE
    }
    assert {tuple(history_stems(query.product_queries[0])) for query in queries if query} == {
        ("говядин",)
    }


def test_today_and_status_phrases_do_not_become_add_items() -> None:
    """Распознаёт вопросы на дату и общий статус без товарной команды."""
    today = parse_history_query("Мне сегодня ждать говядину?")
    status = parse_history_query("Что там с моей говядиной?")

    assert today is not None
    assert today.question_type is HistoryQuestionType.DELIVERY_ON_DATE
    assert today.product_queries == ["говядину"]
    assert status is not None
    assert status.question_type is HistoryQuestionType.CURRENT_STATUS


def test_arrival_cancellation_and_past_questions_have_distinct_types() -> None:
    """Различает доставку, отмену и поиск последней завершённой поставки."""
    arrival = parse_history_query("Говядину уже привезли?")
    cancelled = parse_history_query("Поставка говядины ещё в силе?")
    past = parse_history_query("Когда последний раз приезжала говядина?")

    assert arrival is not None
    assert arrival.question_type is HistoryQuestionType.ARRIVAL_STATUS
    assert cancelled is not None
    assert cancelled.question_type is HistoryQuestionType.CURRENT_STATUS
    assert past is not None
    assert past.question_type is HistoryQuestionType.PAST_DELIVERY


def test_add_command_is_not_reclassified_as_history() -> None:
    """Оставляет обычное добавление товара в существующем маршруте."""
    assert parse_history_query("добавь говядину сегодня") is None


def test_generic_question_without_delivery_signal_is_not_history() -> None:
    """Не направляет разговорный вопрос в чтение истории без сигнала поставки."""
    assert parse_history_query("Почему небо голубое?") is None


def test_venue_delivery_phrases_have_no_product_query() -> None:
    """Распознаёт общие вопросы о поставках без выдуманного товара."""
    phrases = (
        "Доставка вообще сегодня будет?",
        "Что по поставкам?",
        "Сегодня что-нибудь привезут?",
        "Есть сегодня поставки?",
        "Будет сегодня доставка?",
        "Что у нас сегодня по поставкам?",
        "Сегодня вообще что-нибудь ожидается?",
    )

    queries = [parse_history_query(phrase, today=date(2026, 8, 16)) for phrase in phrases]

    assert all(query is not None for query in queries)
    assert all(query.question_type is HistoryQuestionType.VENUE_DELIVERIES for query in queries)
    assert all(query.product_queries == [] for query in queries)
    assert [query.date_reference.value for query in queries] == [
        "today",
        "none",
        "today",
        "today",
        "today",
        "today",
        "today",
    ]


def test_actor_shaped_venue_question_does_not_create_product() -> None:
    """Не принимает имя человека за товар или поставщика."""
    query = parse_history_query("Женя сегодня чего-нибудь привезет?")

    assert query is not None
    assert query.question_type is HistoryQuestionType.VENUE_DELIVERIES
    assert query.product_queries == []
    assert query.actor_specific is True


def test_product_named_like_person_stays_product_scoped() -> None:
    """Сохраняет товар «Иван-чай» в обычном вопросе истории."""
    query = parse_history_query("Иван-чай сегодня приедет?")

    assert query is not None
    assert query.question_type is not HistoryQuestionType.VENUE_DELIVERIES
    assert query.product_queries == ["иван чай"]


def test_venue_today_uses_exact_delivery_date_only() -> None:
    """Показывает на сегодня только строки с точной датой поставки."""
    rows = [
        _row(
            "Говядина",
            delivery_date=date(2026, 8, 16),
            stage="Заявка подтверждена поставщиком",
            supplier="Раджабов",
        ),
        _row("Вино", delivery_date=date(2026, 8, 17), stage="Подтверждена"),
        _row("Хлеб", delivery_date=None, stage="В пути"),
    ]
    query = parse_history_query("Доставка вообще сегодня будет?", today=date(2026, 8, 16))

    assert query is not None
    answer = HistoryQueryService(_Reader(rows), today=date(2026, 8, 16)).execute(
        query,
        spreadsheet_id="sheet",
        venue_name="Кафе",
    )

    assert answer.kind is HistoryAnswerKind.RESULTS
    assert [match.entry.product_name for match in answer.matches] == ["Говядина"]
    assert answer.active_without_delivery_date_count == 1


def test_venue_general_question_lists_active_rows_without_matching() -> None:
    """Возвращает общий список поставок без запуска товарного matching."""
    rows = [
        _row("Говядина", delivery_date=date(2026, 8, 16)),
        _row("Вино", delivery_date=date(2026, 8, 17)),
        _row("Хлеб", delivery_date=None),
        _row("Старый товар", stage="Завершена", delivery_date=date(2026, 8, 1)),
    ]
    query = parse_history_query("Что по поставкам?")

    assert query is not None
    answer = HistoryQueryService(_Reader(rows), today=date(2026, 8, 16)).execute(
        query,
        spreadsheet_id="sheet",
        venue_name="Кафе",
    )

    assert answer.kind is HistoryAnswerKind.RESULTS
    assert [match.entry.product_name for match in answer.matches] == [
        "Говядина",
        "Вино",
        "Хлеб",
    ]
    rendered = history_reply(answer).text
    assert "<u>📦 Актуальные поставки</u>" in rendered
    assert "Старый товар" not in rendered


def test_venue_actor_reply_keeps_delivery_person_unproven() -> None:
    """Показывает поставки, не приписывая доставку названному человеку."""
    row = _row("Говядина", delivery_date=date(2026, 8, 16))
    query = parse_history_query("Женя сегодня чего-нибудь привезет?")

    assert query is not None
    answer = HistoryQueryService(_Reader([row]), today=date(2026, 8, 16)).execute(
        query,
        spreadsheet_id="sheet",
        venue_name="Кафе",
    )

    rendered = history_reply(answer).text
    assert "кто именно их привезёт" in rendered
    assert "Женя привезёт" not in rendered
    assert "Говядина" in rendered


def test_venue_today_without_exact_date_does_not_make_absolute_claim() -> None:
    """Различает отсутствие точной даты и доказанное отсутствие поставки."""
    rows = [
        _row("Вино", delivery_date=date(2026, 8, 17)),
        _row("Хлеб", delivery_date=None),
    ]
    query = parse_history_query("Есть сегодня поставки?", today=date(2026, 8, 16))

    assert query is not None
    answer = HistoryQueryService(_Reader(rows), today=date(2026, 8, 16)).execute(
        query,
        spreadsheet_id="sheet",
        venue_name="Кафе",
    )

    rendered = history_reply(answer).text
    assert answer.kind is HistoryAnswerKind.NO_ACTIVE
    assert "с указанной датой не найдено" in rendered
    assert "активные заявки без указанной даты" in rendered
    assert "поставки не будет" not in rendered


def test_yearless_explicit_date_uses_injected_business_clock() -> None:
    """Разбирает дату без года относительно переданной даты приложения."""
    query = parse_history_query(
        "Говядина на 18.08?",
        today=date(2026, 8, 14),
    )

    assert query is not None
    assert query.explicit_date == date(2026, 8, 18)


def test_morphology_and_reordered_terms_match_same_product() -> None:
    """Поддерживает падежи и перестановку слов в названии товара."""
    row = _row(
        "Говядина Толстый край охлажденная",
        stage="Отправлено поставщику",
    )
    reader = _Reader([row])
    service = HistoryQueryService(reader, today=date(2026, 8, 14))

    for phrase in ("говядина", "говядину", "говядины", "говядиной", "по говядине"):
        query = parse_history_query(f"Когда приедет {phrase}?")
        assert query is not None
        answer = service.execute(query, spreadsheet_id="sheet", venue_name="Кафе")
        assert answer.kind is HistoryAnswerKind.RESULTS
        assert answer.matches[0].entry.product_name.startswith("Говядина")

    query = parse_history_query("Когда приедет по толстому краю?")
    assert query is not None
    answer = service.execute(query, spreadsheet_id="sheet", venue_name="Кафе")
    assert answer.kind is HistoryAnswerKind.RESULTS


def test_ambiguous_category_does_not_choose_one_history_row() -> None:
    """Возвращает уточнение для двух близких актуальных вариантов."""
    rows = [
        _row("Говядина Толстый край", stage="Отправлено поставщику"),
        _row("Говядина Тонкий край", stage="Подтверждена"),
    ]
    query = parse_history_query("Когда приедет говядина?")
    assert query is not None
    answer = HistoryQueryService(_Reader(rows)).execute(
        query,
        spreadsheet_id="sheet",
        venue_name="Кафе",
    )

    assert answer.kind is HistoryAnswerKind.AMBIGUOUS
    assert len(answer.matches) == 2
    assert "Уточните" in history_reply(answer).text


def test_completed_rows_do_not_pollute_active_question() -> None:
    """Скрывает старую завершённую строку при наличии актуальной поставки."""
    rows = [
        _row("Говядина Толстый край", stage="Завершена", delivery_date=date(2026, 7, 1)),
        _row("Говядина Толстый край", stage="В пути", delivery_date=date(2026, 8, 15)),
    ]
    query = parse_history_query("Когда приедет говядина?")
    assert query is not None
    answer = HistoryQueryService(_Reader(rows)).execute(
        query,
        spreadsheet_id="sheet",
        venue_name="Кафе",
    )

    assert answer.kind is HistoryAnswerKind.RESULTS
    assert [match.entry.stage for match in answer.matches] == ["В пути"]


def test_arrival_question_keeps_delivered_evidence() -> None:
    """Показывает фактическую доставленную строку для вопроса «уже привезли»."""
    query = parse_history_query("Говядину уже привезли?")
    assert query is not None
    answer = HistoryQueryService(
        _Reader([_row("Говядина Толстый край", stage="Доставлено")])
    ).execute(query, spreadsheet_id="sheet", venue_name="Кафе")

    assert answer.kind is HistoryAnswerKind.RESULTS
    assert answer.matches[0].entry.stage == "Доставлено"


def test_upcoming_question_excludes_old_delivered_row() -> None:
    """Оставляет только актуальную поставку в вопросе о будущей доставке."""
    rows = [
        _row("Говядина Толстый край", stage="Доставлено", delivery_date=date(2026, 7, 1)),
        _row("Говядина Толстый край", stage="В пути", delivery_date=date(2026, 8, 15)),
    ]
    query = parse_history_query("Когда приедет говядина?")
    assert query is not None
    answer = HistoryQueryService(_Reader(rows)).execute(
        query,
        spreadsheet_id="sheet",
        venue_name="Кафе",
    )

    assert answer.kind is HistoryAnswerKind.RESULTS
    assert [match.entry.stage for match in answer.matches] == ["В пути"]


def test_delivered_stage_has_separate_semantic_class() -> None:
    """Отличает подтверждённую доставку от общей завершённой стадии."""
    assert classify_status("Доставлено") is HistoryStatusClass.DELIVERED
    assert classify_status("Завершена") is HistoryStatusClass.COMPLETED


def test_current_status_keeps_cancelled_evidence() -> None:
    """Сохраняет отменённую поставку для ответа о текущем статусе."""
    query = parse_history_query("Поставка по говядине в силе?")
    assert query is not None
    answer = HistoryQueryService(
        _Reader([_row("Говядина Толстый край", stage="Отменена")])
    ).execute(query, spreadsheet_id="sheet", venue_name="Кафе")

    assert answer.kind is HistoryAnswerKind.RESULTS
    assert answer.matches[0].entry.stage == "Отменена"


def test_only_completed_rows_return_no_active_delivery() -> None:
    """Не показывает старую поставку как текущую, если активных строк нет."""
    query = parse_history_query("Сегодня приедет говядина?")
    assert query is not None
    answer = HistoryQueryService(
        _Reader([_row("Говядина Толстый край", stage="Завершена")])
    ).execute(query, spreadsheet_id="sheet", venue_name="Кафе")

    assert answer.kind is HistoryAnswerKind.NO_ACTIVE
    assert "Активных поставок" in history_reply(answer).text


def test_past_query_can_return_completed_row() -> None:
    """Разрешает завершённую строку для вопроса о последней поставке."""
    query = parse_history_query("Когда последний раз приезжала говядина?")
    assert query is not None
    answer = HistoryQueryService(
        _Reader([_row("Говядина Толстый край", stage="Завершена")])
    ).execute(query, spreadsheet_id="sheet", venue_name="Кафе")

    assert answer.kind is HistoryAnswerKind.RESULTS


def test_unknown_status_is_preserved_and_not_guessed() -> None:
    """Оставляет незнакомую стадию неизвестной и показывает её как есть."""
    assert classify_status("Новая внутренняя стадия") is HistoryStatusClass.UNKNOWN
    query = parse_history_query("Что с говядиной?")
    assert query is not None
    answer = HistoryQueryService(
        _Reader([_row("Говядина Толстый край", stage="Новая внутренняя стадия")])
    ).execute(query, spreadsheet_id="sheet", venue_name="Кафе")

    assert answer.matches[0].entry.stage == "Новая внутренняя стадия"


def test_product_list_keeps_same_row_evidence() -> None:
    """Разворачивает несколько товаров, не теряя поставщика и дату строки."""
    row = _row(
        "Кухня:\n1. Хлеб Бородинский — 2 шт\n2. Сыр — 1 кг",
        supplier="Поставщик",
    )

    entries = parse_history_product_list(row)

    assert [entry.product_name for entry in entries] == ["Хлеб Бородинский", "Сыр"]
    assert all(entry.supplier == "Поставщик" for entry in entries)
    assert all(entry.row.order_number == "A-1" for entry in entries)


def test_product_list_strips_live_quantity_and_price_tail_only_when_structured() -> None:
    """Отделяет подтверждённый live-хвост количества и цены от названия товара."""
    row = _row(
        "\n".join(
            [
                "Вино белое Д/Я КУХНИ - 5 шт - 1037 руб.",
                "Коньяк Кухня - 2 шт - 702 руб.",
                "Говядина Кости, суставы нарезанные (Сахарные) - 10 кг - 700 руб.",
                "Соус - острый",
            ]
        )
    )

    entries = parse_history_product_list(row)

    assert [entry.product_name for entry in entries] == [
        "Вино белое Д/Я КУХНИ",
        "Коньяк Кухня",
        "Говядина Кости, суставы нарезанные (Сахарные)",
        "Соус - острый",
    ]


def test_history_status_format_and_source_are_separate() -> None:
    """Скрывает технический префикс статуса, сохраняя исходную строку истории."""
    row = _row(
        "Вино белое Д/Я КУХНИ",
        stage="Вручную | Заявка подтверждена поставщиком.",
        delivery_date=date(2026, 7, 30),
    )
    query = parse_history_query("Когда приедет вино?")
    assert query is not None
    answer = HistoryQueryService(_Reader([row]), today=date(2026, 8, 14)).execute(
        query,
        spreadsheet_id="sheet",
        venue_name="Кафе",
    )

    assert answer.matches[0].entry.stage == "Вручную | Заявка подтверждена поставщиком."
    assert answer.matches[0].delivery_date_relation is HistoryDeliveryDateRelation.PAST
    rendered = history_reply(answer).text
    assert "Статус: <b>Заявка подтверждена поставщиком</b>." in rendered
    assert "Вручную |" not in rendered


def test_stale_delivery_date_is_not_presented_as_delivery() -> None:
    """Показывает прошедшую плановую дату без вывода о фактической доставке."""
    row = _row(
        "Вино белое Д/Я КУХНИ",
        stage="Вручную | Заявка подтверждена поставщиком.",
        delivery_date=date(2026, 7, 30),
    )
    query = parse_history_query("Когда приедет вино?")
    assert query is not None
    rendered = history_reply(
        HistoryQueryService(_Reader([row]), today=date(2026, 8, 14)).execute(
            query,
            spreadsheet_id="sheet",
            venue_name="Кафе",
        )
    ).text

    assert "В истории указана дата поставки: <b>30 июля 2026</b> — она уже прошла." in rendered
    assert "Новая дата поставки не указана." in rendered
    assert "доставлено" not in rendered.lower()
    assert "уже приехало" not in rendered.lower()


def test_today_delivery_question_explains_stale_date() -> None:
    """Не обещает поставку сегодня, если в истории осталась старая дата."""
    row = _row(
        "Вино белое Д/Я КУХНИ",
        stage="Вручную | Заявка подтверждена поставщиком.",
        delivery_date=date(2026, 7, 30),
    )
    query = parse_history_query("Сегодня приедет вино?")
    assert query is not None
    rendered = history_reply(
        HistoryQueryService(_Reader([row]), today=date(2026, 8, 14)).execute(
            query,
            spreadsheet_id="sheet",
            venue_name="Кафе",
        )
    ).text

    assert "На эту дату поставка в истории не указана." in rendered
    assert "Последняя указанная дата: <b>30 июля 2026</b> — она уже прошла." in rendered
    assert "сегодня приедет" not in rendered.lower()


def test_arrival_question_does_not_infer_delivery_from_stale_date() -> None:
    """Не считает товар доставленным только по прошедшей плановой дате."""
    row = _row(
        "Вино белое Д/Я КУХНИ",
        stage="Вручную | Заявка подтверждена поставщиком.",
        delivery_date=date(2026, 7, 30),
    )
    query = parse_history_query("Вино уже приехало?")
    assert query is not None
    rendered = history_reply(
        HistoryQueryService(_Reader([row]), today=date(2026, 8, 14)).execute(
            query,
            spreadsheet_id="sheet",
            venue_name="Кафе",
        )
    ).text

    assert "Статус: <b>Заявка подтверждена поставщиком</b>." in rendered
    assert "доставлено" not in rendered.lower()
    assert "приехало" not in rendered.lower()


def test_history_answer_starts_with_product_without_generic_heading() -> None:
    """Начинает обычный ответ с товара и не показывает технический заголовок."""
    row = _row(
        "Говядина Кости, суставы нарезанные (Сахарные)",
        stage="Вручную | Заявка подтверждена поставщиком.",
        supplier="Раджабов",
        delivery_date=None,
    )
    query = parse_history_query("Что там по говядине?")
    assert query is not None
    rendered = history_reply(
        HistoryQueryService(_Reader([row]), today=date(2026, 8, 14)).execute(
            query,
            spreadsheet_id="sheet",
            venue_name="Кафе",
        )
    ).text

    assert rendered.startswith("<b>Говядина Кости, суставы нарезанные (Сахарные)</b>")
    assert "Проверка истории" not in rendered
    assert "Поставщик: <b>Раджабов</b>" in rendered


def test_google_history_repository_reads_only_history_and_isolates_venue() -> None:
    """Проверяет единственный лист-источник и tenant isolation."""
    sheets = Mock()
    sheets.read_rows.return_value = [
        {
            "Номер заявки": "A-1",
            "Условное наз-ие заведения": "Кафе A",
            "Стадия": "В пути",
            "Список товаров": "Говядина",
        },
        {
            "Номер заявки": "B-1",
            "Условное наз-ие заведения": "Кафе B",
            "Стадия": "В пути",
            "Список товаров": "Говядина",
        },
    ]

    rows = GoogleHistoryRepository(sheets).read_history("sheet", "Кафе A")

    sheets.read_rows.assert_called_once_with("История", "sheet")
    assert [row.order_number for row in rows] == ["A-1"]


def _row(
    product_list: str,
    *,
    stage: str = "В пути",
    delivery_date: date | None = date(2026, 8, 15),
    supplier: str = "Поставщик",
) -> HistoryRow:
    """Создаёт изолированную строку истории для теста."""
    return HistoryRow(
        order_number="A-1",
        venue_name="Кафе",
        supplier=supplier,
        stage=stage,
        delivery_date=delivery_date,
        product_list=product_list,
    )


class _Reader:
    """Предоставляет фиксированные строки без Google API."""

    def __init__(self, rows: list[HistoryRow]) -> None:
        """Сохраняет строки для одного вызова use case."""
        self.rows = rows
        self.calls = 0

    def read_history(self, spreadsheet_id: str, venue_name: str = "") -> list[HistoryRow]:
        """Возвращает подготовленные строки истории."""
        del spreadsheet_id, venue_name
        self.calls += 1
        return self.rows
