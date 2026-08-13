# Текущая архитектура

Документ описывает фактическую структуру ветки `decompose_bot` после независимого
аудита 2026-08-13. Исполняемый код и тесты имеют приоритет. Историю декомпозиции
сохраняет [`ARCHITECTURE_DECOMPOSITION.md`](archive/architecture/ARCHITECTURE_DECOMPOSITION.md), но её
старые SHA, размеры файлов и verdict не являются текущим состоянием.

## Состояние

- исходный HEAD аудита: `4dc67430336bb90cfad731aca583f51a19948a48`;
- свежий baseline до документных изменений: `1391 collected / 1391 passed`;
- Python: 3.12+, строгий mypy, Ruff;
- реализованный пользовательский канал: Telegram;
- production-код в рамках аудита не менялся;
- следующий шаг: закрыть условия контролируемого pilot, а не продолжать общую
  архитектурную декомпозицию.

Финальный SHA и повторные проверки этой документной кампании записаны в
[`TESTING_READINESS.md`](TESTING_READINESS.md) и `PROJECT_HANDOFF.md` после commit.

## Владельцы ответственности

| Пакет | Текущая ответственность |
|---|---|
| `api` | FastAPI webhook, аутентификация входа и health endpoints |
| `application` | нейтральные conversation-контракты, порты и use case |
| `catalog` | evidence, retrieval, ranking, auto-select safety и resolver |
| `conversation` | state policy, routing decisions, черновик и progression |
| `domain` | модели состояния, команды, товара и ответа |
| `input` | перевод Telegram update в нейтральное взаимодействие |
| `integrations` | Telegram, OpenAI, Google Sheets, Redis, Apps Script и tracing adapters |
| `observability` | события и безопасные для логирования метаданные |
| `orders` | quantity multiple, supplier minimum и catalog-to-draft resolution |
| `parsing` | deterministic extraction, AI schemas и reconciliation |
| `persistence` | общие транзакционные и lifecycle-контракты |
| `presentation` | Telegram copy, кнопки, карточки и legacy reply mapping |
| `repositories` | транзакционный доступ к PostgreSQL |
| `services` | защищённые runtime/effect coordinators |
| `venues` | коды, контракты и правила доступа заведений |
| `workers` | Celery entrypoints, redrive и фоновые операции |

`services` не является универсальным слоем бизнес-логики. Оставшиеся там крупные
модули координируют lease, транзакции, provider calls, checkpoints и доставку;
чистые правила находятся в тематических пакетах.

## Фактический путь Telegram update

```text
Telegram webhook
  → api/app.py: проверка secret + durable inbox
  → workers/tasks.py: Celery entrypoint
  → services/orchestrator.py: claim + chat lease + access + checkpoints
  → input/telegram.py и input/media_recognition.py
  → parsing: deterministic extraction + AI reconciliation
  → application/conversation/use_case.py
  → services/engine.py: авторитетный порядок state machine
  → conversation + catalog + orders
  → repository transaction: state/result/checkpoint
  → presentation/telegram
  → Telegram reply и разрешённые фоновые effects
```

`StateCompatibilityPolicy` остаётся единой точкой решения о продолжении или
прерывании modal context. Text и расшифрованный voice сходятся до state-specific
fallback. Callback остаётся явным UI-путём с проверкой revision.

## Транзакции и надёжность

- `TelegramUpdate.update_id` идемпотентно принимает повторную доставку webhook.
- Update одного чата сериализуются renewable Redis lease и проверкой более ранних
  незавершённых update.
- Старый владелец fencing-проверками останавливается перед сохранением и постановкой
  side-effect task.
- State и обработанный результат сохраняются вместе; доставка ответа и постановка
  задач имеют отдельные checkpoints.
- Submission повторно читает state и доступ, использует per-sheet lock, durable
  record, read-back и явные состояния `uncertain`.
- Начатый внешний POST не повторяется автоматически при неизвестном результате.
- Итоговое уведомление использует at-most-once lifecycle: возможна потеря сообщения,
  но не автоматический дубль.

Оставшиеся точные окна: crash после фактической отправки Telegram reply, но до его
checkpoint, может дать повтор ответа; crash после Celery publish, но до task
checkpoint, может повторно поставить задачу. Опасные submission/product-add effects
имеют собственные durable gates, но общий transactional outbox пока отсутствует.

## Граница AI

Принцип проекта:

```text
AI предлагает структуру
  → исходный текст подтверждает факты
  → каталог уточняет identity
  → deterministic code разрешает действие
```

Структурированные schemas ограничивают AI payload. Quantity reconciliation не
должен подменять доказанное количество заказа числами фасовки, диапазона или title.
AI выбирает только из deterministic shortlist, после чего numeric, qualifier,
packaging, contradiction и confidence gates могут заблокировать auto-select.
Visible action и contextual fallback не подменяют уже распознанный независимый intent.

