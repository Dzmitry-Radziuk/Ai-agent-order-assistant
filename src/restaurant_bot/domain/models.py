from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class InputKind(StrEnum):
    """Перечисляет поддерживаемые типы входных сообщений Telegram."""

    TEXT = "text"
    CALLBACK = "callback"
    VOICE = "voice"
    PHOTO = "photo"
    UNKNOWN = "unknown"


class Intent(StrEnum):
    """Перечисляет команды, которые понимает бот."""

    ADD_ITEMS = "add_items"
    REMOVE_ITEM = "remove_item"
    SKIP_CURRENT = "skip_current"
    CLARIFY_CURRENT = "clarify_current"
    MANUAL_CURRENT = "manual_current"
    CLEAR_CART = "clear_cart"
    START_NEW_ORDER = "start_new_order"
    CONFIRM = "confirm"
    CANCEL = "cancel"
    SHOW_CART = "show_cart"
    SUBMIT_REQUEST = "submit_request"
    EDIT_QUANTITY = "edit_quantity"
    EDIT_COMMENT = "edit_comment"
    SELECT_CANDIDATE = "select_candidate"
    ADD_MORE = "add_more"
    GREETING = "greeting"
    HELP = "help"
    THANKS = "thanks"
    SMALL_TALK = "small_talk"
    BACK = "back"
    CONTINUE_CURRENT = "continue_current"
    CHECK_MIN_SUM = "check_min_sum"
    ADD_SUPPLIER_ITEMS = "add_supplier_items"
    CHOOSE_SUPPLIER_WARNING = "choose_supplier_warning"
    SUBMIT_AS_IS = "submit_as_is"
    ACCEPT_SUGGESTED_QUANTITY = "accept_suggested_quantity"
    KEEP_CURRENT_QUANTITY = "keep_current_quantity"
    ENTER_OTHER_QUANTITY = "enter_other_quantity"
    USE_CATALOG_UNIT = "use_catalog_unit"
    SHOW_FINAL_REVIEW = "show_final_review"
    REVIEW_ORDER = "review_order"
    REVIEW_REFRESH = "review_refresh"
    REVIEW_SUBMIT = "review_submit"
    REVIEW_CANCEL = "review_cancel"
    ORDER_STATUS = "order_status"
    PRODUCT_ADD = "product_add"
    PRODUCT_ADD_RETRY = "product_add_retry"
    PRODUCT_ADD_SKIP = "product_add_skip"
    PRODUCT_ADD_LIST = "product_add_list"
    SEARCH_ALL_SUPPLIERS = "search_all_suppliers"
    SWITCH_SUPPLIER = "switch_supplier"
    KEEP_MULTIPLE = "keep_multiple"
    FIX_MULTIPLE = "fix_multiple"
    EDIT_MULTIPLE = "edit_multiple"
    UNIT_EDIT = "unit_edit"
    UNIT_OK = "unit_ok"
    MERGE_DUPLICATE = "merge_duplicate"
    UNKNOWN = "unknown"


class DialogueResponse(StrEnum):
    """Описывает общий короткий ответ пользователя в диалоге."""

    NONE = "none"
    AFFIRM = "affirm"
    DECLINE = "decline"
    UNCERTAIN = "uncertain"


class CommentSource(StrEnum):
    """Показывает подтверждённое происхождение комментария пользователя."""

    NONE = "none"
    SEMANTIC = "semantic"
    EXPLICIT_MARKER = "explicit_marker"
    CATALOG = "catalog"


class SearchScope(StrEnum):
    """Перечисляет области поиска по каталогу."""

    SUPPLIER_ONLY = "supplier_only"
    ANY_SUPPLIER = "any_supplier"


class IssueKind(StrEnum):
    """Перечисляет проблемы распознавания товарной позиции."""

    CANDIDATE = "candidate"
    NOT_FOUND = "not_found"
    QUANTITY = "quantity"
    UNIT = "unit"
    DUPLICATE = "duplicate"
    PRODUCT_ADD = "product_add"


