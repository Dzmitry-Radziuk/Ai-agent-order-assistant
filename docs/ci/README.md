<!-- generated-by: gsd-doc-writer -->
# GitLab CI и правила веток

## Источник конфигурации

Локальный `.gitlab-ci.yml` задаёт переменные проекта и подключает корпоративные
шаблоны `antipov-devops/ci-templates`:

- `build-hatch.yml` — pytest и Ruff для Python-проекта на Hatchling;
- `security-scan.yml` — Code Quality, SAST и Secret Detection;
- `docker-build.yml` — сборка и публикация Docker image;
- `deploy.yml` — развёртывание development и production.

Локальная конфигурация не копирует и не переопределяет DevOps jobs: она задаёт
переменные приложения, а сборка и доставка приходят из корпоративных шаблонов.
Production image собирается по `docker/Dockerfile`, а серверные контейнеры
описаны в `docker/docker-compose.yml`.

## Фактический pipeline

| Stage / job | Что выполняется |
|---|---|
| `build` | Установка `.[dev]`, полный pytest и HTML coverage |
| `lint` | `ruff check` и `ruff format --check` |
| `scan` | Code Quality, SAST, Secret Detection и публикация Docker image |
| `deploy-dev` | Автоматическое развёртывание ветки `develop` |
| `deploy-prod` | Ручное развёртывание ветки `main` |

Job `build` и `lint` используют runner с тегом `build`. Deploy jobs используют
теги, заданные корпоративным `deploy.yml`: `dev` и `prod`.

## Контракт тестов

Корпоративный `build` запускает:

```bash
python -m pytest ${TEST_PATH:-tests/} ${PYTEST_ARGS:---tb=short} --cov --cov-report=html --cov-report=term-missing
```

Важно: фактическая команда pytest в текущем `build-hatch.yml` заканчивается
`|| true`. Поэтому упавшие тесты видны в логах, но сами по себе не переводят
`build` в статус failed. Изменение этого правила должно выполняться
в корпоративном шаблоне DevOps, а не скрытым переопределением его `script` в
проекте.

## Ветки и доставка

- Feature-ветки создаются от `develop` и возвращаются через Merge Request.
- `develop` собирает image с SHA текущего commit и автоматически обновляет dev.
- `main` использует тот же контракт сборки; production deployment запускается
  вручную.
- Force-push в `develop` не используется.

Актуальные имена registry image, контейнера, сети и портов находятся в
`.gitlab-ci.yml`. Значения из старой схемы GitLab Registry, S3 build cache и
deploy по тегам `v*` к текущему pipeline не относятся.

## Локальная проверка

```bash
python -m pip install -e ".[dev]"
python -m ruff format --check src tests alembic scripts
python -m ruff check src tests alembic scripts
python -m mypy src scripts
python -m pytest
```

## Проверка после изменения CI

Перед merge необходимо убедиться, что:

1. `build` запускает pytest и создаёт HTML coverage;
2. `lint` выполняет проверки Ruff;
3. `build-image` и deploy jobs сохранили прежние правила веток;
4. в логи и артефакты не попали `.env`, токены или service-account JSON.
