"""Проверяет детерминированную авторизацию строк и идентичность фото-каталога."""

from pathlib import Path
from types import SimpleNamespace

from restaurant_bot.application.conversation.contracts import ConversationInput
from restaurant_bot.domain.models import (
    CartItem,
    CatalogProduct,
    ConversationState,
    DepartmentQuantities,
    ExtractedItem,
    InputKind,
    Intent,
    ItemStatus,
    ParsedCommand,
    TelegramEvent,
)
from restaurant_bot.input.photo_ingestion import (
    canonical_photo_identity,
    classify_photo_document,
    normalize_photo_observation,
)
from restaurant_bot.integrations.openai_client import OpenAIService
from restaurant_bot.observability import Tracer
from restaurant_bot.parsing.ai.schemas import PhotoDocumentObservation, PhotoRowObservation
from restaurant_bot.services.engine import ConversationEngine


def _row(product: str, **kwargs: object) -> PhotoRowObservation:
    """Создаёт наблюдение одной строки для детерминированного теста."""
    return PhotoRowObservation(product_text=product, row_text=product, **kwargs)


def _photo_event() -> TelegramEvent:
    """Создаёт фото-событие для convergence-теста ConversationEngine."""
    return TelegramEvent(
        update_id=9001,
        chat_id="photo-test",
        input_type=InputKind.PHOTO,
        file_id="photo",
        mime_type="image/jpeg",
    )


def test_photo_uses_observation_schema_without_cart_item_shape() -> None:
    """Фиксирует отдельный structured-output контракт для vision фотографии."""
    schema = PhotoDocumentObservation.model_json_schema()

    assert "rows" in schema["properties"]
    assert "product_text" in schema["$defs"]["PhotoRowObservation"]["properties"]
    assert "visible_product_row_count" in schema["properties"]
    assert "order_area_complete" in schema["properties"]
    assert "uncertain_order_row_count" in schema["properties"]
    assert "scan_complete" in schema["properties"]
    assert "sheet_row_number" in schema["$defs"]["PhotoRowObservation"]["properties"]
    assert "items" not in schema["properties"]


class _VisionResponses:
    """Возвращает один структурированный ответ vision без второго запроса."""

    def __init__(
        self,
        observation: PhotoDocumentObservation,
        *,
        status: str = "",
        incomplete_details: object | None = None,
    ) -> None:
        """Сохраняет observation и считает вызовы vision endpoint."""
        self.observation = observation
        self.status = status
        self.incomplete_details = incomplete_details
        self.calls: list[dict[str, object]] = []

    def parse(self, **kwargs: object) -> SimpleNamespace:
        """Имитирует один вызов structured vision response."""
        self.calls.append(kwargs)
        return SimpleNamespace(
            output_parsed=self.observation,
            status=self.status,
            incomplete_details=self.incomplete_details,
        )


def test_photo_classification_uses_department_structure_over_model_proposal() -> None:
    """Классифицирует таблицу по колонкам даже при ошибочном proposal модели."""
    observation = PhotoDocumentObservation(
        document_type_proposal="order_table",
        detected_columns=["Товар", "Зал", "Бар", "Кухня"],
        has_table_structure=True,
        rows=[_row("Васаби", kitchen_quantity=3)],
    )

    assert classify_photo_document(observation) == "client_order_sheet"


def test_empty_client_sheet_with_unreadable_order_area_is_incomplete(settings) -> None:  # type: ignore[no-untyped-def]
    """Сохраняет неполный результат vision как нечитаемую область заказа."""
    result = normalize_photo_observation(
        PhotoDocumentObservation(
            document_type_proposal="client_order_sheet",
            detected_columns=["Товар", "Зал", "Бар", "Кухня"],
            has_table_structure=True,
            visible_product_row_count=12,
            order_area_complete=False,
            rows=[],
        ),
        settings,
    )

    assert result.command.photo_outcome == "incomplete_photo_read"
    assert result.document_type == "incomplete"
    assert result.reason == "potential_order_row_unreadable"


