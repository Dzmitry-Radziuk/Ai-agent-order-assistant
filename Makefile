.PHONY: install format lint test check run worker migrate compose-up

install:
	pip install -e '.[dev]'

format:
	ruff format src tests alembic

lint:
	ruff check src tests alembic
	mypy src

test:
	pytest --cov=restaurant_bot --cov-report=term-missing

check: lint test

run:
	uvicorn restaurant_bot.api.app:app --host 0.0.0.0 --port 8000 --reload

worker:
	celery -A restaurant_bot.workers.celery_app.celery_app worker -l INFO

migrate:
	alembic upgrade head

compose-up:
	docker compose up -d --build
