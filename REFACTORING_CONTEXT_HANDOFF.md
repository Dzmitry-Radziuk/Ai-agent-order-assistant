# Контекст рефакторинга проекта

Этот файл передаёт новому архитектору подтверждённый контекст совместной работы
по декомпозиции проекта. Он не является новым планом реализации и не заменяет
исполняемый код, тесты и текущий Git state.

## 1. Текущее состояние

```text
Репозиторий: Dzmitry-Radziuk/test_bot
Ветка: decompose_bot
HEAD: 6a925ce4d038d1696ca2fb008a0dc143cd252c10
Remote: https://github.com/Dzmitry-Radziuk/test_bot.git
Baseline: 1366 collected / 1366 passed
```

Ветка синхронизирована только с GitHub. GitLab не используется для текущей
работы. `.env`, токены, пароли, service-account JSON и connection strings не
входят в этот файл и не должны попадать в диагностику или commit.

Последний завершённый блок — Block 5R: `escape` и `format_number` перенесены в
`presentation/telegram/formatting.py`. Рабочее дерево после commit чистое.

## 2. Что было до большого рефакторинга

Изначально проект был production Telegram-ботом для закупок ресторанов, где
один transitional-пакет `services/` одновременно содержал:

- Telegram input, voice/photo recognition и provider calls;
- text/callback parsing;
- state-machine routing и modal-state guards;
- draft/cart mutations, comments и quantity flows;
- catalog retrieval, ranking, evidence и auto-select;
- submission, checkpoints, Google Sheets и dispatch;
- venue access/registration;
- replies, keyboards, HTML escaping и статусные карточки.

Главные проблемы были не в размере файлов самом по себе, а в смешении
ответственностей и скрытом порядке обработки:

1. modal state мог перехватить новое сообщение до global semantic parsing;
2. AI output иногда конкурировал с source-evidence и deterministic recovery;
3. quantity заказа смешивалось с packaging, ranges и catalog measurements;
4. comment мог потеряться, стать shadow item или быть применён к неверной позиции;
5. catalog search, ranking и auto-select были недостаточно явно разделены;
6. worker/orchestrator cycle и side effects требовали безопасной transaction boundary;
7. compatibility facades скрывали настоящих owners;
8. transitional services импортировались из lower/core слоёв;
9. HTML/presentation formatting находилось среди units и parsing primitives.

Поэтому косметический перенос больших файлов целиком был отвергнут: сначала
нужно было доказать responsibility seam, сделать repository-wide caller audit,
перенести код механически, сравнить старый и новый результат и только затем
удалять facade или старый owner.

## 3. Целевая архитектура

```text
domain / core
    ↓
parsing, catalog, conversation, orders
    ↓
application ports / use cases
    ↓
services transitional coordinators
    ↓
input, Telegram, Sheets, Celery, API adapters
```

Практическая целевая структура:

```text
src/restaurant_bot/
├── domain/
├── parsing/
│   ├── commands/
│   └── ai/
├── catalog/
├── conversation/
│   ├── routing/
│   └── state/
├── orders/
├── application/
├── presentation/telegram/
├── input/
├── repositories/
├── integrations/
├── workers/
└── services/              # временный orchestration/compatibility слой
```

Ключевые правила направления:

- domain/core не импортирует Telegram, workers, orchestrator или repositories;
- catalog resolver не мутирует `ConversationState`;
- parsing не выполняет внешние эффекты;
- presentation форматирует ответ, но не пишет в БД/Sheets/Redis;
- workers реализуют delivery adapters, а не бизнес-правила;
- `services.orchestrator -> workers.tasks` запрещено;
- допустимое направление для delivery: `workers -> application/orchestrator`;
- каждый responsibility имеет одного канонического owner;
- facade допустим только как простой re-export при доказанных callers;
- generic `helpers.py`, `utils.py`, `common.py` и искусственные `part1/part2`
  не создаются.

## 4. Сквозной runtime pipeline

Для text и voice смысловая часть должна стремиться к одному маршруту:

```text
Telegram update
  → input normalization
  → transcription, если voice
  → global parsing / AI structured output
  → source-evidence reconciliation
  → StateCompatibilityPolicy
  → CONTINUE / INTERRUPT / AMBIGUOUS / REJECT
  → state handler или normal intent routing
  → catalog resolution
  → ConversationState / draft
  → BotReply
  → checkpoint и внешние side effects
```