def test_client_sheet_drops_blank_row_without_transferring_neighbor_quantity(settings) -> None:  # type: ignore[no-untyped-def]
    """Не переносит quantity из следующей строки в пустую строку таблицы."""
    result = normalize_photo_observation(
        PhotoDocumentObservation(
            document_type_proposal="order_table",
            detected_columns=["Товар", "Зал", "Бар", "Кухня"],
            has_table_structure=True,
            rows=[
                _row("Тестовый товар"),
                _row("Васаби TM Sango, 1кг, 10 шт/кор, Китай", kitchen_quantity=3),
                _row("Лук жареный Metro Chef, 600 г", kitchen_quantity=10),
            ],
        ),
        settings,
    )

    assert [item.product_query for item in result.command.items] == [
        "Васаби TM Sango, 1кг, 10 шт/кор, Китай",
        "Лук жареный Metro Chef, 600 г",
    ]
    assert [item.quantity for item in result.command.items] == [3, 10]


def test_client_sheet_generic_quantity_without_department_is_incomplete_photo(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    """Не скрывает quantity без department под сообщением об отсутствии количеств."""
    result = normalize_photo_observation(
        PhotoDocumentObservation(
            document_type_proposal="client_order_sheet",
            detected_columns=["Товар", "Зал", "Бар", "Кухня"],
            has_table_structure=True,
            rows=[_row("Васаби", explicit_order_quantity=3, order_entry_text="3")],
        ),
        settings,
    )

    assert result.command.photo_outcome == "incomplete_photo_read"
    assert result.document_type == "incomplete"
    assert result.reason == "client_sheet_quantity_without_department"


def test_row_count_mismatch_keeps_reliable_filled_rows(settings) -> None:  # type: ignore[no-untyped-def]
    """Не отклоняет заполненные строки из-за пропущенной пустой строки каталога."""
    result = normalize_photo_observation(
        PhotoDocumentObservation(
            document_type_proposal="client_order_sheet",
            detected_columns=["Товар", "Зал", "Бар", "Кухня"],
            has_table_structure=True,
            visible_product_row_count=20,
            scan_complete=True,
            order_area_complete=True,
            rows=[
                _row("Васаби", kitchen_quantity=3),
                _row("Лук", kitchen_quantity=10),
                _row("Хрен", kitchen_quantity=2),
                *[_row("") for _ in range(14)],
            ],
        ),
        settings,
    )

    assert [item.quantity for item in result.command.items] == [3, 10, 2]
    assert result.command.photo_outcome == ""
    assert result.reason == "rows_authorized"


def test_scan_complete_false_does_not_override_complete_order_area(settings) -> None:  # type: ignore[no-untyped-def]
    """Не отклоняет заказ, если неполна транскрипция, но order-area evidence полна."""
    result = normalize_photo_observation(
        PhotoDocumentObservation(
            document_type_proposal="client_order_sheet",
            detected_columns=["Товар", "Зал", "Бар", "Кухня"],
            has_table_structure=True,
            scan_complete=False,
            order_area_complete=True,
            scan_warning="неразборчива справочная колонка",
            rows=[_row("Васаби", kitchen_quantity=3)],
        ),
        settings,
    )

    assert result.command.photo_outcome == ""
    assert [item.quantity for item in result.command.items] == [3]


def test_uncertain_order_row_is_incomplete_photo(settings) -> None:  # type: ignore[no-untyped-def]
    """Отклоняет фото, если vision сообщает о потенциально пропущенной строке заказа."""
    result = normalize_photo_observation(
        PhotoDocumentObservation(
            document_type_proposal="client_order_sheet",
            detected_columns=["Товар", "Зал", "Бар", "Кухня"],
            has_table_structure=True,
            order_area_complete=True,
            uncertain_order_row_count=1,
            rows=[_row("Васаби", kitchen_quantity=3)],
        ),
        settings,
    )

    assert result.command.photo_outcome == "incomplete_photo_read"
    assert result.reason == "uncertain_potential_order_row"


def test_cropped_order_area_is_incomplete_photo(settings) -> None:  # type: ignore[no-untyped-def]
    """Отклоняет фото при явном сообщении о нечитабельной order-area области."""
    result = normalize_photo_observation(
        PhotoDocumentObservation(
            document_type_proposal="client_order_sheet",
            detected_columns=["Товар", "Зал", "Бар", "Кухня"],
            has_table_structure=True,
            order_area_complete=False,
            rows=[_row("Васаби", kitchen_quantity=3)],
        ),
        settings,
    )

    assert result.command.photo_outcome == "incomplete_photo_read"
    assert result.reason == "potential_order_row_unreadable"


def test_empty_complete_order_area_keeps_no_quantity_outcome(settings) -> None:  # type: ignore[no-untyped-def]
    """Показывает отсутствие количеств для полностью просмотренной пустой таблицы."""
    result = normalize_photo_observation(
        PhotoDocumentObservation(
            document_type_proposal="client_order_sheet",
            detected_columns=["Товар", "Зал", "Бар", "Кухня"],
            has_table_structure=True,
            order_area_complete=True,
            rows=[_row("Пустая строка")],
        ),
        settings,
    )

    assert result.command.photo_outcome == "no_order_quantities"
    assert result.document_type == "client_order_sheet"


def test_incomplete_photo_has_distinct_user_reply(settings) -> None:  # type: ignore[no-untyped-def]
    """Показывает подсказку о качестве фото вместо ложного отсутствия количеств."""
    result = ConversationEngine(settings).handle(
        _photo_event(),
        ParsedCommand(intent=Intent.ADD_ITEMS, photo_outcome="incomplete_photo_read"),
        ConversationState(),
        [],
    )

    assert "Не удалось надёжно прочитать фото" in result.reply.text
    assert "Не нашёл заполненных количеств" not in result.reply.text


def test_photo_quantity_correction_wins_and_ambiguous_active_values_fail_closed(settings) -> None:  # type: ignore[no-untyped-def]
    """Использует исправление и отклоняет два активных значения без связи."""
    corrected = normalize_photo_observation(
        PhotoDocumentObservation(
            document_type_proposal="free_list",
            rows=[
                _row(
                    "Лук",
                    explicit_order_unit="кг",
                    crossed_out_quantity_text="5",
                    corrected_quantity_text="8",
                    order_entry_type="handwritten_correction",
                )
            ],
        ),
        settings,
    )
    ambiguous = normalize_photo_observation(
        PhotoDocumentObservation(
            document_type_proposal="free_list",
            rows=[
                _row(
                    "Картофель",
                    active_quantity_texts=["5", "8"],
                    explicit_order_quantity=8,
                )
            ],
        ),
        settings,
    )

    assert corrected.command.items[0].quantity == 8
    assert corrected.command.items[0].quantity_source == "handwritten_correction"
    assert ambiguous.command.items == []


def test_ordinary_table_and_inline_comment_default_to_kitchen(settings) -> None:  # type: ignore[no-untyped-def]
    """Сохраняет same-row quantity/comment и не спрашивает отдел."""
    result = normalize_photo_observation(
        PhotoDocumentObservation(
            document_type_proposal="order_table",
            detected_columns=["Товар", "Количество", "Комментарий"],
            has_table_structure=True,
            rows=[
                _row(
                    "Горчица",
                    explicit_order_quantity=3,
                    explicit_order_unit="шт",
                    comment_text="привезти завтра",
                    comment_source="explicit_marker",
                )
            ],
        ),
        settings,
    )

    item = result.command.items[0]
    assert item.department == settings.default_department
    assert item.quantity == 3
    assert item.comment == "привезти завтра"


def test_product_card_and_reference_numbers_create_no_order(settings) -> None:  # type: ignore[no-untyped-def]
    """Не превращает карточку товара и печатную фасовку в заказ."""
    result = normalize_photo_observation(
        PhotoDocumentObservation(
            document_type_proposal="product_card",
            detected_columns=["Товар", "Фасовка", "Цена", "Остаток"],
            has_table_structure=True,
            rows=[_row("Горчица CHATEL, ведро, 1 кг, 6 шт/кор, Франция")],
        ),
        settings,
    )

    assert result.command.items == []


def test_product_card_structure_rejects_model_invented_generic_quantity(settings) -> None:  # type: ignore[no-untyped-def]
    """Не принимает quantity, если структура фотографии доказывает только карточку товара."""
    result = normalize_photo_observation(
        PhotoDocumentObservation(
            document_type_proposal="free_list",
            detected_columns=["Товар", "Фасовка", "Цена", "Остаток"],
            has_table_structure=True,
            rows=[_row("Горчица", explicit_order_quantity=6, explicit_order_unit="шт")],
        ),
        settings,
    )

    assert result.document_type == "product_card"
    assert result.command.items == []


def test_mocked_photo_pipeline_converges_into_existing_cart_pipeline(
    settings, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """После авторизации фото-строка выбирает уникальный exact product без AMBIGUOUS."""
    name = 'Васаби TM "Sango", 1 кг, 10 шт/кор, Китай'
    observation = PhotoDocumentObservation(
        document_type_proposal="client_order_sheet",
        detected_columns=["Товар", "Зал", "Бар", "Кухня", "Комментарий"],
        has_table_structure=True,
        rows=[
            _row(
                "Васаби TM “Sango”, 1кг, 10 шт/кор, Китай",
                kitchen_quantity=3,
                comment_text="привезти завтра до 8",
                comment_source="explicit_marker",
            )
        ],
    )
    responses = _VisionResponses(observation)
    service = object.__new__(OpenAIService)
    service.settings = settings
    service.tracer = Tracer(settings)
    service.vision_client = SimpleNamespace(responses=responses)
    photo = tmp_path / "order.jpg"
    photo.write_bytes(b"image")
    command = service.parse_photo(photo, "image/jpeg")
    catalog = [CatalogProduct(product_id="wasabi", name=name, unit="шт")]

    result = ConversationEngine(settings).handle(
        _photo_event(), command, state=ConversationState(), catalog=catalog
    )

    item = result.state.cart[0]
    assert item.catalog_product_id == "wasabi"
    assert item.status is ItemStatus.MATCHED
    assert item.quantity == 3
    assert item.comment == "привезти завтра до 8"
    assert "какой товар" not in result.reply.text.lower()
    assert len(responses.calls) == 1
    assert responses.calls[0]["text_format"] is PhotoDocumentObservation


def test_incomplete_structured_vision_response_fails_closed_without_second_call(
    settings, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """Закрывает incomplete structured response без повторного vision-вызова."""
    responses = _VisionResponses(
        PhotoDocumentObservation(rows=[_row("Васаби", kitchen_quantity=3)]),
        status="incomplete",
        incomplete_details={"reason": "max_output_tokens"},
    )
    service = object.__new__(OpenAIService)
    service.settings = settings
    service.tracer = Tracer(settings)
    service.vision_client = SimpleNamespace(responses=responses)
    photo = tmp_path / "truncated.jpg"
    photo.write_bytes(b"image")

    result = service.parse_photo(photo, "image/jpeg")

    assert result.photo_outcome == "incomplete_photo_read"
    assert result.items == []
    assert len(responses.calls) == 1
    assert responses.calls[0]["max_output_tokens"] == 8000


def test_photo_identity_normalization_preserves_full_identity() -> None:
    """Сближает только безопасную пунктуацию и пробелы, не удаляя характеристики."""
    left = 'Васаби TM "Sango", 1кг, 10 шт/кор, Китай'
    right = "васаби TM “Sango”, 1 кг, 10 шт/кор, Китай"

    assert canonical_photo_identity(left) == canonical_photo_identity(right)
    assert canonical_photo_identity(left) != canonical_photo_identity(
        left.replace("Китай", "Франция")
    )


def test_horseradish_ocr_measurement_uses_unique_trusted_photo_identity(settings) -> None:  # type: ignore[no-untyped-def]
    """Разрешает уникальную venue-строку при одной OCR-ошибке в измерении."""
    observed = "Хрен столовый Домашний, Кал., 1,6 л, Россия (12/1)"
    catalog = [
        CatalogProduct(
            product_id="horseradish-160g",
            name="Хрен столовый Домашний, Кал-н,160грт/Б, Россия (12/1)",
            unit="шт",
        ),
        CatalogProduct(product_id="mustard", name="Горчица Домашняя", unit="шт"),
        CatalogProduct(product_id="vinegar", name="Уксус столовый", unit="шт"),
        CatalogProduct(product_id="horseradish-5kg", name="Хрен столовый 5кг", unit="кг"),
    ]
    result = ConversationEngine(settings).handle(
        _photo_event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(
                    product_query=observed,
                    quantity=2,
                    unit="шт",
                    quantity_source="department_columns",
                    department_quantities=DepartmentQuantities(kitchen=2),
                    catalog_identity_provenance="venue_table_exact_candidate",
                )
            ],
        ),
        ConversationState(),
        catalog,
    )

    item = result.state.cart[0]
    assert item.catalog_product_id == "horseradish-160g"
    assert item.status is ItemStatus.MATCHED
    assert item.quantity == 2
    assert item.candidates[0].product_id == "horseradish-160g"


def test_live_four_product_photo_case_matches_all_rows(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет четыре заполненные строки, включая OCR-вариант хрена."""
    observation = PhotoDocumentObservation(
        document_type_proposal="client_order_sheet",
        detected_columns=["Наименование", "Зал", "Бар", "Кухня", "Комментарий"],
        has_table_structure=True,
        visible_product_row_count=5,
        order_area_complete=True,
        rows=[
            _row("Васаби TM Sango, 1кг, 10 шт/кор, Китай", kitchen_quantity=3),
            _row("Лук жареный Metro Chef, 600 г", kitchen_quantity=10),
            _row("Паста соевая aka miso темная", kitchen_quantity=12),
            _row("Хрен столовый Домашний, Кал., 1,6 л, Россия (12/1)", kitchen_quantity=2),
        ],
    )
    normalized = normalize_photo_observation(observation, settings)
    catalog = [
        CatalogProduct(
            product_id="wasabi",
            name="Васаби TM Sango, 1кг, 10 шт/кор, Китай",
            unit="шт",
        ),
        CatalogProduct(product_id="onion", name="Лук жареный Metro Chef, 600 г", unit="кг"),
        CatalogProduct(product_id="miso", name="Паста соевая aka miso темная", unit="кг"),
        CatalogProduct(
            product_id="horseradish",
            name="Хрен столовый Домашний, Кал-н,160грт/Б, Россия (12/1)",
            unit="шт",
        ),
    ]

    result = ConversationEngine(settings).handle(
        _photo_event(), normalized.command, ConversationState(), catalog
    )

    assert len(normalized.command.items) == 4
    assert [item.status for item in result.state.cart] == [ItemStatus.MATCHED] * 4
    assert [item.catalog_product_id for item in result.state.cart] == [
        "wasabi",
        "onion",
        "miso",
        "horseradish",
    ]


def test_trusted_photo_does_not_choose_between_size_variants(settings) -> None:  # type: ignore[no-untyped-def]
    """Не считает OCR-tolerance разрешением для вариантов, отличающихся размером."""
    catalog = [
        CatalogProduct(product_id="sauce-500", name="Соус ABC 500 мл", unit="шт"),
        CatalogProduct(product_id="sauce-1l", name="Соус ABC 1 л", unit="шт"),
    ]
    result = ConversationEngine(settings).handle(
        _photo_event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(
                    product_query="Соус ABC",
                    quantity=5,
                    unit="шт",
                    quantity_source="department_columns",
                    department_quantities=DepartmentQuantities(kitchen=5),
                    catalog_identity_provenance="venue_table_exact_candidate",
                )
            ],
        ),
        ConversationState(),
        catalog,
    )

    item = result.state.cart[0]
    assert item.catalog_product_id == ""
    assert item.status is ItemStatus.AMBIGUOUS
    assert {candidate.product_id for candidate in item.candidates} == {"sauce-500", "sauce-1l"}


def test_sheet_row_number_requires_lexical_sanity(settings) -> None:  # type: ignore[no-untyped-def]
    """Не выбирает строку каталога по номеру при противоречащем названии товара."""
    catalog = [
        CatalogProduct(
            product_id="horseradish",
            name="Хрен столовый Домашний, Кал-н,160грт/Б, Россия (12/1)",
            unit="шт",
            row_number=42,
        ),
        CatalogProduct(product_id="onion", name="Лук жареный Metro Chef", unit="кг", row_number=43),
    ]
    result = ConversationEngine(settings).handle(
        _photo_event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(
                    product_query="Лук жареный Metro Chef",
                    quantity=2,
                    unit="кг",
                    quantity_source="department_columns",
                    department_quantities=DepartmentQuantities(kitchen=2),
                    catalog_identity_provenance="venue_table_exact_candidate",
                    photo_sheet_row_number=42,
                    photo_sheet_row_number_confidence=1,
                )
            ],
        ),
        ConversationState(),
        catalog,
    )

    assert result.state.cart[0].catalog_product_id == "onion"


def test_photo_department_quantities_survive_submission_mapping(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет round-trip Зал/Бар/Кухня до строк отправки заявки."""
    state = ConversationState(
        restaurant="Тест",
        spreadsheet_id="venue-sheet",
        cart=[
            CartItem(
                id="item-1",
                source_query="Васаби",
                quantity=10,
                unit="шт",
                department_quantities=DepartmentQuantities(hall=2, bar=3, kitchen=5),
                status=ItemStatus.MATCHED,
                catalog_product_id="wasabi",
                catalog_name="Васаби",
                catalog_unit="шт",
                supplier="Поставщик",
                price=10,
            )
        ],
    )
    event = ConversationInput(
        1,
        "photo-test",
        actor_id="cook",
        channel="telegram",
        kind=InputKind.PHOTO,
    )

    result = ConversationEngine(settings)._prepare_submission(event, state)

    rows = result.state.pending_submission.rows  # type: ignore[union-attr]
    assert [(row["_department"], row["Кол-во"]) for row in rows] == [
        ("Зал", 2),
        ("Бар", 3),
        ("Кухня", 5),
    ]


def test_photo_single_department_quantities_keep_their_department(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет отдельное сохранение Hall, Bar и Kitchen до submission mapping."""
    cases = [
        ("Зал", DepartmentQuantities(hall=4), 4),
        ("Бар", DepartmentQuantities(bar=7), 7),
        ("Кухня", DepartmentQuantities(kitchen=3), 3),
    ]
    event = ConversationInput(
        1,
        "photo-department-test",
        actor_id="cook",
        channel="telegram",
        kind=InputKind.PHOTO,
    )

    for department, quantities, expected in cases:
        state = ConversationState(
            restaurant="Тест",
            spreadsheet_id="venue-sheet",
            cart=[
                CartItem(
                    id="item-1",
                    source_query="Васаби",
                    quantity=sum(value or 0 for value in quantities.model_dump().values()),
                    unit="шт",
                    department_quantities=quantities,
                    status=ItemStatus.MATCHED,
                    catalog_product_id="wasabi",
                    catalog_name="Васаби",
                    catalog_unit="шт",
                    supplier="Поставщик",
                    price=10,
                )
            ],
        )

        result = ConversationEngine(settings)._prepare_submission(event, state)
        rows = result.state.pending_submission.rows  # type: ignore[union-attr]

        assert [(row["_department"], row["Кол-во"]) for row in rows] == [(department, expected)]
