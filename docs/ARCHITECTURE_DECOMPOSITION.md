# План декомпозиции архитектуры

## 1. Назначение и границы

Документ фиксирует безопасную поведенчески нейтральную декомпозицию. Каждый
migration block переносит существующий owner механически, сохраняет public
contracts и проходит focused/full regression до следующего блока.

Текущий checkout: `decompose_bot`, baseline
`f6409891146fad3ce5c82c4329d808b83bc3440c`, автоматический baseline
`1362 collected / 1362 passed`. Локальный
`manual_smoke_forensic_logs.txt` не является частью проекта.

## 2. Runtime boundary

| Точка | Текущий владелец | Контракт |
|---|---|---|
| `api/app.py:telegram_webhook` | FastAPI transport | Проверить Telegram secret, нормализовать update, поставить его в inbox один раз. |
| `workers/tasks.py:process_telegram_update` | Celery delivery | Retry и передача одного update orchestrator. |
| `services/orchestrator.py:UpdateOrchestrator.process` | Application pipeline | Claim, lease, venue access, parse, engine, checkpoint и reply. |
| `services/engine.py:ConversationEngine.handle` | State machine | Применить ParsedCommand к ConversationState и вернуть EngineResult. |
| `services/submission.py:SubmissionService` | Submission use case | Свежий доступ/снимок, запись, read-back, пересчёт, dispatch и уведомление. |
| `integrations/telegram.py` | Telegram adapter | Файлы, ответы и callback acknowledgement. |
| `integrations/google_sheets.py` | Sheets adapter | Каталог, статусы, заказ и операции пересчёта/мутации. |

## 3. Размеры и границы

| Модуль | Строк | Основная ответственность | Решение |
|---|---:|---|---|
| `services/engine.py` | 3353 | State routing, draft, catalog, review, submission preparation | Позже разделить по ответственности. |
| `integrations/openai_prompts.py` | 2378 | Prompt contracts | Переносить как единый внешний контракт. |
| `services/orchestrator.py` | 2152 | Transaction pipeline и scheduling | Позже выделить application pipeline. |
| `services/submission.py` | 2047 | Submission use cases и checkpoints | Разделять по внешним контрактам. |
| `integrations/openai_parsing.py` | 1800 | Schemas и reconciliation | Сначала выделить schemas/reconciliation. |
| `services/parser.py` | 1687 | Global commands и product facade | Product owner выбран первым. |
| `services/replies.py` | 1017 | Cards, keyboards и UX contracts | Делить по экранным семействам. |
| `integrations/openai_client.py` | 927 | Transport и AI use cases | Разделять только после контрактов. |
| `integrations/google_sheets.py` | 884 | Несколько Sheets contracts | Сохранять единый gateway до доказанного split. |
| `services/matching.py` | 830 | Evidence, scoring, ranking и safety | Разделить на catalog owners без изменения правил. |
| `services/venue_registration.py` | 808 | Directory, access и registration | Разделять после базовых миграций. |
| `services/product_parser.py` | 673 | Детеминированный product parsing | Первый низкорисковый coherent owner. |

## 4. Карта владельцев

### Domain и persistence

- `domain/models.py`, `db.py`, `db_models.py`, repositories и миграции —
  **KEEP**; serialized contracts и схема не меняются.
- `repositories/updates.py` отвечает за inbox/sequencing, `sessions.py` —
  за состояние, `submissions.py` — за checkpoints/idempotency.

### Ввод и разбор

| Текущий owner | Действие |
|---|---|
| `services/input_normalizer.py` | Позже MOVE в `input/telegram.py`, временный facade. |
| `services/input_recognition.py` | Позже MOVE в `input/recognition.py`. |
| `services/parser.py` | Сейчас сохраняет global command facade; позже MOVE commands. |
| `services/product_parser.py` | **Block 1: MOVE** реализации в `parsing/products.py`; старый путь — facade. |
| `services/text.py` | Позже MOVE в `parsing/text.py`, после миграции callers. |
| `integrations/openai_parsing.py` | Позже SPLIT schemas и reconciliation. |
| `integrations/openai_prompts.py` | Позже MOVE prompt contract без изменения текста. |
| `integrations/openai_client.py` | Позже SPLIT transport и AI facade в `application/ai_service.py`. |

### Каталог

- `services/matching.py` позднее разделяется на `catalog/evidence.py`,
  `catalog/scoring.py`, `catalog/safety.py`.
- `services/catalog_resolver.py` позднее переносится в
  `catalog/resolver.py`; resolver не меняет state.
- `engine.py` позднее оставляет orchestration, а применение draft выделяется
  отдельно.

### Conversation

- `state_compatibility.py` остаётся единой policy; нельзя дублировать список
  сильных intent в engine/orchestrator/handlers.
