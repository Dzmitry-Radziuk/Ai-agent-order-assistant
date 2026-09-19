"""Проверяет детерминированную авторизацию строк и идентичность фото-каталога."""

from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw

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
    photo_sheet_row_mapping_is_authoritative,
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


def _save_light_order_sheet(path: Path) -> None:
    """Создаёт таблицу, где пиксельная геометрия доказывает две order-строки."""
    image = Image.new("RGB", (1000, 500), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 100, 700, 118), fill=(205, 225, 210))
    for x in [20, 120, 320, 400, 450, 500, 550, 700, 760, 820]:
        draw.line((x, 119, x, 400), fill=(185, 185, 185))
    for y in range(120, 401, 20):
        draw.line((0, y, 700, y), fill=(215, 215, 215))
    draw.text((415, 143), "20", fill=(20, 20, 20))
    draw.text((515, 283), "6", fill=(20, 20, 20))
    image.save(path, format="PNG")


def test_photo_uses_observation_schema_without_cart_item_shape() -> None:
    """Фиксирует отдельный structured-output контракт для vision фотографии."""
    schema = PhotoDocumentObservation.model_json_schema()

    assert "rows" in schema["properties"]
    assert "sheet_row_numbers_visible" in schema["properties"]
    assert "product_text" in schema["$defs"]["PhotoRowObservation"]["properties"]
    assert "visible_product_row_count" in schema["properties"]
    assert "visible_filled_order_row_count" in schema["properties"]
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


