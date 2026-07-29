<!-- generated-by: gsd-doc-writer -->
# АвтоСнаб — Telegram-бот закупок для заведений

Production-приложение на Python принимает заявки от поваров и сотрудников кафе текстом, голосом или фотографией, помогает уточнить товары и записывает готовый заказ в Google Sheets.

Бот работает самостоятельно. n8n не участвует в обработке сообщений и не нужен для запуска или тестирования. Экспорт workflow остаётся только справочником исходного поведения.

## Содержание

- [Что умеет бот](#что-умеет-бот)
- [Как проходит заявка](#как-проходит-заявка)
- [Архитектура](#архитектура)
- [Структура проекта](#структура-проекта)
- [Быстрый запуск через Docker](#быстрый-запуск-через-docker)
- [Локальная разработка](#локальная-разработка)
- [Настройки](#настройки)
- [Google Sheets](#google-sheets)
- [Telegram webhook](#telegram-webhook)
- [API и проверки здоровья](#api-и-проверки-здоровья)
- [База данных и миграции](#база-данных-и-миграции)
- [Фоновые задачи](#фоновые-задачи)
- [Логирование](#логирование)
- [Журнал жизненного цикла заказа](#журнал-жизненного-цикла-заказа)
- [Тесты и качество кода](#тесты-и-качество-кода)
- [GitLab CI](#gitlab-ci)
- [Обновление приложения](#обновление-приложения)
- [Частые проблемы](#частые-проблемы)
- [Документация для эксплуатации](#документация-для-эксплуатации)

## Что умеет бот

- регистрирует пользователя по коду заведения или invite-ссылке;
- изолирует каталог, черновик, историю и настройки каждого заведения;
- принимает один товар или список товаров текстом;
- расшифровывает голосовые сообщения;
- распознаёт печатные и рукописные списки товаров на фотографиях;
- в исходной таблице заказа берёт количество только из колонок «Зал», «Бар» и «Кухня», а строки без заказанного количества пропускает;
- отличает количество заказа от справочной фасовки, остатка и других напечатанных значений;
- учитывает рукописное количество и заменяет им зачёркнутое значение, не складывая старое и новое;
- извлекает комментарии к отдельным товарам и общий комментарий к заявке;
- понимает разговорные текстовые и голосовые команды на разных этапах диалога;
- учитывает отрицание: фразы вроде «не добавляй» или «не отправляй» не запускают противоположное действие;
- после отправки позволяет начать пустую новую заявку, а при активном черновике сначала просит подтверждение;
- не подставляет конкретный товар по слишком общему запросу вроде «говядина»;
- показывает похожие варианты и позволяет выбрать номером или названием;
- запрашивает отсутствующее количество;
- проверяет единицу измерения и кратность;
- предупреждает о повторном товаре и предлагает объединить количество;
- показывает минимальную сумму поставщика;
- создаёт запрос снабженцу, если товара нет в каталоге;
- отправляет заявку по возобновляемому сценарию;
- показывает статусы ранее отправленных заявок.

### Команды Telegram

| Команда | Результат |
|---|---|
| `/start` | Начать работу или проверить привязку к заведению |
| `/help` | Показать справку |
| `/draft` | Открыть текущий черновик |
| `/orders` | Обновить и показать статусы заявок |
| `/reset` | Очистить текущий черновик |
| `/submit` | Перейти к финальной проверке заявки |

Те же действия доступны кнопками и естественными фразами. Например: «покажи черновик», «давай добавим товары», «посмотрим статусы», «очисти заявку».

## Как проходит заявка

1. Telegram отправляет update на HTTPS webhook приложения.
2. API проверяет секретный заголовок Telegram.
3. Update один раз записывается в PostgreSQL.
4. Celery worker получает задачу из Redis.
5. Бот загружает привязку пользователя и состояние диалога.
6. Текст разбирается локальным парсером и при необходимости OpenAI.
7. Голос сначала транскрибируется, фотография анализируется vision-моделью.
8. Товары сопоставляются только с каталогом текущего заведения.
9. Бот показывает уточняющие карточки либо обновлённый черновик.
10. После подтверждения worker обновляет количество и комментарии в листе `Заявка`, затем вызывает Apps Script перерасчёта.
11. Worker вызывает центральный Apps Script отправки и сохраняет возвращённый им номер заявки.
12. Пользователь получает результат отправки, а статусы затем читает из листа `История`.

Обработка одного чата выполняется последовательно под Redis-lock. Повторно доставленный Telegram update не создаёт повторную операцию.

## Архитектура

```text
Пользователь
    │
    ▼
Telegram Bot API
    │ HTTPS webhook
    ▼
FastAPI API ───────────────► PostgreSQL
    │                           │
    │ Celery task               │ состояние, inbox,
    ▼                           │ привязки, checkpoints
Redis ◄───────────────────── Celery worker
                                  │
                      ┌───────────┼───────────┐
                      ▼           ▼           ▼
                   OpenAI    Google Sheets  Telegram API
                                  │
                                  ▼
                             Apps Script
```

### Компоненты

| Компонент | Назначение |
|---|---|
| `api` | Принимает webhook и отдаёт health endpoints |
| `worker` | Обрабатывает сообщения и фоновые операции |
| `beat` | Раз в сутки запускает очистку устаревших данных |
| `migrate` | Однократно выполняет `alembic upgrade head` |
| `postgres` | Хранит долговечное состояние |
| `redis` | Временно хранит очередь Celery, результаты, кэш и блокировки; аудит заказов в Redis не хранится |

API быстро подтверждает получение update, а длительная работа выполняется worker. Поэтому Telegram не должен ждать распознавание голоса, фотографии или запись в таблицу внутри webhook-запроса.

## Структура проекта

```text
.
├── alembic/                 # миграции PostgreSQL
├── secrets/                 # локальные секретные файлы, не попадают в Git
├── src/restaurant_bot/
│   ├── api/                 # FastAPI и Telegram webhook
│   ├── domain/              # модели диалога, команд и товаров
│   ├── integrations/        # Telegram, OpenAI, Google Sheets, Redis
│   ├── repositories/        # транзакционный доступ к PostgreSQL
│   ├── services/            # бизнес-логика и сценарии
│   └── workers/             # Celery и фоновые задачи
├── tests/                   # тесты по бизнес-областям
├── docker/
│   ├── docker-compose.yml   # production-запуск готового образа через CI/CD
│   └── Dockerfile           # production-образ корпоративного pipeline
├── .env.example             # шаблон переменных
├── docker-compose.yml       # локальный полный стек приложения
├── Dockerfile               # локальная сборка образа Python
├── Makefile                 # команды разработки
├── README-DEVOPS.md         # подробная эксплуатационная инструкция
└── SECURITY.md              # правила безопасности
```

## Требования

Для запуска через Docker:

- Docker Engine;
- Docker Compose v2 (`docker compose`);
- Telegram-бот, созданный через BotFather;
- публичный HTTPS-домен или временный HTTPS-туннель;
- OpenAI API key;
- Google service account с доступом к рабочим таблицам;
- URL и токен Apps Script перерасчёта.

Для запуска без Docker:

- Python `>=3.12,<3.14`;
- PostgreSQL;
- Redis;
- те же внешние доступы Telegram, OpenAI и Google.

## Быстрый запуск через Docker

### 1. Подготовить окружение

PowerShell:

```powershell
Copy-Item .env.example .env
New-Item -ItemType Directory -Force secrets
```

Bash:

```bash
cp .env.example .env
mkdir -p secrets
```

Замените все `replace_*`, `replace_me` и `example.com` в `.env`.

### 2. Добавить Google service account

Сохраните JSON-ключ:

```text
secrets/google-service-account.json
```

Внутри контейнеров он доступен только для чтения:

```text
/run/secrets/google-service-account.json
```

### 3. Проверить Compose-конфигурацию

```bash
docker compose config --quiet
```

Корневой `docker-compose.yml` предназначен для локального запуска полного
стека. Production использует готовый image и отдельный
`docker/docker-compose.yml`; его запускает корпоративный GitLab CI/CD.

Команда не должна печатать содержимое `.env` или секретного JSON.

### 4. Собрать и запустить

```bash
docker compose up -d --build
docker compose ps
```

`migrate` должен завершиться с кодом `0`. `api`, `worker`, `beat`, `postgres` и `redis` должны быть запущены; сервисы с healthcheck должны перейти в состояние `healthy`.

### 5. Установить webhook

```bash
docker compose exec api python -m restaurant_bot.cli set-webhook
```

После изменения `PUBLIC_BASE_URL`, токена бота или webhook-secret эту команду нужно выполнить повторно.

### 6. Импортировать существующие привязки

Команда нужна при первом переносе пользователей из регистрационного листа:

```bash
docker compose exec worker python -m restaurant_bot.cli sync-venue-bindings
```

Повторный запуск безопасен: сервис импортирует актуальные привязки.

### 7. Проверить приложение

```bash
curl --fail http://127.0.0.1:8000/health/live
curl --fail http://127.0.0.1:8000/health/ready
docker compose logs --tail 100 api worker
```

Ожидаемые ответы:

```json
{"status":"ok"}
```

```json
{"status":"ready"}
```

Подробный production-порядок находится в [README-DEVOPS.md](README-DEVOPS.md).

## Локальная разработка

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
$env:APP_ENV = "local"
```

### Linux/macOS

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
export APP_ENV=local
```

При `APP_ENV=local` файл `.env` автоматически не читается. Это защищает разработчика от случайного подключения к production. Передайте нужные значения через переменные процесса либо используйте Docker Compose.

Запуск API:

```bash
uvicorn restaurant_bot.api.app:app --host 0.0.0.0 --port 8000 --reload
```

Запуск worker в другом терминале:

```bash
celery -A restaurant_bot.workers.celery_app.celery_app worker -l INFO
```

Миграции:

```bash
alembic upgrade head
alembic current
```

## Настройки

Настройки описаны в `src/restaurant_bot/config.py`, шаблон — в `.env.example`.

### Как загружается `.env`

- `APP_ENV=production`: `get_settings()` читает `.env`;
- `APP_ENV=local` или `APP_ENV=test`: `.env` автоматически не читается;
- переменные процесса имеют приоритет над значениями `.env`;
- Docker Compose передаёт `.env` каждому Python-сервису через `env_file`.

Для прямого production-запуска без Compose сначала задайте `APP_ENV` в окружении процесса:

```bash
export APP_ENV=production
uvicorn restaurant_bot.api.app:app --host 0.0.0.0 --port 8000
```

### Группы переменных

| Группа | Основные переменные |
|---|---|
| Приложение | `APP_ENV`, `PUBLIC_BASE_URL`, `APP_TIMEZONE`, `LOG_LEVEL` |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET` |
| OpenAI | `OPENAI_API_KEY`, модели и timeout |
| PostgreSQL | `POSTGRES_*`, `DATABASE_URL`, настройки пула и сроки хранения аудита |
| Redis/Celery | `REDIS_URL`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, `REDIS_MAXMEMORY` |
| Google | Spreadsheet IDs, названия листов, service account, Apps Script |
| Кэш | `CATALOG_CACHE_TTL_SECONDS`, `VENUE_DIRECTORY_CACHE_TTL_SECONDS` |
| Наблюдаемость | `LANGFUSE_*`, `LOG_USER_CONTENT` |
| Compose | `API_BIND_HOST`, `API_PORT`, `UVICORN_WORKERS`, `CELERY_CONCURRENCY` |

Production-валидация прекращает запуск, если:

- `PUBLIC_BASE_URL` не HTTPS или оставлен примером;
- webhook-secret не содержит 32–256 символов `A-Z`, `a-z`, `0-9`, `_`, `-`;
- Telegram, OpenAI или Apps Script secret не заполнен;
- Google IDs/URL не заполнены;
- `DATABASE_URL` содержит тестовые или шаблонные реквизиты;
- Langfuse включён без полного набора настроек.

Сгенерировать webhook-secret:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Полная таблица переменных, их секретность и способ создания в GitLab приведены в [README-DEVOPS.md](README-DEVOPS.md#переменные-gitlab-cicd).

## Google Sheets

Приложение использует:

- каталог текущего заведения;
- лист истории товаров;
- лист запросов на добавление товара;
- регистрационный лист привязок;
- directory URL со списком заведений;
- Apps Script для перерасчёта.

Названия листов задаются переменными, а не зашиваются в логику развёртывания.

Service account должен иметь минимально необходимый доступ:

- чтение directory/регистрационного источника;
- чтение и запись рабочих таблиц заведений;
- без доступа к лишним файлам Google Drive.

Если service account не добавлен в доступ конкретной таблицы, API и worker запустятся, но операции с этой таблицей будут завершаться ошибкой.

## Telegram webhook

Webhook приложения:

```text
{PUBLIC_BASE_URL}/webhooks/telegram
```

Telegram передаёт secret в заголовке:

```text
X-Telegram-Bot-Api-Secret-Token
```

API сравнивает значение безопасным способом и возвращает `401`, если заголовок неверный.

Webhook работает только через публичный HTTPS URL. Для локальной проверки можно временно использовать Cloudflare Quick Tunnel:

```bash
cloudflared tunnel --url http://127.0.0.1:8000 --no-autoupdate
```

Quick Tunnel не предназначен для production: его URL меняется после перезапуска. После каждого изменения URL обновите `PUBLIC_BASE_URL`, пересоздайте `api` и `worker`, затем снова выполните `set-webhook`.

## API и проверки здоровья

| Метод | URL | Назначение |
|---|---|---|
| `GET` | `/health/live` | Процесс API запущен |
| `GET` | `/health/ready` | PostgreSQL и Redis доступны |
| `POST` | `/webhooks/telegram` | Входящие Telegram updates |

В production Swagger и ReDoc отключены. В `local` и `test` доступны:

- `/docs`;
- `/redoc`.

`/health/ready` не проверяет OpenAI, Telegram и Google, чтобы краткая ошибка внешнего сервиса не перезапускала здоровый API-процесс.

## База данных и миграции

Основные таблицы:

| Таблица | Назначение |
|---|---|
| `bot_sessions` | Состояние диалога и черновик |
| `telegram_updates` | Идемпотентный inbox Telegram |
| `submission_records` | Этапы и контрольные точки отправки |
| `order_events` | Ограниченный журнал заказа от первого товара до завершения |
| `venue_bindings` | Привязка пользователя/чата к заведению |

`migrate` — одноразовый Compose-сервис. Он запускает:

```bash
alembic upgrade head
```

API и worker начинают работу только после успешного завершения миграций. Контейнер `migrate` со статусом `Exited (0)` — нормальное состояние.

Создание новой миграции:

```bash
alembic revision --autogenerate -m "описание изменения"
alembic upgrade head
```

Автогенерированную миграцию обязательно нужно проверить вручную.

## Фоновые задачи

Celery выполняет:

- `restaurant_bot.process_telegram_update`;
- `restaurant_bot.submit_order`;
- `restaurant_bot.submit_product_add`;
- `restaurant_bot.send_order_status`;
- `restaurant_bot.cleanup_expired_audit_data`.

Отправка заявки имеет контрольные точки:

1. обновление количества и комментариев в листе `Заявка`;
2. вызов Apps Script перерасчёта;
3. фиксация начала центральной отправки до HTTP POST;
4. вызов центрального Apps Script и сохранение возвращённого номера заявки;
5. фиксация финального состояния;
6. отправка подтверждения в Telegram.

До начала центральной отправки worker может безопасно повторить незавершённую операцию.
После начала HTTP POST автоматический повтор запрещён: при таймауте бот предупреждает
пользователя и просит проверить заявку через менеджера по снабжению. Это защищает
поставщиков от дублирующих заказов при неоднозначном сетевом результате.

Локально центральная отправка по умолчанию выключена:
`GOOGLE_ORDER_SUBMISSION_ENABLED=false`. В этом режиме финальное подтверждение
записывает количества и комментарии в лист `Заявка`, запускает его перерасчёт и
завершает локальный черновик. После этого процесс останавливается: центральный
Apps Script отправки не вызывается, поэтому поставщики и центральная таблица заявку
не получают. Реальную отправку нужно включать явно и только в разрешённом окружении.

Пока отправка выключена и у локального пользователя ещё нет собственных номеров
заявок, экран статусов показывает последнюю готовую заявку из листа `История` с
явной пометкой «Тестовые данные». Сам экран статусов работает только на чтение.
После переключения `GOOGLE_ORDER_SUBMISSION_ENABLED=true` тестовый
fallback автоматически отключается: бот показывает только номера, полученные при
реальной отправке этого пользователя.

Отправка синхронизируется отдельно для каждой таблицы заведения:
параллельные заявки разных заведений не блокируют друг друга.

Celery Beat запускает очистку ежедневно в `03:15` по `APP_TIMEZONE`. Результаты задач Celery хранятся в Redis не более 24 часов. Redis ограничен `REDIS_MAXMEMORY` и использует `noeviction`: очередь не вытесняется молча, а при достижении лимита запись завершается явной ошибкой, которую нужно отслеживать.

## Логирование

API и worker пишут структурированные JSON-события в stdout:

```bash
docker compose logs -f --tail 200 api worker
docker compose logs --since 15m worker
```

Один запрос удобно отслеживать по `update_id`. Идентификаторы пользователей, чатов, заведений и таблиц хешируются.

По умолчанию:

```dotenv
LOG_LEVEL=INFO
LOG_USER_CONTENT=false
LOG_CONTENT_MAX_LENGTH=500
```

При `LOG_USER_CONTENT=false` пользовательские тексты и комментарии заменяются длиной и SHA-256-отпечатком. Для короткой диагностики можно временно включить `true`, но такие логи содержат данные заявок. После диагностики верните `false`.

Никогда не выводите в лог:

- `.env`;
- service account JSON;
- Telegram/OpenAI tokens;
- `DATABASE_URL`;
- все переменные окружения через `env` или `printenv`.

## Журнал жизненного цикла заказа

Когда в черновике появляется первый товар, бот создаёт внутренний `trace_id`. Он не меняется до отмены или успешного завершения заказа. При подтверждении к нему привязывается пользовательский номер заявки `order_no`.

В PostgreSQL сохраняются только ключевые события:

- начало заказа;
- действия пользователя с названием команды, этапом и количеством позиций — без текста сообщения и названий товаров;
- отмена заказа;
- запрос отправки;
- обновление листа `Заявка`, перерасчёт и центральная отправка;
- успешное завершение и отправка подтверждения;
- окончательная ошибка отправки.

Каждое событие имеет уникальный `idempotency_key`, поэтому повторная доставка Telegram update или Celery task не создаёт дубликат. `telegram_user_id` обозначает человека, `telegram_chat_id` — чат, а `trace_id` — конкретный сценарий заказа.

Сроки хранения задаются в `.env`:

```dotenv
ORDER_EVENT_RETENTION_DAYS=365
TELEGRAM_UPDATE_RETENTION_DAYS=30
REDIS_MAXMEMORY=256mb
```

События заказа старше `ORDER_EVENT_RETENTION_DAYS` удаляются автоматически. Из `telegram_updates` удаляются только старые завершённые записи со статусом `done`, `ignored` или `failed`; активные записи очистка не затрагивает. Большие тексты, аудио, фотографии и полные пользовательские сообщения в `order_events` не сохраняются.

После обновления обязательно примените миграцию:

```bash
docker compose run --rm migrate
```

## Тесты и качество кода

Установка dev-зависимостей:

```bash
python -m pip install -e ".[dev]"
```

Полная проверка:

```bash
ruff format --check src tests alembic
ruff check src tests alembic
mypy src
python -m pytest
```

Только один файл:

```bash
python -m pytest tests/conversation/test_add_more_prompt.py
```

Один тест:

```bash
python -m pytest tests/ai/test_ai_media.py::test_ai_cannot_turn_a_product_name_into_add_more_navigation
```

Тесты не обращаются к production Telegram, OpenAI или Google Sheets и не запускают n8n. Структура тестов описана в [tests/README.md](tests/README.md).

Production-код проверяется Ruff на короткие однострочные docstring. Docstring пишутся на русском языке.

## Пользовательские сценарии

- [Полный каталог сценариев](docs/USER_SCENARIOS.md) — действия сотрудника кафе, ответы бота, результаты и Mermaid-схемы;
- [HTML-каталог](docs/user-scenarios/index.html) — локальная страница с поиском и фильтрами;
- [Единый источник](docs/user-scenarios/scenarios.json) — данные, из которых генерируются оба представления.

Чтобы обновить Markdown и HTML после изменения сценариев:

```powershell
.\.venv\Scripts\python.exe scripts\generate_user_scenarios.py
```

HTML-файл автономный: его можно открыть двойным щелчком в браузере, сервер и интернет не нужны.

## GitLab CI

CI использует корпоративные шаблоны DevOps. Фактическая последовательность:

- `build` устанавливает `.[dev]`, запускает полный pytest и сохраняет HTML-отчёт покрытия;
- `lint` выполняет Ruff;
- `scan` запускает проверки безопасности и собирает Docker image;
- `deploy-dev` автоматически обновляет `develop`, а `deploy-prod` запускается вручную для `main`.

Правила веток, jobs и локальные команды описаны в [docs/ci/README.md](docs/ci/README.md).

## Обновление приложения

Перед обновлением:

1. сохранить backup PostgreSQL;
2. прогнать тесты;
3. проверить новые миграции;
4. сохранить текущий commit/tag для rollback.

Обновление:

```bash
docker compose build api worker migrate
docker compose up -d api worker
docker compose ps
curl --fail http://127.0.0.1:8000/health/ready
```

Миграции применяются через зависимый сервис `migrate`. Обычная пересборка не удаляет volumes `postgres_data` и `redis_data`.

Не выполняйте `docker compose down -v` на сервере с данными.

## Частые проблемы

### Бот не реагирует

Проверьте:

```bash
docker compose ps
curl --fail http://127.0.0.1:8000/health/ready
docker compose logs --since 10m api worker
```

Если используется временный туннель, убедитесь, что процесс `cloudflared` работает и его URL совпадает с `PUBLIC_BASE_URL`. После смены URL повторно установите webhook.

### `migrate` имеет статус `Exited`

`Exited (0)` означает, что миграции успешно применены. Ошибкой является ненулевой exit code:

```bash
docker compose logs migrate
```

### API healthy, но Google Sheets не работает

Проверьте:

- файл `secrets/google-service-account.json`;
- значение `GOOGLE_SERVICE_ACCOUNT_FILE`;
- доступ service account к каждой таблице;
- правильность spreadsheet IDs и названий листов;
- логи worker.

### Заявка записалась частично

Не отправляйте строки вручную повторно до проверки `submission_records` и логов. Worker умеет продолжить с незавершённой контрольной точки.

### После обновления production не запускается

Посмотрите сообщение Pydantic:

```bash
docker compose logs migrate api worker
```

Чаще всего остались `replace_*`, неверный HTTPS URL, короткий webhook-secret либо несогласованные `POSTGRES_PASSWORD` и `DATABASE_URL`.

## Документация для эксплуатации

- [docs/USER_SCENARIOS.md](docs/USER_SCENARIOS.md) — пользовательские маршруты, ожидаемые ответы и связанные pytest-тесты;
- [README-DEVOPS.md](README-DEVOPS.md) — сервер, GitLab Variables, первый deploy, CI/CD, backup, rollback, мониторинг;
- [SECURITY.md](SECURITY.md) — требования безопасности;
- [tests/README.md](tests/README.md) — структура тестов.
