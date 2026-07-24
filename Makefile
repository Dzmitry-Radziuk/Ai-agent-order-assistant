.PHONY: install format lint test docs-check check run worker migrate compose-up

install:
	pip install -e '.[dev]'

format:
	ruff format src tests alembic scripts

lint:
	ruff format --check src tests alembic scripts
	ruff check src tests alembic scripts
	mypy src scripts

test:
	pytest --cov=restaurant_bot --cov-fail-under=84 --cov-report=term-missing

docs-check:
	python scripts/check_markdown_links.py
	python scripts/check_docs_updated.py --base HEAD^ --head HEAD

check: lint test docs-check

run:
	uvicorn restaurant_bot.api.app:app --host 0.0.0.0 --port 8000 --reload

worker:
	celery -A restaurant_bot.workers.celery_app.celery_app worker -l INFO

migrate:
	alembic upgrade head

compose-up:
	docker compose up -d --build