class ItemStatus(StrEnum):
    """Перечисляет состояния позиции в черновике."""

    NEW = "new"
    MATCHED = "matched"
    MISSING_QTY = "missing_qty"
    UNIT_MISMATCH = "unit_mismatch"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"
    AI_PENDING = "ai_pending"
    DUPLICATE_PENDING = "duplicate_pending"
    SKIPPED = "skipped"


class SessionStage(StrEnum):
    """Перечисляет этапы диалога с пользователем."""

    COLLECTING = "collecting"
    REVIEW = "review"
    AWAIT_SUBMIT_CONFIRM = "await_submit_confirm"
    SUBMITTING = "submitting"
    SUBMISSION_FAILED = "submission_failed"
    SUBMITTED = "submitted"
    AWAIT_UNIT_QUANTITY = "await_unit_quantity"
    AWAIT_MULTIPLE_QUANTITY = "await_multiple_quantity"
    AWAIT_MANUAL_DETAILS = "await_manual_details"
    AWAIT_PRODUCT_ADD_DETAILS = "await_product_add_details"
    AWAIT_ADD_MORE_CONFIRM = "await_add_more_confirm"
    AWAIT_COMMENT_SCOPE = "await_comment_scope"


class TelegramEvent(BaseModel):
    """Описывает нормализованное обновление Telegram."""

    update_id: int
    chat_id: str
    input_type: InputKind
    text: str = ""
    bot_command: str = ""
    callback_data: str = ""
    callback_query_id: str = ""
    callback_message_id: int | None = None
    telegram_user_id: str = ""
    telegram_username: str = ""
    telegram_first_name: str = ""
    telegram_last_name: str = ""
    chat_type: str = ""
    file_id: str = ""
    mime_type: str = ""
    raw_update: dict[str, Any] = Field(default_factory=dict)


class DepartmentQuantities(BaseModel):
    """Хранит количества товара по отделам заведения."""

    hall: float | None = None
    bar: float | None = None
    kitchen: float | None = None

    def for_department(self, department: str) -> float | None:
        """Возвращает количество товара для отдела."""
        mapping = {"Зал": self.hall, "Бар": self.bar, "Кухня": self.kitchen}
        return mapping.get(department)


class ExtractedItem(BaseModel):
    """Описывает товар, извлечённый из сообщения пользователя."""

    product_query: str
    quantity: float | None = None
    unit: str = ""
    department: str = ""
    supplier_hint: str = ""
    comment: str = ""
    user_comment_to_supplier: str = ""
    comment_source: CommentSource = CommentSource.NONE
    source_line: str = ""
    source_department: str = ""
    department_quantities: DepartmentQuantities = Field(default_factory=DepartmentQuantities)
    quantity_source: str = ""
    printed_reference_text: str = ""
    order_entry_text: str = ""
    order_entry_type: str = ""
    packaging_text: str = ""
    packaging_role: Literal[
        "none",
        "catalog_attribute",
        "user_preference",
        "ambiguous",
    ] = "none"
    packaging_confidence: float = Field(default=0, ge=0, le=1)

    @field_validator(
        "product_query",
        "unit",
        "department",
        "supplier_hint",
        "comment",
        "user_comment_to_supplier",
        "source_line",
        "source_department",
        "quantity_source",
        "printed_reference_text",
        "order_entry_text",
        "order_entry_type",
        "packaging_text",
    )
    @classmethod
    def strip_text(cls, value: str) -> str:
        """Обрезает пробелы во всех строковых полях."""
        return " ".join(value.split()).strip()

    @model_validator(mode="after")
    def infer_semantic_comment_source(self) -> ExtractedItem:
        """Помечает старый payload с комментарием как семантический."""
        if self.comment_source is CommentSource.NONE and (
            self.comment or self.user_comment_to_supplier
        ):
            self.comment_source = CommentSource.SEMANTIC
        return self


