from uuid import uuid4


def new_review_token() -> str:
    """Создаёт короткий одноразовый токен карточки проверки."""
    return uuid4().hex[:20]
