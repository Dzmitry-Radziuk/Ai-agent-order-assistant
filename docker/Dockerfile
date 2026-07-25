# syntax=docker/dockerfile:1.7

# ── builder ──────────────────────────────────────────────────────────
FROM python:3.14-slim-bookworm AS builder
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && \
    apt-get install -y --no-install-recommends gcc libpq-dev
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir .

# ── runtime ──────────────────────────────────────────────────────────
FROM python:3.14-slim-bookworm AS runtime
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && \
    apt-get install -y --no-install-recommends libpq5 curl
RUN groupadd -r app && useradd --no-log-init -r -g app -d /app app
WORKDIR /app
RUN chown app:app /app
COPY --from=builder --chown=app:app /usr/local/lib/python3.14/site-packages /usr/local/lib/python3.14/site-packages
COPY --from=builder --chown=app:app /usr/local/bin /usr/local/bin
COPY --chown=app:app src ./src
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app docker/certs/root.crt /app/certs/root.crt
RUN mkdir -p /app/.postgresql && \
    cp /app/certs/root.crt /app/.postgresql/root.crt && \
    chown -R app:app /app/.postgresql
ENV PYTHONUNBUFFERED=1
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -sf http://localhost:8000/health/ready || exit 1
CMD ["uvicorn", "restaurant_bot.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
