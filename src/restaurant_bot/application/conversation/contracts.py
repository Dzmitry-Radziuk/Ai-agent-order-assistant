"""Нейтральные контракты прикладного диалога."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from restaurant_bot.domain.models import ConversationState, InputKind


class ConversationInteraction(Protocol):
    """Описывает минимальный вход, необходимый stateful processor."""

    update_id: int
    chat_id: str
    input_type: InputKind
    text: str
    callback_data: str
    callback_query_id: str
    callback_message_id: int | None
    telegram_user_id: str
    telegram_username: str
    telegram_first_name: str
    telegram_last_name: str

    def model_copy(self, *, update: dict[str, Any] | None = None) -> Any:
        """Создаёт копию входа с изменениями."""


class ConversationInput:
    """Описывает входное взаимодействие без зависимости от канала."""

    def __init__(
        self,
        interaction_id: int,
        conversation_id: str,
        *,
        actor_id: str = "",
        channel: str = "unknown",
        kind: InputKind = InputKind.UNKNOWN,
        text: str = "",
        action: str = "",
        media_reference: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Создаёт нейтральное входное взаимодействие."""
        self.interaction_id = interaction_id
        self.conversation_id = conversation_id
        self.actor_id = actor_id
        self.channel = channel
        self.kind = kind
        self.text = text
        self.action = action
        self.media_reference = media_reference
        self.metadata = dict(metadata or {})

    def model_copy(self, *, update: dict[str, Any] | None = None) -> ConversationInput:
        """Создаёт копию взаимодействия с точечными изменениями."""
        values: dict[str, Any] = {
            "interaction_id": self.interaction_id,
            "conversation_id": self.conversation_id,
            "actor_id": self.actor_id,
            "channel": self.channel,
            "kind": self.kind,
            "text": self.text,
            "action": self.action,
            "media_reference": self.media_reference,
            "metadata": dict(self.metadata),
        }
        values.update(update or {})
        return ConversationInput(**values)

    @property
    def update_id(self) -> int:
        """Возвращает идентификатор взаимодействия для старого runtime-контракта."""
        return self.interaction_id

    @property
    def chat_id(self) -> str:
        """Возвращает идентификатор разговора для старого runtime-контракта."""
        return self.conversation_id

    @property
    def input_type(self) -> InputKind:
        """Возвращает нейтральный тип входа."""
        return self.kind

    @property
    def callback_data(self) -> str:
        """Возвращает семантическое действие входа."""
        return self.action

    @property
    def callback_query_id(self) -> str:
        """Возвращает идентификатор подтверждения действия, если он есть."""
        return str(self.metadata.get("callback_query_id", ""))

    @property
    def callback_message_id(self) -> int | None:
        """Возвращает идентификатор сообщения действия, если он есть."""
        value = self.metadata.get("callback_message_id")
        return value if isinstance(value, int) else None

    @property
    def telegram_user_id(self) -> str:
        """Возвращает legacy identity из metadata без требования Telegram в core."""
        return self.actor_id

    @property
    def telegram_username(self) -> str:
        """Возвращает имя пользователя из metadata."""
        return str(self.metadata.get("username", ""))

    @property
    def telegram_first_name(self) -> str:
        """Возвращает имя пользователя из metadata."""
        return str(self.metadata.get("first_name", ""))

    @property
    def telegram_last_name(self) -> str:
        """Возвращает фамилию пользователя из metadata."""
        return str(self.metadata.get("last_name", ""))


@dataclass(frozen=True, slots=True)
class SemanticAction:
    """Описывает действие интерфейса без callback-протокола канала."""

    action_id: str
    label: str
    target: str = ""
    group: int = 0


@dataclass(frozen=True, slots=True)
class ConversationView:
    """Описывает смысловой ответ прикладного сценария."""

    text: str
    actions: tuple[SemanticAction, ...] = ()
    edit_requested: bool = False


@dataclass(frozen=True, slots=True)
class ConversationEffectPlan:
    """Описывает отложенные прикладные эффекты."""

    enqueue_submission: bool = False
    enqueue_order_status: bool = False
    enqueue_product_add: bool = False
    enqueue_review_submission: bool = False
    invalidate_catalog: bool = False


@dataclass(frozen=True, slots=True)
class ConversationResult:
    """Описывает результат диалога до channel-specific rendering."""

    state: ConversationState
    view: ConversationView
    effects: ConversationEffectPlan = field(default_factory=ConversationEffectPlan)
    order_status_page: int = 0
    order_status_detail_page: int = 0
    order_status_selected_index: int | None = None
    order_status_order_number: str = ""
