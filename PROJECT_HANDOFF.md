# Передача проекта

## 1. Назначение

Это production Python-бот для закупок ресторана в Telegram. Он принимает текст,
голос, фото и callback, ведёт редактируемый черновик, сопоставляет товары с
каталогом выбранного заведения и после подтверждения записывает заявку в Google
Sheets.

AI помогает понять свободную речь и найти кандидатов. Детерминированный код
владеет состоянием, проверками безопасности, идемпотентностью, хранением и
внешними эффектами.

## 2. Текущий checkout

- Репозиторий: `Dzmitry-Radziuk/test_bot`.
- Ветка: `decompose_bot`.
- Semantic baseline: `f9cbc3195c0eae843de3208e488c3f46baa5a5ec`.
- Accepted Block 3 code baseline: `e4fb29e4d2d0ba906c91beef5c02d3e87d7a0b09`.
- Block 3C correction baseline: `21358755ebbc36b95b9fb6b4799021027a2158e7`.
- Accepted Block 4 code baseline: `1583d0ee17f94948f6bc110ebcc03a5463a18af9`.
- Текущий Git HEAD всегда определяется командой `git rev-parse HEAD`, а не
  фиксируется в handoff после каждого commit.
- Единственный рабочий remote: GitHub `origin/decompose_bot`.
- Автоматический baseline: `1362 collected / 1362 passed`.
- `manual_smoke_forensic_logs.txt` — необязательный локальный diagnostic artifact,
  не tracked-файл репозитория. Если он существует локально, его нельзя менять,
  удалять или добавлять в commit; отсутствие файла нормально.

## 3. Основной pipeline

```text
Telegram update
  -> webhook и inbox
  -> Celery delivery
  -> UpdateOrchestrator
  -> авторизация и lease чата
  -> нормализация text/voice/photo
  -> глобальный ParsedCommand
  -> reconciliation исходного текста
  -> StateCompatibilityPolicy
  -> ConversationEngine и CatalogResolver
  -> изменение ConversationState
  -> checkpoint сессии и ответ Telegram
```

Подтверждение заявки — отдельная граница: свежий снимок, запись в лист, read-back,
пересчёт, optional dispatch и контрольная точка уведомления.

## 4. Владельцы текущих модулей

### Domain и persistence

- `domain/models.py` — Intent, ParsedCommand, ExtractedItem, CartItem,
  ConversationState, ответы, submission-контракты и сериализованные enum.
- `db.py`, `db_models.py` — SQLAlchemy и схема.
- `repositories/updates.py` — inbox и идемпотентная последовательность update.
- `repositories/sessions.py` — версионированное состояние диалога.
- `repositories/submissions.py` — checkpoints и idempotency отправки.
- `repositories/order_events.py`, `venue_bindings.py` — аудит и привязки
  пользователей/чатов к заведению.

### Ввод и разбор

- `services/input_normalizer.py` — Telegram payload -> TelegramEvent.
- `services/input_recognition.py` — voice/photo, транскрибация и visible actions.
- `services/parser.py` — глобальный intent/callback parser и совместимый фасад.
- `parsing/products.py` — orchestration разбора товарных строк и сборка
  итогового списка `ExtractedItem`.
- `parsing/quantities.py` — короткие ответы количества и quantity primitives.
- `parsing/packaging.py` — фасовка, диапазоны и catalog measurement parsing.
- `parsing/comment_scope.py` — явная область общего комментария.
- `services/text.py` — лексическая нормализация, единицы, числа и диапазоны.
- `parsing/ai/schemas.py` — structured AI schemas без алгоритмов.
- `parsing/ai/quantity_reconciliation.py` — reconciliation количеств, фасовки и диапазонов.
- `parsing/ai/comment_reconciliation.py` — provenance и comment bindings.
- `parsing/ai/item_reconciliation.py` — source qualifier cleanup и восстановление позиций.
- `parsing/ai/shadow_items.py` — shadow projections и ссылочные дубли.
- `parsing/ai/reconciliation.py` — единый порядок AI reconciliation.
- `integrations/openai_parsing.py` — compatibility re-export facade без алгоритмов.
- `integrations/openai_prompts.py` — неизменяемые prompt-контракты.

### Каталог и диалог

- `catalog/evidence.py` — каноническое представление, токены, query/catalog
  evidence и сопоставление supplier hint.
- `catalog/scoring.py` — детерминированная оценка одного товара.
- `catalog/retrieval.py` — bounded in-memory enumeration, admission и порядок
  кандидатов.
