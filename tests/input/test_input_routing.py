from restaurant_bot.domain.models import (
    ConversationState,
    ExtractedItem,
    InputKind,
    Intent,
    ParsedCommand,
    SessionStage,
    TelegramEvent,
)
from restaurant_bot.input.telegram import normalize_telegram_update
from restaurant_bot.services.orchestrator import UpdateOrchestrator
from restaurant_bot.services.order_review import ReviewSnapshot
from restaurant_bot.services.parser import infer_intent


def test_bot_suffix_is_removed_from_slash_command_before_routing() -> None:
    """Проверяет, что бота суффикс является removed из slash команда до маршрутизация."""
    event = normalize_telegram_update(
        {"update_id": 1, "message": {"chat": {"id": 7}, "text": "/draft@restaurant_order_bot"}}
    )

    assert event.input_type is InputKind.TEXT
    assert event.bot_command == "draft"
    assert infer_intent(event.text).intent is Intent.SHOW_CART


def test_product_typo_is_never_interpreted_as_skip_command() -> None:
    """Проверяет, что товар опечатка является никогда не interpreted как пропуск команда."""
    command = infer_intent("гонядина 5 кг")

    assert command.intent is Intent.ADD_ITEMS
    assert command.items[0].product_query == "гонядина"
    assert command.items[0].quantity == 5


def test_direct_commands_bypass_catalog_but_product_operations_read_it() -> None:
    """Проверяет, что direct команды обходит каталог but товар operations чтение it."""
    assert not UpdateOrchestrator._needs_catalog(ParsedCommand(intent=Intent.SHOW_CART))
    assert not UpdateOrchestrator._needs_catalog(ParsedCommand(intent=Intent.CLEAR_CART))
    assert not UpdateOrchestrator._needs_catalog(ParsedCommand(intent=Intent.EDIT_QUANTITY))
    assert UpdateOrchestrator._needs_catalog(ParsedCommand(intent=Intent.ADD_ITEMS))
    assert UpdateOrchestrator._needs_catalog(ParsedCommand(intent=Intent.SELECT_CANDIDATE))
    assert UpdateOrchestrator._needs_catalog(ParsedCommand(intent=Intent.SUBMIT_REQUEST))
    assert UpdateOrchestrator._needs_catalog(ParsedCommand(intent=Intent.SHOW_FINAL_REVIEW))
    assert UpdateOrchestrator._needs_catalog(ParsedCommand(intent=Intent.CHECK_MIN_SUM))


def test_final_checks_require_fresh_catalog_values() -> None:
    """Обходит кэш для проверок суммы и количества перед отправкой."""
    assert UpdateOrchestrator._requires_fresh_catalog(ParsedCommand(intent=Intent.SUBMIT_REQUEST))
    assert UpdateOrchestrator._requires_fresh_catalog(
        ParsedCommand(intent=Intent.SHOW_FINAL_REVIEW)
    )
    assert UpdateOrchestrator._requires_fresh_catalog(ParsedCommand(intent=Intent.CHECK_MIN_SUM))
    assert not UpdateOrchestrator._requires_fresh_catalog(ParsedCommand(intent=Intent.ADD_ITEMS))


def test_callback_input_preserves_callback_data_for_engine() -> None:
    """Проверяет, что callback ввод сохраняет callback data for engine."""
    event = normalize_telegram_update(
        {
            "update_id": 2,
            "callback_query": {
                "id": "cb",
                "data": "v2:back:r3",
                "from": {"id": 7},
                "message": {"chat": {"id": 7}, "message_id": 9},
            },
        }
    )

    assert event.input_type is InputKind.CALLBACK
    assert event.callback_data == "v2:back:r3"
    assert event.callback_message_id == 9


def test_review_deep_link_is_a_dedicated_command() -> None:
    """Проверяет, что deep-link просмотра не превращается в товар или регистрацию."""
    orchestrator = UpdateOrchestrator.__new__(UpdateOrchestrator)
    command = orchestrator._parse_text_in_context(
        "/start review_6461W6",
        ConversationState(),
    )

    assert command.intent is Intent.REVIEW_ORDER
    assert command.callback_target == "6461W6"