State — контекст уже понятого сообщения, а не абсолютный механизм определения
смысла следующего сообщения. Сильный новый intent может прервать modal state.
Слабая или случайная фраза не должна становиться quantity, товаром, комментарием
или candidate selection только из-за текущего state.

## 5. Хронология завершённых Blocks

### Block 0 — контекст и дизайн

Зафиксированы цели безопасной декомпозиции, dependency direction, запрет
массового механического переноса и правило: сначала audit, затем доказанный seam,
mechanical migration, semantic comparison, regression и quality gates.

### Block 1 — deterministic product parser

Product parser перенесён в `parsing/products.py`. Сохранены public contracts,
`ExtractedItem`, порядок правил и пользовательское поведение. Внутренние этапы
разбора позже разделены на `parsing/quantities.py` и `parsing/packaging.py`, но
`products.py` остаётся orchestration-level owner товарной строки.

### Block 2A — product parsing responsibilities

Выделены реальные связные обязанности quantity primitives, packaging/measurement
spans и product-line orchestration. Regex и quantity semantics не переписывались
ради уменьшения файла.

### Block 2B — text command parsing

`services/parser.py` оставлен компактным public facade для `infer_intent`,
callback contract и dispatcher. Реальные command owners находятся в:

- `parsing/commands/patterns.py`;
- `normalization.py`;
- `navigation.py`;
- `item_commands.py`;
- `comment_commands.py`;
- `dialogue.py`;
- `router.py`.

Callback mapping не смешивается с channel-neutral text parsing.

### Block 3 — AI schemas и reconciliation

AI parsing разделён по responsibility в `parsing/ai/`:

- `schemas.py` — декларативные structured-output schemas;
- `quantity_reconciliation.py` — order quantity, packaging и ranges;
- `comment_reconciliation.py` — provenance, scope и bindings;
- `item_reconciliation.py` — source qualifiers и item recovery;
- `shadow_items.py` — shadow projections и fragments;
- `reconciliation.py` — единая последовательность pipeline.

Правило источников истины: AI предлагает, source text подтверждает, каталог
уточняет, deterministic code выполняет. Пустой AI result не должен разрушать
полученные source-supported items.

### Block 3C — correction review

Исправлялись только фактически подтверждённые проблемы после Block 3. Новые
guards не добавлялись без доказанного owning layer и regression coverage.

### Block 4 — catalog decomposition

Catalog responsibilities разделены:

- `catalog/evidence.py` — canonical representation, tokens, evidence и supplier hint;
- `catalog/scoring.py` — deterministic score одного candidate;
- `catalog/retrieval.py` — bounded retrieval и порядок shortlist;
- `catalog/safety.py` — qualifier conflicts, numeric compatibility, hard veto и
  auto-select safety;
- `catalog/resolver.py` — supplier scope и `CatalogDecision`.

Retrieval/ranking/auto-select — разные решения. Candidate admission может быть
широким, но auto-select должен быть консервативным. Похожий или категорийный
товар нельзя автоматически считать заказанным.

### Block 4C — cleanup и correction review

Убраны только подтверждённые остаточные дубли и исправлена документация после
catalog decomposition. Matching algorithm не менялся без нового доказательства.

### Block 5A — conversation routing/state policy

Созданы channel-neutral owners в `conversation/routing/`:

- `contracts.py`;
- `item_resolution.py`;
- `order_flow.py`;
- `comment_scope.py`;
- `state_compatibility.py`;
- `modal_routing.py`.

`StateCompatibilityPolicy` — единая точка CONTINUE/INTERRUPT/AMBIGUOUS/REJECT.
Не создаются отдельные списки strong intents в orchestrator и handlers.

### Block 5B — draft/comments core

`conversation/comments.py` стал owner подтверждённых comments, scope,
provenance normalization и comment shadows. `conversation/draft.py` владеет
целостностью draft и merge только подтверждённых дублей. Telegram handlers не
владеют core operations.

### Block 5C — selection core

`conversation/selection.py` владеет channel-neutral selection core, targeting и
candidate scoring. `CandidateSelectionHandler` остаётся presentation/state
adapter. Callback contract сохранён.

