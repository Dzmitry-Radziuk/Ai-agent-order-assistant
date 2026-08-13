"""Определяет хранение данных «database»."""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from restaurant_bot.config import get_settings


class Base(DeclarativeBase):
    """Задаёт декларативную базу моделей базы данных."""

    pass


settings = get_settings()
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=settings.database_pool_size,
    max_overflow=settings.database_max_overflow,
    pool_timeout=settings.database_pool_timeout_seconds,
    pool_recycle=settings.database_pool_recycle_seconds,
    connect_args=(
        {"sslcert": "", "sslkey": ""} if settings.database_url.startswith("postgresql") else {}
    ),
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session]:
    """Предоставляет сессию базы данных для запроса FastAPI."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
