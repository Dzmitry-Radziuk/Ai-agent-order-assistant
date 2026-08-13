"""Описывает служебную конфигурацию и миграции Alembic."""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, make_url, pool

from alembic import context
from restaurant_bot import db_models  # noqa: F401
from restaurant_bot.config import get_settings
from restaurant_bot.persistence.database import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
config.set_main_option("sqlalchemy.url", get_settings().database_url)
target_metadata = Base.metadata


def _connect_args(url_str: str) -> dict[str, str]:
    """Empty sslcert/sslkey prevents libpq from looking for client certs."""
    if make_url(url_str).get_backend_name() == "postgresql":
        return {"sslcert": "", "sslkey": ""}
    return {}


def run_migrations_offline() -> None:
    """Запускает миграции без активного подключения."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Запускает миграции через активное подключение."""
    settings = get_settings()
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=_connect_args(settings.database_url),
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
