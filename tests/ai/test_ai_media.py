"""Проверяет поведение, связанное с модулем «test ai media»."""

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError

from restaurant_bot.domain.history import HistoryQuery, HistoryQuestionType
from restaurant_bot.domain.models import ExtractedItem, Intent, ParsedCommand
from restaurant_bot.integrations import openai_client
from restaurant_bot.integrations.openai_client import (
    CommentScopeDecision,
    OpenAIService,
    ParsedInputSchema,
    VisibleActionDecision,
    restore_explicit_order_terms,
)
from restaurant_bot.observability import Tracer


class _Transcriptions:
    """Имитирует endpoint транскрипции OpenAI в тестах."""

    def __init__(self, responses: list[str]):
        """Инициализирует тестовый двойник зависимости."""
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs):  # type: ignore[no-untyped-def]
        """Имитирует создание результата транскрипции."""
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


def test_text_client_uses_bounded_timeout_without_hidden_retries(settings, mocker) -> None:  # type: ignore[no-untyped-def]
    """Не позволяет текстовому запросу блокировать чат повторными ожиданиями."""
    client_factory = mocker.patch.object(openai_client, "OpenAI")

    OpenAIService(settings)

    assert client_factory.call_args_list[0].kwargs == {
        "api_key": settings.openai_api_key.get_secret_value(),
        "timeout": settings.openai_text_timeout_seconds,
        "max_retries": settings.openai_text_max_retries,
    }


def test_openai_input_schema_has_fixed_department_quantity_fields() -> None:
    """Проверяет, что openai ввод схема имеет фиксированные подразделение количество fields."""
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
    service.tracer = Tracer(settings)
    service.client = SimpleNamespace(responses=_FailingResponses())
    service.vision_client = SimpleNamespace(responses=_Responses(parsed))
    photo = tmp_path / "order.jpg"
    photo.write_bytes(b"image")

    result = service.parse_photo(photo, "image/jpeg")

    assert result.intent is Intent.ADD_ITEMS


class _Responses:
    """Имитирует endpoint структурированных ответов OpenAI в тестах."""

    def __init__(self, parsed):  # type: ignore[no-untyped-def]
        """Инициализирует тестовый двойник зависимости."""
        self.parsed = parsed
        self.calls: list[dict[str, object]] = []

    def parse(self, **kwargs):  # type: ignore[no-untyped-def]
        """Имитирует разбор структурированного ответа OpenAI."""
        self.calls.append(kwargs)
        return SimpleNamespace(output_parsed=self.parsed)


class _SequenceResponses:
    """Возвращает отдельный структурированный ответ для каждого чанка списка."""

    def __init__(self, parsed: list[ParsedInputSchema]):
        """Инициализирует последовательность ответов."""
        self.parsed = parsed
        self.calls: list[dict[str, object]] = []

    def parse(self, **kwargs):  # type: ignore[no-untyped-def]
        """Сохраняет вход чанка и возвращает следующий ответ ИИ."""
        self.calls.append(kwargs)
        return SimpleNamespace(output_parsed=self.parsed.pop(0))


class _FailingResponses:
    """Имитирует ошибку endpoint структурированных ответов OpenAI."""

    def parse(self, **kwargs):  # type: ignore[no-untyped-def]
        """Имитирует разбор структурированного ответа OpenAI."""
        raise RuntimeError("OpenAI HTTP 429")


class _TimeoutResponses:
    """Имитирует тайм-аут endpoint структурированных ответов OpenAI."""

    def parse(self, **kwargs):  # type: ignore[no-untyped-def]
        """Имитирует разбор структурированного ответа OpenAI."""
        raise APITimeoutError(httpx.Request("POST", "https://api.openai.test/responses"))


def _service(settings, client):  # type: ignore[no-untyped-def]
    """Создаёт настроенный тестовый экземпляр сервиса."""
    service = object.__new__(OpenAIService)
    service.settings = settings
    service.tracer = Tracer(settings)
    service.client = client
    service.vision_client = client
    return service


def test_openai_usage_is_normalized_for_langfuse() -> None:
    """Передаёт Langfuse непересекающиеся бакеты входа, выхода и итога."""
    response = SimpleNamespace(
        usage={
            "type": "tokens",
            "input_tokens": 14,
            "input_token_details": {"audio_tokens": 14, "text_tokens": 0},
            "output_tokens": 45,
            "total_tokens": 59,
        }
    )

    assert OpenAIService._usage_details(response) == {
        "input": 14,
        "output": 45,
        "total": 59,
    }


def test_openai_usage_separates_discounted_cached_tokens() -> None:
    """Исключает кэшированные токены из обычного входного бакета."""
    response = SimpleNamespace(
        usage={
            "input_tokens": 100,
            "input_tokens_details": {"cached_tokens": 20},
            "output_tokens": 10,
            "total_tokens": 110,
        }
    )

    assert OpenAIService._usage_details(response) == {
        "input": 80,
        "input_cached_tokens": 20,
        "output": 10,
        "total": 110,
    }


