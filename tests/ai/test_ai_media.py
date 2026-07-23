from pathlib import Path
from types import SimpleNamespace

import pytest

from restaurant_bot.domain.models import ExtractedItem, Intent, ParsedCommand
from restaurant_bot.integrations import openai_client
from restaurant_bot.integrations.openai_client import OpenAIService, ParsedInputSchema


class _Transcriptions:
    def __init__(self, responses: list[str]):
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        return SimpleNamespace(text=self.responses.pop(0))


def test_vision_client_uses_long_timeout_without_hidden_retries(settings, mocker) -> None:  # type: ignore[no-untyped-def]
    """Даёт большому фото время на один ответ без повторной отправки."""
    client_factory = mocker.patch.object(openai_client, "OpenAI")

    OpenAIService(settings)

    assert client_factory.call_args_list[1].kwargs == {
        "api_key": settings.openai_api_key.get_secret_value(),
        "timeout": settings.openai_vision_timeout_seconds,
        "max_retries": 0,
    }


def test_openai_input_schema_has_fixed_department_quantity_fields() -> None:
    schema = ParsedInputSchema.model_json_schema()
    extracted_item = schema["$defs"]["ExtractedItem"]
    quantities = schema["$defs"]["DepartmentQuantities"]

    assert "additionalProperties" not in extracted_item["properties"]["department_quantities"]
    assert set(quantities["properties"]) == {"hall", "bar", "kitchen"}


