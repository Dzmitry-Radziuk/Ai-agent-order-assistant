<!-- generated-by: gsd-doc-writer -->
# GitLab CI и правила веток

## Ветки

- `develop` — основная ветка Python-приложения.
- Новая разработка выполняется в короткоживущих feature-ветках от `develop`.
- Feature-ветка возвращается в `develop` через Merge Request с успешным pipeline.
- `main` в текущем GitLab-проекте содержит отдельную историю n8n и не является базой Python-кода.
- Force-push в `develop` не используется.

## Когда запускается pipeline

`.gitlab-ci.yml` создаёт pipeline для веток, тегов и Merge Request. Если для ветки уже открыт Merge Request, отдельный push-pipeline не создаётся — остаётся один MR pipeline.

Все jobs используют тег `dev` и выполняются общим instance Runner `DEVELOP gitlab.testant.online`. Для проекта должна быть включена настройка `Turn on instance runners for this project`.

## Обязательные jobs

| Job | Проверка |
|---|---|
| `lint` | `ruff format --check`, `ruff check`, строгий `mypy` |
| `compose-config` | базовая и production Compose-конфигурации корректно объединяются |
| `tests` | полный `pytest`, JUnit-отчёт, Cobertura coverage и порог покрытия 84% |
| `documentation` | документация изменена вместе со значимым кодом; локальные Markdown-ссылки существуют |
| `c4-architecture` | Structurizr DSL проходит `validate` и `inspect`; Mermaid-экспорт воспроизводим |

Все jobs блокирующие: `allow_failure` не используется.

## Правило актуальности документации

Изменения в следующих областях требуют изменения `README.md`, `README-DEVOPS.md`, `SECURITY.md` или файла внутри `docs/` в том же commit/Merge Request:

- `src/restaurant_bot/`;
- миграции Alembic;
- `Dockerfile`, `docker-compose.yml` и `docker-compose.prod.yml`;
- `.env.example`, `pyproject.toml` и `Makefile`;
- `.gitlab-ci.yml` и скрипты документационных проверок.

Проверку выполняет `scripts/check_docs_updated.py`. Изменения только в тестах не требуют искусственной правки документации.

## Локальная проверка

Перед push:

```bash
python -m ruff format --check src tests alembic scripts
python -m ruff check src tests alembic scripts
python -m mypy src scripts
python -m pytest
python scripts/check_markdown_links.py
python scripts/check_docs_updated.py --base HEAD^ --head HEAD
```

C4 проверяется командами из `docs/architecture/c4/README.md`.

## Настройки GitLab

После первого успешного pipeline рекомендуется защитить `develop`:

- запретить прямой push;
- разрешить изменения только через Merge Request;
- включить требование успешного pipeline перед merge;
- запретить force-push;
- потребовать минимум одного reviewer для архитектурно значимых изменений.
