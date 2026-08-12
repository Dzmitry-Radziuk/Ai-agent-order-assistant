"""Определяет порт постановки фоновых задач."""

from __future__ import annotations

from typing import Protocol


class BackgroundTaskDispatcher(Protocol):
    """Описывает фоновые эффекты, доступные application-слою."""

    def submit_order(self, chat_id: str) -> None:
        """Ставит в очередь отправку заявки."""

    def send_order_status(
        self,
        chat_id: str,
        *,
        page: int,
        detail_page: int,
        selected_index: int | None,
        order_number: str,
    ) -> None:
        """Ставит в очередь отправку статуса заявки."""

    def submit_product_add(self, chat_id: str) -> None:
        """Ставит в очередь добавление нового товара."""

    def submit_review_order(self, chat_id: str, token: str) -> None:
        """Ставит в очередь отправку заявки из review-ссылки."""
