from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Protocol

from restaurant_bot.config import Settings


class Observation(Protocol):
    """Задаёт интерфейс наблюдения за трассировкой."""

    def update(self, **kwargs: Any) -> Any:
        """Обновляет наблюдение трассировки."""
        ...


class NoopObservation:
    """Игнорирует обновления трассировки, когда она выключена."""

    def update(self, **kwargs: Any) -> None:
        """Обновляет наблюдение трассировки."""
        return None


class Tracer:
    """Создаёт обезличенные наблюдения трассировки."""

    def __init__(self, settings: Settings):
        """Инициализирует компонент."""
        self.client = None
        if (
            settings.langfuse_enabled
            and settings.langfuse_public_key
            and settings.langfuse_secret_key
        ):
            from langfuse import Langfuse

            self.client = Langfuse(
                public_key=settings.langfuse_public_key.get_secret_value(),
                secret_key=settings.langfuse_secret_key.get_secret_value(),
                base_url=settings.langfuse_base_url,
                environment=settings.langfuse_tracing_environment,
            )

    @contextmanager
    def observation(
        self,
        name: str,
        *,
        input: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[Observation]:
        """Создаёт наблюдение трассировки."""
        if self.client is None:
            yield NoopObservation()
            return
        with self.client.start_as_current_observation(
            name=name,
            as_type="span",
            input=input,
            metadata=metadata,
        ) as observation:
            yield observation

    @staticmethod
    def anonymized_chat_id(chat_id: str) -> str:
        """Возвращает обезличенный идентификатор чата."""
        return hashlib.sha256(chat_id.encode("utf-8")).hexdigest()[:16]
