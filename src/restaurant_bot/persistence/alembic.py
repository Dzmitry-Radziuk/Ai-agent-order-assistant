"""Предоставляет зарегистрированные ORM-метаданные для Alembic."""

from restaurant_bot.persistence import models as persistence_models  # noqa: F401
from restaurant_bot.persistence.database import Base

target_metadata = Base.metadata