### Block 5D — progression core

`conversation/progression.py` владеет переходом к следующей unresolved позиции и
progression stage. Engine сохраняет только presentation adapter.

### Block 5DC — unresolved status contract

Зафиксирован один owner membership/priority unresolved items в
`conversation/state/queries.py`. Дублирующие статусы и скрытые варианты
unresolved не создаются.

### Block 5E — quantity multiple policy

`conversation/quantity_resolution.py` владеет кратностью, рекомендованным
количеством и предупреждениями. Quantity policy channel-neutral и не смешивается
с Telegram UI.

### Block 5F — supplier minimum policy

`orders/supplier_minimums.py` владеет расчётом минимальных сумм поставщиков и
предупреждений без изменения state. UI только отображает результат.

### Block 5G — catalog-to-draft resolution

`orders/catalog_resolution.py` применяет решение каталога к `CartItem`, включая
catalog fields, quantity/comment provenance, status и refresh draft. Resolver
сам не мутирует `ConversationState`.

### Block 5H/5HC — services transition audit

`services/` зафиксирован как transitional layer. Для каждого крупного сервиса
описаны реальные responsibilities, callers и возможный seam. Большие файлы не
переносятся целиком без отдельного доказательства.

### Block 5I — comment policy owner

Три pure supplier-comment функции перенесены в `parsing/comment_policy.py`.
Старый `services/comment_policy.py` удалён после нулевого caller audit.

### Block 5J — obsolete compatibility facades

Удалены test-only facades для catalog matching/resolver и старых conversation
handlers после проверки production, test и dynamic callers. Импорты переведены на
canonical owners. Новые facade без реальных callers не создаются.

### Block 5K — orchestrator/workers cycle

Введён typed application port `application/background_tasks.py`. Orchestrator
использует порт, а Celery остаётся adapter в workers. Обратный импорт
`orchestrator -> workers.tasks` устранён; background-task wiring не менялся.

### Block 5L — services/text audit

Проведён read-only audit transitional `services/text.py`. Владелец файла был
смешанным: units, number words, overlap, numeric parsing и Telegram formatting.
Определены отдельные migration groups; без доказательства не переносится весь
файл.

### Block 5M/5MC — text normalization

`clean_text` и `normalize_text` перенесены в
`restaurant_bot/text_normalization.py`. Сохранены whitespace, normalization
semantics и callers. Дубликаты не создавались.

### Block 5N/5NC — Telegram input normalization

Telegram raw-update adapter перенесён из `services/input_normalizer.py` в
`input/telegram.py`. Старый module удалён после caller audit. `TelegramEvent`, API
и orchestrator contract сохранены.

### Block 5O — input recognition audit

Полный перенос `InputRecognitionService` отклонён: в нём смешаны Telegram
transport, download/cleanup, OpenAI voice/photo, retry, visible actions и
presentation. Следующим доказанным seam признана только transcript policy.

### Block 5P — voice transcript policy

Pure-функции `has_supported_voice_letters` и `select_transcription_result`
перенесены в `input/voice_transcript_policy.py`. Voice/photo orchestration
сохранён в `services/input_recognition.py`. Text и voice semantic pipeline не
разделены искусственно.

### Block 5Q — submission presenter

`services/submission_presenter.py` механически перенесён в
`presentation/telegram/submission.py`. Сохранены 28 функций, callbacks, HTML,
emoji, pagination, status grouping и revision. `SubmissionService`, engine и
tests используют canonical path. Presenter не выполняет DB/Redis/Sheets/OpenAI
или submission side effects.

### Block 5R — Telegram formatting

`escape` и `format_number` механически перенесены в
`presentation/telegram/formatting.py`. Новый модуль зависит только от stdlib и
`text_normalization.clean_text`. `services/text.py` теперь не содержит эти
presentation primitives; остальные units/numbers/overlap/parsing symbols не
трогались. Сравнение старых и новых функций дало `MISMATCHES=0`.

## 6. Canonical owners сейчас