@pytest.mark.parametrize(
    ("include_user_content", "expected_output"),
    [
        (False, {"transcript_characters": 33}),
        (
            True,
            {
                "transcript_characters": 33,
                "transcript": "Мне нужна кукуруза два килограмма",
            },
        ),
    ],
)
def test_voice_transcription_trace_respects_user_content_setting(
    settings,
    tmp_path: Path,
    mocker,
    include_user_content: bool,
    expected_output: dict[str, object],
) -> None:  # type: ignore[no-untyped-def]
    """Показывает расшифровку в Langfuse только при разрешённом логировании."""
    configured_settings = settings.model_copy(update={"log_user_content": include_user_content})
    transcriptions = _Transcriptions(["Мне нужна кукуруза два килограмма"])
    service = _service(
        configured_settings,
        SimpleNamespace(audio=SimpleNamespace(transcriptions=transcriptions)),
    )
    generation = mocker.Mock()
    service.tracer = mocker.MagicMock()
    service.tracer.generation.return_value.__enter__.return_value = generation
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"audio")

    assert service.transcribe(audio) == "Мне нужна кукуруза два килограмма"
    assert generation.update.call_args.kwargs["output"] == expected_output


def test_voice_transcription_uses_fallback_when_primary_returns_empty(
    settings, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что голос распознавание голоса использует резервную модель когда основная модель возвращает пустой результат."""
    settings = settings.model_copy(
        update={"openai_transcribe_fallback_model": "gpt-4o-mini-transcribe"}
    )
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


def test_voice_transcription_does_not_repeat_the_same_model(settings, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Не делает второй дорогой запрос, если резервная модель совпадает с основной."""
    transcriptions = _Transcriptions([""])
    service = _service(
        settings, SimpleNamespace(audio=SimpleNamespace(transcriptions=transcriptions))
    )
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"audio")

    assert service.transcribe(audio) == ""
    assert len(transcriptions.calls) == 1