class ParsedCommand(BaseModel):
    """Описывает нормализованную команду пользователя."""

    intent: Intent = Intent.UNKNOWN
    explicit_add_items: bool = False
    dialogue_response: DialogueResponse = DialogueResponse.NONE
    text: str = ""
    items: list[ExtractedItem] = Field(default_factory=list)
    target_query: str = ""
    target_queries: list[str] = Field(default_factory=list)
    selected_index: int | None = None
    selection_query: str = ""
    edit_quantity: float | None = None
    edit_unit: str = ""
    global_comment: str = ""
    comment_target_query: str = ""
    comment_text: str = ""
    comment_action: Literal["add", "remove"] = "add"
    comment_scope: Literal["item", "order"] = "item"
    comment_clarification: str = ""
    comment_scope_action: str = ""
    comment_target_indexes: list[int] = Field(default_factory=list)
    confidence: float | None = None
    callback_revision: int | None = None
    callback_target: str = ""
    order_status_detail_page: int = 0


class CatalogProduct(BaseModel):
    """Описывает товар из каталога заведения."""

    product_id: str
    name: str
    supplier: str = ""
    unit: str = ""
    price: float | None = None
    minimum_multiple: float | None = None
    useful_volume: float | None = None
    supplier_minimum_amount: float | None = None
    restaurant: str = ""
    supplier_schedule: str = ""
    department_quantities: DepartmentQuantities = Field(default_factory=DepartmentQuantities)
    supplier_current_sum: float | None = None
    comment: str = ""
    row_number: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class Candidate(BaseModel):
    """Описывает ранжированного кандидата из каталога."""

    product_id: str
    name: str
    supplier: str = ""
    unit: str = ""
    score: float = 0
    reason: str = ""


class CartItem(BaseModel):
    """Описывает одну товарную позицию черновика."""

    id: str
    source_query: str
    source_line: str = ""
    quantity_source: str = ""
    order_entry_type: str = ""
    packaging_text: str = ""
    packaging_role: Literal[
        "none",
        "catalog_attribute",
        "user_preference",
        "ambiguous",
    ] = "none"
    packaging_confidence: float = Field(default=0, ge=0, le=1)
    quantity: float | None = None
    unit: str = ""
    department: str = "Кухня"
    department_quantities: DepartmentQuantities = Field(default_factory=DepartmentQuantities)
    supplier_hint: str = ""
    supplier_search_locked: bool = False
    rename_attempted: bool = False
    comment: str = ""
    comment_source: CommentSource = CommentSource.NONE
    catalog_comment: str = ""
    catalog_comment_source: CommentSource = CommentSource.NONE
    status: ItemStatus = ItemStatus.NEW
    catalog_product_id: str = ""
    catalog_name: str = ""
    supplier: str = ""
    catalog_unit: str = ""
    price: float | None = None
    minimum_multiple: float | None = None
    useful_volume: float | None = None
    supplier_minimum_amount: float | None = None
    supplier_current_sum: float | None = None
    existing_quantity: float = 0
    candidates: list[Candidate] = Field(default_factory=list)
    suggested_quantity: float | None = None
    issue_message: str = ""
    duplicate_existing_quantity: float = 0
    duplicate_existing_unit: str = ""
    product_add_request_id: str = ""

    @field_validator(
        "source_query",
        "source_line",
        "quantity_source",
        "order_entry_type",
        "catalog_comment",
    )
    @classmethod
    def strip_source_text(cls, value: str) -> str:
        """Обрезает пробелы в исходном названии товара."""
        return " ".join(value.split()).strip()

    @model_validator(mode="after")
    def infer_semantic_comment_source(self) -> CartItem:
        """Поддерживает черновики до появления provenance комментария."""
        if self.comment_source is CommentSource.NONE and self.comment:
            self.comment_source = CommentSource.SEMANTIC
        return self

    @property
    def amount(self) -> float:
        """Возвращает стоимость товарной позиции."""
        if self.quantity is None or self.price is None:
            return 0.0
        return self.quantity * self.price