| Область | Owner |
|---|---|
| Telegram raw update | `input/telegram.py` |
| Voice transcript choice | `input/voice_transcript_policy.py` |
| Text command parsing | `parsing/commands/` |
| Product lines | `parsing/products.py` |
| Quantity primitives | `parsing/quantities.py` |
| Packaging measurements | `parsing/packaging.py` |
| AI structured schemas | `parsing/ai/schemas.py` |
| AI quantity reconciliation | `parsing/ai/quantity_reconciliation.py` |
| AI comment reconciliation | `parsing/ai/comment_reconciliation.py` |
| Draft/comments | `conversation/draft.py`, `conversation/comments.py` |
| State compatibility | `conversation/routing/state_compatibility.py` |
| Modal routing | `conversation/routing/modal_routing.py` |
| Candidate selection | `conversation/selection.py` |
| Progression | `conversation/progression.py` |
| Quantity multiple policy | `conversation/quantity_resolution.py` |
| Supplier minimums | `orders/supplier_minimums.py` |
| Catalog-to-draft application | `orders/catalog_resolution.py` |
| Catalog evidence | `catalog/evidence.py` |
| Catalog score | `catalog/scoring.py` |
| Candidate retrieval | `catalog/retrieval.py` |
| Catalog safety | `catalog/safety.py` |
| Catalog resolver | `catalog/resolver.py` |
| Telegram formatting | `presentation/telegram/formatting.py` |
| Submission/status presentation | `presentation/telegram/submission.py` |
| Submission side effects | `services/submission.py` (transitional) |
| Main conversation orchestration | `services/engine.py` (transitional) |
| Update pipeline | `services/orchestrator.py` (transitional) |

## 7. Стабилизированные invariants

### State и intent

- Global parsing выполняется до contextual fallback.
- Strong independent intent может прервать modal state.
- `MISSING_QTY`, `AWAIT_COMMENT_SCOPE`, `AMBIGUOUS`, `NOT_FOUND`, duplicate и
  unit modal contexts не должны превращать новое намерение в ответ старому state.
- При interrupt старый incomplete/ambiguous item сохраняется, если существующая
  модель это позволяет.
- Старые pending comment IDs очищаются только при фактическом удалении item;
  новый ParsedCommand не изменяется pending context.
- Callback остаётся явным UI path и проверяет актуальную revision.

### AI, source evidence и quantity

- AI не является единственным источником истины.
- Source-supported product, order quantity и comment не перезаписываются
  деструктивным fallback.
- Quantity заказа и packaging/catalog measurement — разные роли.
- Диапазон фасовки не становится endpoint quantity.
- Comment может одновременно повторять product facts в `product_query` и `comment`.
- Отсутствующий товар не заменяется похожим catalog item автоматически.

### Catalog

- Search/retrieval, ranking и auto-select разделены.
- Candidate shortlist может быть permissive.
- Auto-select проходит deterministic safety gates и должен быть conservative.
- AI matcher получает deterministic shortlist и не обходит hard veto.
- Resolver не меняет `ConversationState`; engine/orders слой применяет решение.

### Side effects и concurrency

- Telegram update принимается идемпотентно.
- Изменения одного чата сериализуются.
- Stale owner не должен выполнить side effect после потери lease.
- Submission перечитывает свежие данные и использует checkpoints/read-back.
- Неопределённый внешний результат не превращается в успешное повторное действие.
- Completion notification имеет at-most-once защиту.
- Выключенная внешняя dispatch не отменяет локальную запись заявки, но не
  вызывает central send.

## 8. Что сознательно не делали

Следующие подходы обсуждались и были отклонены:

- массовый перенос всего `engine.py`, `orchestrator.py`, `submission.py` или
  `InputRecognitionService` одним блоком;
- generic `common.py`, `helpers.py`, `utils.py`;
- искусственные `engine_part1.py`/`engine_part2.py`;
- отдельные strong-intent списки в orchestrator и handlers;
- новый `suspended_interaction` без доказанной потери существующего context;
- fallback/regex под конкретные товары или фразы;
- ослабление catalog safety ради одного candidate;
- переписывание prompts/parser во время mechanical migration;
- перемещение measurement symbols вместе с presentation formatting;
- facade без подтверждённых callers;
- преждевременная миграция на PostgreSQL search, `pg_trgm` или `pgvector` без
  benchmark и доказанной необходимости;
- изменение UX, HTML, emoji, callback order или Telegram parse-mode вне явной
  продуктовой задачи.

## 9. Текущие большие transitional модули