- `catalog/safety.py` — конфликты квалификаторов, numeric compatibility, safe
  equivalence, broad-category policy и auto-select safety.
- `catalog/resolver.py` — поиск в supplier scope и чистое решение
  `CatalogDecision` без изменения `ConversationState`.
- `conversation/quantity_resolution.py` — единый channel-neutral владелец
  кратности заказа: `nearest_valid_multiple`, расчёт рекомендации и
  предупреждения без изменения черновика.
- `orders/supplier_minimums.py` — единый channel-neutral владелец агрегации
  минимальных сумм поставщиков и структурированных предупреждений без мутации
  состояния.
- `orders/catalog_resolution.py` — единый channel-neutral владелец применения
  результата `CatalogResolver` к `CartItem`: каталожные поля, quantity
  reconciliation, comment provenance, статусы и refresh черновика.
- `services/matching.py` — transitional compatibility path: catalog symbols
  и совместимый re-export `nearest_valid_multiple`.
- `services/catalog_resolver.py` — чистый compatibility re-export facade для
  `catalog/resolver.py`.
- `conversation/selection.py` — channel-neutral score, targeting и выбор
  кандидата; не знает о ParsedCommand или callback semantics.
- `conversation/progression.py` — channel-neutral progression core: выбор
  следующей нерешённой позиции, issue mapping и смена stage без presentation.
- `services/conversation_handlers/` — quantity, candidate, comment scope,
  review, navigation и единая StateCompatibilityPolicy.
- `services/engine.py` — transitional state machine и orchestration caller;
  применение решения каталога делегируется `orders/catalog_resolution.py`, а
  остальные переходы черновика, comments, quantity, duplicate flow и подготовка
  submission остаются в engine.
- `services/replies.py` — карточки, клавиатуры и пользовательские тексты.

### Внешние эффекты

- `services/submission.py` — запись, checkpoints, пересчёт, dispatch и статусы.
- `services/venue_registration.py` — каталог заведений, доступ и регистрацию.
- `integrations/telegram.py` — Telegram transport.
- `integrations/google_sheets.py` — каталог и листы заведения.
- `integrations/cache.py` — catalog cache и Redis locks.
- `workers/tasks.py` — только Celery delivery и фоновые границы.

## 5. Неприкосновенные инварианты

1. AI предлагает структуру, а источник пользователя и каталог подтверждают её.
2. `product_query` и supplier `comment` могут намеренно содержать одну
   характеристику одновременно.
3. Количество заказа отдельно от фасовки, веса упаковки, диапазона и размеров.
4. Retrieval, ranking и auto-select — разные решения; слабый токен не даёт
   право выбрать другой товар.
5. Текст и голос после транскрибации используют один semantic pipeline.
6. Сначала выполняется global parsing, затем contextual modal fallback.
7. Сильное независимое намерение может прервать старый modal state, не перенося
   quantity/comment в новый товар.
8. Комментарий поставщику требует подтверждённого происхождения.
9. Draft mutation и submission идемпотентны и привязаны к заведению.
10. Старый callback revision не изменяет новый черновик.
11. Ненайденный товар не заменяется похожим автоматически.

## 6. Защищённые границы

Без отдельного разрешения не менять Docker, deployment, CI/CD, `.env`,
секреты, миграции, схему БД, serialized state, Telegram UX/callback,
prompts, matching thresholds, submission contracts, Google Sheets,
Redis locking, workers и API entrypoints.

GitLab не используется. В этой задаче разрешён только GitHub.

## 7. Известные ручные acceptance-проблемы

Это текущие нерешённые acceptance-проблемы:

- после вопроса о количестве для «хлеб» фраза «новый товар» не должна стать
  количеством хлеба;
- «удали все комментарии» не должна очищать товары;
- исправление комментария «не X, а Y» должно менять только исправленную часть.

## 8. Статус декомпозиции

Block 0 завершён: зафиксированы владельцы, dependency rules, compatibility
facades и порядок миграции в `docs/ARCHITECTURE_DECOMPOSITION.md`.

Block 1 завершён механически: реализация product parser находится в
`parsing/products.py`, а `services/parser.py` импортирует его напрямую.
Поведение, prompts, state machine, matching, UX, persistence и deployment не
менялись. Focused и полный regression baseline проходят.

Block 2A завершён: из `products.py` вынесены три доказанных независимых
кластера — quantities, packaging и explicit global comment scope. Старый
`services/product_parser.py` удалён после полного import audit: callers старого
пути не найдены.

