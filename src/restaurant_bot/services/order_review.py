from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import structlog
from redis import Redis

from restaurant_bot.config import Settings
from restaurant_bot.db import SessionLocal
from restaurant_bot.domain.models import BotReply, Button, SessionStage
from restaurant_bot.integrations.cache import chat_lock
from restaurant_bot.integrations.google_sheets import GoogleSheetsGateway
from restaurant_bot.integrations.telegram import TelegramClient
from restaurant_bot.repositories.sessions import SessionRepository
from restaurant_bot.services.text import escape
from restaurant_bot.services.venue_registration import VenueContext, VenueRegistrationService

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ReviewItem:
    """Хранит одну активную позицию из текущего листа заявки."""

    product_id: str
    name: str
    supplier: str
    unit: str
    quantity: float
    comment: str = ""


@dataclass(frozen=True, slots=True)
class ReviewSnapshot:
    """Хранит проверяемый снимок текущей заявки заведения."""

    venue_code: str
    venue_name: str
    spreadsheet_id: str
    items: tuple[ReviewItem, ...]
    fingerprint: str

    @property
    def supplier_count(self) -> int:
        """Возвращает количество поставщиков в текущей заявке."""
        return len({item.supplier for item in self.items if item.supplier})


class OrderReviewService:
    """Читает текущую заявку и безопасно обрабатывает её подтверждение."""

    def __init__(
        self,
        settings: Settings,
        redis: Redis[Any],
        telegram: TelegramClient,
        sheets: GoogleSheetsGateway,
    ) -> None:
        """Инициализирует сервис просмотра заявки."""
        self.settings = settings
        self.redis = redis
        self.telegram = telegram
        self.sheets = sheets
        self.registration = VenueRegistrationService(settings, redis, sheets)

    def snapshot(self, context: VenueContext) -> ReviewSnapshot:
        """Читает позиции с ненулевым количеством из листа каталога-заявки."""
        items: list[ReviewItem] = []
        for product in self.sheets.load_catalog(context.spreadsheet_id):
            quantities = (
                product.department_quantities.hall or 0,
                product.department_quantities.bar or 0,
                product.department_quantities.kitchen or 0,
            )
            quantity = sum(value for value in quantities if value > 0)
            if quantity <= 0:
                continue
            items.append(
                ReviewItem(
                    product_id=product.product_id,
                    name=product.name,
                    supplier=product.supplier,
                    unit=product.unit or "шт",
                    quantity=quantity,
                    comment=product.comment,
                )
            )
        payload = [
            {
                "product_id": item.product_id,
                "name": item.name,
                "supplier": item.supplier,
                "unit": item.unit,
                "quantity": item.quantity,
                "comment": item.comment,
            }
            for item in items
        ]
        fingerprint = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return ReviewSnapshot(
            venue_code=context.venue_code,
            venue_name=context.venue_name,
            spreadsheet_id=context.spreadsheet_id,
            items=tuple(items),
            fingerprint=fingerprint,
        )

    @staticmethod
    def new_token() -> str:
        """Создаёт короткий одноразовый токен карточки проверки."""
        return uuid4().hex[:20]

    @staticmethod
    def preview_reply(
        snapshot: ReviewSnapshot,
        token: str,
        *,
        changed: bool = False,
        edit_message_id: int | None = None,
    ) -> BotReply:
        """Формирует карточку заявки с кнопками подтверждения и отмены."""
        if not snapshot.items:
            return BotReply(
                text=(
                    "🛒 <b>Текущая заявка пуста</b>\n\n"
                    "В таблице пока нет товаров с указанным количеством."
                ),
                rows=[[Button(text="↩️ Закрыть", callback_data=f"v2:review_cancel:{token}")]],
                edit_message_id=edit_message_id,
            )
        lines = [
            "🛒 <b>Проверьте текущую заявку</b>",
            f"Заведение: {escape(snapshot.venue_name)}",
            f"Товаров: {len(snapshot.items)}",
            f"Поставщиков: {snapshot.supplier_count}",
        ]
        if changed:
            lines += ["", "⚠️ Таблица изменилась. Проверьте обновлённый состав заявки."]
        lines += ["", "<b>Товары по поставщикам:</b>"]
        grouped: dict[str, list[ReviewItem]] = {}
        for item in snapshot.items:
            supplier = item.supplier.strip() or "Поставщик не указан"
            grouped.setdefault(supplier, []).append(item)

        rendered_count = 0
        truncated = False
        for supplier, items in grouped.items():
            supplier_header = f"<b>{escape(supplier)}</b>"
            for item_index, item in enumerate(items):
                item_lines = [
                    f"• {escape(item.name)} — {format_quantity(item.quantity)} {escape(item.unit)}"
                ]
                if item.comment:
                    item_lines.append(f"  Комментарий: {escape(item.comment)}")
                item_block = "\n".join(item_lines)
                prefix = supplier_header if item_index == 0 else ""
                separator = "\n\n" if prefix else "\n"
                candidate = (
                    "\n".join(lines)
                    + separator
                    + (f"{prefix}\n{item_block}" if prefix else item_block)
                )
                if len(candidate) > 3600:
                    truncated = True
                    break
                if prefix:
                    lines.extend(["", prefix])
                lines.extend(item_lines)
                rendered_count += 1
            if truncated:
                break
        if truncated:
            lines.append(
                f"… и ещё {len(snapshot.items) - rendered_count} поз. "
                "Все товары повторно проверяются перед отправкой."
            )
        rows = [
            [Button(text="✅ Отправить заявку", callback_data=f"v2:review_submit:{token}")],
            [Button(text="↩️ Отмена", callback_data=f"v2:review_cancel:{token}")],
        ]
        return BotReply(text="\n".join(lines), rows=rows, edit_message_id=edit_message_id)

    def submit(self, chat_id: str, token: str) -> None:
        """Повторно проверяет заявку и отправляет её через существующий Web App."""
        with chat_lock(self.redis, chat_id, timeout=300), SessionLocal.begin() as db:
            sessions = SessionRepository(db)
            row, state = sessions.get_for_update(chat_id)
            if state.review_token != token:
                return
            context = self.registration.context_for_identity(
                state.telegram_user_id or chat_id,
                state.telegram_chat_id or chat_id,
                force_refresh=True,
            )
            if context is None or context.venue_code != state.review_venue_code:
                state.review_submission_in_progress = False
                state.review_mode = "cart"
                sessions.save(chat_id, state, row)
                self.telegram.send_reply(
                    chat_id,
                    self.registration.access_disabled_reply(),
                )
                return
            current = self.snapshot(context)
            if current.fingerprint != state.review_snapshot_hash:
                token = self.new_token()
                state.review_token = token
                state.review_snapshot_hash = current.fingerprint
                state.review_submission_in_progress = False
                state.stage = SessionStage.REVIEW
                state.status = "review"
                sessions.save(chat_id, state, row)
                self.telegram.send_reply(
                    chat_id,
                    self.preview_reply(
                        current,
                        token,
                        changed=True,
                        edit_message_id=state.ui_message_id,
                    ),
                )
                return
            if not self.settings.google_order_submission_enabled:
                state.review_submission_in_progress = False
                state.stage = SessionStage.REVIEW
                state.status = "review"
                sessions.save(chat_id, state, row)
                self.telegram.send_reply(
                    chat_id,
                    BotReply(
                        text=(
                            "ℹ️ <b>Отправка пока отключена</b>\n\n"
                            "Заявка проверена, но поставщикам ничего не отправлено. "
                            "Данные таблицы не изменены."
                        ),
                        rows=[
                            [
                                Button(
                                    text="🔄 Обновить заявку",
                                    callback_data=f"v2:review:{state.review_venue_code}",
                                )
                            ]
                        ],
                        edit_message_id=state.ui_message_id,
                    ),
                )
                return
            try:
                prepared = self.sheets.prepare_order_submission(
                    current.spreadsheet_id,
                    f"review:{token}",
                )
                result = self.sheets.send_order_submission(prepared)
            except Exception:
                logger.exception("review_order_submission_failed", chat_id=chat_id)
                state.review_submission_in_progress = False
                state.stage = SessionStage.REVIEW
                state.status = "review"
                sessions.save(chat_id, state, row)
                self.telegram.send_reply(
                    chat_id,
                    BotReply(
                        text=(
                            "⚠️ <b>Не удалось отправить заявку</b>\n\n"
                            "Таблица не изменена. Попробуйте ещё раз позже."
                        ),
                        rows=[
                            [
                                Button(
                                    text="🔄 Повторить",
                                    callback_data=f"v2:review_submit:{token}",
                                )
                            ]
                        ],
                        edit_message_id=state.ui_message_id,
                    ),
                )
                return
            state.review_token = ""
            state.review_snapshot_hash = ""
            state.review_venue_code = ""
            state.review_submission_in_progress = False
            state.review_mode = "cart"
            state.stage = SessionStage.SUBMITTED
            state.status = "submitted"
            sessions.save(chat_id, state, row)
            self.telegram.send_reply(
                chat_id,
                BotReply(
                    text=(
                        f"✅ <b>Заявка отправлена</b>\n\nНомер заявки: "
                        f"{escape(result.order_number)}\nТоваров: {result.base_rows}"
                    ),
                    edit_message_id=state.ui_message_id,
                ),
            )


def format_quantity(value: float) -> str:
    """Форматирует количество без лишних нулей."""
    return str(int(value)) if value.is_integer() else f"{value:.3f}".rstrip("0").rstrip(".")
