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
| `services/parser.py` | 224 | Public text/callback facade и dispatcher | Callback остаётся channel contract. |
| `services/replies.py` | 1017 | Cards, keyboards и UX contracts | Делить по экранным семействам. |
| `integrations/openai_client.py` | 927 | Transport и AI use cases | Разделять только после контрактов. |
| `integrations/google_sheets.py` | 884 | Несколько Sheets contracts | Сохранять единый gateway до доказанного split. |
| `services/matching.py` | 830 | Evidence, scoring, ranking и safety | Разделить на catalog owners без изменения правил. |
| `services/venue_registration.py` | 808 | Directory, access и registration | Разделять после базовых миграций. |
| `parsing/products.py` | 511 | Orchestration товарных строк | Текущий owner после Block 2A. |
| `parsing/quantities.py` | 57 | Quantity primitives | Самостоятельные короткие ответы количества. |
| `parsing/packaging.py` | 187 | Фасовка и catalog measurements | Отдельный measurement owner. |
| `parsing/comment_scope.py` | 52 | Explicit global comment scope | Только разбор области комментария. |
| `parsing/commands/patterns.py` | 329 | Статические шаблоны text-команд | Один owner таблиц команд. |
| `parsing/commands/normalization.py` | 85 | Нормализация и отрицание | Без state mutation. |
| `parsing/commands/navigation.py` | 496 | Свободная навигация и статусы | Не смешивается с callback. |
| `parsing/commands/item_commands.py` | 228 | Add/remove/edit item commands | Использует общие quantity primitives. |
| `parsing/commands/comment_commands.py` | 162 | Изменение комментариев черновика | Не владеет comment scope parsing. |
| `parsing/commands/dialogue.py` | 100 | Retry, quantity hint и dialogue metadata | Не меняет ConversationState. |
| `parsing/commands/router.py` | 208 | Text command routing | Не принимает callback data. |

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
| `services/parser.py` | **Block 2B: FACADE** для text/callback public contract и dispatcher. |
| `parsing/commands/` | **Block 2B: CREATE** owners text command parsing по responsibility. |
| `parsing/products.py` | **Block 1/2A: MOVE** orchestration в parsing package; после extraction остаётся центральным entry point. |
| `parsing/quantities.py` | **Block 2A: CREATE** quantity primitives. |
| `parsing/packaging.py` | **Block 2A: CREATE** фасовка и каталожные измерения. |
| `parsing/comment_scope.py` | **Block 2A: CREATE** явная область общего комментария. |
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

После Block 2A пакет `parsing/` содержит `products.py`, `quantities.py`,
`packaging.py` и `comment_scope.py`. `commands.py` и `text.py` пока не
создаются.

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

Перед каждым переносом, слиянием или удалением выполняется repository-wide
usage/duplicate audit по исходникам, тестам, scripts, entrypoints и aliases.

## 8. Block 1 и Block 2A — выполненные механические переносы

До кода нужно было подтвердить реальные callers product parser:

- единственный production-import implementation был `services/parser.py`;
- application и tests используют публичные aliases `services.parser`;
- отдельные private symbols были доступны через старый facade, но repository-wide
  audit не нашёл оправданных callers старого пути.

Block 1 перенёс цельный owner без изменения алгоритма, а Block 2A разделил
внутри него независимые responsibilities:

```text
services/product_parser.py
  -> удалён после import audit
parsing/products.py
  -> orchestration и порядок parsing rules (511 строк)
parsing/quantities.py
  -> _is_standalone_quantity, parse_quantity_unit
parsing/packaging.py
  -> _PACKAGING_REFERENCE_PREFIX_RE, _spoken_measurement_pair,
     _is_packaging_reference_prefix, _is_compact_catalog_measurement,
     _single_product_packaging_item
parsing/comment_scope.py
  -> _extract_global_comment, has_explicit_global_comment_scope
services/parser.py
  -> imports из реальных owners; command parsing не переносился
```