def test_review_callbacks_keep_the_token_and_revision() -> None:
    """Проверяет, что подтверждение заявки сохраняет токен и ревизию интерфейса."""
    command = infer_intent("", "v2:review_submit:token123:r7")

    assert command.intent is Intent.REVIEW_SUBMIT
    assert command.callback_target == "token123"
    assert command.callback_revision == 7


def test_review_confirmation_stays_local_when_submission_is_disabled() -> None:
    """Проверяет, что отключённая отправка не ставит внешний Web App в очередь."""
    orchestrator = UpdateOrchestrator.__new__(UpdateOrchestrator)
    orchestrator.settings = type("SettingsStub", (), {"google_order_submission_enabled": False})()
    orchestrator.order_review = type(
        "ReviewStub",
        (),
        {
            "snapshot": lambda _self, _context: ReviewSnapshot(
                venue_code="6461W6",
                venue_name="Качели",
                spreadsheet_id="sheet",
                items=(),
                fingerprint="hash",
            ),
        },
    )()
    state = ConversationState(
        review_token="token123",
        review_snapshot_hash="hash",
        review_venue_code="6461W6",
        review_mode="sheet_link",
        venue_code="6461W6",
        ui_revision=7,
    )
    event = TelegramEvent(
        update_id=1,
        chat_id="chat-1",
        telegram_user_id="user-1",
        input_type=InputKind.CALLBACK,
        callback_data="v2:review_submit:token123:r7",
    )

    result = orchestrator._handle_review_command(
        event,
        state,
        ParsedCommand(
            intent=Intent.REVIEW_SUBMIT,
            callback_target="token123",
            callback_revision=7,
        ),
    )

    assert result.enqueue_review_submission is False
    assert "Отправка пока отключена" in result.reply.text


def test_sheet_review_does_not_map_arbitrary_text_to_visible_action() -> None:
    """Проверяет, что произвольный текст не запускает действие карточки sheet-review."""

    class ReviewVoiceAI:
        """Изолирует AI-выбор кнопки в маршрутизаторном тесте."""

        def parse_text(self, text: str) -> ParsedCommand:
            """Возвращает неизвестное намерение для проверки свободной фразы."""
            return ParsedCommand(intent=Intent.UNKNOWN, text=text)

        def choose_visible_action(
            self,
            text: str,
            screen_text: str,
            actions: list[dict[str, str]],
        ) -> str:
            """Падает, если произвольный текст передали в выбор кнопки."""
            raise AssertionError("visible action fallback must not run in sheet review")

    orchestrator = UpdateOrchestrator.__new__(UpdateOrchestrator)
    orchestrator.openai = ReviewVoiceAI()
    state = ConversationState(
        stage=SessionStage.REVIEW,
        review_mode="sheet_link",
        ui_message_text="Проверьте текущую заявку",
        visible_actions=[
            {"label": "✅ Отправить заявку", "action_id": "v2:review_submit:token"},
            {"label": "↩️ Отмена", "action_id": "v2:review_cancel:token"},
        ],
    )

    command = orchestrator._parse_text_in_context(
        "ну всё готово, можно передавать снабженцу",
        state,
    )

    assert command.intent is Intent.UNKNOWN
    assert command.text == "ну всё готово, можно передавать снабженцу"


def test_review_voice_maps_negative_and_refresh_phrases_without_product_addition() -> None:
    """Проверяет безопасную обработку отмены и повторной проверки голосом."""

    class ReviewVoiceAI:
        """Возвращает заранее определённые намерения для двух команд карточки."""

        def __init__(self, intent: Intent) -> None:
            """Сохраняет намерение, которое вернул бы общий AI-парсер."""
            self.intent = intent

        def parse_text(self, text: str) -> ParsedCommand:
            """Возвращает сохранённое намерение без обращения к OpenAI."""
            return ParsedCommand(intent=self.intent, text=text)

    state = ConversationState(
        stage=SessionStage.REVIEW,
        review_mode="sheet_link",
        review_token="token123",
    )
    for phrase, parsed_intent, expected in (
        ("нет, не отправляй", Intent.CANCEL, Intent.REVIEW_CANCEL),
        ("покажи заявку ещё раз", Intent.ADD_ITEMS, Intent.UNKNOWN),
    ):
        orchestrator = UpdateOrchestrator.__new__(UpdateOrchestrator)
        orchestrator.openai = ReviewVoiceAI(parsed_intent)
        command = orchestrator._parse_text_in_context(phrase, state)
        assert command.intent is expected


