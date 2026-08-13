"""Нейтральные контракты прикладного диалога."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from restaurant_bot.domain.models import ConversationState, InputKind


class ConversationInteraction(Protocol):
    """Описывает минимальный вход, необходимый обработчику состояния."""

    interaction_id: int
    conversation_id: str
    actor_id: str
    channel: str
    kind: InputKind
    text: str
    action: str
    media_reference: str
    metadata: dict[str, Any]

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


@dataclass(frozen=True, slots=True)
class SemanticAction:
    """Описывает действие интерфейса без протокола конкретного канала."""

    kind: str
    label: str
    namespace: str = ""
    target: str = ""
    value: str = ""
    revision: int | None = None
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
    """Описывает результат диалога до отрисовки в конкретном канале."""

    state: ConversationState
    view: ConversationView
    effects: ConversationEffectPlan = field(default_factory=ConversationEffectPlan)
    order_status_page: int = 0
    order_status_detail_page: int = 0
    order_status_selected_index: int | None = None
    order_status_order_number: str = ""