class _SequenceVisionResponses:
    """Возвращает последовательные наблюдения для проверки повторного vision-прохода."""

    def __init__(self, observations: list[PhotoDocumentObservation]) -> None:
        """Сохраняет наблюдения и историю запросов."""
        self.observations = observations
        self.calls: list[dict[str, object]] = []

    def parse(self, **kwargs: object) -> SimpleNamespace:
        """Возвращает следующее наблюдение без скрытых повторных запросов."""
        self.calls.append(kwargs)
        return SimpleNamespace(
            output_parsed=self.observations.pop(0),
            status="",
            incomplete_details=None,
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


def test_headerless_table_fragment_uses_aligned_explicit_quantity(settings) -> None:  # type: ignore[no-untyped-def]
    """Разбирает фрагмент таблицы без заголовков по количеству в той же строке товара."""
    observation = PhotoDocumentObservation(
        document_type_proposal="order_table",
        has_table_structure=True,
        rows=[
            _row(
                "Горчица дижонская",
                explicit_order_quantity=1,
                explicit_order_unit="шт",
                order_entry_text="1",
                row_alignment_confidence=1,
                quantity_confidence=1,
            )
        ],
    )

    result = normalize_photo_observation(observation, settings)

    assert result.document_type == "order_table"
    assert result.command.items[0].product_query == "Горчица дижонская"
    assert result.command.items[0].quantity == 1


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


def test_model_filled_row_count_mismatch_is_advisory_without_geometry(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    """Не отклоняет произвольное фото только по self-reported счётчику vision."""
    result = normalize_photo_observation(
        PhotoDocumentObservation(
            document_type_proposal="client_order_sheet",
            detected_columns=["Товар", "Зал", "Бар", "Кухня"],
            has_table_structure=True,
            visible_filled_order_row_count=2,
            rows=[_row("Горчица дижонская", kitchen_quantity=1)],
        ),
        settings,
    )

    assert result.command.photo_outcome == ""
    assert result.command.items[0].quantity == 1


def test_headerless_layout_with_explicit_quantities_is_accepted(settings) -> None:  # type: ignore[no-untyped-def]
    """Принимает фото без заголовков и номеров строк по same-row количеству."""
    result = normalize_photo_observation(
        PhotoDocumentObservation(
            document_type_proposal="free_list",
            has_table_structure=True,
            detected_columns=[],
            rows=[
                _row(
                    "Молоко",
                    explicit_order_quantity=4,
                    explicit_order_unit="шт",
                    order_entry_text="4 шт",
                    row_alignment_confidence=1,
                    quantity_confidence=1,
                ),
                _row(
                    "Картофель",
                    explicit_order_quantity=6,
                    explicit_order_unit="кг",
                    order_entry_text="6 кг",
                    row_alignment_confidence=1,
                    quantity_confidence=1,
                ),
            ],
        ),
        settings,
    )

    assert [(item.product_query, item.quantity) for item in result.command.items] == [
        ("Молоко", 4),
        ("Картофель", 6),
    ]
    assert result.command.photo_outcome == ""


def test_global_photo_comment_can_live_in_regular_row(settings) -> None:  # type: ignore[no-untyped-def]
    """Поднимает явный комментарий «все товары» на уровень всей заявки."""
    result = normalize_photo_observation(
        PhotoDocumentObservation(
            document_type_proposal="order_table",
            has_table_structure=False,
            rows=[
                _row(
                    "Молоко",
                    explicit_order_quantity=4,
                    explicit_order_unit="шт",
                    order_entry_text="4",
                    comment_text="все товары доставить завтра до восьми вечера",
                    comment_source="user_note",
                )
            ],
        ),
        settings,
    )

    assert result.command.global_comment == "доставить завтра до восьми вечера"
    assert result.command.items[0].comment == ""


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


def test_free_list_without_department_columns_defaults_to_kitchen(settings) -> None:  # type: ignore[no-untyped-def]
    """Обрабатывает фото списка без колонок Hall/Bar/Kitchen через отдел по умолчанию."""
    result = normalize_photo_observation(
        PhotoDocumentObservation(
            document_type_proposal="free_list",
            has_table_structure=False,
            rows=[_row("Картофель", explicit_order_quantity=3, explicit_order_unit="кг")],
        ),
        settings,
    )

    assert len(result.command.items) == 1
    assert result.command.items[0].department == settings.default_department
    assert result.command.items[0].quantity == 3


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


def test_short_screenshot_order_headers_are_not_classified_as_product_card() -> None:
    """Распознаёт технические варианты order-заголовков как таблицу заказа."""
    observation = PhotoDocumentObservation(
        document_type_proposal="order_table",
        detected_columns=["product", "order_quantity", "quantity_decimal", "price"],
        has_table_structure=True,
        rows=[_row("Васаби", explicit_order_quantity=3, order_entry_text="3")],
    )

    assert classify_photo_document(observation) == "order_table"


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


def test_preprocessed_row_count_retries_shifted_photo_and_selects_aligned_rows(
    settings, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """Повторяет vision при self-consistent пропуске и принимает полный row-focused результат."""
    first = PhotoDocumentObservation(
        document_type_proposal="client_order_sheet",
        detected_columns=["Товар", "Зал", "Бар", "Кухня", "Комментарий"],
        has_table_structure=True,
        visible_filled_order_row_count=1,
        rows=[
            _row(
                "Судак Филе охл 500+ Крупный",
                kitchen_quantity=6,
                comment_text="доставить завтра до восьми вечера",
                comment_source="explicit_marker",
            )
        ],
    )
    second = PhotoDocumentObservation(
        document_type_proposal="client_order_sheet",
        detected_columns=["Товар", "Зал", "Бар", "Кухня", "Комментарий"],
        has_table_structure=True,
        visible_filled_order_row_count=2,
        rows=[
            _row("Горчица дижонская", hall_quantity=20),
            _row(
                "зам. Щука филе б/к, Россия",
                kitchen_quantity=6,
                comment_text="доставить завтра до восьми вечера",
                comment_source="explicit_marker",
            ),
        ],
    )
    responses = _SequenceVisionResponses([first, second])
    service = object.__new__(OpenAIService)
    service.settings = settings
    service.tracer = Tracer(settings)
    service.vision_client = SimpleNamespace(responses=responses)
    photo = tmp_path / "light-order-sheet.png"
    _save_light_order_sheet(photo)

    result = service.parse_photo(photo, "image/png")

    assert len(responses.calls) == 2
    assert [(item.product_query, item.quantity) for item in result.items] == [
        ("Горчица дижонская", 20),
        ("зам. Щука филе б/к, Россия", 6),
    ]
    assert all(item.product_query != "Судак Филе охл 500+ Крупный" for item in result.items)
    retry_content = responses.calls[1]["input"][0]["content"]  # type: ignore[index]
    assert len([part for part in retry_content if part["type"] == "input_image"]) == 2


def test_preprocessed_row_count_fails_closed_after_second_incomplete_read(
    settings, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """Не создаёт частичную заявку, если оба vision-прохода пропустили заполненную строку."""
    shifted = PhotoDocumentObservation(
        document_type_proposal="client_order_sheet",
        detected_columns=["Товар", "Зал", "Бар", "Кухня", "Комментарий"],
        has_table_structure=True,
        visible_filled_order_row_count=1,
        rows=[_row("Судак Филе охл 500+ Крупный", kitchen_quantity=6)],
    )
    responses = _SequenceVisionResponses([shifted, shifted.model_copy(deep=True)])
    service = object.__new__(OpenAIService)
    service.settings = settings
    service.tracer = Tracer(settings)
    service.vision_client = SimpleNamespace(responses=responses)
    photo = tmp_path / "light-order-sheet.png"
    _save_light_order_sheet(photo)

    result = service.parse_photo(photo, "image/png")

    assert len(responses.calls) == 2
    assert result.photo_outcome == "incomplete_photo_read"
    assert result.items == []


def test_uncertain_photo_observation_retries_with_original_and_focus_views(
    settings, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """Повторно проверяет неопределённую строку и принимает только полный результат."""
    first = PhotoDocumentObservation(
        document_type_proposal="client_order_sheet",
        detected_columns=["Товар", "Зал", "Бар", "Кухня", "Комментарий"],
        has_table_structure=True,
        rows=[_row("Васаби", kitchen_quantity=3)],
        visible_product_row_count=1,
        order_area_complete=False,
        uncertain_order_row_count=1,
    )
    second = PhotoDocumentObservation(
        document_type_proposal="client_order_sheet",
        detected_columns=["Товар", "Зал", "Бар", "Кухня", "Комментарий"],
        has_table_structure=True,
        rows=[_row("Васаби", kitchen_quantity=3)],
        visible_product_row_count=1,
        order_area_complete=True,
        uncertain_order_row_count=0,
    )
    responses = _SequenceVisionResponses([first, second])
    service = object.__new__(OpenAIService)
    service.settings = settings
    service.tracer = Tracer(settings)
    service.vision_client = SimpleNamespace(responses=responses)
    photo = tmp_path / "dense-table.png"
    Image.new("RGB", (1600, 900), "white").save(photo, format="PNG")

    result = service.parse_photo(photo, "image/png")

    assert len(responses.calls) == 2
    retry_content = responses.calls[1]["input"][0]["content"]  # type: ignore[index]
    assert len([part for part in retry_content if part["type"] == "input_image"]) == 2
    assert result.items[0].product_query == "Васаби"


def test_ai_row_number_claim_does_not_make_numbers_mandatory(settings, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Не требует номера строк только на основании заявления vision-модели."""
    observation = PhotoDocumentObservation(
        document_type_proposal="client_order_sheet",
        detected_columns=["Товар", "Зал", "Бар", "Кухня"],
        has_table_structure=True,
        sheet_row_numbers_visible=True,
        rows=[_row("Горчица дижонская", kitchen_quantity=1)],
    )
    responses = _SequenceVisionResponses([observation])
    service = object.__new__(OpenAIService)
    service.settings = settings
    service.tracer = Tracer(settings)
    service.vision_client = SimpleNamespace(responses=responses)
    photo = tmp_path / "fragment.png"
    Image.new("RGB", (800, 500), "white").save(photo, format="PNG")

    result = service.parse_photo(photo, "image/png")

    assert len(responses.calls) == 1
    assert result.items[0].product_query == "Горчица дижонская"
    assert result.items[0].photo_sheet_row_number is None
    assert result.items[0].photo_sheet_row_number_authoritative is False


def test_unverified_sheet_row_conflict_does_not_override_product_identity(
    settings, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """Игнорирует неподтверждённый номер строки и сохраняет прочитанный товар."""
    observation = PhotoDocumentObservation(
        document_type_proposal="client_order_sheet",
        detected_columns=["Товар", "Зал", "Бар", "Кухня", "Комментарий"],
        has_table_structure=True,
        sheet_row_numbers_visible=True,
        rows=[
            _row(
                "Горчица дижонская большое зерно",
                kitchen_quantity=1,
                comment_text="комментарий",
                comment_source="explicit_marker",
                sheet_row_number=11,
                sheet_row_number_confidence=1,
            ),
            _row(
                "Хрен столовый Домашний, Кал-й,160гр/Б, Россия (12/1)",
                kitchen_quantity=2,
                sheet_row_number=14,
                sheet_row_number_confidence=1,
            ),
        ],
    )
    responses = _SequenceVisionResponses([observation])
    service = object.__new__(OpenAIService)
    service.settings = settings
    service.tracer = Tracer(settings)
    service.vision_client = SimpleNamespace(responses=responses)
    photo = tmp_path / "shifted-sheet.png"
    Image.new("RGB", (1280, 332), "white").save(photo, format="PNG")

    result = service.parse_photo(photo, "image/png")

    assert len(responses.calls) == 1
    assert [item.product_query for item in result.items] == [
        "Горчица дижонская большое зерно",
        "Хрен столовый Домашний, Кал-й,160гр/Б, Россия (12/1)",
    ]
    assert [item.photo_sheet_row_number for item in result.items] == [11, 14]
    assert all(item.photo_sheet_row_number_authoritative for item in result.items)


def test_headerless_table_with_catalog_rows_is_accepted_without_sheet_numbers(
    settings, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """Читает обрезанную таблицу без заголовков и номеров строк листа."""
    observation = PhotoDocumentObservation(
        document_type_proposal="order_table",
        has_table_structure=True,
        rows=[
            _row(
                "Паста соевая aka miso темная, Китай 1 кг, 10 шт/кор 202₽",
                explicit_order_quantity=3,
                explicit_order_unit="шт",
                order_entry_text="3",
                row_alignment_confidence=1,
                quantity_confidence=1,
            ),
            _row(
                "Хрен столовый Домашний, Кал-й,160гр/Б, Россия (12/1)",
                explicit_order_quantity=2,
                explicit_order_unit="шт",
                order_entry_text="2",
                row_alignment_confidence=1,
                quantity_confidence=1,
            ),
        ],
    )
    responses = _SequenceVisionResponses([observation])
    service = object.__new__(OpenAIService)
    service.settings = settings
    service.tracer = Tracer(settings)
    service.vision_client = SimpleNamespace(responses=responses)
    photo = tmp_path / "headerless-sheet.png"
    Image.new("RGB", (1280, 332), "white").save(photo, format="PNG")

    result = service.parse_photo(photo, "image/png")

    assert len(responses.calls) == 1
    assert [(item.product_query, item.quantity) for item in result.items] == [
        ("Паста соевая aka miso темная, Китай 1 кг, 10 шт/кор 202₽", 3),
        ("Хрен столовый Домашний, Кал-й,160гр/Б, Россия (12/1)", 2),
    ]


def test_explicitly_required_sheet_rows_without_numbers_fail_closed(settings) -> None:  # type: ignore[no-untyped-def]
    """Не принимает номера строк только при явном доверенном требовании вызывающего слоя."""
    result = normalize_photo_observation(
        PhotoDocumentObservation(
            document_type_proposal="client_order_sheet",
            detected_columns=["Товар", "Зал", "Бар", "Кухня"],
            has_table_structure=True,
            sheet_row_numbers_visible=True,
            rows=[_row("Горчица дижонская", kitchen_quantity=1)],
        ),
        settings,
        require_sheet_row_numbers=True,
    )

    assert result.command.photo_outcome == "incomplete_photo_read"
    assert result.reason == "sheet_row_identity_unreadable"


def test_authoritative_order_rows_allow_gaps_between_sheet_numbers() -> None:
    """Разрешает пропуски пустых строк между двумя заполненными строками заказа."""
    observation = PhotoDocumentObservation(
        document_type_proposal="client_order_sheet",
        sheet_row_numbers_visible=True,
        rows=[
            _row(
                "Горчица дижонская",
                kitchen_quantity=1,
                sheet_row_number=8,
                sheet_row_number_confidence=1,
            ),
            _row(
                "Хрен столовый",
                kitchen_quantity=2,
                sheet_row_number=12,
                sheet_row_number_confidence=1,
                row_index=1,
            ),
        ],
    )

    assert photo_sheet_row_mapping_is_authoritative(observation) is False
    assert (
        photo_sheet_row_mapping_is_authoritative(
            observation,
            require_sheet_row_numbers=True,
        )
        is True
    )


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


def test_authoritative_sheet_row_does_not_override_conflicting_product(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    """Не подменяет прочитанный товар содержимым противоречащей строки листа."""
    catalog = [
        CatalogProduct(
            product_id="mustard",
            name="Горчица дижонская",
            unit="шт",
            row_number=7,
        ),
        CatalogProduct(
            product_id="grain-mustard",
            name="Горчица дижонская большое зерно",
            unit="шт",
            row_number=8,
        ),
    ]
    result = ConversationEngine(settings).handle(
        _photo_event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(
                    product_query="Горчица дижонская большое зерно",
                    quantity=1,
                    unit="шт",
                    quantity_source="department_columns",
                    department_quantities=DepartmentQuantities(kitchen=1),
                    catalog_identity_provenance="venue_table_exact_candidate",
                    photo_sheet_row_number=7,
                    photo_sheet_row_number_confidence=1,
                    photo_sheet_row_number_authoritative=True,
                )
            ],
        ),
        ConversationState(),
        catalog,
    )

    item = result.state.cart[0]
    assert item.catalog_product_id == "grain-mustard"
    assert item.catalog_name == "Горчица дижонская большое зерно"


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
