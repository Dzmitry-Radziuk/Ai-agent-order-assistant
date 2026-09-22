"""Проверяет детерминированную авторизацию строк и идентичность фото-каталога."""

from pathlib import Path
from types import SimpleNamespace

import pytest
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
from restaurant_bot.input.photo_views import PhotoImagePreparation, PhotoImageView
from restaurant_bot.integrations.openai_client import (
    OpenAIService,
    _confirmed_partial_photo_command,
    _lettered_department_reads_match,
    _read_lettered_department_column_values,
)
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


@pytest.mark.parametrize("named_fields", [True, False])
def test_lettered_sheet_accepts_equivalent_department_field_representations(
    settings, named_fields
) -> None:  # type: ignore[no-untyped-def]
    """Не теряет количества, когда модель использует имена отделов при видимых N/O/P."""
    first = PhotoDocumentObservation(
        has_table_structure=True,
        detected_columns=["Товар", "N", "O", "P"],
        rows=[
            _row(
                "Молоко",
                **(
                    {"hall_quantity": 2, "bar_quantity": 3}
                    if named_fields
                    else {
                        "sheet_column_n_quantity": 2,
                        "sheet_column_o_quantity": 3,
                    }
                ),
            )
        ],
    )
    second = first.model_copy(
        update={
            "rows": [
                _row(
                    "Молоко",
                    sheet_column_n_quantity=2,
                    sheet_column_o_quantity=3,
                )
            ]
        }
    )
    assert _lettered_department_reads_match(first, second)
    command = normalize_photo_observation(first, settings).command
    assert len(command.items) == 1
    assert command.items[0].quantity == 5
    assert command.items[0].department_quantities == DepartmentQuantities(hall=2, bar=3)


def test_conflicting_department_field_representations_are_not_accepted(settings) -> None:  # type: ignore[no-untyped-def]
    """Не выбирает произвольно отдел при противоречии полей одного наблюдения."""
    observation = PhotoDocumentObservation(
        has_table_structure=True,
        detected_columns=["Товар", "N", "O", "P"],
        rows=[_row("Молоко", hall_quantity=2, sheet_column_o_quantity=2)],
    )
    command = normalize_photo_observation(observation, settings).command
    assert command.items == []
    assert command.photo_outcome == "incomplete_photo_read"


@pytest.mark.parametrize(
    "columns,fields,expected",
    [
        (["Товар", "Зал", "Бар", "Кухня"], {"hall_quantity": 2}, DepartmentQuantities(hall=2)),
        (["Товар", "Бар"], {"bar_quantity": 3}, DepartmentQuantities(bar=3)),
        (["Товар", "N", "O", "P"], {"sheet_column_p_quantity": 4}, DepartmentQuantities(kitchen=4)),
        (["Товар"], {"explicit_order_quantity": 5}, DepartmentQuantities()),
    ],
)
def test_photo_department_headers_and_letters_are_independent_evidence(
    settings, columns, fields, expected
) -> None:  # type: ignore[no-untyped-def]
    """Распознаёт отдел по названиям либо N/O/P и не угадывает его без заголовков."""
    observation = PhotoDocumentObservation(
        has_table_structure=True,
        detected_columns=columns,
        rows=[_row("Молоко", **fields)],
    )
    command = normalize_photo_observation(observation, settings).command
    assert len(command.items) == 1
    assert command.items[0].department_quantities == expected


def test_partial_photo_keeps_only_agreed_items_without_multiplying_duplicates(settings) -> None:  # type: ignore[no-untyped-def]
    """Исключает спорные количества и лишние повторения из частичного результата."""
    first = PhotoDocumentObservation(
        rows=[
            _row("Хлеб", explicit_order_quantity=3),
            _row("Хлеб", explicit_order_quantity=3),
            _row("Молоко", explicit_order_quantity=2),
        ]
    )
    second = PhotoDocumentObservation(
        rows=[
            _row("Хлеб", explicit_order_quantity=3),
            _row("Молоко", explicit_order_quantity=8),
        ]
    )
    command = _confirmed_partial_photo_command(first, second, settings)
    assert command.photo_outcome == "partial_photo_read"
    assert [(item.product_query, item.quantity) for item in command.items] == [("Хлеб", 3)]
    assert _confirmed_partial_photo_command(first, None, settings).items == []


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
    row_properties = schema["$defs"]["PhotoRowObservation"]["properties"]
    assert "sheet_column_n_quantity" in row_properties
    assert "sheet_column_o_quantity" in row_properties
    assert "sheet_column_p_quantity" in row_properties
    assert "sheet_column_q_comment" in row_properties
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


def test_lettered_sheet_columns_map_departments_and_row_comment(settings) -> None:  # type: ignore[no-untyped-def]
    """Детерминированно сопоставляет видимые N/O/P/Q с отделами и комментарием."""
    observation = PhotoDocumentObservation(
        document_type_proposal="order_table",
        detected_columns=["Наименование", "N", "O", "P", "Q"],
        has_table_structure=True,
        rows=[
            _row(
                "Васаби",
                sheet_column_n_quantity=2,
                sheet_column_o_quantity=3,
                sheet_column_p_quantity=4,
                sheet_column_q_comment="привезти завтра",
            ),
            _row(
                "Горчица",
                sheet_column_o_quantity=5,
                sheet_column_q_comment="не заменять",
            ),
        ],
    )

    result = normalize_photo_observation(observation, settings)

    assert result.document_type == "client_order_sheet"
    assert result.command.items[0].quantity == 9
    assert result.command.items[0].department_quantities == DepartmentQuantities(
        hall=2,
        bar=3,
        kitchen=4,
    )
    assert result.command.items[0].comment == "привезти завтра"
    assert result.command.items[1].department_quantities == DepartmentQuantities(bar=5)
    assert result.command.items[1].comment == "не заменять"


