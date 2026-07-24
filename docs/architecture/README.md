<!-- generated-by: gsd-doc-writer -->

# Архитектура Restaurant Procurement Bot

Этот документ описывает архитектуру Python-приложения по модели C4. Модель построена по текущим исходникам в `src/restaurant_bot/`, конфигурациям `docker-compose.yml` и `docker-compose.prod.yml`, миграциям Alembic и настройкам из `pyproject.toml`.

## Границы системы

Restaurant Procurement Bot отвечает за приём сообщения из Telegram, ведение черновика заказа, проверку товаров по каталогу заведения и надёжную отправку заявки. Telegram, OpenAI, Google Sheets, Google Apps Script и опциональный Langfuse являются внешними системами.

PostgreSQL — долговечное хранилище состояния и аудита. Redis используется только как временный Celery broker/backend, кэш и механизм блокировки чата.

## C4-представления

Исходная модель находится в [`c4/workspace.dsl`](c4/workspace.dsl).

| Представление | Что отвечает |
|---|---|
| C1 — System Context | Кто использует бота и от каких внешних систем он зависит |
| C2 — Containers | Какие исполняемые процессы и хранилища составляют приложение |
| C3 — FastAPI API | Как webhook проверяется, нормализуется и ставится в очередь |
| C3 — Celery Worker | Как устроены оркестрация диалога, отправка заявки и интеграции |
| Dynamic — Order Lifecycle | Как заказ проходит путь от Telegram update до подтверждения |
| Deployment — Docker Compose | Как API, Worker, Beat, migration, PostgreSQL и Redis размещены при запуске |

Инструкция просмотра, проверки и готовые Mermaid-файлы для Wiki находятся в [`c4/README.md`](c4/README.md).

## Контейнеры приложения

| Контейнер | Реализация | Ответственность |
|---|---|---|
| FastAPI API | `src/restaurant_bot/api/app.py` | Health endpoints, проверка Telegram webhook, inbox и постановка задачи |
| Celery Worker | `src/restaurant_bot/workers/tasks.py` | Обработка диалога, внешние интеграции и возобновляемая отправка |
| Celery Beat | `src/restaurant_bot/workers/celery_app.py` | Ежедневный запуск очистки устаревшего аудита |
| Database Migration | `alembic/` | Применение схемы PostgreSQL до запуска API и Worker |
| PostgreSQL | `src/restaurant_bot/db_models.py` | Сессии, Telegram updates, заявки, привязки заведений и аудит |
| Redis | `src/restaurant_bot/integrations/cache.py` | Очередь, результаты Celery, кэш каталога и блокировки чатов |

## Основной поток заказа

1. Telegram вызывает `POST /webhooks/telegram`.
2. API проверяет `X-Telegram-Bot-Api-Secret-Token` и нормализует payload.
3. `UpdateRepository` идемпотентно сохраняет update в PostgreSQL.
4. API публикует `restaurant_bot.process_telegram_update` через Redis.
5. Worker блокирует чат, загружает привязку заведения и состояние диалога.
6. Ввод разбирается локально и, при необходимости, через OpenAI.
7. `ConversationEngine` применяет бизнес-правила и формирует новое состояние.
8. При подтверждении `SubmissionService` записывает историю, обновляет каталог и запускает Apps Script.
9. Состояние, контрольные точки и `order_events` фиксируются в PostgreSQL.
10. Telegram получает итоговый ответ с номером заявки.

## Ключевые компоненты Worker

| Компонент | Реализация |
|---|---|
| Оркестрация update | `src/restaurant_bot/services/orchestrator.py` |
| Бизнес-состояние диалога | `src/restaurant_bot/services/engine.py`, `src/restaurant_bot/domain/models.py` |
| Локальный разбор команд | `src/restaurant_bot/services/parser.py` |
| Регистрация заведения | `src/restaurant_bot/services/venue_registration.py` |
| Отправка заявки | `src/restaurant_bot/services/submission.py` |
| PostgreSQL repositories | `src/restaurant_bot/repositories/` |
| Telegram gateway | `src/restaurant_bot/integrations/telegram.py` |
| OpenAI gateway | `src/restaurant_bot/integrations/openai_client.py` |
| Google Sheets gateway | `src/restaurant_bot/integrations/google_sheets.py` |
| Кэш и блокировки | `src/restaurant_bot/integrations/cache.py` |

## Правила сопровождения

- C4 изменяется в том же Merge Request, что и архитектурно значимый код.
- `docs/architecture/c4/workspace.dsl` является источником истины; Wiki и изображения не редактируются отдельно.
- Имена контейнеров должны соответствовать реально разворачиваемым процессам.
- C3 показывает только устойчивые компоненты и не перечисляет каждую функцию.
- После изменения необходимо выполнить Structurizr `validate`.

## Размещение в GitLab Wiki

В Wiki рекомендуется создать короткую страницу `Архитектура`:

```markdown
# Архитектура

Актуальная C4-модель хранится вместе с кодом:
[C4 Architecture](https://gitlab.testant.online/antipov-backend/orders-asistant-2/-/blob/develop/docs/architecture/README.md).

Источник модели:
[workspace.dsl](https://gitlab.testant.online/antipov-backend/orders-asistant-2/-/blob/develop/docs/architecture/c4/workspace.dsl).
```

Постоянная отдельная ветка для C4 не нужна. Изменения готовятся в обычной feature-ветке, проходят Merge Request и попадают в основную ветку вместе с кодом.
