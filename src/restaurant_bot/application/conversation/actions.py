"""Нейтральное представление действий интерфейса."""

from __future__ import annotations

import re

from restaurant_bot.application.conversation.contracts import SemanticAction


def decode_action_token(token: str, label: str, group: int) -> SemanticAction:
    """Разбирает технический токен действия в нейтральные поля."""
    parts = [part for part in token.split(":") if part != ""]
    namespace = parts.pop(0) if parts and parts[0] == "v2" else ""
    revision: int | None = None
    if parts and (match := re.fullmatch(r"r(\d+)", parts[-1], re.IGNORECASE)):
        revision = int(match.group(1))
        parts.pop()
    kind = parts.pop(0) if parts else ""
    target = parts.pop(0) if parts else ""
    value = ":".join(parts)
    return SemanticAction(
        kind=kind,
        label=label,
        namespace=namespace,
        target=target,
        value=value,
        revision=revision,
        group=group,
    )


def encode_action_token(action: SemanticAction) -> str:
    """Кодирует нейтральное действие в совместимый токен канала."""
    parts = [part for part in (action.namespace, action.kind, action.target, action.value) if part]
    token = ":".join(parts)
    if action.revision is not None:
        token = f"{token}:r{action.revision}"
    return token