def test_unlettered_department_values_are_not_mapped_to_n_o_p(settings) -> None:  # type: ignore[no-untyped-def]
    """Не назначает отделы по геометрии без названий или видимых букв колонок."""
    observation = PhotoDocumentObservation(
        document_type_proposal="other",
        has_table_structure=False,
        rows=[
            _row("Заловый товар", sheet_column_n_quantity=2),
            _row("Барный товар", sheet_column_o_quantity=3),
            _row("Кухонный товар", sheet_column_p_quantity=4),
        ],
    )

    result = normalize_photo_observation(observation, settings)

    assert result.command.items == []
    assert result.command.photo_outcome in {"unsupported_photo", "no_order_quantities"}


def test_unlabelled_table_cells_become_plain_order_and_require_department_selection(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    """Суммирует ячейки фрагмента таблицы и оставляет отдел для финального выбора."""
    observation = PhotoDocumentObservation(
        document_type_proposal="order_table",
        has_table_structure=True,
        rows=[
            _row("Горчица острая", hall_quantity=8),
            _row("Горчица дижонская", bar_quantity=2, kitchen_quantity=3),
        ],
    )

    normalized = normalize_photo_observation(observation, settings)
    catalog = [
        CatalogProduct(product_id="hot", name="Горчица острая", unit="шт"),
        CatalogProduct(product_id="dijon", name="Горчица дижонская", unit="шт"),
    ]
    intake = ConversationEngine(settings).handle(
        _photo_event(), normalized.command, ConversationState(), catalog
    )
    review = ConversationEngine(settings).handle(
        _photo_event(),
        ParsedCommand(intent=Intent.SHOW_FINAL_REVIEW),
        intake.state,
        catalog,
    )

    assert normalized.document_type == "order_table"
    assert [(item.product_query, item.quantity) for item in normalized.command.items] == [
        ("Горчица острая", 8),
        ("Горчица дижонская", 5),
    ]
    assert all(
        item.department_quantities == DepartmentQuantities() for item in normalized.command.items
    )
    assert all(item.status is ItemStatus.MATCHED for item in intake.state.cart)
    assert intake.state.department_confirmation_required is True
    assert intake.state.department_confirmed is False
    assert "К какому подразделению" in review.reply.text


def test_visible_department_names_take_precedence_over_sheet_column_letters(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    """Не подменяет явные названия отделов значениями сырых буквенных колонок."""
    observation = PhotoDocumentObservation(
        document_type_proposal="client_order_sheet",
        detected_columns=["N", "O", "P", "Q", "Зал", "Бар", "Кухня", "Комментарий"],
        has_table_structure=True,
        rows=[
            _row(
                "Васаби",
                hall_quantity=1,
                bar_quantity=2,
                kitchen_quantity=3,
                sheet_column_n_quantity=40,
                sheet_column_o_quantity=50,
                sheet_column_p_quantity=60,
                sheet_column_q_comment="не использовать",
                comment_text="видимый комментарий",
                comment_source="user_note",
            )
        ],
    )

    result = normalize_photo_observation(observation, settings)

    assert result.command.items[0].department_quantities == DepartmentQuantities(
        hall=1,
        bar=2,
        kitchen=3,
    )
    assert result.command.items[0].comment == "видимый комментарий"


def test_incomplete_lettered_department_columns_do_not_assign_departments(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    """Не включает фиксированную схему N/O/P, если видны не все три столбца."""
    observation = PhotoDocumentObservation(
        document_type_proposal="order_table",
        detected_columns=["Товар", "N", "P", "Q"],
        has_table_structure=True,
        rows=[
            _row(
                "Васаби",
                sheet_column_n_quantity=2,
                sheet_column_p_quantity=3,
                explicit_order_quantity=5,
                order_entry_text="5",
            )
        ],
    )

    result = normalize_photo_observation(observation, settings)

    assert result.document_type == "order_table"
    assert result.command.items[0].quantity == 5
    assert result.command.items[0].department_quantities == DepartmentQuantities()


def test_sheet_column_letters_do_not_change_free_list_photo(settings) -> None:  # type: ignore[no-untyped-def]
    """Не применяет N/O/P/Q к обычному списку без структуры электронной таблицы."""
    observation = PhotoDocumentObservation(
        document_type_proposal="free_list",
        detected_columns=["N", "O", "P", "Q"],
        has_table_structure=False,
        rows=[
            _row(
                "Васаби",
                explicit_order_quantity=3,
                sheet_column_n_quantity=2,
                sheet_column_q_comment="не привязанный комментарий",
            )
        ],
    )

    result = normalize_photo_observation(observation, settings)

    assert result.document_type == "free_list"
    assert result.command.items[0].quantity == 3
    assert result.command.items[0].department == settings.default_department
    assert result.command.items[0].department_quantities == DepartmentQuantities()
    assert result.command.items[0].comment == ""


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


def test_headerless_table_sums_multiple_confirmed_order_cells_in_same_row(settings) -> None:  # type: ignore[no-untyped-def]
    """Суммирует несколько order-ячеек обрезанной таблицы без выдумывания отделов."""
    product = "Свинина Окорок Тамбовский В/К"
    observation = PhotoDocumentObservation(
        document_type_proposal="order_table",
        detected_columns=["department", "product", "unit", "supplier", "order_quantity"],
        has_table_structure=True,
        rows=[
            PhotoRowObservation(
                row_index=0,
                row_text=f"Кухня | {product} | кг | Тестовый поставщик | 1 | 25",
                product_text=product,
                kitchen_quantity=1,
                order_entry_text="1",
                order_entry_type="typed",
            )
        ],
    )

    result = normalize_photo_observation(observation, settings)

    assert result.document_type == "order_table"
    assert result.command.items[0].quantity == 26
    assert result.command.items[0].quantity_source == "table_order_cells"
    assert result.command.items[0].department_quantities == DepartmentQuantities()


def test_headerless_table_does_not_sum_packaging_or_price_from_product_cell(settings) -> None:  # type: ignore[no-untyped-def]
    """Не считает числа названия, фасовки и цены дополнительными order-ячейками."""
    product = "Паста aka miso, 1 кг, 10 шт/кор 202₽"
    observation = PhotoDocumentObservation(
        document_type_proposal="order_table",
        detected_columns=["product", "unit", "supplier", "order_quantity"],
        has_table_structure=True,
        rows=[
            PhotoRowObservation(
                row_index=0,
                row_text=f"Кухня | {product} | шт | Тестовый поставщик | 2",
                product_text=product,
                kitchen_quantity=2,
                order_entry_text="2",
                order_entry_type="typed",
            )
        ],
    )

    result = normalize_photo_observation(observation, settings)

    assert result.command.items[0].quantity == 2
    assert result.command.items[0].quantity_source == "typed"


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


def test_simple_product_quantity_table_keeps_quantity_without_department(settings) -> None:  # type: ignore[no-untyped-def]
    """Оставляет товар-количество обычным заказом, не включая схему трёх отделов."""
    observation = PhotoDocumentObservation(
        document_type_proposal="order_table",
        detected_columns=["Товар", "Количество"],
        has_table_structure=True,
        rows=[
            _row(
                "Молоко",
                explicit_order_quantity=3,
                explicit_order_unit="шт",
                order_entry_text="3 шт",
            )
        ],
    )

    result = normalize_photo_observation(observation, settings)

    assert result.document_type == "order_table"
    assert [(item.product_query, item.quantity) for item in result.command.items] == [("Молоко", 3)]
    assert result.command.items[0].department_quantities == DepartmentQuantities()
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


def test_lettered_department_columns_use_isolated_row_aligned_reads(
    settings,
    tmp_path: Path,
    mocker,
) -> None:  # type: ignore[no-untyped-def]
    """Сверяет N/O/P отдельно и сохраняет несколько отделов у одной товарной строки."""
    columns = ["Наименование", "N", "O", "P", "Q"]
    first = PhotoDocumentObservation(
        document_type_proposal="order_table",
        detected_columns=columns,
        has_table_structure=True,
        rows=[
            _row(
                "Горчица",
                sheet_column_o_quantity=50,
                sheet_row_number=7,
                sheet_row_number_confidence=1,
                visual_row_index=0,
            ),
            _row(
                "Свинина Окорок Тамбовский В/К",
                bar_quantity=1,
                sheet_row_number=8,
                sheet_row_number_confidence=1,
                visual_row_index=1,
            ),
            _row(
                "Лук жареный Metro Chef",
                kitchen_quantity=2,
                sheet_row_number=9,
                sheet_row_number_confidence=1,
                visual_row_index=2,
            ),
        ],
    )
    hall_read = PhotoDocumentObservation(
        document_type_proposal="order_table",
        detected_columns=columns,
        has_table_structure=True,
        rows=[
            _row(
                "Свинина Окорок — вариант распознавания",
                sheet_column_n_quantity=1,
                sheet_row_number=8,
                sheet_row_number_confidence=1,
                visual_row_index=0,
            ),
        ],
    )
    bar_read = PhotoDocumentObservation(
        document_type_proposal="order_table",
        detected_columns=columns,
        has_table_structure=True,
        rows=[_row("Горчица", sheet_column_o_quantity=50, visual_row_index=0)],
        order_area_complete=False,
        uncertain_order_row_count=2,
        scan_complete=False,
    )
    kitchen_read = PhotoDocumentObservation(
        document_type_proposal="order_table",
        detected_columns=columns,
        has_table_structure=True,
        rows=[
            _row(
                "Свинина Окорок Тамбовский В/К",
                sheet_column_p_quantity=25,
                visual_row_index=1,
            ),
            _row(
                "Лук жареный Metro Chef",
                sheet_column_p_quantity=2,
                visual_row_index=2,
            ),
        ],
    )
    responses = _SequenceVisionResponses([first, hall_read, bar_read, kitchen_read])
    service = object.__new__(OpenAIService)
    service.settings = settings
    service.tracer = Tracer(settings)
    service.vision_client = SimpleNamespace(responses=responses)
    photo = tmp_path / "lettered-order.png"
    photo.write_bytes(b"image")
    focus = PhotoImageView("table_focus", b"focus", "image/png", 800, 400)
    check_views = tuple(
        PhotoImageView(
            f"department_column_{letter}",
            b"check",
            "image/png",
            400,
            300,
            expected_quantity_cell_count=expected_count,
        )
        for letter, expected_count in zip("nop", (1, 1, 2), strict=True)
    )
    prepare = mocker.patch(
        "restaurant_bot.integrations.openai_client.prepare_photo_views",
        side_effect=[
            PhotoImagePreparation(
                views=(focus,),
                original_width=1000,
                original_height=500,
                upscale_factor=2,
                dense_table_views_used=True,
                spreadsheet_layout_detected=True,
            ),
            PhotoImagePreparation(
                views=(
                    focus,
                    *check_views,
                    PhotoImageView("original", b"raw", "image/png", 1000, 500),
                ),
                original_width=1000,
                original_height=500,
                upscale_factor=2,
                dense_table_views_used=True,
                spreadsheet_layout_detected=True,
            ),
        ],
    )

    result = service.parse_photo(photo, "image/png")

    assert len(responses.calls) == 4
    assert len(result.items) == 3
    assert result.items[0].department_quantities == DepartmentQuantities(bar=50)
    assert result.items[1].department_quantities == DepartmentQuantities(hall=1, kitchen=25)
    assert result.items[2].department_quantities == DepartmentQuantities(kitchen=2)
    prepare.assert_called_with(
        photo,
        "image/png",
        include_original=True,
        prefer_filled_order_rows=True,
        include_department_column_check=True,
    )
    for call, letter in zip(responses.calls[1:], "NOP", strict=True):
        content = call["input"][0]["content"]  # type: ignore[index]
        assert len([part for part in content if part["type"] == "input_image"]) == 1
        assert f"колонка {letter}" in content[0]["text"]


def test_headerless_department_values_are_not_mapped_by_visual_position(
    settings,
    tmp_path: Path,
    mocker,
) -> None:  # type: ignore[no-untyped-def]
    """Оставляет фото без заголовков и букв на ручной выбор подразделения."""
    first = PhotoDocumentObservation(
        document_type_proposal="other",
        has_table_structure=False,
        visible_filled_order_row_count=4,
        rows=[
            _row("Горчица острая", hall_quantity=8),
            _row("Горчица дижонская", bar_quantity=2),
            _row("Лук жареный Metro Chef", kitchen_quantity=5),
        ],
    )
    responses = _SequenceVisionResponses([first])
    service = object.__new__(OpenAIService)
    service.settings = settings
    service.tracer = Tracer(settings)
    service.vision_client = SimpleNamespace(responses=responses)
    photo = tmp_path / "headerless-departments.png"
    photo.write_bytes(b"image")
    prepare = mocker.patch(
        "restaurant_bot.integrations.openai_client.prepare_photo_views",
        return_value=PhotoImagePreparation(
            views=(PhotoImageView("original", b"raw", "image/png", 764, 340),),
            original_width=764,
            original_height=340,
            upscale_factor=1,
            dense_table_views_used=False,
            spreadsheet_layout_detected=True,
            detected_filled_order_row_count=3,
        ),
    )

    result = service.parse_photo(photo, "image/png")

    assert len(responses.calls) == 1
    assert result.items == []
    assert result.photo_outcome == "unsupported_photo"
    prepare.assert_called_once_with(
        photo,
        "image/png",
        include_original=True,
        prefer_filled_order_rows=True,
    )


def test_incomplete_first_photo_read_retries_with_enlarged_and_original_views(
    settings,
    tmp_path: Path,
    mocker,
) -> None:  # type: ignore[no-untyped-def]
    """Повторяет неудачное первое чтение и принимает полное чтение исходного кадра."""
    observation = PhotoDocumentObservation(
        document_type_proposal="client_order_sheet",
        detected_columns=["Product", "Hall", "Bar", "Kitchen", "Comment"],
        has_table_structure=True,
        rows=[_row("Wasabi", hall_quantity=2, kitchen_quantity=3)],
    )
    service = object.__new__(OpenAIService)
    service.settings = settings
    service.tracer = Tracer(settings)
    focus = PhotoImageView("table_focus", b"focus", "image/png", 800, 400)
    original = PhotoImageView("original", b"raw", "image/png", 400, 200)
    mocker.patch(
        "restaurant_bot.integrations.openai_client.prepare_photo_views",
        side_effect=[
            PhotoImagePreparation(
                views=(focus,),
                original_width=400,
                original_height=200,
                upscale_factor=2,
                dense_table_views_used=True,
                spreadsheet_layout_detected=False,
            ),
            PhotoImagePreparation(
                views=(focus, original),
                original_width=400,
                original_height=200,
                upscale_factor=2,
                dense_table_views_used=True,
                spreadsheet_layout_detected=False,
            ),
        ],
    )
    read = mocker.patch.object(
        service,
        "_request_photo_observation",
        side_effect=[(None, False), (observation, True), (observation.model_copy(deep=True), True)],
    )
    photo = tmp_path / "incomplete-first-read.png"
    photo.write_bytes(b"image")

    command = service.parse_photo(photo, "image/png")

    assert command.photo_outcome == ""
    assert command.items[0].department_quantities == DepartmentQuantities(
        hall=2,
        kitchen=3,
    )
    retry_preparation = read.call_args_list[1].args[3]
    assert [view.name for view in retry_preparation.views] == ["table_focus", "original"]
    assert read.call_args_list[1].kwargs["pass_number"] == 2
    assert len(read.call_args_list) == 2
    assert "Повторно внимательно прочитай" in read.call_args_list[1].args[4]


def test_lettered_departments_confirm_full_read_without_detected_grid(
    settings,
    tmp_path: Path,
    mocker,
) -> None:  # type: ignore[no-untyped-def]
    """Требует согласия двух чтений N/O/P, если геометрию колонок выделить нельзя."""
    observation = PhotoDocumentObservation(
        document_type_proposal="order_table",
        detected_columns=["Product", "N", "O", "P", "Q"],
        has_table_structure=True,
        rows=[
            _row("Wasabi", sheet_column_n_quantity=2),
            _row("Mustard", sheet_column_p_quantity=3),
        ],
    )
    service = object.__new__(OpenAIService)
    service.settings = settings
    service.tracer = Tracer(settings)
    focus = PhotoImageView("table_focus", b"focus", "image/png", 800, 400)
    original = PhotoImageView("original", b"raw", "image/png", 400, 200)
    mocker.patch(
        "restaurant_bot.integrations.openai_client.prepare_photo_views",
        side_effect=[
            PhotoImagePreparation(
                views=(focus,),
                original_width=400,
                original_height=200,
                upscale_factor=2,
                dense_table_views_used=True,
                spreadsheet_layout_detected=False,
            ),
            PhotoImagePreparation(
                views=(focus, original),
                original_width=400,
                original_height=200,
                upscale_factor=2,
                dense_table_views_used=True,
                spreadsheet_layout_detected=False,
            ),
        ],
    )
    read = mocker.patch.object(
        service,
        "_request_photo_observation",
        side_effect=[(observation, True), (observation.model_copy(deep=True), True)],
    )
    photo = tmp_path / "lettered-no-grid.png"
    photo.write_bytes(b"image")

    command = service.parse_photo(photo, "image/png")

    assert len(read.call_args_list) == 2
    assert [item.department_quantities for item in command.items] == [
        DepartmentQuantities(hall=2),
        DepartmentQuantities(kitchen=3),
    ]


def test_lettered_full_reads_reject_quantity_shift_to_adjacent_product(
    settings,
    tmp_path: Path,
    mocker,
) -> None:  # type: ignore[no-untyped-def]
    """Не добавляет количество лука, если независимое чтение связывает его с горчицей."""
    columns = ["Product", "N", "O", "P", "Q"]
    first = PhotoDocumentObservation(
        document_type_proposal="order_table",
        detected_columns=columns,
        has_table_structure=True,
        rows=[
            _row("Горчица дижонская большое зерно", sheet_column_o_quantity=6),
            _row("Лук жареный Metro Chef, 600 г"),
        ],
    )
    second = PhotoDocumentObservation(
        document_type_proposal="order_table",
        detected_columns=columns,
        has_table_structure=True,
        rows=[
            _row("Горчица дижонская большое зерно"),
            _row("Лук жареный Metro Chef, 600 г", sheet_column_o_quantity=6),
        ],
    )
    service = object.__new__(OpenAIService)
    service.settings = settings
    service.tracer = Tracer(settings)
    focus = PhotoImageView("table_focus", b"focus", "image/png", 800, 400)
    original = PhotoImageView("original", b"raw", "image/png", 400, 200)
    mocker.patch(
        "restaurant_bot.integrations.openai_client.prepare_photo_views",
        side_effect=[
            PhotoImagePreparation(
                views=(focus,),
                original_width=400,
                original_height=200,
                upscale_factor=2,
                dense_table_views_used=True,
                spreadsheet_layout_detected=False,
            ),
            PhotoImagePreparation(
                views=(focus, original),
                original_width=400,
                original_height=200,
                upscale_factor=2,
                dense_table_views_used=True,
                spreadsheet_layout_detected=False,
            ),
        ],
    )
    read = mocker.patch.object(
        service,
        "_request_photo_observation",
        side_effect=[(first, True), (second, True)],
    )
    photo = tmp_path / "adjacent-product-quantity-shift.png"
    photo.write_bytes(b"image")

    command = service.parse_photo(photo, "image/png")

    assert command.photo_outcome == "incomplete_photo_read"
    assert command.items == []
    assert len(read.call_args_list) == 2


def test_lettered_read_confirmation_keeps_comments_with_their_rows() -> None:
    """Отклоняет повторное чтение, которое переносит комментарий на соседний товар."""
    columns = ["Product", "N", "O", "P", "Q"]
    first = PhotoDocumentObservation(
        detected_columns=columns,
        has_table_structure=True,
        rows=[
            _row("Mustard grain", sheet_column_o_quantity=6),
            _row("Fried onion", sheet_column_q_comment="deliver tomorrow"),
        ],
    )
    second = PhotoDocumentObservation(
        detected_columns=columns,
        has_table_structure=True,
        rows=[
            _row(
                "Mustard grain",
                sheet_column_o_quantity=6,
                sheet_column_q_comment="deliver tomorrow",
            ),
            _row("Fried onion"),
        ],
    )

    assert _lettered_department_reads_match(first, second) is False


def test_complete_department_read_survives_unstable_duplicate_read(
    settings,
    tmp_path: Path,
    mocker,
) -> None:  # type: ignore[no-untyped-def]
    """Сохраняет полный первый результат при расхождении необязательной перепроверки."""
    first = PhotoDocumentObservation(
        document_type_proposal="client_order_sheet",
        detected_columns=["Product", "Hall", "Bar", "Kitchen"],
        has_table_structure=True,
        rows=[_row("Wasabi", hall_quantity=2)],
    )
    second = first.model_copy(
        update={"rows": [_row("Wasabi", bar_quantity=2)]},
        deep=True,
    )
    service = object.__new__(OpenAIService)
    service.settings = settings
    service.tracer = Tracer(settings)
    focus = PhotoImageView("table_focus", b"focus", "image/png", 800, 400)
    original = PhotoImageView("original", b"raw", "image/png", 400, 200)
    mocker.patch(
        "restaurant_bot.integrations.openai_client.prepare_photo_views",
        side_effect=[
            PhotoImagePreparation(
                views=(focus,),
                original_width=400,
                original_height=200,
                upscale_factor=2,
                dense_table_views_used=True,
                spreadsheet_layout_detected=False,
            ),
            PhotoImagePreparation(
                views=(focus, original),
                original_width=400,
                original_height=200,
                upscale_factor=2,
                dense_table_views_used=True,
                spreadsheet_layout_detected=False,
            ),
        ],
    )
    read = mocker.patch.object(
        service,
        "_request_photo_observation",
        side_effect=[(first, True), (second, True)],
    )
    photo = tmp_path / "department-disagreement.png"
    photo.write_bytes(b"image")

    command = service.parse_photo(photo, "image/png")

    assert command.photo_outcome == ""
    assert [item.department_quantities for item in command.items] == [
        DepartmentQuantities(hall=2),
    ]
    assert len(read.call_args_list) == 1


def test_complete_department_read_is_not_replaced_by_inconsistent_second_pass(
    settings,
    tmp_path: Path,
    mocker,
) -> None:  # type: ignore[no-untyped-def]
    """Не отменяет полный первый список из-за более короткого повторного чтения."""
    columns = ["Product", "Hall", "Bar", "Kitchen"]
    wasabi = _row("Wasabi", hall_quantity=2)
    mustard = _row("Mustard grain", bar_quantity=4)
    complete_rows = [wasabi, mustard]
    first = PhotoDocumentObservation(
        document_type_proposal="client_order_sheet",
        detected_columns=columns,
        has_table_structure=True,
        rows=complete_rows,
    )
    second = PhotoDocumentObservation(
        document_type_proposal="client_order_sheet",
        detected_columns=columns,
        has_table_structure=True,
        rows=[wasabi.model_copy(deep=True)],
    )
    service = object.__new__(OpenAIService)
    service.settings = settings
    service.tracer = Tracer(settings)
    focus = PhotoImageView("table_focus", b"focus", "image/png", 800, 400)
    original = PhotoImageView("original", b"raw", "image/png", 400, 200)
    mocker.patch(
        "restaurant_bot.integrations.openai_client.prepare_photo_views",
        side_effect=[
            PhotoImagePreparation(
                views=(focus,),
                original_width=400,
                original_height=200,
                upscale_factor=2,
                dense_table_views_used=True,
                spreadsheet_layout_detected=False,
            ),
            PhotoImagePreparation(
                views=(focus, original),
                original_width=400,
                original_height=200,
                upscale_factor=2,
                dense_table_views_used=True,
                spreadsheet_layout_detected=False,
            ),
        ],
    )
    read = mocker.patch.object(
        service,
        "_request_photo_observation",
        side_effect=[(first, True), (second, True)],
    )
    photo = tmp_path / "partial-department-reread.png"
    photo.write_bytes(b"image")

    command = service.parse_photo(photo, "image/png")

    assert command.photo_outcome == ""
    assert [item.product_query for item in command.items] == ["Wasabi", "Mustard grain"]
    assert [item.department_quantities for item in command.items] == [
        DepartmentQuantities(hall=2),
        DepartmentQuantities(bar=4),
    ]
    assert len(read.call_args_list) == 1


def test_lettered_department_full_read_recovers_ambiguous_column_mapping(
    settings,
    tmp_path: Path,
    mocker,
) -> None:  # type: ignore[no-untyped-def]
    """Заменяет неоднозначное чтение колонки согласованным чтением всей таблицы."""
    columns = ["Наименование", "N", "O", "P", "Q"]
    first = PhotoDocumentObservation(
        document_type_proposal="order_table",
        detected_columns=columns,
        has_table_structure=True,
        rows=[_row("Колбаса Мортадела", sheet_column_o_quantity=30)],
    )
    misaligned_hall_read = PhotoDocumentObservation(
        document_type_proposal="order_table",
        detected_columns=columns,
        has_table_structure=True,
        rows=[_row("Свинина Окорок", sheet_column_n_quantity=30)],
    )
    responses = _SequenceVisionResponses([first, misaligned_hall_read, first.model_copy(deep=True)])
    service = object.__new__(OpenAIService)
    service.settings = settings
    service.tracer = Tracer(settings)
    service.vision_client = SimpleNamespace(responses=responses)
    photo = tmp_path / "misaligned-lettered-order.png"
    photo.write_bytes(b"image")
    focus = PhotoImageView("table_focus", b"focus", "image/png", 800, 400)
    check_views = tuple(
        PhotoImageView(
            f"department_column_{letter}",
            b"check",
            "image/png",
            400,
            300,
            expected_quantity_cell_count=1,
        )
        for letter in "nop"
    )
    mocker.patch(
        "restaurant_bot.integrations.openai_client.prepare_photo_views",
        side_effect=[
            PhotoImagePreparation(
                views=(focus,),
                original_width=1000,
                original_height=500,
                upscale_factor=2,
                dense_table_views_used=True,
                spreadsheet_layout_detected=True,
            ),
            PhotoImagePreparation(
                views=check_views,
                original_width=1000,
                original_height=500,
                upscale_factor=2,
                dense_table_views_used=True,
                spreadsheet_layout_detected=True,
            ),
            PhotoImagePreparation(
                views=(focus, PhotoImageView("original", b"raw", "image/png", 1000, 500)),
                original_width=1000,
                original_height=500,
                upscale_factor=2,
                dense_table_views_used=True,
                spreadsheet_layout_detected=True,
            ),
        ],
    )

    result = service.parse_photo(photo, "image/png")

    assert result.photo_outcome == ""
    assert len(result.items) == 1
    assert result.items[0].department_quantities == DepartmentQuantities(bar=30)
    assert len(responses.calls) == 3


def test_lettered_department_column_read_rejects_missing_visible_quantity() -> None:
    """Не принимает разреженное чтение, если пиксели показывают пропущенное количество."""
    baseline = PhotoDocumentObservation(
        rows=[_row("Горчица дижонская"), _row("Горчица острая")],
    )
    column_read = PhotoDocumentObservation(
        rows=[_row("Горчица дижонская", sheet_column_n_quantity=20)],
    )

    values, rejection_reason = _read_lettered_department_column_values(
        baseline,
        column_read,
        department_field="sheet_column_n_quantity",
        expected_quantity_cell_count=2,
    )

    assert values is None
    assert rejection_reason == "quantity_cell_count_mismatch"


def test_ambiguous_department_crop_keeps_complete_full_image_read(
    settings,
    tmp_path: Path,
    mocker,
) -> None:  # type: ignore[no-untyped-def]
    """Сохраняет полный разбор кадра, если вырезка и повтор не подтверждают колонку."""
    columns = ["Product", "N", "O", "P", "Q"]
    full_read = PhotoDocumentObservation(
        document_type_proposal="order_table",
        detected_columns=columns,
        has_table_structure=True,
        rows=[_row("Wasabi", sheet_column_o_quantity=2)],
    )
    ambiguous_crop = PhotoDocumentObservation(
        document_type_proposal="order_table",
        detected_columns=columns,
        has_table_structure=True,
        rows=[_row("Different product", sheet_column_n_quantity=2)],
    )
    conflicting_full_read = PhotoDocumentObservation(
        document_type_proposal="order_table",
        detected_columns=columns,
        has_table_structure=True,
        rows=[_row("Wasabi", sheet_column_n_quantity=2)],
    )
    responses = _SequenceVisionResponses([full_read, ambiguous_crop, conflicting_full_read])
    service = object.__new__(OpenAIService)
    service.settings = settings
    service.tracer = Tracer(settings)
    service.vision_client = SimpleNamespace(responses=responses)
    photo = tmp_path / "ambiguous-department-crop.png"
    photo.write_bytes(b"image")
    focus = PhotoImageView("table_focus", b"focus", "image/png", 800, 400)
    check_views = tuple(
        PhotoImageView(
            f"department_column_{letter}",
            b"check",
            "image/png",
            400,
            300,
            expected_quantity_cell_count=1,
        )
        for letter in "nop"
    )
    mocker.patch(
        "restaurant_bot.integrations.openai_client.prepare_photo_views",
        side_effect=[
            PhotoImagePreparation(
                views=(focus,),
                original_width=1000,
                original_height=500,
                upscale_factor=2,
                dense_table_views_used=True,
                spreadsheet_layout_detected=True,
            ),
            PhotoImagePreparation(
                views=check_views,
                original_width=1000,
                original_height=500,
                upscale_factor=2,
                dense_table_views_used=True,
                spreadsheet_layout_detected=True,
            ),
            PhotoImagePreparation(
                views=(focus, PhotoImageView("original", b"raw", "image/png", 1000, 500)),
                original_width=1000,
                original_height=500,
                upscale_factor=2,
                dense_table_views_used=True,
                spreadsheet_layout_detected=True,
            ),
        ],
    )

    command = service.parse_photo(photo, "image/png")

    assert command.photo_outcome == ""
    assert len(command.items) == 1
    assert command.items[0].department_quantities == DepartmentQuantities(bar=2)
    assert len(responses.calls) == 3


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


def test_preprocessed_row_count_returns_confirmed_partial_read_with_warning(
    settings, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """Помечает совпавшую часть как неполную, если счётчик указывает на пропуск строки."""
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
    assert result.photo_outcome == "partial_photo_read"
    assert [(item.product_query, item.quantity) for item in result.items] == [
        ("Судак Филе охл 500+ Крупный", 6)
    ]


def test_uncertain_photo_observation_retries_with_full_frame_when_no_safe_crop_exists(
    settings, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """Повторно проверяет неопределённость полным кадром без неподтверждённого кропа."""
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
    assert len([part for part in retry_content if part["type"] == "input_image"]) == 1
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


def test_headerless_grid_retries_with_column_views_without_guessing_department(
    settings,
    tmp_path: Path,
    mocker,
) -> None:  # type: ignore[no-untyped-def]
    """Суммирует видимые ячейки без шапки и оставляет отдел для выбора пользователю."""
    first = PhotoDocumentObservation(
        document_type_proposal="order_table",
        has_table_structure=True,
        rows=[_row("Горчица"), _row("Лук")],
    )
    second = PhotoDocumentObservation(
        document_type_proposal="order_table",
        has_table_structure=True,
        rows=[
            _row("Горчица", explicit_order_quantity=8, order_entry_text="8"),
            _row("Лук", explicit_order_quantity=3, order_entry_text="3"),
        ],
    )
    service = object.__new__(OpenAIService)
    service.settings = settings
    service.tracer = Tracer(settings)
    focus = PhotoImageView("table_focus", b"focus", "image/png", 800, 400)
    checks = tuple(
        PhotoImageView(f"department_column_{letter}", b"check", "image/png", 300, 400, 1)
        for letter in "nop"
    )
    original = PhotoImageView("original", b"raw", "image/png", 400, 200)
    mocker.patch(
        "restaurant_bot.integrations.openai_client.prepare_photo_views",
        side_effect=[
            PhotoImagePreparation(
                views=(focus, original),
                original_width=400,
                original_height=200,
                upscale_factor=2,
                dense_table_views_used=True,
                spreadsheet_layout_detected=True,
                detected_filled_order_row_count=2,
            ),
            PhotoImagePreparation(
                views=(focus, *checks, original),
                original_width=400,
                original_height=200,
                upscale_factor=2,
                dense_table_views_used=True,
                spreadsheet_layout_detected=True,
                detected_filled_order_row_count=2,
            ),
        ],
    )
    read = mocker.patch.object(
        service,
        "_request_photo_observation",
        side_effect=[(first, True), (second, True)],
    )
    photo = tmp_path / "headerless-grid.png"
    photo.write_bytes(b"image")

    result = service.parse_photo(photo, "image/png")

    assert [(item.product_query, item.quantity) for item in result.items] == [
        ("Горчица", 8),
        ("Лук", 3),
    ]
    assert all(item.department == settings.default_department for item in result.items)
    assert len(read.call_args_list) == 2
    assert "Названия колонок на этом кадре не видны" in read.call_args_list[1].args[4]


def test_lettered_grid_accepts_complete_read_when_pixel_detector_returns_zero(
    settings,
    tmp_path: Path,
    mocker,
) -> None:  # type: ignore[no-untyped-def]
    """Не отвергает N/O/P, когда геометрия сетки не подтвердила тёмные цифры."""
    observation = PhotoDocumentObservation(
        document_type_proposal="order_table",
        detected_columns=["Product", "N", "O", "P", "Q"],
        has_table_structure=True,
        rows=[_row("Утка", sheet_column_p_quantity=3)],
    )
    service = object.__new__(OpenAIService)
    service.settings = settings
    service.tracer = Tracer(settings)
    focus = PhotoImageView("table_focus", b"focus", "image/png", 800, 400)
    original = PhotoImageView("original", b"raw", "image/png", 400, 200)
    mocker.patch(
        "restaurant_bot.integrations.openai_client.prepare_photo_views",
        return_value=PhotoImagePreparation(
            views=(focus, original),
            original_width=400,
            original_height=200,
            upscale_factor=2,
            dense_table_views_used=True,
            spreadsheet_layout_detected=True,
            detected_filled_order_row_count=0,
        ),
    )
    read = mocker.patch.object(
        service, "_request_photo_observation", return_value=(observation, True)
    )
    photo = tmp_path / "lettered-zero-pixels.png"
    photo.write_bytes(b"image")

    result = service.parse_photo(photo, "image/png")

    assert result.photo_outcome == ""
    assert result.items[0].department_quantities == DepartmentQuantities(kitchen=3)
    assert len(read.call_args_list) == 1


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


def test_incomplete_structured_vision_response_retries_then_fails_closed(
    settings, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """Повторяет неполный structured response и не принимает неполный повтор."""
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
    assert len(responses.calls) == 2
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


def test_authoritative_sheet_row_missing_from_catalog_falls_back_to_exact_name(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    """Не показывает пустой выбор, когда номер строки не совпал с каталогом."""
    catalog = [
        CatalogProduct(
            product_id="dijon",
            name="Горчица дижонская",
            unit="шт",
            row_number=8,
        ),
        CatalogProduct(
            product_id="dijon-grain",
            name="Горчица дижонская большое зерно",
            unit="шт",
            row_number=9,
        ),
    ]
    result = ConversationEngine(settings).handle(
        _photo_event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(
                    product_query="Горчица дижонская",
                    quantity=5,
                    unit="шт",
                    quantity_source="department_columns",
                    department_quantities=DepartmentQuantities(kitchen=5),
                    catalog_identity_provenance="venue_table_exact_candidate",
                    photo_sheet_row_number=5,
                    photo_sheet_row_number_confidence=1,
                    photo_sheet_row_number_authoritative=True,
                )
            ],
        ),
        ConversationState(),
        catalog,
    )

    item = result.state.cart[0]
    assert item.status is ItemStatus.MATCHED
    assert item.catalog_product_id == "dijon"
    assert item.catalog_name == "Горчица дижонская"


def test_photo_identity_does_not_merge_dijon_and_grain_chatel(settings) -> None:  # type: ignore[no-untyped-def]
    """Не считает «дижонская» и «зернистая» одним товаром из-за общих слов."""
    catalog = [
        CatalogProduct(
            product_id="grain-chatel",
            name="Горчица Зернистая CHATEL, ведро, 1 кг, 6 шт/кор, Франция",
            unit="шт",
            row_number=20,
        ),
        CatalogProduct(
            product_id="dijon-chatel",
            name="Горчица Дижонская CHATEL, ведро, 1 кг, 6 шт/кор, Франция",
            unit="шт",
            row_number=21,
        ),
    ]
    result = ConversationEngine(settings).handle(
        _photo_event(),
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(
                    product_query="Горчица Дижонская CHATEL, ведро, 1 кг, 6 шт/кор, Франция",
                    quantity=5,
                    unit="шт",
                    quantity_source="department_columns",
                    department_quantities=DepartmentQuantities(kitchen=5),
                    catalog_identity_provenance="venue_table_exact_candidate",
                    photo_sheet_row_number=20,
                    photo_sheet_row_number_confidence=1,
                    photo_sheet_row_number_authoritative=True,
                )
            ],
        ),
        ConversationState(),
        catalog,
    )

    item = result.state.cart[0]
    assert item.status is ItemStatus.MATCHED
    assert item.catalog_product_id == "dijon-chatel"
    assert item.catalog_name == "Горчица Дижонская CHATEL, ведро, 1 кг, 6 шт/кор, Франция"


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