def test_photo_uses_dedicated_vision_client(settings, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Направляет фото в клиент с отдельным длительным таймаутом."""
    parsed = ParsedInputSchema(intent=Intent.ADD_ITEMS)
    service = object.__new__(OpenAIService)
    service.settings = settings
    service.client = SimpleNamespace(responses=_FailingResponses())
    service.vision_client = SimpleNamespace(responses=_Responses(parsed))
    photo = tmp_path / "order.jpg"
    photo.write_bytes(b"image")

    result = service.parse_photo(photo, "image/jpeg")

    assert result.intent is Intent.ADD_ITEMS


class _Responses:
    def __init__(self, parsed):  # type: ignore[no-untyped-def]
        self.parsed = parsed
        self.calls: list[dict[str, object]] = []

    def parse(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        return SimpleNamespace(output_parsed=self.parsed)


class _FailingResponses:
    def parse(self, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("OpenAI HTTP 429")


def _service(settings, client):  # type: ignore[no-untyped-def]
    service = object.__new__(OpenAIService)
    service.settings = settings
    service.client = client
    service.vision_client = client
    return service


def test_voice_transcription_uses_fallback_when_primary_returns_empty(
    settings, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    transcriptions = _Transcriptions(["", "сироп роза десять штук"])
    service = _service(
        settings, SimpleNamespace(audio=SimpleNamespace(transcriptions=transcriptions))
    )
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"audio")

    assert service.transcribe(audio) == "сироп роза десять штук"
    assert [call["model"] for call in transcriptions.calls] == [
        settings.openai_transcribe_model,
        settings.openai_transcribe_fallback_model,
    ]
    assert all(call["language"] == "ru" for call in transcriptions.calls)


def test_voice_transcription_receives_context_prompt(settings, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    transcriptions = _Transcriptions(["первый вариант"])
    service = _service(
        settings, SimpleNamespace(audio=SimpleNamespace(transcriptions=transcriptions))
    )
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"audio")

    assert service.transcribe(audio, prompt="Выберите: Сироп Роза") == "первый вариант"
    assert transcriptions.calls[0]["prompt"] == "Выберите: Сироп Роза"


def test_high_accuracy_voice_transcription_uses_the_stronger_model(
    settings, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    transcriptions = _Transcriptions(["добавить товары"])
    service = _service(
        settings, SimpleNamespace(audio=SimpleNamespace(transcriptions=transcriptions))
    )
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"audio")

    assert service.transcribe(audio, high_accuracy=True) == "добавить товары"
    assert transcriptions.calls[0]["model"] == settings.openai_transcribe_fallback_model


def test_photo_parser_passes_caption_and_high_detail_image_as_structured_input(
    settings, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    parsed = ParsedInputSchema(intent=Intent.ADD_ITEMS)
    responses = _Responses(parsed)
    service = _service(settings, SimpleNamespace(responses=responses))
    photo = tmp_path / "order.jpg"
    photo.write_bytes(b"image")

    result = service.parse_photo(photo, "image/jpeg", "заказ кухни")

    assert result.intent is Intent.ADD_ITEMS
    call = responses.calls[0]
    assert call["model"] == settings.openai_vision_model
    content = call["input"][0]["content"]  # type: ignore[index]
    assert content[0] == {"type": "input_text", "text": "заказ кухни"}
    assert content[1]["type"] == "input_image"
    assert content[1]["image_url"].startswith("data:image/jpeg;base64,")
    assert content[1]["detail"] == "high"


def test_catalog_matcher_uses_only_structured_candidate_decision(settings) -> None:  # type: ignore[no-untyped-def]
    parsed = SimpleNamespace(
        action="select", selected_product_id="rose", candidate_product_ids=["rose"], reason="exact"
    )
    responses = _Responses(parsed)
    service = _service(settings, SimpleNamespace(responses=responses))

    result = service.choose_catalog_candidate(
        "сироп роза", [{"product_id": "rose", "name": "Сироп Роза"}]
    )

    assert result.selected_product_id == "rose"
    call = responses.calls[0]
    assert call["model"] == settings.openai_match_model
    assert call["text_format"].__name__ == "ProductMatchDecision"


def test_ai_item_supplier_comment_is_preserved_as_working_item_comment(settings) -> None:  # type: ignore[no-untyped-def]
    parsed = ParsedInputSchema(
        intent=Intent.ADD_ITEMS,
        global_comment="на завтра",
        items=[
            ExtractedItem(
                product_query="Сироп роза",
                quantity=5,
                unit="шт",
                user_comment_to_supplier="охлаждённым",
                source_line="Сироп роза 5 шт",
            )
        ],
    )
    service = _service(settings, SimpleNamespace(responses=_Responses(parsed)))

    command = service.parse_text("Сироп роза 5 шт")

    assert command.global_comment == "на завтра"
    assert command.items[0].comment == "охлаждённым"
    assert command.items[0].user_comment_to_supplier == "охлаждённым"


def test_ai_cannot_turn_a_product_name_into_add_more_navigation(settings) -> None:  # type: ignore[no-untyped-def]
    """Сохраняет название товара, ошибочно признанное ИИ навигацией."""
    parsed = ParsedInputSchema(intent=Intent.ADD_MORE)
    service = _service(settings, SimpleNamespace(responses=_Responses(parsed)))

    command = service.parse_text("сироп роза")

    assert command.intent is Intent.ADD_ITEMS
    assert len(command.items) == 1
    assert command.items[0].product_query == "сироп роза"
    assert command.items[0].quantity is None


def test_simple_explicit_product_list_skips_the_second_ai_call(settings) -> None:  # type: ignore[no-untyped-def]
    service = _service(settings, SimpleNamespace(responses=_FailingResponses()))

    command = service.parse_text("Сироп роза 10 штук, говядина 5 килограмм")

    assert command.intent is Intent.ADD_ITEMS
    assert [(item.product_query, item.quantity, item.unit) for item in command.items] == [
        ("Сироп роза", 10, "шт"),
        ("говядина", 5, "кг"),
    ]


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("Давай пойдём и добавим товары", Intent.ADD_MORE),
        ("Давай добавим товаров", Intent.ADD_MORE),
        ("Давай посмотрим наши статусы", Intent.ORDER_STATUS),
    ],
)
def test_free_form_navigation_skips_ai_product_extraction(
    settings, text: str, intent: Intent
) -> None:  # type: ignore[no-untyped-def]
    """Не позволяет разговорной команде превратиться в товар каталога."""
    service = _service(settings, SimpleNamespace(responses=_FailingResponses()))

    command = service.parse_text(text)

    assert command.intent is intent
    assert command.items == []


@pytest.mark.parametrize(
    "text",
    [
        "Сироп роза 10 штук только охлажденный, говядина 5 кг",
        "Сироп Роза 1 л 10 штук, говядина 5 кг",
        "10 штук сироп роза, 5 кг говядина",
    ],
)
def test_ambiguous_product_lists_still_use_structured_ai(settings, text: str) -> None:  # type: ignore[no-untyped-def]
    service = _service(settings, SimpleNamespace(responses=_FailingResponses()))

    with pytest.raises(RuntimeError, match="OpenAI HTTP 429"):
        service.parse_text(text)


def test_openai_parse_error_is_not_silently_replaced_with_an_empty_command(settings) -> None:  # type: ignore[no-untyped-def]
    service = _service(settings, SimpleNamespace(responses=_FailingResponses()))

    with pytest.raises(RuntimeError, match="OpenAI HTTP 429"):
        service.parse_text("Мисо-паста Genzo 1 кг")


def test_client_order_sheet_uses_the_selected_department_quantity(settings) -> None:  # type: ignore[no-untyped-def]
    service = _service(settings, SimpleNamespace())
    command = service._normalise_photo_command(
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(
                    product_query="Сироп Роза, 1л",
                    quantity=None,
                    unit="шт",
                    source_department="Кухня",
                    department_quantities={"hall": None, "bar": None, "kitchen": 5},
                    quantity_source="printed_order_column",
                )
            ],
        ),
        "client_order_sheet",
    )

    assert command.items[0].department == "Кухня"
    assert command.items[0].quantity == 5


def test_client_order_sheet_sums_only_filled_department_order_cells(settings) -> None:  # type: ignore[no-untyped-def]
    """Суммирует фактический заказ из колонок подразделений для текущего пилота."""
    service = _service(settings, SimpleNamespace())
    command = service._normalise_photo_command(
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(
                    product_query="Сливки",
                    quantity=99,
                    unit="шт",
                    department_quantities={"hall": 2, "bar": None, "kitchen": 3},
                    quantity_source="printed_order_column",
                )
            ],
        ),
        "client_order_sheet",
    )

    assert command.items[0].department == settings.default_department
    assert command.items[0].quantity == 5


def test_client_order_sheet_drops_row_without_department_order_quantity(settings) -> None:  # type: ignore[no-untyped-def]
    """Не принимает фасовку строки за заказ при пустых колонках подразделений."""
    service = _service(settings, SimpleNamespace())
    command = service._normalise_photo_command(
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(
                    product_query="Сыр Пармезан",
                    quantity=5,
                    unit="кг",
                    department_quantities={"hall": None, "bar": None, "kitchen": None},
                    quantity_source="printed_reference",
                    printed_reference_text="5 кг",
                )
            ],
        ),
        "client_order_sheet",
    )

    assert command.items == []


def test_regular_order_table_keeps_its_explicit_order_quantity(settings) -> None:  # type: ignore[no-untyped-def]
    """Не требует колонок подразделений от обычной таблицы заказа."""
    service = _service(settings, SimpleNamespace())
    command = service._normalise_photo_command(
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(
                    product_query="Морковь",
                    quantity=4,
                    unit="кг",
                    order_entry_text="4",
                    order_entry_type="typed_order_entry",
                    quantity_source="printed_order_column",
                )
            ],
        ),
        "order_table",
    )

    assert command.items[0].quantity == 4


def test_misclassified_order_table_drops_printed_packaging_without_order_cell(settings) -> None:  # type: ignore[no-untyped-def]
    """Не пропускает фасовку, даже если бланк ошибочно назван обычной таблицей."""
    service = _service(settings, SimpleNamespace())
    command = service._normalise_photo_command(
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(
                    product_query="Сосиски Боварские",
                    quantity=3,
                    unit="кг",
                    order_entry_text="",
                    order_entry_type="",
                    quantity_source="printed_order_column",
                    printed_reference_text="3 кг",
                ),
                ExtractedItem(
                    product_query="Гребешки мелкие гц",
                    quantity=2,
                    unit="кг",
                    order_entry_text="",
                    order_entry_type="",
                    quantity_source="printed_order_column",
                    printed_reference_text="2 кг",
                ),
            ],
        ),
        "order_table",
    )

    assert command.items == []


def test_unknown_document_drops_printed_column_without_proven_order_cell(settings) -> None:  # type: ignore[no-untyped-def]
    """Защищает от фасовки при ошибке классификации типа фотографии."""
    service = _service(settings, SimpleNamespace())
    command = service._normalise_photo_command(
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(
                    product_query="Осьминог/батат фри",
                    quantity=8,
                    unit="кг",
                    quantity_source="printed_order_column",
                )
            ],
        ),
        "unknown",
    )

    assert command.items == []


def test_supplier_form_keeps_only_filled_printed_or_handwritten_order_cells(settings) -> None:  # type: ignore[no-untyped-def]
    """Отличает фасовку от заполненной ячейки количества заказа."""
    service = _service(settings, SimpleNamespace())
    command = service._normalise_photo_command(
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(
                    product_query="Пепперони",
                    quantity=2,
                    unit="кг",
                    quantity_source="printed_order_column",
                ),
                ExtractedItem(
                    product_query="Осьминог фри",
                    quantity=8,
                    unit="кг",
                    order_entry_text="4",
                    order_entry_type="handwritten_correction",
                    quantity_source="printed_order_column",
                ),
                ExtractedItem(
                    product_query="Сыр Пармезан",
                    quantity=5,
                    unit="кг",
                    order_entry_text="5 кг",
                    order_entry_type="typed_order_entry",
                    quantity_source="printed_order_column",
                ),
            ],
        ),
        "printed_order_form",
    )

    assert [(item.product_query, item.quantity) for item in command.items] == [
        ("Осьминог фри", 4),
        ("Сыр Пармезан", 5),
    ]


def test_photo_drops_every_item_without_actual_order_quantity(settings) -> None:  # type: ignore[no-untyped-def]
    """Не переносит в черновик строки каталога без количества заказа."""
    service = _service(settings, SimpleNamespace())
    command = service._normalise_photo_command(
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(product_query="Картофель", quantity=None, unit="кг"),
                ExtractedItem(
                    product_query="Масло",
                    quantity=1,
                    unit="кг",
                    quantity_source="packaging",
                ),
            ],
        ),
        "unknown_document",
    )

    assert command.items == []


def test_handwritten_replacement_wins_over_crossed_out_client_sheet_quantity(settings) -> None:  # type: ignore[no-untyped-def]
    service = _service(settings, SimpleNamespace())
    command = service._normalise_photo_command(
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(
                    product_query="Сироп Роза, 1л",
                    quantity=12,
                    unit="шт",
                    source_department="Кухня",
                    department_quantities={"kitchen": 7},
                    quantity_source="handwritten_correction",
                )
            ],
        ),
        "client_order_sheet",
    )

    assert command.items[0].quantity == 7