В `products.py` остались `_query_with_unmarked_tail`,
`parse_product_lines`, product-line splitting, границы позиций, порядок правил
и координация leaf primitives. `_query_with_unmarked_tail` сознательно не
переносился в comment scope: он собирает product query из неподтверждённого
хвоста. Новые `helpers.py`, `utils.py` и service-классы не создавались.

Побайтное сравнение реализации после нормализации переводов строк с исходным
файлом из baseline для каждой функции совпало. `products.py` уменьшился с 673
до 511 строк; сама функция `parse_product_lines` занимает 11 строк, а
`_parse_product_line` — 84 строки как orchestration одной исходной строки.
Focused и полный regression baseline проходят.

## 9. Долговременная архитектурная цель

Проект развивается как production-grade платформа закупок, а не как набор
Telegram-specific сценариев. Telegram Bot, Telegram Mini App, MAX и Web должны
оставаться внешними adapters одного application/domain core. Новые владельцы
parsing, routing, catalog, conversation, history и submission не импортируют
объекты конкретного канала.

Будущие свободные вопросы о прошлых заявках проходят через структурированный
`HistoryQuery` и отдельный history use case. Этот migration block историю не
реализует и не добавляет новые history intents.

Перед каждым `MOVE`, `MERGE`, `DELETE` или `SPLIT` выполняется repository-wide
usage/duplicate/dead-code audit до и после изменения. Для одной ответственности
остаётся один owner; facade возможен только как простой re-export при доказанных
callers.

## 10. Block 2B — выполненный механический перенос text command parsing

`services/parser.py` уменьшился с 1687 до 224 строк. В нём остались только
public `infer_intent`, callback `parse_callback` и compatibility exports.
Свободный текст теперь маршрутизируется через `parsing/commands/router.py`.

```text
parsing/commands/patterns.py
  -> статические _COMMANDS и _NATURAL_COMMANDS
parsing/commands/normalization.py
  -> normalize_command_text и guards отрицания
parsing/commands/navigation.py
  -> свободная навигация и order-status navigation
parsing/commands/item_commands.py
  -> add/remove/edit item commands и target cleaning
parsing/commands/comment_commands.py
  -> edit comment command без мутации состояния
parsing/commands/dialogue.py
  -> retry, quantity hint и dialogue response
parsing/commands/router.py
  -> порядок text-only правил и ParsedCommand
services/parser.py
  -> text/callback dispatcher и обратная совместимость imports
```

`parse_callback()` намеренно не переносился: его mapping и revision являются
контрактом кнопок канального адаптера. Product parsing Block 2A не менялся.
Сравнение старой и новой реализации на 62 существующих тестовых строках дало
нулевые расхождения по полной модели `ParsedCommand`.

## 11. Проверки Block 2A и Block 2B

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
`restaurant_bot.parsing.quantities`, `restaurant_bot.parsing.packaging`,
`restaurant_bot.parsing.comment_scope` и `restaurant_bot.services.parser`;
repository-wide scan подтверждает отсутствие старого facade и второй
реализации каждой перенесённой функции.

## 12. Порядок следующих миграций

1. AI schemas/reconciliation.
2. Catalog evidence/scoring/safety и resolver.
3. Modal policy, handlers, draft/comments и затем engine.
4. Input normalizer/recognition и application pipeline.
5. Orders, submission, venues и внешние adapters.
6. Только после контрактов уменьшать `engine.py` и `orchestrator.py`.

Следующий Block 2B/Block 3 не начинается автоматически после Block 2A и
требует внешнего review.

## 11. Запреты текущего блока

Не менять prompts, state machine, matching, quantity/comment semantics, UX,
persistence, migrations, DB schema, Docker/deploy, workers, API entrypoints,
tests только ради нового пути импорта или внешние сервисы. Известные manual
acceptance issues остаются в `PROJECT_HANDOFF.md`.