Block 2B завершён механически: text command parsing разделён по ответственностям
в `parsing/commands/`, а `services/parser.py` стал facade/dispatcher на 224 строки.
`parse_callback()` оставлен отдельным channel contract. Поведение подтверждено
сравнением на 62 существующих случаях и полным baseline `1362 passed`.

Block 3 завершён механически: structured AI schemas и reconciliation разделены
по ответственности в `parsing/ai/`. `integrations/openai_parsing.py` оставлен
совместимым re-export facade на 39 строк; OpenAI transport остаётся в
`integrations/openai_client.py`. Поведение подтверждено focused AI suite
`220 passed` и полным baseline `1362 passed`.

Сохранены инварианты: Google Sheets остаётся source of truth; PostgreSQL,
pgvector, catalog migrations и Sheets sync в Block 3 не реализовывались.
AI предлагает структуру, source phrase подтверждает, catalog уточняет,
детерминированный код выполняет действие.

Block 4 завершён механически: смешанный catalog matching разделён на owners
`catalog/evidence.py` (323 строки), `catalog/scoring.py` (103),
`catalog/retrieval.py` (53), `catalog/safety.py` (385) и
`catalog/resolver.py` (199). `services/matching.py` оставлен transitional
compatibility module: каталоговые symbols re-exported, а
`nearest_valid_multiple()` оставлен совместимым re-export из
`conversation/quantity_resolution.py`, который теперь является владельцем
non-catalog политики кратности.
`services/catalog_resolver.py` остаётся чистым re-export facade. Production callers
переведены на новые owners. Сравнение старого и нового pipeline на 18
представительных corpus-классах дало `0 mismatches`; полный baseline —
`1362 collected / 1362 passed`. Retrieval остался bounded in-memory; PostgreSQL,
pgvector, embeddings, catalog migrations и Sheets sync в Block 4 не добавлялись.

Block 4C review подтвердил: `catalog/retrieval.py` — текущий in-memory retrieval
seam, который позднее можно заменить searchable projection PostgreSQL с
лексическими/full-text или `pg_trgm` индексами и, только после benchmark,
`pgvector`, не меняя evidence, safety, ConversationEngine и channel adapters.
Реализация PostgreSQL, индексов, embeddings, миграций и Sheets sync не входит
в этот блок. `catalog/resolver.py` сохраняет существующие transitional imports
`services/comment_policy` и `services/text`; это остаточная зависимость будущего
parsing/conversation cleanup, а не дублирующая реализация.
Repository-wide audit подтвердил одного owner для catalog responsibilities,
отсутствие циклов и workflow-изменений в engine/orchestrator; единственный
политика кратности вынесена в `conversation/quantity_resolution.py`, а
`services/matching.py` сохраняет только compatibility re-export. Conversation
Block не начинался.

Block 5A выполнен как поведенчески нейтральная декомпозиция conversation
routing/state policy. Новые channel-neutral owners находятся в
`conversation/routing/`: `contracts.py`, `item_resolution.py`, `order_flow.py`,
`comment_scope.py`, `state_compatibility.py` и `modal_routing.py`. Чистые
canonical unresolved membership/priority и state-query функции находятся в
`conversation/state/queries.py`. Production
imports переведены на новые owners, а старые
`services/conversation_handlers/state_compatibility.py`, `modal_routing.py` и
`state.py` оставлены только как re-export facades.

`StateCompatibilityPolicy` сохранила public methods `evaluate`, `context_for`,
`should_try_contextual_fallback` и `submission_failure_mode`, все enum/decision
contracts, context priority и mode values. `has_named_product_items` имеет
одного channel-neutral owner в `conversation/routing/item_resolution.py`;
`PendingQuantityHandler` делегирует ему, поэтому routing больше не импортирует
handler. `PendingQuantityHandler.handle` и `OrderStatusHandler.handle` намеренно
остались в legacy services-пакете: они принимают TelegramEvent или
presentation-зависимые ответы и будут перенесены только вместе с input/
application boundary.

После Block 5AC `StateCompatibilityPolicy` остаётся единым public coordinator,
но больше не наследует routing mixin-классы. `item_resolution.py`,
`order_flow.py` и `comment_scope.py` содержат явные чистые module-level policy
functions; coordinator передаёт им необходимые зависимости. Скрытая связь
через members будущего subclass и `type: ignore[attr-defined]` устранена без
изменения context priority, action/mode contracts или engine workflow.

