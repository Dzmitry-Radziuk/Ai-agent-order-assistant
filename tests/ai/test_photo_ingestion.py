"""Проверяет детерминированную авторизацию строк и идентичность фото-каталога."""

from pathlib import Path
from types import SimpleNamespace

from restaurant_bot.domain.models import (
    CatalogProduct,
    ConversationState,
    InputKind,
    ItemStatus,
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
    assert "items" not in schema["properties"]


class _VisionResponses:
    """Возвращает один структурированный ответ vision без второго запроса."""

    def __init__(self, observation: PhotoDocumentObservation) -> None:
        """Сохраняет observation и считает вызовы vision endpoint."""
        self.observation = observation
        self.calls: list[dict[str, object]] = []

    def parse(self, **kwargs: object) -> SimpleNamespace:
        """Имитирует один вызов structured vision response."""
        self.calls.append(kwargs)
        return SimpleNamespace(output_parsed=self.observation)


def test_photo_classification_uses_department_structure_over_model_proposal() -> None:
    """Классифицирует таблицу по колонкам даже при ошибочном proposal модели."""
    observation = PhotoDocumentObservation(
        document_type_proposal="order_table",
        detected_columns=["Товар", "Зал", "Бар", "Кухня"],
        has_table_structure=True,
        rows=[_row("Васаби", kitchen_quantity=3)],
    )

    assert classify_photo_document(observation) == "client_order_sheet"


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


def test_photo_identity_normalization_preserves_full_identity() -> None:
    """Сближает только безопасную пунктуацию и пробелы, не удаляя характеристики."""
    left = 'Васаби TM "Sango", 1кг, 10 шт/кор, Китай'
    right = "васаби TM “Sango”, 1 кг, 10 шт/кор, Китай"

    assert canonical_photo_identity(left) == canonical_photo_identity(right)
    assert canonical_photo_identity(left) != canonical_photo_identity(
        left.replace("Китай", "Франция")
    )