def test_regular_cart_review_does_not_use_sheet_review_voice_router() -> None:
    """Проверяет, что обычный черновик не маршрутизируется как карточка из таблицы."""

    class RegularReviewAI:
        """Возвращает стандартное намерение отправки черновика."""

        def parse_text(self, text: str) -> ParsedCommand:
            """Имитирует распознавание произвольной фразы «отправляй»."""
            return ParsedCommand(intent=Intent.SUBMIT_REQUEST, text=text)

    orchestrator = UpdateOrchestrator.__new__(UpdateOrchestrator)
    orchestrator.openai = RegularReviewAI()
    command = orchestrator._parse_text_in_context(
        "Отправляй",
        ConversationState(stage=SessionStage.REVIEW, review_mode="cart"),
    )

    assert command.intent is Intent.SUBMIT_REQUEST


def test_short_sheet_review_voice_transcription_never_selects_a_button() -> None:
    """Проверяет, что шумная короткая расшифровка «А.» не нажимает кнопку."""

    class ShortVoiceAI:
        """Запрещает вызов выбора кнопки для непригодного транскрипта."""

        def parse_text(self, text: str) -> ParsedCommand:
            """Возвращает неизвестное намерение для короткого звука."""
            return ParsedCommand(intent=Intent.UNKNOWN, text=text)

        def choose_visible_action(
            self,
            text: str,
            screen_text: str,
            actions: list[dict[str, str]],
        ) -> str:
            """Падает, если шум ошибочно передали в модель выбора кнопки."""
            raise AssertionError("short transcription must not choose a visible action")

    orchestrator = UpdateOrchestrator.__new__(UpdateOrchestrator)
    orchestrator.openai = ShortVoiceAI()
    command = orchestrator._parse_text_in_context(
        "А.",
        ConversationState(
            stage=SessionStage.REVIEW,
            review_mode="sheet_link",
            visible_actions=[
                {"label": "✅ Отправить заявку", "action_id": "v2:review_submit:token"},
                {"label": "↩️ Отмена", "action_id": "v2:review_cancel:token"},
            ],
        ),
    )

    assert command.intent is Intent.UNKNOWN


def test_concrete_product_command_wins_over_visible_action_fallback() -> None:
    """Не заменяет распознанный товар действием видимой кнопки."""

    class ProductAI:
        """Возвращает конкретный товар и запрещает fallback-вызов кнопки."""

        def parse_text(self, text: str) -> ParsedCommand:
            """Возвращает структурированную новую позицию."""
            return ParsedCommand(
                intent=Intent.ADD_ITEMS,
                text=text,
                items=[ExtractedItem(product_query="пармезан", quantity=3, unit="кг")],
            )

        def choose_visible_action(
            self,
            text: str,
            screen_text: str,
            actions: list[dict[str, str]],
        ) -> str:
            """Падает, если concrete product был ошибочно заменён кнопкой."""
            raise AssertionError("visible action fallback must not run for a product")

    orchestrator = UpdateOrchestrator.__new__(UpdateOrchestrator)
    orchestrator.openai = ProductAI()
    state = ConversationState(
        stage=SessionStage.AWAIT_UNIT_QUANTITY,
        visible_actions=[{"label": "Добавить ещё товары", "action_id": "v2:add"}],
    )

    command = orchestrator._parse_text_in_context("пармезан 3 кг", state)

    assert command.intent is Intent.ADD_ITEMS
    assert command.items[0].product_query == "пармезан"