Block 5A не меняет engine workflow, parser, catalog, prompts, comments,
quantity semantics, database, Docker, Sheets или Telegram UX. Focused modal
suite, полный baseline и quality gates подтверждают сохранение routing behavior.
После review этой границы выполнен отдельный Block 5B по core-операциям draft и
comments; Telegram/presentation handlers в нём не переносились. Block 5C также
завершён: `conversation/selection.py` владеет channel-neutral selection core,
а `CandidateSelectionHandler` остаётся presentation-only адаптером. Engine и
input recognition используют этот общий owner.
Block 5D также завершает поведенчески нейтральное выделение progression core:
`conversation/progression.py` владеет переходом к следующей нерешённой позиции,
а engine сохраняет только presentation adapter.

Постоянное правило: перед каждым `MOVE`/`MERGE`/`DELETE` выполняются
repository-wide usage и duplicate audit. Мёртвый или дублирующий код не
переносится; для одной ответственности остаётся одна реализация. Временный
facade допустим только как явный re-export при подтверждённых callers.

## 9. Block 2B

Документационный commit с постоянными правилами создан отдельно. Text command
parsing механически разделён по доказанным ответственностям в `parsing/commands/`.
`services/parser.py` оставлен компактным facade/dispatcher: `infer_intent`,
`parse_callback` и public compatibility exports. Callback mapping не смешан с
channel-agnostic text parsing. Product parsing Block 2A не изменялся.

До code commit выполнены повторные usage/duplicate/dead-code audit и semantic
comparison на 62 существующих тестовых строках: расхождений нет. Полный
regression baseline остаётся `1362 passed`; code migration прошёл все quality
gates.

## 10. Block 5B — выделение conversation draft и comments

Block 5B выполняется как поведенчески нейтральное выделение channel-neutral
операций из `services/engine.py`. Владельцами становятся:

- `conversation/comments.py` — подтверждённые комментарии, comment scope,
  provenance-нормализация и удаление comment shadows;
- `conversation/draft.py` — наличие активного черновика, поиск дублей и
  слияние только подтверждённых одинаковых строк.

`services/comment_policy.py` остаётся единым владельцем лексических и
provenance-примитивов. `services/conversation_handlers/comment_scope.py`
сохраняет presentation/state handler и делегирует core-операции новым
модулям. Две исторически разные семантики объединения комментариев сохранены:
`merge_comments()` нормализует внутренние пробелы как прежний engine, а
`merge_scope_comments()` сохраняет пробелы как прежний CommentScopeHandler.
Ни один обработчик Telegram, prompt, parser, matching, state-machine workflow
или внешний контракт в этом блоке не изменяется.

Сравнение старой и новой реализаций на corpus состояний и строк дало
`MISMATCHES=0`; полный regression baseline — `1362 collected / 1362 passed`.

Текущий selection core не импортирует `ParsedCommand`: handler преобразует
callback target и поля команды в нейтральные аргументы core. Это сохраняет
channel boundary без изменения callback format и пользовательского поведения.

## 11. Следующий функциональный блок

Следующий seam — только отдельно подтверждённый audit оставшихся `services/`
модулей или внешний review. Автоматически начинать следующий перенос, уменьшать
`engine.py` или менять поведение state machine нельзя.

## 12. Проверки

Для Block 4 подтверждены focused catalog/resolver/supplier/AI suite `89 passed`
и полный baseline `1362 collected / 1362 passed`. Также пройдены:

```text
ruff check src tests
ruff format --check <изменённые Python-файлы>
mypy src
python scripts/check_markdown_links.py
git diff --check
```

Импортированы новые catalog owners и compatibility facades; циклических
импортов не обнаружено. `.env` и `manual_smoke_forensic_logs.txt` в commit не
входят.

## 13. Правила передачи

Текущий код, тесты и Git-diff важнее старых заметок. Не удалять пользовательские
файлы, не использовать destructive Git commands и force push. Не читать и не
выводить `.env`. После каждого блока обновлять этот короткий handoff одной
актуальной записью и отдельно указывать подтверждённые факты, ограничения и
следующий блок.

Критическое состояние проекта не должно существовать только в истории ChatGPT
или Codex-сессии. Для восстановления работы достаточно AGENTS.md,
PROJECT_HANDOFF.md, `.agents/DECISIONS.md`, `.agents/PROJECT_MAP.md`,
архитектурной документации, тестов и текущего Git state. История обсуждений
в handoff не копируется.