## Многоканальность

### Нейтрально сегодня

- `ConversationInteraction` и `SemanticAction`;
- parsing и source reconciliation;
- state compatibility, conversation decisions и order/catalog rules;
- application use case;
- сериализуемый conversation state.

### Telegram-specific сегодня

- webhook и event mapping;
- media download/progress;
- callback payload/revision;
- тексты, клавиатуры, edit/send semantics;
- legacy `EngineResult`/`BotReply` на выходе engine.

`ConversationApplication` временно преобразует legacy reply rows через внедрённый
mapper. Это не блокирует текущий Telegram runtime. Для MAX/Web нужен отдельный
input/action/renderer/delivery adapter; bridge следует убирать после первого
утверждённого вертикального сценария второго канала, когда станет виден реальный
нейтральный output contract.

## Каталог и 100 000+ позиций

`CatalogSearch` задаёт bounded search contract, а `ListCatalogSearch` адаптирует
нынешний список. Архитектурный тест на 100 000 строк доказывает, что resolver может
принять ограниченного provider без передачи полного каталога в core.

Production runtime пока делает следующее:

1. Google adapter читает полный диапазон каталога.
2. Redis cache хранит полный JSON.
3. Orchestrator получает полный `list[CatalogProduct]`.
4. `ListCatalogSearch` материализует и копирует список.
5. Ranking и token-frequency проходят по кандидатам линейно.

Следовательно, граница готова архитектурно, но производительность 100 000 позиций не
доказана. До такого масштаба нужен venue/supplier-scoped indexed provider
(PostgreSQL/API), подключённый в runtime composition, и benchmark p95, памяти и
recall на реальных запросах.

## Крупные файлы

| Файл | Проблема сегодня | Безопасный seam |
|---|---|---|
| `integrations/openai_prompts.py` (>2300 строк) | Да: несколько независимо меняющихся prompt-контрактов находятся в одном модуле | модули по типу запроса с byte-for-byte contract tests |
| `services/submission.py` (>2000 строк) | Да: order submission, status/history и product-add протоколы имеют разные причины изменения | сначала вынести status/history, затем product-add coordinator, не дробя основной protocol |
| `services/orchestrator.py` (>1900 строк) | Да: durable update protocol соседствует с AI candidate resolution, analytics и presentation progress | вынос чистого catalog-AI decision и analytics serialization при сохранении одного checkpoint coordinator |
| `services/engine.py` (>1500 строк) | Да: `handle()` содержит порядок многих state flows и presentation decisions | извлекать state-specific decision/result builders только после characterization tests |
| `tests/ai/test_ai_media.py` (>1400 строк) | Умеренно: тематически связан, но дорог в навигации | группировать по parsing contract при следующем изменении соответствующих тестов |

Размер сам по себе не является дефектом. Эти seams отмечены для изменения по
триггеру; новый общий refactor до pilot не нужен.

## Направление зависимостей

Core-пакеты `domain`, `conversation`, `catalog`, `orders` и `parsing` не импортируют
`api`, `workers`, concrete repositories/integrations/presentation или `services`.
`application` задаёт порты. `services` и `workers` собирают concrete adapters и
внешние эффекты. Архитектурные тесты проверяют границы и циклы.

## Защищённый долг

| Долг | Почему сохраняется | Триггер оплаты |
|---|---|---|
| Legacy reply bridge | сейчас стабилизирует весь Telegram UX | первый вертикальный сценарий второго канала |
| Крупный engine/orchestrator | порядок state/checkpoint доказан большим regression suite | изменение затронутого flow или явная стоимость сопровождения |
| Submission monolith | protocol locality снижает риск unsafe retry | отдельная работа с failure-injection proof |
| List-backed catalog | достаточен для текущего малого каталога | утверждённый рост до десятков/сотен тысяч позиций |
| Нет общего transactional outbox | опасные effects имеют локальные gates | дубли status/reply становятся наблюдаемой production-проблемой или растёт нагрузка |

## Куда вносить изменения

- новый intent/state rule: `conversation/routing` и соответствующий decision owner;
- новый формат входа: adapter в `input`, затем общий application use case;
- новый канал: собственные input/action/presentation/delivery adapters;
- новый catalog backend: реализация `CatalogSearch` и runtime composition;
- новый внешний provider: `integrations` плюс порт application/core;
- persistence: repository и Alembic migration;
- UX Telegram: `presentation/telegram`;
- durable порядок effects: защищённые `services` и `workers`.

Перед изменением соблюдайте `AGENTS.md`, `.agents/DEVELOPMENT_PROCESS.md` и
актуальный `PROJECT_HANDOFF.md`.