Они остаются намеренно и не должны автоматически переноситься следующим шагом:

- `services/engine.py` — state machine, routing, draft application, catalog и
  review orchestration;
- `services/orchestrator.py` — transaction/update pipeline;
- `services/submission.py` — checkpoints, read-back, Sheets и dispatch;
- `services/venue_registration.py` — access, directory и registration;
- `services/replies.py` — Telegram cards и keyboards;
- `services/order_review.py` — review presentation/use case;
- `services/input_recognition.py` — voice/photo transport/provider/retry;
- `integrations/openai_client.py` — AI transport/use cases;
- `integrations/google_sheets.py` — несколько Sheets contracts;
- `integrations/openai_prompts.py` — внешний prompt contract.

Следующий перенос допустим только после отдельного audit callers, duplicate
analysis и controlled comparison. Большой файл сам по себе не является поводом
для decomposition.

## 10. Baseline и проверки

Последний подтверждённый baseline:

```text
1366 collected / 1366 passed
```

Используемые gates:

- focused pytest для затронутой области;
- полный `python -m pytest -o addopts='' -q --tb=short`;
- `ruff check src tests`;
- `ruff format --check` для touched Python;
- `mypy src`;
- `python scripts/check_markdown_links.py`;
- `python -m compileall src/restaurant_bot`;
- `git diff --check`;
- AST import/cycle audit;
- controlled old/new semantic corpus comparison.

`python scripts/build_agent_context.py` в текущем Windows checkout ранее
завершался `PermissionError` при записи `.agents/runtime/CURRENT_CONTEXT.md`.
Права на `.agents/runtime` не изменялись; это не является основанием менять
production code или обходить ограничения.

## 11. Что ещё не доказано и не должно начинаться автоматически

### CONFIRMED

- Blocks 0–5R из хронологии выше завершены в текущей ветке;
- GitHub branch синхронизирована;
- baseline зелёный;
- основные canonical owners и dependency rules зафиксированы в документации;
- presentation formatting больше не зависит от `services`.

### LIKELY, но требует отдельного аудита

- дальнейшее уменьшение `services/replies.py` по presentation seams;
- выделение use-case/application boundary из `engine.py`;
- split `SubmissionService` по внешним контрактам;
- отдельный owner для оставшихся `services.text` measurement/numeric primitives;
- разделение input recognition на transport, provider и recovery policy.

### NOT YET PROVEN

- что любой следующий seam не изменит скрытые Telegram/Sheets contracts;
- что PostgreSQL search даст выигрыш без benchmark на реальном каталоге;
- что можно безопасно удалить оставшиеся compatibility facades без нового caller
  audit;
- что полный MOVE engine/orchestrator уменьшит риск, а не увеличит его.

### DO NOT TOUCH YET

- production behavior, prompts, parser semantics, catalog matching, UX и
  persistence без отдельного пользовательского запроса;
- `.env`, credentials, webhook, GitLab, Docker/CI/CD;
- следующий Block до отдельного audit-only решения;
- найденные acceptance bugs во время чистого architectural migration.

## 12. Локальный контекст предыдущей работы

Пользователь ожидает работу senior backend architect:

- сначала фактический checkout, callers, tests и Git diff;
- claims должны быть подтверждены кодом, тестом, логом или явной пометкой
  `CONFIRMED`/`LIKELY`/`NOT YET PROVEN`;
- read-only задача не должна приводить к исправлениям или commit;
- mechanical refactor должен сохранять public contracts и проходить semantic
  comparison;
- новые пользовательские тексты должны быть простыми для повара или сотрудника
  кафе;
- ответы, новые docstrings и комментарии писать по-русски, кроме неизбежных
  технических идентификаторов.

Нельзя выдавать локальный/container smoke за production confirmation. Необходимо
явно разделять код, state, persistence, внешний эффект и наблюдаемый Telegram
ответ. При неопределённом внешнем результате сначала сохраняется безопасный
checkpoint, а не выполняется повторная мутация.

## 13. Git handoff

Единственный файл, добавленный этой задачей, —
`REFACTORING_CONTEXT_HANDOFF.md`. После его создания нужно проверить:

```powershell
git status --short
git diff --check
```

Commit и push должны включать только этот файл, в ветку
`origin/decompose_bot`. Следующий Block после публикации не начинать.
