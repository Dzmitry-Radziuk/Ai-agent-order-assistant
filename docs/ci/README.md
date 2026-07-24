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

Quality, test и documentation jobs используют тег `dev` и выполняются общим
instance Runner `DEVELOP gitlab.testant.online`. Для проекта должна быть
включена настройка `Turn on instance runners for this project`.

Сборка и развёртывание используют корпоративные runners:

- `build-did` — BuildKit/buildx, GitLab Registry и S3 build cache;
- `group-deploy-shell` — development Docker host;
- `server-a-shell` — production Docker host.

## Обязательные jobs

| Job | Проверка |
|---|---|
| `lint` | `ruff format --check`, `ruff check`, строгий `mypy` |
| `compose-config` | базовая и production Compose-конфигурации корректно объединяются |
| `tests` | полный `pytest`, JUnit-отчёт, Cobertura coverage и порог покрытия 84% |
| `documentation` | документация изменена вместе со значимым кодом; локальные Markdown-ссылки существуют |
| `c4-architecture` | Structurizr DSL проходит `validate` и `inspect`; Mermaid-экспорт воспроизводим |
| `build` | единый immutable Docker image публикуется в GitLab Registry с S3 build cache |
| `deploy dev` | `develop` автоматически разворачивается в environment `development` |
| `deploy prod` | тег `v*` вручную разворачивается в environment `production` |

Все jobs блокирующие: `allow_failure` не используется.

## Маршрутизация доставки

Ветка `develop` публикуется в `${CI_REGISTRY_IMAGE}/dev` с SHA-тегом и
автоматически разворачивается на development host. Теги `v*` публикуются в
`${CI_REGISTRY_IMAGE}/prod`; production deploy запускается вручную.

Ветка `main` не используется для production deployment, пока она содержит
отдельную историю n8n. Полный перечень GitLab variables, требования к runners и
операционные команды описаны в [`deploy/README.md`](../../deploy/README.md).

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