class PendingSubmission(BaseModel):
    """Хранит неизменяемый снимок отправляемой заявки."""

    order_no: str
    trace_id: str = ""
    telegram_user_id: str = ""
    telegram_chat_id: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    rows: list[dict[str, Any]] = Field(default_factory=list)
    last_error: str = ""
    failed_stage: str = ""
    last_attempt_at: datetime | None = None
    spreadsheet_id: str = ""
    venue_code: str = ""


class ConversationState(BaseModel):
    """Хранит полное состояние диалога с пользователем."""

    stage: SessionStage = SessionStage.COLLECTING
    cart: list[CartItem] = Field(default_factory=list)
    current_issue_item_id: str = ""
    last_order_no: str = ""
    submitted_order_numbers: list[str] = Field(default_factory=list)
    pending_submission: PendingSubmission | None = None
    order_trace_id: str = ""
    ui_message_id: int | None = None
    last_input_text: str = ""
    restaurant: str = ""
    telegram_user_id: str = ""
    telegram_chat_id: str = ""
    venue_code: str = ""
    venue_name: str = ""
    spreadsheet_id: str = ""
    spreadsheet_url: str = ""
    role: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: str = "collecting"
    department: str = "Кухня"
    ui_revision: int = 0
    ui_message_text: str = ""
    visible_actions: list[dict[str, str]] = Field(default_factory=list)
    supplier_hint_context: str = ""
    current_issue_kind: IssueKind | None = None
    search_scope: SearchScope = SearchScope.SUPPLIER_ONLY
    supplier_search_locked: bool = False
    pending_product_add_request_id: str = ""
    pending_product_add_item_index: int | None = None
    product_add_write_in_progress: bool = False
    product_add_requests: list[dict[str, Any]] = Field(default_factory=list)
    manual_item_index: int | None = None
    unit_item_index: int | None = None
    edit_multiple_index: int | None = None
    pending_added_items_count: int = 0
    pending_comment_items: list[ExtractedItem] = Field(default_factory=list)
    pending_comment_existing_item_ids: list[str] = Field(default_factory=list)
    pending_comment_text: str = ""
    pending_comment_global_comment: str = ""
    pending_new_order_confirmation: bool = False
    order_status_view_active: bool = False
    order_status_page: int = 0
    order_status_detail_page: int = 0
    order_status_detail_active: bool = False
    order_status_selected_index: int | None = None
    order_status_selected_order_number: str = ""
    order_status_order_numbers: list[str] = Field(default_factory=list)
    cart_page: int = 0
    final_review_page: int = 0
    review_token: str = ""
    review_snapshot_hash: str = ""
    review_venue_code: str = ""
    review_submission_in_progress: bool = False
    # ``cart`` is the regular draft review.  ``sheet_link`` is reserved for
    # the review card opened from a Google Sheets Telegram deep-link.  Keeping
    # the origin in persisted state prevents a voice command in the regular
    # draft from being routed to the sheet-review handler.
    review_mode: str = "cart"

    def current_item(self) -> CartItem | None:
        """Возвращает позицию, ожидающую действия пользователя."""
        if self.current_issue_item_id:
            return next((item for item in self.cart if item.id == self.current_issue_item_id), None)
        return None


class Button(BaseModel):
    """Описывает кнопку встроенной клавиатуры Telegram."""

    text: str
    callback_data: str


class BotReply(BaseModel):
    """Описывает ответ бота и его клавиатуру."""

    text: str
    rows: list[list[Button]] = Field(default_factory=list)
    edit_message_id: int | None = None
    disable_previous_keyboard: bool = False
    parse_mode: str = "HTML"


class EngineResult(BaseModel):
    """Описывает новое состояние, ответ и отложенные действия."""

    state: ConversationState
    reply: BotReply
    enqueue_submission: bool = False
    enqueue_order_status: bool = False
    order_status_page: int = 0
    order_status_detail_page: int = 0
    order_status_selected_index: int | None = None
    order_status_order_number: str = ""
    enqueue_product_add: bool = False
    enqueue_review_submission: bool = False
    invalidate_catalog: bool = False
