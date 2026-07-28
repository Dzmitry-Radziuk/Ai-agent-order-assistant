<!-- generated-by: gsd-doc-writer -->
# Развёртывание и эксплуатация АвтоСнаб

Документ описывает подготовку production-сервера, настройку GitLab CI/CD, первый запуск, обновление, резервное копирование, откат и диагностику Telegram-бота.

Команды ниже рассчитаны на Linux-сервер и Docker Compose v2. Перед выполнением замените примеры домена, путей и идентификаторов реальными значениями.

## Содержание

- [Что разворачивается](#что-разворачивается)
- [Что нужно получить до начала работ](#что-нужно-получить-до-начала-работ)
- [Подготовка сервера](#подготовка-сервера)
- [DNS, HTTPS и firewall](#dns-https-и-firewall)
- [Переменные приложения](#переменные-приложения)
- [Переменные GitLab CI/CD](#переменные-gitlab-cicd)
- [Первый ручной deploy](#первый-ручной-deploy)
- [Telegram webhook](#telegram-webhook)
- [GitLab CI/CD](#gitlab-cicd)
- [Проверка после deploy](#проверка-после-deploy)
- [Обновление](#обновление)
- [Миграции](#миграции)
- [Backup и восстановление](#backup-и-восстановление)
- [Rollback](#rollback)
- [Мониторинг и алерты](#мониторинг-и-алерты)
- [Диагностика](#диагностика)
- [Регламент эксплуатации](#регламент-эксплуатации)
- [Чек-лист приёмки production](#чек-лист-приёмки-production)

## Что разворачивается

```text
Internet
   │
   │ HTTPS :443
   ▼
Reverse proxy / Cloudflare
   │
   │ HTTP 127.0.0.1:8000
   ▼
FastAPI API ──► PostgreSQL
   │
   ▼
Redis ──► Celery worker
              ├── OpenAI
              ├── Telegram Bot API
              ├── Google Sheets API
              └── Google Apps Script
```

Compose-сервисы:

| Сервис | Постоянный | Назначение |
|---|---:|---|
| `postgres` | Да | Сессии, inbox Telegram, аудит заказов, привязки, этапы отправки |
| `redis` | Да | Временная очередь Celery, результаты, кэш и locks |
| `migrate` | Нет | Однократно применяет Alembic migrations |
| `api` | Да | Webhook и health endpoints |
| `worker` | Да | Вся длительная бизнес-логика |
| `beat` | Да | Ежедневно запускает очистку устаревшего аудита |

`migrate` после успешной работы имеет состояние `Exited (0)`. Это нормально.

`beat` хранит служебный файл расписания в `/tmp/celerybeat-schedule`. Файл временный:
расписание очистки задаётся в коде приложения и восстанавливается после перезапуска.
Не меняйте путь на `/app/celerybeat-schedule` — контейнер работает без root-прав,
поэтому запись в `/app` завершится ошибкой `Permission denied` и циклическим перезапуском `beat`.

Корневой `docker-compose.yml` поднимает полный локальный стек. Production
использует `docker/Dockerfile` и `docker/docker-compose.yml`: CI собирает image,
а deploy запускает API, worker и beat в серверной сети `auto-snab`.

## Что нужно получить до начала работ

Не начинайте deploy, пока нет всех пунктов:

- URL GitLab-репозитория и доступ минимум на чтение;
- имя production-ветки или тега;
- Linux-сервер с SSH-доступом;
- production-домен, например `bot.company.ru`;
- возможность создать DNS-запись домена;
- Telegram bot token;
- OpenAI API key;
- случайный Telegram webhook secret;
- JSON service account Google;
- доступ service account ко всем нужным таблицам;
- directory URL списка заведений;
- registration Spreadsheet ID;
- названия рабочих листов;
- URL и token Apps Script перерасчёта;
- список ответственных за deploy и инциденты;
- решение, где хранятся production-секреты: GitLab Variables, внешний secret manager или серверный `.env`.

Зафиксируйте отдельно:

- домен;
- IP сервера;
- путь приложения;
- ветку/tag для production;
- время ежедневного backup;
- срок хранения backup;
- канал алертов.

## Подготовка сервера

### Рекомендуемые ресурсы

Для начальной нагрузки:

- 2–4 vCPU;
- 4–8 GB RAM;
- 30 GB SSD;
- Linux x86_64;
- отдельный production-сервер.

Vision-запросы выполняются в OpenAI и не требуют GPU на сервере.

### Установить системные компоненты

Нужны:

- Git;
- Docker Engine;
- Docker Compose v2;
- `curl`;
- `rsync` для CI deploy;
- reverse proxy: Caddy, Nginx или инфраструктурный Cloudflare Tunnel.

Проверка:

```bash
git --version
docker version
docker compose version
curl --version
rsync --version
```

Не используйте устаревшую команду `docker-compose`, если доступна `docker compose`.

### Создать пользователя и каталог

Пример:

```bash
sudo useradd --create-home --shell /bin/bash autosnab
sudo usermod -aG docker autosnab
sudo install -d -o autosnab -g autosnab -m 750 /opt/autosnab-bot
sudo install -d -o autosnab -g autosnab -m 700 /opt/autosnab-bot/secrets
```

После добавления пользователя в группу `docker` нужно открыть новую login-сессию.

Участник группы `docker` фактически получает высокий уровень доступа к серверу. Добавляйте только доверенных пользователей и runner.

### Настроить время

```bash
timedatectl
sudo timedatectl set-timezone Europe/Minsk
```

Приложение отдельно использует `APP_TIMEZONE`, но правильное системное время необходимо для логов, TLS и backup.

## DNS, HTTPS и firewall

### DNS

Создайте `A`/`AAAA` запись:

```text
bot.company.ru -> production server IP
```

Проверьте:

```bash
getent hosts bot.company.ru
```

### Firewall

Снаружи обычно нужны:

- `22/tcp` — SSH, лучше только с административных IP/VPN;
- `80/tcp` — получение/обновление TLS-сертификата и redirect;
- `443/tcp` — Telegram webhook.

Порт `8000` не должен быть публичным. В `.env` используется:

```dotenv
API_BIND_HOST=127.0.0.1
API_PORT=8000
```

PostgreSQL и Redis в Compose не публикуются на host.

### HTTPS reverse proxy

Пример Caddy:

```caddyfile
bot.company.ru {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8000
}
```

После изменения:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
curl --fail https://bot.company.ru/health/live
```

Можно использовать Nginx или постоянный Cloudflare Tunnel. Cloudflare Quick Tunnel со случайным URL подходит только для разработки.

## Переменные приложения

Источник шаблона — `.env.example`.

Обозначения:

- **обязательная** — без неё production не должен запускаться;
- **секрет** — нельзя хранить в Git, Wiki, задачах и открытых логах;
- **Compose** — используется `docker-compose.yml`, а не Python `Settings`;
- **опциональная** — можно оставить значение по умолчанию.

### Приложение и процессы

| Переменная | Обязательная | Секрет | Пример/назначение |
|---|---:|---:|---|
| `APP_ENV` | Да | Нет | Только `production` на сервере |
| `PUBLIC_BASE_URL` | Да | Нет | `https://bot.company.ru`, без `/` в конце |
| `APP_TIMEZONE` | Нет | Нет | `Europe/Minsk` |
| `LOG_LEVEL` | Нет | Нет | `INFO` |
| `LOG_USER_CONTENT` | Нет | Нет | `false`; включать временно |
| `LOG_CONTENT_MAX_LENGTH` | Нет | Нет | `500`, допустимо 50–5000 |
| `API_BIND_HOST` | Да | Нет | `127.0.0.1` |
| `API_PORT` | Нет | Нет | Host-порт, обычно `8000` |
| `UVICORN_WORKERS` | Нет | Нет | Обычно `2` |
| `CELERY_CONCURRENCY` | Нет | Нет | `10`; число одновременно выполняемых задач worker |

### Telegram

| Переменная | Обязательная | Секрет | Назначение |
|---|---:|---:|---|
| `TELEGRAM_BOT_TOKEN` | Да | Да | Token от BotFather |
| `TELEGRAM_WEBHOOK_SECRET` | Да | Да | 32–256 символов `A-Z`, `a-z`, `0-9`, `_`, `-` |
| `TELEGRAM_REQUEST_TIMEOUT_SECONDS` | Нет | Нет | `15`, общее ожидание ответа Telegram API |
| `TELEGRAM_CONNECT_TIMEOUT_SECONDS` | Нет | Нет | `5`, ожидание TCP/TLS-соединения с Telegram API |

Создать webhook secret:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

### OpenAI

| Переменная | Обязательная | Секрет | Значение по умолчанию/назначение |
|---|---:|---:|---|
| `OPENAI_API_KEY` | Да | Да | API key проекта |
| `OPENAI_TEXT_MODEL` | Нет | Нет | `gpt-4o-mini` |
| `OPENAI_TEXT_TIMEOUT_SECONDS` | Нет | Нет | `12`, максимальное ожидание текстового разбора |
| `OPENAI_TEXT_MAX_RETRIES` | Нет | Нет | `0`, без скрытых повторов внутри заблокированного чата |
| `OPENAI_VISION_MODEL` | Нет | Нет | `gpt-5-mini` |
| `OPENAI_MATCH_MODEL` | Нет | Нет | `gpt-4o-mini` |
| `OPENAI_TRANSCRIBE_MODEL` | Нет | Нет | `gpt-4o-mini-transcribe` |
| `OPENAI_TRANSCRIBE_FALLBACK_MODEL` | Нет | Нет | `gpt-4o-transcribe` |
| `OPENAI_VISION_TIMEOUT_SECONDS` | Нет | Нет | `180`, допустимо 30–600 |

Перед изменением моделей прогоните тесты и ручной smoke test текста, голоса и фото.

### PostgreSQL

| Переменная | Обязательная | Секрет | Назначение |
|---|---:|---:|---|
| `POSTGRES_DB` | Да | Нет | Имя БД, например `autosnab` |
| `POSTGRES_USER` | Да | Нет | Отдельный пользователь приложения |
| `POSTGRES_PASSWORD` | Да | Да | Случайный пароль |
| `DATABASE_URL` | Да | Да | SQLAlchemy DSN с теми же DB/user/password |
| `DATABASE_POOL_SIZE` | Нет | Нет | `10` на один API/worker процесс |
| `DATABASE_MAX_OVERFLOW` | Нет | Нет | `20` |
| `DATABASE_POOL_TIMEOUT_SECONDS` | Нет | Нет | `30` |
| `DATABASE_POOL_RECYCLE_SECONDS` | Нет | Нет | `1800` |
| `ORDER_EVENT_RETENTION_DAYS` | Нет | Нет | `365`, хранение аудита заказов |
| `TELEGRAM_UPDATE_RETENTION_DAYS` | Нет | Нет | `30`, хранение завершённых Telegram updates |

Пример согласованных значений:

```dotenv
POSTGRES_DB=autosnab
POSTGRES_USER=autosnab
POSTGRES_PASSWORD=<STRONG_RANDOM_PASSWORD>
DATABASE_URL=postgresql+psycopg://autosnab:<URL_ENCODED_PASSWORD>@postgres:5432/autosnab
```

Если пароль содержит специальные символы, password внутри `DATABASE_URL` должен быть URL-encoded. Самый простой безопасный вариант — сгенерировать длинный пароль из букв и цифр.

Не меняйте `POSTGRES_DB`, `POSTGRES_USER` и `POSTGRES_PASSWORD` у уже созданного volume без плана миграции. Переменные образа PostgreSQL применяются только при первичной инициализации пустого каталога данных.

### Redis и Celery

| Переменная | Обязательная | Секрет | Значение |
|---|---:|---:|---|
| `REDIS_URL` | Да | Нет внутри Compose | `redis://redis:6379/0` |
| `CELERY_BROKER_URL` | Да | Нет внутри Compose | `redis://redis:6379/1` |
| `CELERY_RESULT_BACKEND` | Да | Нет внутри Compose | `redis://redis:6379/2` |
| `REDIS_MAXMEMORY` | Нет | Нет | `256mb`; при заполнении Redis возвращает ошибку без тихого удаления очереди |

Redis не публикуется наружу и не используется как долговечный журнал заказов. Результаты Celery автоматически истекают через 24 часа. Если Redis выносится в отдельную инфраструктуру, включите authentication/TLS и считайте URL секретом.

### Google Sheets и Apps Script

| Переменная | Обязательная | Секрет | Назначение |
|---|---:|---:|---|
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Да | Нет | `/run/secrets/google-service-account.json` |
| `GOOGLE_VENUE_DIRECTORY_URL` | Да | Обычно нет | HTTPS GViz URL directory-таблицы |
| `GOOGLE_REGISTRATION_SPREADSHEET_ID` | Да | Нет | Таблица регистрации |
| `GOOGLE_REGISTRATION_SHEET` | Нет | Нет | `Чаты` |
| `GOOGLE_CATALOG_SHEET` | Нет | Нет | `Заявка` |
| `GOOGLE_HISTORY_SHEET` | Нет | Нет | `История товары(API)`, переходная запись товарных строк |
| `GOOGLE_ORDER_STATUS_SHEET` | Нет | Нет | `История`, источник статусов заявок |
| `GOOGLE_PRODUCT_ADD_SHEET` | Нет | Нет | `Добавить` |
| `GOOGLE_RECALC_URL` | Да | Ограниченный доступ | HTTPS URL Apps Script |
| `GOOGLE_RECALC_TOKEN` | Да | Да | Token проверки вызова Apps Script |
| `GOOGLE_RECALC_SHEET` | Нет | Нет | `Заявка` |
| `VENUE_DIRECTORY_CACHE_TTL_SECONDS` | Нет | Нет | `120`, допустимо 30–3600 |
| `CATALOG_CACHE_TTL_SECONDS` | Нет | Нет | `60`, допустимо 5–3600 |
| `DEFAULT_DEPARTMENT` | Нет | Нет | `Кухня` |

Общей или резервной рабочей таблицы у бота нет. После подтверждения кода бот получает
ID таблицы конкретного заведения из `GOOGLE_VENUE_DIRECTORY_URL`. Если привязка или ID
таблицы отсутствуют, обработка заявки останавливается без обращения к Google Sheets.

Секретным является содержимое service account JSON. Spreadsheet IDs сами по себе не дают доступ к закрытой таблице, но их всё равно лучше не публиковать.

### Langfuse

| Переменная | Обязательная | Секрет | Назначение |
|---|---:|---:|---|
| `LANGFUSE_ENABLED` | Нет | Нет | `false` до настройки |
| `LANGFUSE_PUBLIC_KEY` | Если включён | Ограниченный доступ | Public key проекта |
| `LANGFUSE_SECRET_KEY` | Если включён | Да | Secret key |
| `LANGFUSE_BASE_URL` | Если включён | Нет | URL Langfuse |
| `LANGFUSE_TRACING_ENVIRONMENT` | Нет | Нет | `production` |

## Переменные GitLab CI/CD

GitLab: `Settings → CI/CD → Variables`.

Для production рекомендуется:

- `Environment scope`: `production`;
- `Protect variable`: включено;
- секреты: `Masked and hidden`, если значение соответствует требованиям GitLab;
- `Expand variable reference`: выключено;
- deploy только из protected branch/tag;
- production environment сделать protected;
- runner закрепить за проектом и защитить тегом.

GitLab предупреждает, что masking не защищает от вредоносного pipeline-кода. Любой merge, получающий production deploy, должен проходить review.

### Секретные GitLab Variables

| Key | Type | Visibility | Protected |
|---|---|---|---:|
| `TELEGRAM_BOT_TOKEN` | Variable | Masked and hidden | Да |
| `TELEGRAM_WEBHOOK_SECRET` | Variable | Masked and hidden | Да |
| `OPENAI_API_KEY` | Variable | Masked and hidden | Да |
| `POSTGRES_PASSWORD` | Variable | Masked and hidden | Да |
| `DATABASE_URL` | Variable | Masked and hidden | Да |
| `GOOGLE_RECALC_TOKEN` | Variable | Masked and hidden | Да |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | **File** | Hidden/доступное для File | Да |
| `LANGFUSE_SECRET_KEY` | Variable | Masked and hidden | Да |

Для File variable `GOOGLE_SERVICE_ACCOUNT_JSON` значение — полный JSON. Во время job переменная содержит путь к временному файлу, а не сам JSON.

### Несекретные production Variables

Их также рекомендуется сделать Protected с environment scope `production`:

```text
PUBLIC_BASE_URL
APP_TIMEZONE
API_BIND_HOST
API_PORT
UVICORN_WORKERS
CELERY_CONCURRENCY
OPENAI_TEXT_MODEL
OPENAI_VISION_MODEL
OPENAI_MATCH_MODEL
OPENAI_TRANSCRIBE_MODEL
OPENAI_TRANSCRIBE_FALLBACK_MODEL
OPENAI_VISION_TIMEOUT_SECONDS
POSTGRES_DB
POSTGRES_USER
GOOGLE_VENUE_DIRECTORY_URL
GOOGLE_REGISTRATION_SPREADSHEET_ID
GOOGLE_REGISTRATION_SHEET
GOOGLE_CATALOG_SHEET
GOOGLE_HISTORY_SHEET
GOOGLE_ORDER_STATUS_SHEET
GOOGLE_PRODUCT_ADD_SHEET
GOOGLE_RECALC_URL
GOOGLE_RECALC_SHEET
DEFAULT_DEPARTMENT
LANGFUSE_ENABLED
LANGFUSE_PUBLIC_KEY
LANGFUSE_BASE_URL
LANGFUSE_TRACING_ENVIRONMENT
```

Redis URLs и настройки pool/cache можно оставить в `.env.example`, если используется штатный Compose и значения не меняются.

### Вариант без хранения секретов в GitLab

Допустимо один раз создать на сервере:

```text
/opt/autosnab-bot/.env
/opt/autosnab-bot/secrets/google-service-account.json
```

с правами `600`, а deploy job обновляет только код. Тогда app secrets не нужны в GitLab. Этот вариант требует отдельного процесса резервного хранения и ротации серверных секретов.

Для развитой инфраструктуры лучше использовать Vault, OpenBao, Infisical или облачный secret manager и выдавать секреты job кратковременно.

## Первый ручной локальный запуск

### 1. Клонировать проект

```bash
sudo -iu autosnab
git clone <GITLAB_REPOSITORY_URL> /opt/autosnab-bot
cd /opt/autosnab-bot
```

Если Python-проект находится во вложенной директории репозитория, перейдите в каталог, содержащий:

```text
docker-compose.yml
Dockerfile
pyproject.toml
```

### 2. Создать `.env`

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Обязательные проверки:

```bash
grep -nE 'replace_|replace_me|example\.com' .env
```

Команда не должна ничего найти. Не публикуйте её вывод, если он содержит строки с секретами.

Проверьте соответствие:

- `POSTGRES_DB` в `DATABASE_URL`;
- `POSTGRES_USER` в `DATABASE_URL`;
- password в `POSTGRES_PASSWORD` и `DATABASE_URL`;
- `PUBLIC_BASE_URL` реальному домену;
- названий Google-листов реальным вкладкам.

### 3. Добавить Google JSON

```bash
install -d -m 700 secrets
install -m 600 /secure/source/google-service-account.json \
  secrets/google-service-account.json
```

Проверка без вывода содержимого:

```bash
test -s secrets/google-service-account.json
stat -c '%a %n' secrets/google-service-account.json .env
```

Ожидаемые права — `600`.

### 4. Проверить Compose

```bash
docker compose config --quiet
```

Не выполняйте `docker compose config` без `--quiet` в CI-логе: развёрнутая конфигурация может содержать секреты.

### 5. Запустить

```bash
docker compose pull postgres redis
docker compose build api worker migrate beat
docker compose up -d
docker compose ps
```

### 6. Проверить миграции

```bash
docker compose ps -a migrate
docker compose logs migrate
docker compose exec api alembic current
```

`migrate` должен завершиться с кодом `0`.

### 7. Установить webhook

```bash
docker compose exec api python -m restaurant_bot.cli set-webhook
```

### 8. Импортировать существующие привязки

Если выполняется перенос с прежней системы:

```bash
docker compose exec worker python -m restaurant_bot.cli sync-venue-bindings
```

### 9. Выполнить smoke test

1. Открыть бота.
2. Выполнить `/start`.
3. Привязаться тестовым кодом заведения.
4. Отправить товар с количеством текстом.
5. Отправить товар без количества и указать число.
6. Отправить короткое голосовое сообщение.
7. Открыть черновик.
8. Отправить тестовую заявку.
9. Проверить историю, комментарий и перерасчёт в Google Sheets.
10. Открыть «Мои заявки».

## Telegram webhook

Приложение устанавливает:

```text
https://<DOMAIN>/webhooks/telegram
```

Команда:

```bash
docker compose exec api python -m restaurant_bot.cli set-webhook
```

Повторите её после изменения:

- `PUBLIC_BASE_URL`;
- `TELEGRAM_BOT_TOKEN`;
- `TELEGRAM_WEBHOOK_SECRET`;
- самого Telegram-бота.

Webhook secret должен содержать только `A-Z`, `a-z`, `0-9`, `_`, `-`. Telegram отправляет его в `X-Telegram-Bot-Api-Secret-Token`.

Пока webhook установлен, бот не использует long polling/getUpdates.

## GitLab CI/CD

В проекте используется `.gitlab-ci.yml`. Актуальное описание обязательных jobs и правил ветки `develop` находится в [docs/ci/README.md](docs/ci/README.md).

### Фактический pipeline

```text
build: pytest + coverage
        │
        ▼
lint: Ruff
        │
        ▼
scan: Code Quality + SAST + Secret Detection + Docker build
        │
        ▼
develop → deploy-dev автоматически
main    → deploy-prod вручную
```

Jobs импортируются из `antipov-devops/ci-templates`. Проект не копирует их
реализацию, а задаёт только переменные приложения.

Source of truth для production-контейнеров:

- `docker/Dockerfile` — сборка application image;
- `docker/docker-compose.yml` — запуск API, worker и beat из готового image.

Текущий `build-hatch.yml` завершает pytest-команду через `|| true`: ошибки
тестов видны в логах, но не блокируют `build`. Если тесты должны стать
обязательной проверкой, DevOps должен убрать это поведение в корпоративном
шаблоне.

### Runners

Фактические теги берутся из корпоративных шаблонов:

- `build` — pytest, Ruff, security scan и Docker image;
- `dev` — автоматическое развёртывание `develop`;
- `prod` — ручное развёртывание `main`.

Runner должен быть locked к доверенному проекту. Production variables и runner
с тегом `prod` должны быть protected. На production нельзя запускать pipeline
из недоверенных Merge Request.

### Deploy

`build-image` публикует:

```text
${IMAGE_NAME}:${CI_COMMIT_SHORT_SHA}
${IMAGE_NAME}:latest
```

`deploy-dev` запускается для `develop`, копирует file-variable `ENV_DEV` в
`docker/.env` и выполняет `docker compose`. `deploy-prod` делает то же с
`ENV_PROD` для `main`, но требует ручного запуска.

Перед включением production:

1. проверить protected branch `main` и environment `production`;
2. проверить права runner `prod`;
3. убедиться, что `ENV_PROD` имеет тип File и не выводится в лог;
4. проверить backup и rollback;
5. выполнить тестовый deploy и health-check.

### Передача `.env`

Source of truth для runtime-конфигурации — GitLab CI/CD file variables:

- `ENV_DEV` — полный development `.env`;
- `ENV_PROD` — полный production `.env`.

Корпоративный deploy-шаблон копирует выбранный файл в `docker/.env`
непосредственно перед `docker compose`. Значения должны соответствовать
актуальному `.env.example`.

Нельзя:

- выполнять `cat .env`;
- выполнять `printenv`;
- включать `CI_DEBUG_TRACE` на production job;
- передавать секреты параметрами командной строки;
- сохранять `.env` как artifact;
- использовать `set -x`.

## Проверка после deploy

### Контейнеры

```bash
docker compose ps
docker compose ps -a migrate
```

Ожидается:

- `postgres` — `healthy`;
- `redis` — `healthy`;
- `api` — `healthy`;
- `worker` — `healthy`;
- `beat` — `running`;
- `migrate` — `Exited (0)`.

### Health

```bash
curl --fail http://127.0.0.1:8000/health/live
curl --fail http://127.0.0.1:8000/health/ready
curl --fail https://bot.company.ru/health/ready
```

### Логи

```bash
docker compose logs --tail 100 migrate
docker compose logs --tail 200 api worker beat
```

Не должно быть циклических рестартов и повторяющихся ошибок настройки.

### Ресурсы

```bash
docker stats --no-stream
docker system df
df -h
free -h
```

## Обновление

### Перед обновлением

```bash
cd /opt/autosnab-bot
git rev-parse HEAD
docker compose ps
docker compose exec -T postgres sh -c \
  'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' \
  | gzip > "/var/backups/autosnab/predeploy-$(date +%F-%H%M%S).sql.gz"
```

Проверьте, что backup не пустой:

```bash
ls -lh /var/backups/autosnab/
gzip -t /var/backups/autosnab/predeploy-*.sql.gz
```

### Выполнить обновление

При ручном deploy:

```bash
git fetch --all --tags --prune
git checkout <PRODUCTION_BRANCH_OR_TAG>
git pull --ff-only
docker compose config --quiet
docker compose build api worker migrate
docker compose up -d
docker compose ps
curl --fail --retry 10 --retry-delay 3 \
  http://127.0.0.1:8000/health/ready
```

Если изменился домен или Telegram secrets:

```bash
docker compose exec api python -m restaurant_bot.cli set-webhook
```

## Миграции

`migrate` выполняется до API и worker:

```bash
alembic upgrade head
```

Проверка:

```bash
docker compose logs migrate
docker compose exec api alembic current
docker compose exec api alembic heads
```

Перед deploy с новой миграцией:

1. прочитать файл в `alembic/versions/`;
2. проверить, нет ли удаления/переименования колонок;
3. оценить время блокировки таблиц;
4. сделать backup;
5. проверить совместимость отката приложения со схемой;
6. для рискованной миграции назначить maintenance window.

Не выполняйте `alembic downgrade` автоматически. Некоторые изменения схемы необратимы без потери данных.

## Backup и восстановление

### Что резервировать

Обязательно:

- PostgreSQL;
- `.env` в защищённом secret storage;
- Google service account JSON в защищённом secret storage;
- Caddy/Nginx/Cloudflare config;
- GitLab Runner config и deploy-инструкции.

Redis используется для очереди и кэша. Его volume постоянный, но главным источником бизнес-состояния является PostgreSQL и Google Sheets.

### Создать backup PostgreSQL

```bash
sudo install -d -m 700 /var/backups/autosnab
cd /opt/autosnab-bot
docker compose exec -T postgres sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' \
  > "/var/backups/autosnab/autosnab-$(date +%F-%H%M%S).dump"
chmod 600 /var/backups/autosnab/autosnab-*.dump
```

Проверить:

```bash
test -s /var/backups/autosnab/autosnab-YYYY-MM-DD-HHMMSS.dump
```

Копируйте backup в отдельное хранилище. Backup на том же диске не защищает от потери сервера.

### Рекомендуемый график

- ежедневно — backup PostgreSQL;
- перед каждым deploy с миграциями — отдельный backup;
- хранение daily — не менее 14 дней;
- хранение monthly — по политике компании;
- тест восстановления — минимум раз в квартал.

### Восстановление

Восстановление изменяет данные. Выполняйте в согласованное окно и сначала остановите API/worker:

```bash
cd /opt/autosnab-bot
docker compose stop api worker
```

Создайте backup текущего состояния перед восстановлением. Конкретная команда `pg_restore` зависит от того, восстанавливается пустая БД или существующая. Сначала отработайте процедуру на staging.

После восстановления:

```bash
docker compose up -d
docker compose ps
curl --fail http://127.0.0.1:8000/health/ready
```

## Rollback

### Откат только приложения

Подходит, если новая версия не внесла несовместимую миграцию:

```bash
cd /opt/autosnab-bot
git fetch --all --tags
git checkout <PREVIOUS_GOOD_TAG_OR_COMMIT>
docker compose build api worker migrate
docker compose up -d
curl --fail --retry 10 --retry-delay 3 \
  http://127.0.0.1:8000/health/ready
```

### Откат после несовместимой миграции

Нужен план для конкретной миграции:

1. остановить `api` и `worker`;
2. сохранить аварийный backup;
3. восстановить pre-deploy backup либо выполнить проверенный downgrade;
4. развернуть предыдущий commit;
5. проверить health;
6. выполнить smoke test;
7. повторно установить webhook только при изменении URL/secrets.

Не используйте `git reset --hard` и `docker compose down -v` как способ rollback.

## Мониторинг и алерты

### Что контролировать

- доступность `https://<domain>/health/ready`;
- состояние контейнеров;
- restart count;
- свободное место;
- RAM и CPU;
- размер PostgreSQL;
- очередь Celery;
- ошибки `telegram_update_failed`;
- ошибки `submission_failed`;
- ошибки OpenAI, Google Sheets, Apps Script и Telegram;
- длительность распознавания голоса/фото;
- возраст последнего успешного backup.

### Минимальные алерты

| Условие | Реакция |
|---|---|
| `/health/ready` недоступен 2–5 минут | Проверить API, PostgreSQL, Redis |
| `api` или `worker` постоянно перезапускается | Открыть logs, остановить deploy |
| Диск > 80% | Очистить безопасные logs/images, расширить диск |
| Нет backup > 26 часов | Запустить backup и проверить scheduler |
| Серия ошибок Google/Apps Script | Проверить credentials, quota и доступы |
| Celery queue растёт | Проверить worker и внешние timeout |

Для старта подходят Uptime Kuma/Prometheus + Alertmanager/Grafana. Health endpoint нужно проверять снаружи, а не только с самого сервера.

## Диагностика

### Общий порядок

```bash
cd /opt/autosnab-bot
docker compose ps
curl --fail http://127.0.0.1:8000/health/live
curl --fail http://127.0.0.1:8000/health/ready
docker compose logs --since 15m api worker
```

### Бот не отвечает

Проверить:

1. публичный HTTPS health;
2. `api` и `worker`;
3. Redis/PostgreSQL;
4. webhook после изменения домена;
5. Telegram token;
6. логи по `update_id`;
7. не закончился ли OpenAI credit/quota.

### API healthy, worker unhealthy

```bash
docker compose logs --tail 300 worker
docker compose exec redis redis-cli ping
docker compose restart worker
```

Сначала выясните причину. Restart не исправляет неверные secrets или сломанную миграцию.

### `migrate` завершился ошибкой

```bash
docker compose ps -a migrate
docker compose logs migrate
docker compose exec postgres pg_isready
```

Не запускайте старый API против частично обновлённой схемы, пока причина не понятна.

### Google Sheets возвращает ошибку

Проверить:

- существует ли JSON-файл;
- права `600`;
- email service account добавлен к таблице;
- Spreadsheet ID;
- названия вкладок;
- Google API quota;
- системное время;
- логи worker.

### Проблема только с фото

Проверить:

- `OPENAI_VISION_MODEL`;
- `OPENAI_VISION_TIMEOUT_SECONDS`;
- размер и качество фотографии;
- события `photo_ai_parsed` и `photo_command_normalized`;
- network timeout и OpenAI status.

### Медленные ответы

По `telegram_update_processed` сравнить:

- `wait_ms`;
- `parse_ms`;
- `catalog_ms`;
- `engine_ms`;
- `reply_ms`;
- `total_ms`.

Большой `wait_ms` означает очередь одного чата или занятый worker. Большой `parse_ms` — OpenAI/транскрипция/vision. Большой `catalog_ms` — Google Sheets или холодный кэш.

### Очистка Docker

Сначала посмотреть:

```bash
docker system df
```

Допустимо удалить неиспользуемые build cache/images после проверки:

```bash
docker builder prune
docker image prune
```

Не удаляйте volumes. Команды с `-v` могут уничтожить PostgreSQL и Redis data.

## Регламент эксплуатации

### Каждый deploy

- успешный pipeline;
- backup перед миграциями;
- зафиксирован предыдущий commit/tag;
- `docker compose config --quiet`;
- health после запуска;
- smoke test;
- проверка логов 10–15 минут.

### Ежедневно автоматически

- PostgreSQL backup;
- очистка `order_events` и завершённых `telegram_updates` через Celery Beat;
- внешний health check;
- disk/RAM monitoring;
- container restart monitoring.

### Еженедельно

- проверить ошибки API/worker;
- проверить длительность фото/голоса;
- проверить размер volumes и logs;
- проверить успешность backup.

### Ежемесячно

- обновить security patches ОС;
- проверить новые Docker base images и Python dependencies на staging;
- проверить права GitLab Variables и runner;
- проверить доступы Google service account;
- проверить актуальность списка ответственных.

### Ежеквартально

- тест восстановления backup;
- тест rollback;
- ротация секретов по политике;
- аудит публичных портов;
- аудит production-доступов.

## Чек-лист приёмки production

### Сервер

- [ ] Docker Engine и Compose v2 установлены.
- [ ] Приложение работает не от `root`.
- [ ] `/opt/autosnab-bot` имеет ограниченные права.
- [ ] Публичны только согласованные порты.
- [ ] Системное время синхронизировано.

### Секреты

- [ ] `.env` и Google JSON отсутствуют в Git.
- [ ] Права файлов — `600`.
- [ ] В `.env` нет `replace_*` и `example.com`.
- [ ] GitLab secrets Protected и Masked/Hidden.
- [ ] `CI_DEBUG_TRACE` выключен.
- [ ] Есть процедура ротации.

### Приложение

- [ ] `migrate` завершён с кодом `0`.
- [ ] `api`, `worker`, `postgres`, `redis` healthy, `beat` запущен.
- [ ] Локальный и публичный `/health/ready` отвечают `200`.
- [ ] Webhook установлен на правильный домен.
- [ ] `/start`, текст, голос, фото и кнопки проверены.
- [ ] Заявка записана в `История товары(API)` на переходном этапе.
- [ ] Комментарии записаны.
- [ ] Apps Script перерасчёта вызван.
- [ ] «Мои заявки» читает статусы из листа `История`.

### Надёжность

- [ ] Ежедневный backup настроен.
- [ ] Backup копируется вне сервера.
- [ ] Восстановление проверено на staging.
- [ ] Есть предыдущий production tag/commit.
- [ ] Настроены health и disk alerts.
- [ ] Ответственные знают порядок диагностики и rollback.

## Официальные справочные материалы

- GitLab CI/CD Variables: <https://docs.gitlab.com/ci/variables/>
- GitLab Pipeline Security: <https://docs.gitlab.com/ci/pipeline_security/>
- GitLab Deployment Safety: <https://docs.gitlab.com/ci/environments/deployment_safety/>
- Telegram Bot API `setWebhook`: <https://core.telegram.org/bots/api#setwebhook>