def test_voice_transcription_receives_context_prompt(settings, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что распознавание голоса получает контекстную инструкцию модели."""
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
    """Проверяет, что точное распознавание голоса использует усиленную модель."""
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
    """Проверяет, что photo парсер передаёт подпись и повышенная detail изображение как структурированный ввод."""
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
    """Проверяет, что каталог сопоставление использует только структурированный кандидат decision."""
    parsed = SimpleNamespace(
        action="select",
        selected_product_id="rose",
        candidate_product_ids=["rose"],
        confidence=0.98,
        contradictions=[],
        reason="exact",
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


def test_visible_action_ai_returns_only_an_allowed_confident_action(settings) -> None:  # type: ignore[no-untyped-def]
    """Возвращает только уверенно выбранную кнопку текущего экрана."""
    responses = _Responses(
        VisibleActionDecision(
            action_id="v2:mulone:r7",
            confidence=0.93,
            reason="Пользователь просит выбрать количество",
        )
    )
    service = _service(settings, SimpleNamespace(responses=responses))

    selected = service.choose_visible_action(
        "Давайте выберем другое количество",
        "Проверьте количество",
        [
            {"label": "Выбрать количество", "action_id": "v2:mulone:r7"},
            {"label": "К черновику", "action_id": "v2:back:r7"},
        ],
    )

    assert selected == "v2:mulone:r7"
    assert responses.calls[0]["text_format"] is VisibleActionDecision


@pytest.mark.parametrize(
    "decision",
    [
        VisibleActionDecision(action_id="v2:clear:r7", confidence=0.99),
        VisibleActionDecision(action_id="v2:mulone:r7", confidence=0.8),
        VisibleActionDecision(action_id="v2:mulone:r7", confidence=0.5),
    ],
)
def test_visible_action_ai_rejects_hidden_or_uncertain_action(
    settings,
    decision: VisibleActionDecision,
) -> None:  # type: ignore[no-untyped-def]
    """Не выполняет скрытое или неуверенно распознанное действие."""
    service = _service(
        settings,
        SimpleNamespace(responses=_Responses(decision)),
    )

    assert (
        service.choose_visible_action(
            "сделай что-нибудь",
            "Проверьте количество",
            [{"label": "Выбрать количество", "action_id": "v2:mulone:r7"}],
        )
        == ""
    )


def test_ai_invented_supplier_comment_is_not_preserved(settings) -> None:  # type: ignore[no-untyped-def]
    """Не сохраняет комментарий поставщику, которого не было в исходной фразе."""
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

    command = service.parse_text("Сироп роза 5 шт. Всё на завтра")

    assert command.global_comment == "на завтра"
    assert command.items[0].comment == ""
    assert command.items[0].user_comment_to_supplier == ""


def test_ai_history_payload_is_normalized_to_read_only_command(settings) -> None:  # type: ignore[no-untyped-def]
    """Оставляет history_query отдельной командой только для чтения при лишних items."""
    parsed = ParsedInputSchema(
        intent=Intent.ADD_ITEMS,
        history_query=HistoryQuery(
            product_queries=["говядину"],
            question_type=HistoryQuestionType.CURRENT_STATUS,
        ),
        items=[ExtractedItem(product_query="говядина", quantity=5, unit="кг")],
    )
    service = _service(settings, SimpleNamespace(responses=_Responses(parsed)))

    command = service._parse_text_once("Есть ли информация насчёт говядины")

    assert command.intent is Intent.HISTORY_QUERY
    assert command.history_query is not None
    assert command.items == []


def test_parse_text_runtime_preserves_explicit_quantity_over_packaging(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет через runtime, что фасовка не меняет количество заказа."""
    source = (
        "\u0413\u043e\u0440\u0447\u0438\u0446\u0430 \u0437\u0435\u0440\u043d\u0438\u0441\u0442\u0430\u044f Chatel \u0432\u0435\u0434\u0440\u043e 1 \u043a\u0433, "
        "6 \u0448\u0442\u0443\u043a \u0432 \u043a\u043e\u0440\u043e\u0431\u043a\u0435, \u0424\u0440\u0430\u043d\u0446\u0438\u044f, \u043c\u043d\u0435 \u043d\u0443\u0436\u043d\u043e 22 \u0448\u0442\u0443\u043a\u0438."
    )
    parsed = ParsedInputSchema(
        intent=Intent.ADD_ITEMS,
        items=[
            ExtractedItem(
                product_query="\u0413\u043e\u0440\u0447\u0438\u0446\u0430 \u0437\u0435\u0440\u043d\u0438\u0441\u0442\u0430\u044f Chatel \u0432\u0435\u0434\u0440\u043e 1 \u043a\u0433",
                quantity=22,
                unit="\u0448\u0442",
                comment="6 \u0448\u0442\u0443\u043a \u0432 \u043a\u043e\u0440\u043e\u0431\u043a\u0435, \u0424\u0440\u0430\u043d\u0446\u0438\u044f",
                source_line=source,
            )
        ],
    )
    service = _service(settings, SimpleNamespace(responses=_Responses(parsed)))

    command = service._parse_text_once(source)

    item = command.items[0]
    assert (item.quantity, item.unit) == (22.0, "\u0448\u0442")
    assert item.comment == ""
    assert item.user_comment_to_supplier == ""


def test_parse_text_runtime_reconciles_ai_comment_with_source(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет provenance reconciliation через runtime boundary."""
    parsed = ParsedInputSchema(
        intent=Intent.ADD_ITEMS,
        items=[
            ExtractedItem(
                product_query="курица",
                quantity=5,
                unit="кг",
                comment="охлаждённая",
                source_line="",
            )
        ],
    )
    service = _service(settings, SimpleNamespace(responses=_Responses(parsed)))

    command = service._parse_text_once("курица 5 кг")

    assert command.items[0].comment == ""
    assert command.items[0].quantity == 5
    assert command.items[0].unit == "кг"


def test_parse_text_runtime_collapses_shadow_item(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет подключение shadow reconciliation через runtime boundary."""
    source = "сироп роза холодным 3 штуки"
    parsed = ParsedInputSchema(
        intent=Intent.ADD_ITEMS,
        items=[
            ExtractedItem(
                product_query="сироп роза",
                quantity=None,
                unit="",
                comment="холодным",
                source_line=source,
            ),
            ExtractedItem(
                product_query="холодным",
                quantity=3,
                unit="шт",
                comment=".",
                source_line=source,
            ),
        ],
    )
    service = _service(settings, SimpleNamespace(responses=_Responses(parsed)))

    command = service._parse_text_once(source)

    assert len(command.items) == 1
    assert command.items[0].quantity == 3
    assert command.items[0].unit == "шт"


def test_ai_global_comment_scope_filler_is_not_saved_as_local_comment(settings) -> None:  # type: ignore[no-untyped-def]
    """Удаляет разговорную связку общего комментария из комментария последнего товара."""
    source = "Сироп роза 5 штук, главное быстро, и сироп тархун — всё это дело на завтра."
    parsed = ParsedInputSchema(
        intent=Intent.ADD_ITEMS,
        global_comment="всё это дело на завтра",
        items=[
            ExtractedItem(
                product_query="Сироп роза",
                quantity=5,
                unit="шт",
                comment="главное быстро",
                source_line=source,
            ),
            ExtractedItem(
                product_query="Сироп тархун",
                comment="всё это дело на завтра",
                source_line=source,
            ),
        ],
    )
    service = _service(settings, SimpleNamespace(responses=_Responses(parsed)))

    command = service.parse_text(source)

    assert command.global_comment == "на завтра"
    assert [item.comment for item in command.items] == ["главное быстро", ""]


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
    """Проверяет, что простой явный товар список пропускает второй ИИ call."""
    service = _service(settings, SimpleNamespace(responses=_FailingResponses()))

    command = service.parse_text("Сироп роза 10 штук, говядина 5 килограмм")

    assert command.intent is Intent.ADD_ITEMS
    assert [(item.product_query, item.quantity, item.unit) for item in command.items] == [
        ("Сироп роза", 10, "шт"),
        ("говядина", 5, "кг"),
    ]


def test_simple_single_product_with_terminal_punctuation_skips_ai(settings) -> None:  # type: ignore[no-untyped-def]
    """Разбирает очевидный голосовой товар локально без второго запроса к ИИ."""
    service = _service(settings, SimpleNamespace(responses=_FailingResponses()))

    command = service.parse_text("Сыр швейцарский Сыробогатов 10 штук.")

    assert command.intent is Intent.ADD_ITEMS
    assert len(command.items) == 1
    assert command.items[0].product_query == "Сыр швейцарский Сыробогатов"
    assert command.items[0].quantity == 10
    assert command.items[0].unit == "шт"
    assert command.items[0].comment == ""


def test_single_product_with_comment_still_uses_semantic_ai(settings) -> None:  # type: ignore[no-untyped-def]
    """Оставляет ИИ сообщения с пользовательским комментарием."""
    text = "Сыр швейцарский Сыробогатов 10 штук обязательно свежий"
    parsed = ParsedInputSchema(
        intent=Intent.ADD_ITEMS,
        items=[
            ExtractedItem(
                product_query="Сыр швейцарский Сыробогатов",
                quantity=10,
                unit="шт",
                comment="обязательно свежий",
                source_line=text,
            )
        ],
    )
    responses = _Responses(parsed)
    service = _service(settings, SimpleNamespace(responses=responses))

    command = service.parse_text(text)

    assert len(responses.calls) == 1
    assert command.items[0].comment == "обязательно свежий"


@pytest.mark.parametrize(
    "text",
    [
        "Сироп роза, одна штука, желательно холодный.",
        "Сироп роза одна штука, желательно холодный.",
        "Сироп роза, желательно холодным, одна штука.",
        "Одна штука сиропа роза, желательно холодного.",
    ],
)
def test_spoken_word_quantity_survives_ai_normalization(settings, text: str) -> None:  # type: ignore[no-untyped-def]
    """Не отделяет словесное количество от товара после корректного AI-разбора."""
    parsed = ParsedInputSchema(
        intent=Intent.ADD_ITEMS,
        items=[
            ExtractedItem(
                product_query="Сироп роза",
                quantity=1,
                comment="желательно холодный",
                source_line=text,
            )
        ],
    )
    service = _service(settings, SimpleNamespace(responses=_Responses(parsed)))

    command = service.parse_text(text)

    assert command.intent is Intent.ADD_ITEMS
    assert len(command.items) == 1
    assert (command.items[0].product_query, command.items[0].quantity, command.items[0].unit) == (
        "Сироп роза",
        1,
        "шт",
    )
    assert command.items[0].comment == "желательно холодный"


def test_text_timeout_does_not_accept_deterministic_product_guess(settings) -> None:  # type: ignore[no-untyped-def]
    """Не изменяет заявку локальной догадкой после исчерпания повторов AI."""
    service = _service(settings, SimpleNamespace(responses=_TimeoutResponses()))

    with pytest.raises(APITimeoutError):
        service.parse_text("Сироп роза 5 штук обязательно охлаждённым")


def test_text_timeout_does_not_guess_comments_for_spoken_products(settings) -> None:  # type: ignore[no-untyped-def]
    """Не разбирает локально список с комментариями после тайм-аута AI."""
    service = _service(settings, SimpleNamespace(responses=_TimeoutResponses()))

    with pytest.raises(APITimeoutError):
        service.parse_text(
            "Добавь сироп розы 5 штук на завтра и сироп сангрия 10 штук, желательно холодным."
        )


def test_text_timeout_does_not_guess_explicit_global_comment(settings) -> None:  # type: ignore[no-untyped-def]
    """Не применяет общий комментарий без успешного ответа AI."""
    service = _service(settings, SimpleNamespace(responses=_TimeoutResponses()))

    with pytest.raises(APITimeoutError):
        service.parse_text(
            "Добавь сироп роза 5 штук в банках и сироп сангрия 10 штук. "
            "Всё желательно привезти завтра."
        )


def test_text_timeout_does_not_guess_complex_spoken_item_boundaries(settings) -> None:  # type: ignore[no-untyped-def]
    """Не изменяет черновик догадкой для сложной голосовой заявки."""
    service = _service(settings, SimpleNamespace(responses=_TimeoutResponses()))

    with pytest.raises(APITimeoutError):
        service.parse_text(
            "Сироп Роза 5 штук, желательно холодным. "
            "Сироп Тархун 10 штук в банках. "
            "Говядина, кости продольный распил 10 килограмм. "
            "И куриные лапы 15 штук. Желательно завтра с 9 до 14."
        )


def test_ai_global_comment_without_scope_is_moved_to_last_item(settings) -> None:  # type: ignore[no-untyped-def]
    """Считает неявное пожелание после списка комментарием последнего товара."""
    text = "Сироп Роза 5 штук. Куриные лапы 15 килограмм. Желательно завтра с 9 до 14."
    parsed = ParsedInputSchema(
        intent=Intent.ADD_ITEMS,
        global_comment="Желательно завтра с 9 до 14",
        items=[
            ExtractedItem(
                product_query="Сироп Роза",
                quantity=5,
                unit="шт",
                source_line="Сироп Роза 5 штук.",
            ),
            ExtractedItem(
                product_query="Куриные лапы",
                quantity=15,
                unit="кг",
                source_line="Куриные лапы 15 килограмм. Желательно завтра с 9 до 14.",
            ),
        ],
    )
    service = _service(settings, SimpleNamespace(responses=_Responses(parsed)))

    command = service.parse_text(text)

    assert command.global_comment == ""
    assert command.items[-1].comment == "Желательно завтра с 9 до 14"


def test_hyphenated_multiline_product_list_skips_ai_and_keeps_quantities(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    """Разбирает простой список с тире локально и без задержки ИИ."""
    service = _service(settings, SimpleNamespace(responses=_FailingResponses()))

    command = service.parse_text("Сироп Снгря - 2 шт\nКордиал Апельсин - 3 шт")

    assert command.intent is Intent.ADD_ITEMS
    assert [(item.product_query, item.quantity, item.unit) for item in command.items] == [
        ("Сироп Снгря", 2, "шт"),
        ("Кордиал Апельсин", 3, "шт"),
    ]


@pytest.mark.parametrize("text", ["сироп шка", "креветки королевские"])
def test_short_product_name_without_quantity_skips_ai(settings, text: str) -> None:  # type: ignore[no-untyped-def]
    """Не зависит от ИИ при поиске короткого названия без количества."""
    service = _service(settings, SimpleNamespace(responses=_FailingResponses()))

    command = service.parse_text(text)

    assert command.intent is Intent.ADD_ITEMS
    assert len(command.items) == 1
    assert command.items[0].product_query == text
    assert command.items[0].quantity is None


def test_support_failure_phrase_uses_semantic_ai_instead_of_becoming_product(settings) -> None:  # type: ignore[no-untyped-def]
    """Распознаёт жалобу без зависимости от решения или доступности ИИ."""
    service = _service(settings, SimpleNamespace(responses=_FailingResponses()))

    command = service.parse_text("Доброе, не работает")

    assert command.intent is Intent.SMALL_TALK
    assert command.items == []


@pytest.mark.parametrize(
    "text",
    [
        "Бот опять не работает",
        "У меня ничего не происходит",
        "Не могу добавить товар",
        "Почему не находится кукуруза?",
    ],
)
def test_support_phrases_never_become_draft_items(settings, text: str) -> None:  # type: ignore[no-untyped-def]
    """Не записывает распространённые сообщения о сбое как названия товаров."""
    service = _service(settings, SimpleNamespace(responses=_FailingResponses()))

    command = service.parse_text(text)

    assert command.intent is Intent.SMALL_TALK
    assert command.items == []


def test_mixed_latin_and_cyrillic_product_word_is_safely_repaired(settings) -> None:  # type: ignore[no-untyped-def]
    """Исправляет однозначную смешанную раскладку, не подбирая похожий товар."""
    service = _service(settings, SimpleNamespace(responses=_FailingResponses()))

    command = service.parse_text("Tomаты")

    assert command.intent is Intent.ADD_ITEMS
    assert command.items[0].product_query == "Томаты"


def test_pure_latin_brand_is_not_rewritten(settings) -> None:  # type: ignore[no-untyped-def]
    """Не изменяет легальное латинское название товара или бренда."""
    service = _service(settings, SimpleNamespace(responses=_FailingResponses()))

    command = service.parse_text("Coca-Cola")

    assert command.intent is Intent.ADD_ITEMS
    assert command.items[0].product_query == "Coca-Cola"


def test_conversational_product_leadin_is_removed_by_semantic_parser(settings) -> None:  # type: ignore[no-untyped-def]
    """Не включает разговорную вводную в название товара для каталога."""
    parsed = ParsedInputSchema(
        intent=Intent.ADD_ITEMS,
        items=[
            ExtractedItem(
                product_query="кукуруза",
                quantity=10,
                unit="шт",
                comment="свежая",
            )
        ],
    )
    responses = _Responses(parsed)
    service = _service(settings, SimpleNamespace(responses=responses))

    command = service.parse_text("Мне нужна свежая кукуруза 10 штук.")

    assert len(responses.calls) == 1
    assert command.items[0].product_query == "свежая кукуруза"
    assert command.items[0].comment == ""


def test_conversational_product_leadin_is_not_recovered_as_comment(settings) -> None:  # type: ignore[no-untyped-def]
    """Не превращает слова «мне нужна» в комментарий к найденному товару."""
    parsed = ParsedInputSchema(
        intent=Intent.ADD_ITEMS,
        items=[
            ExtractedItem(
                product_query="свежая кукуруза",
                quantity=10,
                unit="шт",
                source_line="Мне нужна свежая кукуруза 10 штук.",
            )
        ],
    )
    service = _service(settings, SimpleNamespace(responses=_Responses(parsed)))

    command = service.parse_text("Мне нужна свежая кукуруза 10 штук.")

    assert command.items[0].product_query == "свежая кукуруза"
    assert command.items[0].comment == ""


def test_multiple_spoken_quantities_force_semantic_product_parser(settings) -> None:  # type: ignore[no-untyped-def]
    """Не склеивает два товара, если первое количество произнесено словами."""
    text = "Сироп роза пять штук, сироп вунди 10 штук."
    parsed = ParsedInputSchema(
        intent=Intent.ADD_ITEMS,
        items=[
            ExtractedItem(product_query="Сироп роза", quantity=5, unit="шт"),
            ExtractedItem(product_query="Сироп вунди", quantity=10, unit="шт"),
        ],
    )
    responses = _Responses(parsed)
    service = _service(settings, SimpleNamespace(responses=responses))

    command = service.parse_text(text)

    assert len(responses.calls) == 1
    assert [(item.product_query, item.quantity) for item in command.items] == [
        ("Сироп роза", 5),
        ("Сироп вунди", 10),
    ]


def test_comment_scope_resolver_is_limited_to_provided_item_indexes(settings) -> None:  # type: ignore[no-untyped-def]
    """Передаёт специальному AI-маршруту только ответ и ожидающие товары."""
    parsed = CommentScopeDecision(
        action="items",
        target_item_indexes=[0, 1],
        confidence=0.99,
    )
    responses = _Responses(parsed)
    service = _service(settings, SimpleNamespace(responses=responses))

    decision = service.resolve_comment_scope(
        "Это относится к обоим товарам",
        ["Помидоры", "Огурцы"],
    )

    assert decision == parsed
    request = responses.calls[0]
    assert request["text_format"] is CommentScopeDecision
    assert '"index": 0' in str(request["input"])
    assert '"index": 1' in str(request["input"])


def test_vague_comment_scope_is_rejected_even_when_ai_is_overconfident(settings) -> None:  # type: ignore[no-untyped-def]
    """Переспрашивает при расплывчатом ответе вместо выбора всех товаров."""
    parsed = CommentScopeDecision(
        action="items",
        target_item_indexes=[0, 1],
        confidence=0.95,
        reason="Вероятно, пользователь имеет в виду все товары.",
    )
    service = _service(settings, SimpleNamespace(responses=_Responses(parsed)))

    decision = service.resolve_comment_scope(
        "Ну, для нужных товаров",
        ["Сироп Роза", "Сироп Тархун"],
    )

    assert decision.action == "ambiguous"
    assert decision.target_item_indexes == []
    assert decision.confidence < 0.9


def test_inflected_product_name_is_a_safe_comment_scope_anchor(settings) -> None:  # type: ignore[no-untyped-def]
    """Принимает название товара в обычной разговорной падежной форме."""
    parsed = CommentScopeDecision(
        action="items",
        target_item_indexes=[1],
        confidence=0.99,
    )
    service = _service(settings, SimpleNamespace(responses=_Responses(parsed)))

    decision = service.resolve_comment_scope(
        "Только у тархуна",
        ["Сироп Роза", "Сироп Тархун"],
    )

    assert decision == parsed


def test_short_product_with_possible_comment_skips_slow_initial_ai(settings) -> None:  # type: ignore[no-untyped-def]
    """Оставляет короткую фразу каталогу для разделения названия и комментария."""
    service = _service(settings, SimpleNamespace(responses=_FailingResponses()))

    command = service.parse_text("сироп рза холодным")

    assert command.intent is Intent.ADD_ITEMS
    assert len(command.items) == 1
    assert command.items[0].product_query == "сироп рза холодным"


def test_variant_qualified_short_product_uses_semantic_ai(settings) -> None:  # type: ignore[no-untyped-def]
    """Передаёт неизвестный признак товара в ИИ, а не угадывает базовую позицию."""
    parsed = ParsedInputSchema(
        intent=Intent.ADD_ITEMS,
        items=[ExtractedItem(product_query="Свинина сало обжаренное")],
    )
    responses = _Responses(parsed)
    service = _service(settings, SimpleNamespace(responses=responses))

    command = service.parse_text("Свинина сало обжаренное")

    assert len(responses.calls) == 1
    assert command.items[0].product_query == "Свинина сало обжаренное"


def test_original_packaged_product_line_is_preserved_for_catalog_check(settings) -> None:  # type: ignore[no-untyped-def]
    """Сохраняет исходное название для отделения фасовки по каталогу."""
    text = "Концентрат Интерквас красного сусла, 650г"
    parsed = ParsedInputSchema(
        intent=Intent.ADD_ITEMS,
        items=[
            ExtractedItem(
                product_query="Концентрат Интерквас красного сусла",
                quantity=650,
                unit="г",
            )
        ],
    )
    service = _service(settings, SimpleNamespace(responses=_Responses(parsed)))

    command = service.parse_text(text)

    assert command.items[0].source_line == text


def test_numeric_range_with_supplier_qualifier_uses_semantic_ai(settings) -> None:  # type: ignore[no-untyped-def]
    """Не принимает поставщика и признак товара за часть названия при диапазоне."""
    text = "Форель филе свежая 0,8-1,2 килограмма, зачищенная Тринца 5 килограмм."
    parsed = ParsedInputSchema(
        intent=Intent.ADD_ITEMS,
        items=[
            ExtractedItem(
                product_query="Форель филе свежая 0,8-1,2 килограмма",
                quantity=5,
                unit="кг",
                supplier_hint="Тринца",
                comment="зачищенная",
                source_line=text,
            )
        ],
    )
    service = _service(settings, SimpleNamespace(responses=_Responses(parsed)))

    command = service.parse_text(text)

    assert command.items[0].supplier_hint == "Тринца"
    assert command.items[0].product_query == "Форель филе свежая 0,8-1,2 килограмма зачищенная"
    assert command.items[0].comment == ""
    assert (command.items[0].quantity, command.items[0].unit) == (5, "кг")


def test_text_ai_unknown_placeholders_do_not_lock_supplier_search(settings) -> None:  # type: ignore[no-untyped-def]
    """Не принимает служебное unknown за отдел или выбранного поставщика."""
    text = "Сыр Швейцарский Сыробогатов 180гр 10 штук"
    parsed = ParsedInputSchema(
        intent=Intent.ADD_ITEMS,
        items=[
            ExtractedItem(
                product_query="Сыр Швейцарский Сыробогатов",
                quantity=10,
                unit="штук",
                department="unknown",
                supplier_hint="unknown",
                source_line=text,
            )
        ],
    )
    service = _service(settings, SimpleNamespace(responses=_Responses(parsed)))

    command = service.parse_text(text)

    assert command.items[0].quantity == 10
    assert command.items[0].unit == "шт"
    assert command.items[0].department == ""
    assert command.items[0].supplier_hint == ""


def test_explicit_global_comment_scope_still_uses_semantic_ai(settings) -> None:  # type: ignore[no-untyped-def]
    """Не превращает явный общий комментарий в комментарий одной позиции."""
    service = _service(settings, SimpleNamespace(responses=_FailingResponses()))

    with pytest.raises(RuntimeError, match="OpenAI HTTP 429"):
        service.parse_text("сироп роза всем завтра")


def test_standalone_global_comment_is_not_replaced_by_fake_deterministic_items(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    """Сохраняет отдельный общий комментарий как изменение текущего черновика."""
    parsed = ParsedInputSchema(
        intent=Intent.ADD_MORE,
        global_comment="желательно на завтра",
        items=[],
    )
    service = _service(settings, SimpleNamespace(responses=_Responses(parsed)))

    command = service.parse_text("Добавь комментарий, желательно на завтра, общий.")

    assert command.intent is Intent.EDIT_COMMENT
    assert command.comment_action == "add"
    assert command.comment_scope == "order"
    assert command.comment_text == "желательно на завтра"
    assert command.global_comment == ""
    assert command.items == []


@pytest.mark.parametrize(
    ("suffix", "expected_quantity"),
    [
        ("", None),
        (" 100 кг", 100),
    ],
)
def test_exact_packaged_product_skips_ai_and_keeps_full_catalog_name(
    settings, suffix: str, expected_quantity: int | None
) -> None:  # type: ignore[no-untyped-def]
    """Сохраняет точное название с фасовкой и не ждёт вызов ИИ."""
    service = _service(settings, SimpleNamespace(responses=_FailingResponses()))
    name = (
        "ME-ФБ-Глазной мускул говяжий с/м В/У ~ 2кг*10(~20кг) Фермерский бычок Мираторг (Брянск) Ро"
    )

    command = service.parse_text(f"{name}{suffix}")

    assert command.intent is Intent.ADD_ITEMS
    assert len(command.items) == 1
    assert command.items[0].product_query == name
    assert command.items[0].quantity == expected_quantity
    assert command.items[0].unit == ("кг" if expected_quantity is not None else "")


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
    """Проверяет, что для списка неоднозначных товаров по-прежнему используется структурированный ИИ."""
    service = _service(settings, SimpleNamespace(responses=_FailingResponses()))

    with pytest.raises(RuntimeError, match="OpenAI HTTP 429"):
        service.parse_text(text)


def test_openai_parse_error_is_not_silently_replaced_with_an_empty_command(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что ошибка разбора OpenAI без подтверждённого результата заменяется пустой командой."""
    service = _service(settings, SimpleNamespace(responses=_FailingResponses()))

    with pytest.raises(RuntimeError, match="OpenAI HTTP 429"):
        service.parse_text("Мисо-паста Genzo 1 кг")


def test_client_order_sheet_uses_the_selected_department_quantity(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что клиент заказ таблица использует выбранный подразделение количество."""
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
    assert command.items[0].department_quantities.model_dump() == {
        "hall": None,
        "bar": None,
        "kitchen": 5,
    }


def test_client_order_sheet_ignores_supplier_recognized_from_a_neighbouring_row(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    """Не ограничивает поиск поставщиком, ошибочно перенесённым OCR из соседней строки."""
    service = _service(settings, SimpleNamespace())
    command = service._normalise_photo_command(
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(
                    product_query="Сироп Роза, 1л",
                    quantity=10,
                    unit="шт",
                    supplier_hint="Тестовый поставщик",
                    department_quantities={"hall": None, "bar": None, "kitchen": 10},
                    quantity_source="printed_order_column",
                ),
                ExtractedItem(
                    product_query="Сироп Фундук, 1л",
                    quantity=20,
                    unit="шт",
                    supplier_hint="Тестовый поставщик",
                    department_quantities={"hall": None, "bar": None, "kitchen": 20},
                    quantity_source="printed_order_column",
                ),
            ],
        ),
        "client_order_sheet",
    )

    assert [item.supplier_hint for item in command.items] == ["", ""]
    assert [item.quantity for item in command.items] == [10, 20]


def test_regular_order_table_keeps_explicit_supplier_hint(settings) -> None:  # type: ignore[no-untyped-def]
    """Сохраняет поставщика в других форматах, где он относится к товарной строке."""
    service = _service(settings, SimpleNamespace())
    command = service._normalise_photo_command(
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(
                    product_query="Сыр Пармезан",
                    quantity=5,
                    unit="кг",
                    supplier_hint="Поставщик сыра",
                    order_entry_text="5",
                    order_entry_type="typed_order_entry",
                )
            ],
        ),
        "order_table",
    )

    assert command.items[0].supplier_hint == "Поставщик сыра"


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
    assert command.items[0].department_quantities.model_dump() == {
        "hall": 2,
        "bar": None,
        "kitchen": 3,
    }


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


@pytest.mark.parametrize(
    "document_type",
    [
        "client_order_sheet",
        "printed_order_form",
        "order_table",
        "free_list",
        "unknown_document",
        "product_card",
    ],
)
def test_photo_never_keeps_product_without_positive_quantity(
    settings,
    document_type: str,
) -> None:  # type: ignore[no-untyped-def]
    """Отбрасывает товар без количества для любого типа изображения."""
    service = _service(settings, SimpleNamespace())
    command = service._normalise_photo_command(
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[ExtractedItem(product_query="Товар без количества", quantity=None)],
        ),
        document_type,
    )

    assert command.items == []


def test_handwritten_replacement_wins_over_crossed_out_client_sheet_quantity(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что рукописная замена имеет приоритет над зачёркнутым количеством в клиентской таблице."""
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


def test_photo_normalizer_marks_retained_free_list_quantity_as_photo_derived(settings) -> None:  # type: ignore[no-untyped-def]
    """Помечает количество из свободного списка, чтобы каталог его не перезаписал."""
    service = _service(settings, SimpleNamespace())
    command = service._normalise_photo_command(
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(
                    product_query="Курица",
                    quantity=1,
                    unit="кг",
                    source_line="Курица — 1 кг",
                )
            ],
        ),
        "free_list",
    )

    assert command.items[0].quantity == 1
    assert command.items[0].quantity_source == "photo_order_entry"


def test_free_list_keeps_explicit_quantity_even_if_model_calls_it_order_column(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    """Не удаляет видимое количество свободного списка из-за названия источника."""
    service = _service(settings, SimpleNamespace())
    command = service._normalise_photo_command(
        ParsedCommand(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(
                    product_query="Сироп Роза",
                    quantity=6,
                    unit="шт",
                    source_line="Сироп Роза — 6 шт",
                    quantity_source="printed_order_column",
                )
            ],
        ),
        "free_list",
    )

    assert [(item.product_query, item.quantity) for item in command.items] == [("Сироп Роза", 6)]


def test_large_explicit_list_is_processed_by_ai_chunks(settings) -> None:  # type: ignore[no-untyped-def]
    """Разбирает длинный список несколькими запросами ИИ и объединяет результат."""
    parsed = [
        ParsedInputSchema(
            intent=Intent.ADD_ITEMS,
            items=[
                ExtractedItem(product_query=f"Товар {index}", quantity=1, unit="шт")
                for index in range(start, start + 8)
            ],
        )
        for start in (0, 8)
    ]
    responses = _SequenceResponses(parsed)
    service = _service(settings, SimpleNamespace(responses=responses))
    source = "\n".join(f"Товар {index} — 1" for index in range(16))

    command = service.parse_text(source)

    assert command.intent is Intent.ADD_ITEMS
    assert len(command.items) == 16
    assert len(responses.calls) == 2


def test_large_list_transport_error_is_not_replaced_with_guessed_items(settings) -> None:  # type: ignore[no-untyped-def]
    """Не добавляет частичный или детерминированно угаданный список при тайм-ауте ИИ."""
    service = _service(settings, SimpleNamespace(responses=_TimeoutResponses()))
    source = "\n".join(f"Товар {index} — 1" for index in range(16))

    with pytest.raises(APITimeoutError):
        service.parse_text(source)


def test_product_name_digits_are_not_bare_quantities() -> None:
    """Не принимает артикул или год в конце названия за количество заказа."""
    from restaurant_bot.parsing.products import parse_product_lines

    items = parse_product_lines("Вино Кюве 2026")
    assert len(items) == 1
    assert items[0].product_query == "Вино Кюве 2026"
    assert items[0].quantity is None


def test_terminal_order_quantity_wins_over_packaging() -> None:
    """Берёт единицу заказа после тире, а не фасовку из названия товара."""
    payload = [
        {
            "product_query": "Сыр 180 г",
            "quantity": 180,
            "unit": "г",
            "source_line": "Сыр 180 г — 1",
        }
    ]

    restored = restore_explicit_order_terms(payload, "Сыр 180 г — 1")

    assert restored[0]["quantity"] == 1
    assert restored[0]["unit"] == ""
