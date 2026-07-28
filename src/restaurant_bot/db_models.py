from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from restaurant_bot.db import Base


class BotSession(Base):
    """Хранит состояние диалога в Telegram-чате."""

    __tablename__ = "bot_sessions"

    telegram_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    state_name: Mapped[str] = mapped_column(String(64), nullable=False, default="collecting")
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TelegramUpdate(Base):
    """Хранит идемпотентно обрабатываемое обновление Telegram."""

    __tablename__ = "telegram_updates"

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    state_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reply_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tasks_enqueued: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SubmissionRecord(Base):
    """Хранит контрольные точки отправки заявки."""

    __tablename__ = "submission_records"

    order_no: Mapped[str] = mapped_column(String(64), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(36), nullable=False, default="", index=True)
    telegram_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    catalog_updated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    recalc_done: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dispatch_started: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dispatch_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dispatch_uncertain_notified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    external_order_no: Mapped[str | None] = mapped_column(String(64), index=True)
    dispatch_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatch_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    completion_notified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class OrderEvent(Base):
    """Хранит ограниченный по размеру аудит жизненного цикла заказа."""

    __tablename__ = "order_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    trace_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    order_no: Mapped[str | None] = mapped_column(String(64), index=True)
    telegram_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    telegram_chat_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    venue_code: Mapped[str] = mapped_column(String(32), nullable=False, default="", index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ok")
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class VenueBinding(Base):
    """Хранит активную привязку пользователя к заведению."""

    __tablename__ = "venue_bindings"
    __table_args__ = (
        UniqueConstraint(
            "channel",
            "telegram_user_id",
            "telegram_chat_id",
            "venue_code",
            name="uq_venue_binding_identity",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(32), nullable=False, default="telegram")
    telegram_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    telegram_chat_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    venue_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    venue_name: Mapped[str] = mapped_column(String(255), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(255))
    spreadsheet_id: Mapped[str] = mapped_column(String(255), nullable=False)
    spreadsheet_url: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    sync_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    sync_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
