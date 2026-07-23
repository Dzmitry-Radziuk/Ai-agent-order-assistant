FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN addgroup --system app && adduser --system --ingroup app app
WORKDIR /app

COPY --chown=app:app pyproject.toml README.md ./
COPY --chown=app:app src ./src
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app alembic.ini ./
RUN pip install .

USER app
EXPOSE 8000
STOPSIGNAL SIGTERM
CMD ["uvicorn", "restaurant_bot.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