- Existing handlers позднее переносятся в `conversation/handlers`, а
  routing — в `conversation/routing`.
- `engine.py` в финале должен координировать pipeline, а не владеть каждым
  сценарием.

### Orders, submission и adapters

- review/product-add, submission stages и venue registration переносятся по
  внешним контрактам, сохраняя facade и checkpoints.
- Telegram, OpenAI, Sheets и cache остаются adapters; нижние слои не импортируют
  orchestrator, workers или API.

## 5. Целевое дерево

```text
restaurant_bot/
  parsing/
    text.py
    commands.py
    products.py
    ai/{schemas.py,reconciliation.py}
  catalog/{evidence.py,scoring.py,safety.py,resolver.py}
  conversation/
    engine.py
    draft.py
    comments.py
    state/queries.py
    routing/{state_compatibility.py,modal_routing.py,visible_actions.py}
    handlers/
  orders/{review.py,product_add.py}
  submission/{service.py,checkpoints.py,catalog.py,status.py,presenter.py}
  venues/{directory.py,access.py,registration.py}
  application/{update_pipeline.py,ai_service.py}
  input/{telegram.py,recognition.py}
  integrations/{telegram,openai,google_sheets}
```

В Block 1 создаётся только `parsing/__init__.py` и
`parsing/products.py`; остальные каталоги — будущий план.

## 6. Правила зависимостей

```text
domain
  <- parsing, catalog, conversation
  <- orders, venues, submission, input
  <- application
  <- api, workers, integrations, repositories, infrastructure
```

`parsing` импортирует только domain и допустимые лексические service
primitives; `catalog` не меняет state; `conversation` не импортирует
Celery/DB/Sheets; `repositories` не импортируют services/integrations.
В migration period facade допускает временное нарушение формы, но содержит
только явный re-export и имеет записанный шаг удаления.

## 7. Compatibility strategy

Каждый перенос проходит четыре фазы:

1. Создать нового owner механически, без изменения поведения.
2. Перевести внутренние imports и оставить старый путь re-export facade.
3. Просканировать source/tests/scripts/workers/packaging и прогнать gates.
4. Удалить facade только отдельным блоком после доказанного отсутствия callers.

Facade не содержит второго алгоритма, не меняет serialization, prompt, UX или
внешний контракт.

## 8. Block 1 — выполненный механический перенос

До кода нужно было подтвердить реальные callers product parser:

- единственный production-import implementation был `services/parser.py`;
- application и tests используют публичные aliases `services.parser`;
- отдельные private symbols нужны parser facade/reconciliation path и должны
  остаться совместимыми.

Block 1 перенёс цельный owner без изменения алгоритма:

```text
services/product_parser.py
  -> parsing/products.py       MOVE реализации выполнен
services/product_parser.py
  -> явный compatibility facade
services/parser.py
  -> прямой import из parsing.products
```

Внутри `products.py` сохраняются все constants, regex и private helpers:
quantity, packaging, ranges, spoken measurements, product-line splitting и
comment scope. Новые `quantities.py`, `packaging.py`, `helpers.py` не создаются.

Побайтное сравнение реализации после нормализации переводов строк с исходным
файлом из baseline совпало. Focused и полный regression baseline проходят.

## 9. Проверки Block 1

Focused:

```text
tests/input/test_parser.py
tests/input/test_input_edge_cases.py
tests/input/test_voice_quantity_recovery.py
tests/conversation/test_comment_handling.py
tests/quantity/test_quantity_contract.py
tests/conversation/test_quantity_state_preemption.py
```

Full и quality gates:

```text
python -m pytest -q --tb=short
ruff check src tests
ruff format --check <изменённые Python-файлы>
mypy src
python scripts/check_markdown_links.py
git diff --check
```

Дополнительно импортируются `restaurant_bot.parsing.products`,
`restaurant_bot.services.product_parser` и `restaurant_bot.services.parser`;
repository-wide scan подтверждает отсутствие незапланированной второй реализации.

## 10. Порядок следующих миграций

1. Block 2: command parsing в `parsing/commands.py`.
2. AI schemas/reconciliation.
3. Catalog evidence/scoring/safety и resolver.
4. Modal policy, handlers, draft/comments и затем engine.
5. Input normalizer/recognition и application pipeline.
6. Orders, submission, venues и внешние adapters.
7. Только после контрактов уменьшать `engine.py` и `orchestrator.py`.

Block 2 не начинается автоматически после Block 1.

## 11. Запреты текущего блока

Не менять prompts, state machine, matching, quantity/comment semantics, UX,
persistence, migrations, DB schema, Docker/deploy, workers, API entrypoints,
tests только ради нового пути импорта или внешние сервисы. Известные manual
acceptance issues остаются в `PROJECT_HANDOFF.md`.
