# BLOCK 5Z — forensic-аудит `UpdateOrchestrator`

## IMPLEMENTED AFTER 5Z / BLOCK 6A

### Результат extraction

После analysis-only аудита Block 5Z выполнен один поведенчески нейтральный
перенос. `TelegramInputInterpreter` в
`src/restaurant_bot/input/telegram_interpretation.py` стал владельцем
Telegram-specific interpretation pipeline. Перенесены методы:

- `_parse` → `interpret`;
- `_parse_text_in_context` → `interpret_text`;
- `_parse_sheet_review_command`;
- `_parse_pending_comment_scope`;
- `_match_visible_action`;
- `_needs_visible_action_ai`.

`UpdateOrchestrator._recognizer()` оставлен lazy factory и передаётся в новый
owner через injection. Новый модуль зависит только от domain state/event,
Telegram callback decoder, pure visible-action policy, parsing API,
comment-scope helper и injected provider/recognizer. DB, Redis, TelegramClient,
Google Sheets, CatalogCache, ConversationEngine, review, registration,
checkpoint и background tasks в него не импортируются.

Сохранены без изменения: callback `v2:review*` и pending-comment callbacks;
deep-link review; text order (review deep-link → visible action без pending
comment → global parse → sheet review → comment compatibility → visible-action
fallback → visible-action AI); voice/photo через `InputRecognitionService` с
общим `interpret_text`; сильный ADD_ITEMS не заменяется кнопкой; transient
provider errors дают прежние безопасные результаты.

Механические caller updates выполнены в input/conversation tests: прямых
вызовов старых orchestrator parsing helpers больше нет. Защищённые методы
claim/lease/checkpoint/delivery/review/catalog не менялись.

Фактические метрики после переноса: `orchestrator.py` — 1888 строк, 49
функций/методов, 2 класса; `telegram_interpretation.py` — 330 строк, 11
функций/методов, 3 класса. Коммит: `c6feeb7`. Focused suite: `444 passed`;
full suite: `1377 collected / 1377 passed`.

Дата аудита: 2026-08-13
Ветка: `decompose_bot`
Исходный commit: `d1c5a2150e6a9c1749c10088847b1ce7c491a738`
Проверенный baseline: `1377 collected / 1377 passed` (`16.96 s`)

Это analysis-only документ. В рамках аудита не изменялись application-код,
тесты, схема базы, Alembic, Docker/CI, prompts, OpenAI schemas, catalog safety
thresholds, callback protocol, serialized state, submission protocol, Redis
lease implementation и `UpdateRepository`.

## POST-6A REASSESSMENT / BLOCK 6B

Дата: 2026-08-13. Проверен commit `c3bde2ff1229d9850dcb8dd21543331956f20794`,
ветка `decompose_bot`, origin — GitHub. Свежий полный запуск на текущем HEAD:
`1377 collected / 1377 passed` за `16.19 s`. Изменений в `src/`, tests, scripts,
Alembic, DB, Docker и CI в этом блоке нет.

### Sanity-проверка границы Block 6A

`TelegramInputInterpreter` действительно является владельцем semantic input
interpretation: callback, text, voice и photo доходят до единого
`interpret(...)`; глобальный parsing выполняется до contextual fallback и
`StateCompatibilityPolicy`. В `UpdateOrchestrator` остались только injected
`_recognizer()` и координация жизненного цикла. AST-проверка import graph не
нашла циклов (`cycles=[]`). Новый owner не импортирует DB, Redis, Telegram
transport, Sheets, catalog cache, engine, review, registration, checkpoints или
background tasks. Это sanity-проверка уже принятого 6A, а не повторный аудит
input pipeline.

### Текущие метрики и инвентарь `UpdateOrchestrator`

Текущий файл: `1888` строк, `81567` байт, `2` класса, `49` функций/методов,
`38` import declarations. Метод `process()` занимает строки `159–628` и
остаётся единственным lifecycle coordinator. Полный inventory сгруппирован по
реальным контрактам; в скобках указаны callers/callees, effects и решение.

| Группа и методы | Callers/callees и связанный контракт | Риск | Решение |
|---|---|---|---|
| `__init__`, `process` | worker/API вызывают `process`; внутри claim → lease → venue → state → input owner → catalog → engine → AI → checkpoints → delivery → tasks → finish | критический: DB, Redis lease, Telegram, Sheets, Celery | **PROTECTED / KEEP_COORDINATOR** |
| `_ensure_lease`, `_lease_kwargs` | вызываются вокруг каждой guarded mutation и внешнего эффекта | критический lease fencing | **PROTECTED** |
| `_leave_sheet_review_on_regular_command`, `_handle_review_command`, `_is_review_event`, `_review_stale_reply`, `_sheet_review_ambiguous_reply` | `process` → `OrderReviewService`, repositories, Telegram; token/fingerprint/venue/revision/submission state | высокий: state + DB + Sheets + reply | **PROTECTED** |
| `_scenario_for_intent`, `_effective_analytics_command`, `_request_analytics`, `_analytics_request_text` | только `process` и analytics tests; строят projection outcome/scenario/counts, читают settings и вызывают `PendingQuantityHandler.spoken_quantity` через приватный engine adapter | средний: semantic compatibility, но без mutation | **KEEP_TEMP**; не выделять отдельно |
| `_record_request_outcome` | `process`; `structlog`, `Tracer.observation.update`, anonymized identifiers | средний: внешний telemetry effect | **KEEP_COORDINATOR** |
| `_command_log`, `_state_log`, `_reply_log` | только `process`; формируют privacy-safe log payload | низкий, но schema coupling | **KEEP_COORDINATOR** |
| `_answer_callback_best_effort`, `_send_processing_best_effort`, `_disable_keyboard_best_effort` | только `process`; прямой Telegram transport, broad best-effort exceptions | высокий: ACK timing и stale keyboard safety | **PROTECTED** |
| `_complete_registration`, `_complete_unauthorized`, `_apply_venue_context`, `_ensure_venue_session` | `process`; `VenueRegistrationService`, `SessionRepository`, DB/Redis, reply/checkpoint | высокий: access/venue/session transaction | **PROTECTED** |
| `_recognizer`, `_voice_transcription_prompt`, `_requires_high_accuracy_transcription`, `_has_distinct_transcription_fallback`, `_update_processing` | `process`/`InputRecognitionService`; tests вызывают wrappers; media progress уже принадлежит input service | низкий как seam, высокий риск дублирования | **COMPATIBILITY_FACADE / KEEP_TEMP** |
| `_attach_ui_revision`, `_store_visible_actions` | `process`, registration completion; мутируют `ConversationState` перед checkpoint и поддерживают stale callback guard | высокий: revision + edit-message contract | **PROTECTED** |
| `_voice_processing_reply`, `_photo_received_reply`, `_all_suppliers_processing_reply`, `_text_processing_reply`, `_should_show_text_processing` | `process` и UI tests; shape-only replies, но timing определяется coordinator | средний/высокий из-за UX order | **KEEP_COORDINATOR** |
| `_needs_catalog`, `_requires_fresh_catalog` | `process` и focused tests; чистые статические predicates | низкий, benefit мал | **KEEP_COORDINATOR** |
| `_catalog_match_evidence`, `_resolve_ai_pending` | `process`; `CatalogCache`, catalog evidence/safety, OpenAI shortlist, `ConversationEngine.catalog_resolution` | критический: catalog safety и AI gate | **PROTECTED** |
| `_claim`, `_defer_if_current_attempt` | `process`; `UpdateRepository`, DB transaction, sequence and stale-owner fencing | критический durable protocol | **PROTECTED** |
| `_checkpoint_state`, `_append_order_transition_events`, `_checkpoint_reply`, `_checkpoint_tasks` | `process`; session/update/order-event repositories, lease checks, idempotency | критический transaction/checkpoint coupling | **PROTECTED** |
| `_enqueue_side_effects`, `_finish`, `_error_reply` | `process`; background dispatcher, catalog invalidation, Telegram error reply, DB status | критический effect and retry boundary | **PROTECTED** |

Analytics field contract is currently assembled by `_request_analytics`:
`status`, `scenario`, `intent`, `outcome`, `failure_reason`, `input_type`,
`stage_from`, `stage_to`, `item_count`, `cart_count`, `issue_count`,
`issue_types`, `confidence` and optional privacy-controlled `user_text` /
`request_fingerprint`. `_record_request_outcome` additionally writes structured
logs and Langfuse metadata (`chat_hash`, `venue_hash`). `observability.Tracer`
is an existing telemetry adapter, but it does not own this product outcome
schema. `_effective_analytics_command` depends on the canonical quantity parser
`PendingQuantityHandler.spoken_quantity`; `ConversationEngine._spoken_quantity`
is only a compatibility adapter. Moving analytics now would either split this
schema or introduce a new owner without a stable injected logging-policy
contract. Therefore no analytics implementation is selected in 6B.

Processing UI is also not a safe independent seam. The coordinator sends the
callback ACK before the chat lock, creates voice/photo/text processing cards
after state load, disables the previous keyboard, reuses `processing_message_id`
for the final edit, and preserves best-effort exception handling. Photo progress
updates are already owned by `InputRecognitionService._recognize_photo` through
`update_processing`. Extracting only reply constructors would not remove the
ordering and transport responsibilities; extracting the whole cluster would
duplicate or split that owner. Candidate B is therefore rejected for now.

### Candidate matrix

Scores are 1–5. For benefit, owner maturity, testability and future multi-channel
value, 5 is better. For behavioral risk, transaction/lease/external coupling and
diff size, 5 is worse.

| Candidate | Benefit | Risk | Tx coupling | Lease coupling | External effects | Owner maturity | Testability | Diff size | Multi-channel | Decision |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| A Analytics/observability | 3 | 2 | 1 | 1 | 2 | 3 | 4 | 2 | 3 | KEEP_TEMP |
| B Telegram processing/UI lifecycle | 2 | 4 | 2 | 3 | 5 | 2 | 3 | 3 | 1 | KEEP_COORDINATOR |
| C UI revision/visible-action checkpoint | 2 | 4 | 3 | 4 | 4 | 2 | 3 | 2 | 1 | PROTECTED |
| D Catalog load policy | 1 | 1 | 1 | 1 | 1 | 3 | 4 | 1 | 1 | KEEP_COORDINATOR |
| E Voice compatibility wrappers | 1 | 1 | 1 | 1 | 2 | 4 | 4 | 1 | 2 | COMPATIBILITY_FACADE |
| F Venue/session | 3 | 5 | 5 | 5 | 5 | 3 | 2 | 4 | 1 | PROTECTED |
| G Sheet review | 3 | 5 | 5 | 4 | 5 | 3 | 2 | 5 | 1 | PROTECTED |
| H AI catalog reconciliation | 4 | 5 | 4 | 4 | 5 | 4 | 2 | 5 | 2 | PROTECTED |
| I Durable update protocol | 1 | 5 | 5 | 5 | 5 | 5 | 1 | 5 | 1 | PROTECTED |

### Safety and final decision

`process()` должен остаться coordinator skeleton:

```text
claim → normalize → callback ACK → chat lease/fence → venue/access → state load
→ processing UX → TelegramInputInterpreter → catalog dependencies
→ review или ConversationEngine → AI reconciliation
→ UI revision/visible actions → state checkpoint → Telegram delivery
→ reply checkpoint → background effects → tasks checkpoint → finish/outcome
```

Старые lease/claim/checkpoint/delivery/review/registration/catalog AI contracts
сохраняются. Нижние owners не импортируют `UpdateOrchestrator`; новый input owner
не создаёт обратной зависимости. `observability.Tracer` и
`PendingQuantityHandler.spoken_quantity` остаются текущими владельцами своих
низкоуровневых контрактов.

Итог: **ORCHESTRATOR_DECOMPOSITION_SUFFICIENT**. После Block 6A не найден
достаточно зрелый, низкорисковый следующий implementation seam: analytics и
voice wrappers слишком малы/связаны с compatibility, processing UI и revision
завязаны на timing/checkpoint, catalog/review/venue/durable блоки защищены.

Следующая единственная кампания: **BLOCK 6C — STABILIZATION / REALISTIC SMOKE /
ACCEPTANCE PREP** (сначала plan-only). Block 6C должен проверить реальный
runtime smoke и acceptance readiness; новый production decomposition в этом
блоке не начинать. Block 6B завершён analysis-only.

## 1. Краткий вывод

`UpdateOrchestrator` — не обычный service-handler, а координатор одного
идемпотентного Telegram update. Он одновременно:

1. claim-ит update и сериализует события одного чата;
2. проверяет регистрацию и venue context;
3. выбирает input/parser path для text, voice, photo и callback;
4. загружает catalog и вызывает `ConversationEngine`;
5. выполняет AI candidate reconciliation;
6. фиксирует state, order events, reply checkpoint и background tasks;
7. доставляет Telegram reply и завершает update;
8. записывает analytics и outcome logs.

Поэтому безопасно выделять из него только чистые или уже существующие
application-port seams. Нельзя механически выносить checkpoint, lease,
claim и delivery по отдельности: их порядок является частью safety protocol.

Текущее состояние не требует немедленного production-рефакторинга. Пост-6A
reassessment в разделе `POST-6A REASSESSMENT / BLOCK 6B` завершён решением
`ORCHESTRATOR_DECOMPOSITION_SUFFICIENT`; единственная следующая кампания —
`BLOCK 6C — STABILIZATION / REALISTIC SMOKE / ACCEPTANCE PREP`.

## 2. Проверенный scope и методы

### 2.1. Historical metrics и inventory на Block 5Z

Следующая таблица сохранена как исторический снимок Block 5Z. Актуальные
метрики и inventory после Block 6A находятся в разделе `POST-6A REASSESSMENT /
BLOCK 6B` выше.

| Метрика | Значение | Источник |
|---|---:|---|
| Строки | 2147 | AST/файловый снимок текущего checkout |
| Классы | 2 | `UpdateOrchestrator`, `ClaimedUpdate` |
| Функции и методы | 55 | AST, включая module-level helpers |
| Top-level import declarations | 41 | AST |
| Прямые DB-touching методы | 9 | caller/effect audit |
| Методы checkpoint/claim protocol | 6 | `_claim`, `_defer_if_current_attempt`, `_checkpoint_state`, `_checkpoint_reply`, `_checkpoint_tasks`, `_finish` |

`process()` занимает строки 160–625 и является главным coordinator method.
Наиболее крупная отдельная safety/business зона — `_resolve_ai_pending()`
(1678–1896). Эти размеры не являются сами по себе основанием для переноса:
границы должны следовать transaction, lease, input и catalog contracts.

### 2.2. Полный inventory методов

| Область | Методы | Строки |
|---|---|---:|
| Lease/evidence helpers | `_ensure_lease`, `_lease_kwargs`, `_catalog_match_evidence` | 91–119 |
| Lifecycle coordinator | `__init__`, `process` | 137–625 |
| Review routing | `_leave_sheet_review_on_regular_command`, `_handle_review_command`, `_is_review_event`, `_review_stale_reply`, `_sheet_review_ambiguous_reply` | 628–842 |
| Analytics/observability | `_scenario_for_intent`, `_effective_analytics_command`, `_request_analytics`, `_analytics_request_text`, `_record_request_outcome`, `_command_log`, `_state_log`, `_reply_log` | 845–1163 |
| Telegram best-effort adapters | `_answer_callback_best_effort`, `_send_processing_best_effort`, `_disable_keyboard_best_effort` | 1165–1203 |
| Registration/venue session | `_complete_registration`, `_complete_unauthorized`, `_apply_venue_context`, `_ensure_venue_session` | 1205–1304 |
| Input parsing | `_parse`, `_recognizer`, `_parse_text_in_context`, `_parse_sheet_review_command`, `_parse_pending_comment_scope`, `_match_visible_action`, `_needs_visible_action_ai` | 1306–1574 |
| Voice/photo presentation policy | `_voice_transcription_prompt`, `_requires_high_accuracy_transcription`, `_has_distinct_transcription_fallback`, `_attach_ui_revision`, `_store_visible_actions`, `_voice_processing_reply`, `_photo_received_reply`, `_all_suppliers_processing_reply`, `_update_processing`, `_text_processing_reply`, `_should_show_text_processing` | 1577–1655 |
| Catalog/AI | `_needs_catalog`, `_requires_fresh_catalog`, `_resolve_ai_pending` | 1658–1896 |
| Update claim/checkpoints | `_claim`, `_defer_if_current_attempt`, `_checkpoint_state`, `_append_order_transition_events`, `_checkpoint_reply`, `_checkpoint_tasks`, `_enqueue_side_effects`, `_finish`, `_error_reply` | 1898–2147 |

### 2.3. Прямые внешние эффекты

| Effect | Точки в orchestrator | Канонический владелец/адаптер |
|---|---|---|
| DB read/write | `process`, `_complete_registration`, `_ensure_venue_session`, `_claim`, `_defer_if_current_attempt`, `_checkpoint_state`, `_checkpoint_reply`, `_checkpoint_tasks`, `_finish` | `SessionLocal`, `SessionRepository`, `UpdateRepository`, `OrderEventRepository` |
| Redis chat lease | `process`, registration/session/checkpoint helpers, `_finish` | `integrations/cache.py` (`ChatLease`, `chat_lock`) |
| Telegram send/edit/delete | processing helpers, registration completion, direct search-all-suppliers branch, final reply | `integrations/telegram.py`; reply shape — `presentation/telegram/*` |
| OpenAI | `_parse_text_in_context`, `_parse_pending_comment_scope`, `_resolve_ai_pending`; media delegated to `InputRecognitionService` | `OpenAIService` |
| Google Sheets/catalog | catalog load in `process`; review snapshot through `OrderReviewService`; invalidation after tasks | `CatalogCache`, `GoogleSheetsGateway`, `OrderReviewService` |
| Celery/background work | `_enqueue_side_effects` | `BackgroundTaskDispatcher` / `CeleryBackgroundTaskDispatcher` |
| Analytics/logging | `_request_analytics`, `_record_request_outcome`, `_command_log`, `_state_log`, `_reply_log` | `structlog`, tracer/Langfuse adapter |

Главный признак смешения ответственности: `process()` удерживает одновременно
порядок durable protocol и выбор semantic/input pipeline. Это не означает, что
lease/checkpoint можно вынести независимо: они должны остаться одной
координационной последовательностью.

## 3. Callers и callees

### 3.1. Callers

| Caller | Вызов | Контракт |
|---|---|---|
| `api/app.py` | `process_telegram_update.delay(event.update_id)` после `enqueue_once` | webhook только принимает update; commit→delay gap остаётся эксплуатационным риском |
| `workers/tasks.py` | `orchestrator.process(update_id)` | Celery retry для transient Telegram/lock/sequence/lease ошибок |
| `workers/tasks.py:redrive_telegram_updates` | повторно ставит recoverable update | выбирает earliest unfinished update по чату |
| tests | прямые вызовы helpers и `process` | фиксируют текущие contracts; не являются production callers |

### 3.2. Ключевые callees

| Зона | Callees |
|---|---|
| Input | `normalize_telegram_update`, `InputRecognitionService.recognize_media`, `parse_callback`, `parse_text`, `StateCompatibilityPolicy`, visible-action policy |
| State | `ConversationEngine.handle`, `SessionRepository.get_for_update/save` |
| Catalog | `CatalogCache.get/invalidate`, `CatalogResolutionService`, `CatalogEvidence`, `OpenAIService.choose_catalog_candidate` |
| Review | `OrderReviewService.snapshot`, `presentation.telegram.order_review`, review token/fingerprint contracts |
| Registration | `VenueRegistrationService.handle/context_for/context_for_identity`, registration presentation |
| Durable update | `UpdateRepository.get_for_update`, `enqueue_once`, `claim`, `defer`, `recoverable_for_redrive` |
| Durable audit | `OrderEventRepository.append_once` |
| Delivery | `TelegramClient.send_reply/answer_callback/delete_message` |
| Background | `BackgroundTaskDispatcher.submit_order/submit_product_add/search_all_suppliers/status/review` |

Нижние слои не импортируют `UpdateOrchestrator` для production logic. Тестовые
прямые вызовы helper-методов — compatibility surface, но они не дают права
менять production ownership.

## 4. Точный pipeline `process()`

Фактический порядок на текущем checkout:

```text
webhook
  -> enqueue_once(update_id, chat_id, payload) в DB transaction
  -> Celery delay(update_id)
  -> UpdateOrchestrator.process(update_id)
      -> первичный _claim(update_id, mark_processing=False)
      -> normalize_telegram_update(payload)
      -> bind structured logger / telegram_update_started
      -> если нет chat_id: finish(ignored)
      -> callback ACK best-effort (до chat lock)
      -> contextvars + tracer + chat_lock(chat_id)
      -> lease.ensure_owned()
      -> авторитетный _claim(mark_processing=True)
         -> lower unfinished? UpdateSequenceDeferred
         -> fresh processing? skip
         -> stale processing? reclaim + attempts += 1
      -> replay checkpoint result, если state_applied/result уже сохранены
      -> registration.handle(event)
         -> registration checkpoint/reply/tasks/finish при handled
      -> venue context / access check
         -> unauthorized checkpoint/reply/finish при отказе
      -> ensure venue session (row lock, venue reset/context save)
      -> load state (short DB read transaction)
      -> processing card (voice/photo всегда; text только для ADD_ITEMS)
      -> _parse(callback | text contextual | voice/photo)
      -> sheet-review compatibility policy
      -> SEARCH_ALL_SUPPLIERS processing path
      -> catalog_needed/fresh decision + CatalogCache.get
      -> review handler либо engine.handle
      -> lease check
      -> _resolve_ai_pending (кроме review command)
      -> leave sheet-review mode on regular command
      -> trace enrichment / engine_transition_completed
      -> analytics request/outcome preparation
      -> choose reply edit target
      -> ui_revision += 1; attach revision; store visible actions
      -> _checkpoint_state (session + order_events + update result/state_applied)
      -> send reply if not reply_sent; _checkpoint_reply
      -> enqueue side effects if not tasks_enqueued; _checkpoint_tasks
      -> invalidate catalog if requested
      -> _finish(done)
      -> analytics/tracing processed
  -> ChatLeaseLostError: guarded defer queued + Celery retry
  -> generic error: lease fence; finish(failed); error reply or transient redrive
```

Важно: `_resolve_ai_pending` выполняется после `engine.handle`, меняет статусы
и catalog fields, затем повторно вызывает engine только с
`CONTINUE_CURRENT`. Это continuation/progression, а не повтор исходного
пользовательского `ADD_ITEMS`; исходная команда не применяется к item повторно.

## 5. Safety protocol: claim, sequence, checkpoints, lease

### 5.1. Claim и идемпотентность

`telegram_updates.update_id` — первичный ключ. Webhook использует nested
transaction в `UpdateRepository.enqueue_once`; duplicate insert возвращает
`False`, после чего уже существующий `queued/processing` update всё равно может
быть redelivered. `done/ignored` не запускаются повторно.

Второй claim внутри chat lock выполняется в DB transaction и блокирует update row:

- `queued` → `processing`, `attempts += 1`;
- свежий `processing` не трогается;
- processing старше пяти минут может быть reclaimed;
- lower unfinished update в том же чате вызывает `UpdateSequenceDeferred`;
- deferred update возвращается в `queued`, checkpoint booleans не сбрасываются.

Это обеспечивает порядок по chat/update_id, но commit→Celery delay gap в webhook
остаётся отдельным инфраструктурным риском: его покрывает redrive, а не
orchestrator.

### 5.2. Checkpoint A — state/result

`_checkpoint_state()` в одной `SessionLocal.begin()` transaction:

1. блокирует `bot_sessions` row;
2. проверяет lease;
3. сохраняет ConversationState;
4. в той же transaction добавляет order transition events через `append_once`;
5. блокирует `telegram_updates` row;
6. сохраняет serialized `EngineResult` и `state_applied=True`.

Нельзя отделять order events от state checkpoint: иначе replay может увидеть
state без аудита или аудит без соответствующего state.

### 5.3. Checkpoint B — reply

После успешной Telegram delivery `_checkpoint_reply()` блокирует update row,
ставит `reply_sent=True` и при наличии message id сохраняет `state.ui_message_id`
в той же transaction. Повтор после checkpoint не должен отправлять второй reply.

### 5.4. Checkpoint C — tasks

`_enqueue_side_effects()` проверяет lease перед каждой фоновой задачей.
После успешного enqueue `_checkpoint_tasks()` durable-фиксирует
`tasks_enqueued=True`. При повторе уже установленный флаг не позволяет
поставить side effects второй раз.

### 5.5. Lease и fencing

Текущий `ChatLease` в `integrations/cache.py` имеет heartbeat thread с interval
`timeout / 3`, `ensure_owned()`, `refresh()` и safe close. Это исправляет старый
исторический риск «lease не продлевается». Однако Redis outage или потеря
heartbeat всё ещё может привести к fencing.

Проверки стоят перед/после наиболее опасных границ: перед вторым claim, после
engine, до/после checkpoint, до/после Telegram, до/после background tasks,
до/после cache invalidation и внутри finish. Внутри некоторых checkpoint
transactions последняя проверка находится перед SQL update, а не непосредственно
перед commit; это наблюдение для будущего отдельного safety review, а не
основание менять код в Block 5Z.

После `ChatLeaseLostError` `_defer_if_current_attempt()` делает guarded update
только при совпадении `status=processing` и номера `attempts`, после чего Celery
повторяет update. Старый owner не должен завершить update после fencing.

## 6. Transaction map

| Transaction | Locks/effects | Почему нельзя разрывать |
|---|---|---|
| webhook `enqueue_once` | insert `telegram_updates` | duplicate idempotency |
| `_claim` | update row, sequence/status/attempt | один владелец попытки |
| `_defer_if_current_attempt` | guarded status reset | stale owner fencing |
| `_ensure_venue_session` | session row, venue reset/context | venue isolation |
| `_checkpoint_state` | session + order events + update result | replay consistency |
| `_checkpoint_reply` | update + UI message id | delivery replay consistency |
| `_checkpoint_tasks` | update tasks flag | side-effect idempotency |
| `_finish` | update terminal status | durable terminal outcome |
| `OrderReviewService.submit` | lease + session row + Sheets + reply | protected review protocol |

Read-only state loads in `process` и registration происходят в коротком
`SessionLocal()` context, а durable writes — в перечисленных `begin()` blocks.
Внешние AI/Sheets операции не выполняются внутри `_checkpoint_state` DB
transaction; комментарий в коде это явно отмечает.

## 7. Registration и access

До обычного venue context `process()` вызывает `VenueRegistrationService.handle`.
Для handled registration `_complete_registration` сохраняет state, отправляет
reply, фиксирует reply/tasks и завершает update. Для остальных событий:

- review deep-link принудительно обновляет identity access context;
- обычные события используют cached `context_for(event)`;
- отсутствие context ведёт в `_complete_unauthorized`;
- `_ensure_venue_session` сбрасывает state при смене venue и сохраняет новый
  `VenueContext` под row lock.

Регистрация, access registry и Telegram registration input уже разделены:
`input/telegram_venue_registration.py`, `integrations/venue_directory.py`,
`integrations/venue_access_registry.py`,
`presentation/telegram/venue_registration.py`, а
`services/venue_registration.py` остаётся DB/Sheets/cache/rollback coordinator.

## 8. Parse order и contextual routing

### Callback

`_parse()` обрабатывает callback отдельно:

1. `v2:review*` → review command;
2. при `pending_comment_items` → comment-scope callback path;
3. иначе `parse_callback` + enrichment.

Callback остаётся явным UI-путём и не должен проходить через текстовый
semantic preemption.

### Text

`_parse_text_in_context()` соблюдает порядок:

1. review deep-link;
2. если нет pending comment — deterministic visible action;
3. обычный global `OpenAIService.parse_text`;
4. sheet-review compatibility;
5. при pending comment — `StateCompatibilityPolicy` и contextual comment fallback;
6. visible-action fallback только для UNKNOWN или ADD_ITEMS без items;
7. при необходимости — `choose_visible_action` через OpenAI;
8. selected visible action превращается в enriched callback command.

Таким образом, сильный global intent не должен быть заменён candidate/comment/
visible fallback. Это соответствует каноническому владельцу
`conversation/routing/state_compatibility.py`.

### Voice/photo

`InputRecognitionService` владеет download/provider calls, transcription,
retry, media progress и передачей транскрипта в тот же `_parse_text_in_context`.
Чистые политики находятся в `input/voice_policy.py` и
`input/voice_transcript_policy.py`. В voice path отсутствует отдельный второй
semantic router: после ASR транскрипт идёт в общий text contextual pipeline.

## 9. Visible actions и UI revision

Visible actions — contextual fallback, а не глобальный parser. Их извлечение
страниц принадлежит `input/telegram_visible_actions.py`, а pure text/voice
matching — `input/voice_policy.py`. Orchestrator только:

- решает, нужен ли visible-action AI fallback;
- прикрепляет `:r{ui_revision}` к callback data;
- сохраняет список visible actions в state;
- при callback передаёт revision в `parse_callback`/engine.

Перед render `state.ui_revision` увеличивается, reply text сохраняется в state,
visible actions checkpoint-ятся вместе с state. Callback со старой revision
должен возвращать stale reply и не менять draft. Это правило нельзя обходить
переносом visible matcher в orchestrator-specific blacklist.

## 10. Catalog loading и `_resolve_ai_pending`

`_needs_catalog()`/`_requires_fresh_catalog()` выбирают, нужно ли читать
`CatalogCache`. Review commands не требуют catalog. Для обычного `ADD_ITEMS`,
candidate selection и catalog-dependent intents cache загружается по venue
spreadsheet id; fresh path использует `force_refresh=True`.

`_resolve_ai_pending()` выполняет следующие этапы:

1. deterministic equivalence pass для `AMBIGUOUS` items;
2. skip unsafe packaging/variant-comment cases;
3. применяет точно один безопасный catalog equivalent;
4. остальные candidate items переводит в `AI_PENDING`;
5. строит deterministic shortlist и evidence log;
6. вызывает `choose_catalog_candidate`;
7. применяет conservative safety gate: action, id, score, confidence, no
   contradictions, numeric/packaging compatibility, no conflicting qualifiers,
   no unscoped variant comment, no ambiguous packaging и verified terms;
8. safe select → `CatalogResolutionService.apply_catalog`;
9. not-found с достаточной evidence сохраняет ambiguity либо ставит NOT_FOUND;
10. blocked select остаётся AMBIGUOUS;
11. запускает `engine.handle(... CONTINUE_CURRENT ...)` для progression.

AI получает deterministic shortlist и не обходит deterministic gates. Название,
количество, фасовка, поставщик и комментарий не смешиваются этим coordinator-ом;
канонический catalog owner — `orders/catalog_resolution.py` плюс catalog evidence
helpers.

## 11. Review flow

`_handle_review_command()` обслуживает `REVIEW_ORDER`, `REVIEW_REFRESH`,
`REVIEW_SUBMIT`, `REVIEW_CANCEL`:

- получает venue context с `force_refresh=True`;
- `OrderReviewService.snapshot()` читает Sheets и строит frozen snapshot/fingerprint;
- генерирует review token;
- сохраняет review mode/stage/status;
- presentation строит preview/replies без side effects;
- stale callback revision/token получает безопасный stale reply;
- `REVIEW_SUBMIT` только ставит `enqueue_review_submission`, а фактическая
  отправка выполняется защищённым `OrderReviewService.submit` в background path.

`OrderReviewService.submit()` не входит в campaign 5Z: он держит lease 300 s,
перечитывает identity и snapshot перед submission, проверяет token/fingerprint,
использует Sheets и Telegram side effects. Его протокол уже является отдельным
protected owner.

## 12. Analytics, order events и побочные эффекты

### Analytics

`_request_analytics()` строит scenario/outcome/counts/status reason; `_record_request_outcome`
пишет structured log и trace. `_effective_analytics_command()` использует
production compatibility вызов `ConversationEngine._spoken_quantity`, поэтому
этот thin facade пока нельзя удалять без caller migration.

### Order events

`_append_order_transition_events()` вызывается только внутри `_checkpoint_state`.
Idempotency keys текущего кода:

- `trace:{trace_id}:started`;
- `update:{update_id}:order-action`;
- `trace:{previous_trace_id}:cancelled`;
- `order:{pending.order_no}:requested`.

`OrderEventRepository.append_once` и state checkpoint должны оставаться одной
transaction boundary.

### Side effects

`_enqueue_side_effects()` dispatch-ит submit order/product add, supplier search,
status и review tasks через application port. Каждая задача защищена lease check
перед enqueue; durable flag записывается после enqueue. Catalog invalidation
выполняется после task checkpoint и до finish. Submission service имеет отдельную
инвалидацию/side-effect safety и не должен быть перенесён в этот audit.

## 13. Ошибки и retry

| Ошибка | Текущий путь | Результат |
|---|---|---|
| `ChatLockBusyError` | возникает при входе в `chat_lock` | Celery autoretry; lock contention не теряется |
| `UpdateSequenceDeferred` | второй claim видит lower unfinished | update остаётся/возвращается queued, Celery retry |
| `ChatLeaseLostError` | fence в любом effect boundary | guarded defer текущей attempt, Celery retry |
| OpenAI transient в voice | `InputRecognitionService` возвращает UNKNOWN | semantic safe fallback без мутации draft |
| Telegram transient после state checkpoint | generic handler видит `delivery_deferred` | finish failed/re-raise для autoretry, reply не дублируется |
| generic error до reply | `_finish(failed)` + best-effort error reply | повтор опирается на checkpoints |
| catalog refresh error | cache может вернуть имеющийся cached value | отдельная cache policy; orchestrator не маскирует safety gate |

Исторический `docs/RAPID_INPUT_CONCURRENCY_ANALYSIS.md` описывает pre-heartbeat
состояние и утверждение, что TimeoutError не retry-ится. Это не текущий факт:
current `workers/tasks.py` retry-ит lock/sequence/lease ошибки, а `ChatLease`
имеет heartbeat. Документ следует читать только как historical snapshot.

## 14. Replay matrix

| Точка повтора | Что уже durable | Ожидаемое действие |
|---|---|---|
| до `_checkpoint_state` | только claim | повторно выполнить semantic/engine path под тем же update attempt |
| после state checkpoint, до reply | `state_applied=True`, result | не вызывать engine повторно; доставить сохранённый reply |
| после reply checkpoint, до tasks | `reply_sent=True` | не отправлять второй reply; enqueue tasks |
| после tasks checkpoint, до finish | `tasks_enqueued=True` | не ставить задачи второй раз; finish |
| после finish done | terminal status/result/reply/tasks | no-op |
| после lease loss | guarded queued + сохранённые flags | новый owner продолжает с checkpoint flags |
| stale processing | DB age > 5 min | redrive только earliest recoverable per chat |

Важное ограничение: checkpoint booleans не являются полной distributed
transaction. Между внешним эффектом и его checkpoint остаётся crash window; код
минимизирует повтор durable guards, но не делает Telegram/Celery exactly-once.

## 15. Concurrency review

Текущая защита состоит из трёх уровней:

1. DB primary-key idempotency update;
2. Redis per-chat lease с heartbeat и `ensure_owned` fencing;
3. DB sequence claim по `update_id` внутри одного чата.

Сильные стороны:

- разные чаты не блокируют друг друга;
- lower update не перескакивает через unfinished predecessor;
- stale owner не может пройти guarded effect boundary;
- Celery retry покрывает lock busy, sequence defer и lease loss;
- side-effect tasks защищены durable `tasks_enqueued`.

Оставшиеся доказуемые риски для отдельного будущего review:

- commit→delay gap webhook требует redrive;
- heartbeat зависит от Redis availability;
- некоторые checkpoint transactions не делают отдельный lease check прямо перед
  commit;
- нет полного multi-worker integration test, только focused concurrency tests;
- отправка Telegram и enqueue Celery неизбежно имеют внешний crash window.

Это не новые дефекты Block 5Z и не исправляются в рамках analysis-only audit.

## 16. Дублирование ответственности

| Наблюдение | Текущий статус | Решение аудита |
|---|---|---|
| Voice policy wrappers в `InputRecognitionService` и pure policy modules | thin compatibility adapters | оставить до caller migration |
| Visible action matching через service static wrapper | thin adapter → pure `input/voice_policy.py` | оставить, не дублировать в orchestrator |
| Review reply builders и review coordinator | presentation vs effect coordination | граница корректна |
| Catalog evidence и AI decision gate | evidence/safety helpers + orchestrator orchestration | future seam возможен, но не в 5Z |
| Analytics mapping в orchestrator | использует engine compatibility `_spoken_quantity` | не удалять facade без доказанного production caller migration |
| `services/venue_registration.py` re-exports | compatibility boundary | protected transitional owner |

Не обнаружено безопасного основания вводить `engine_part*.py`, `helpers.py`,
`utils.py` или новый параллельный modal/router owner.

## 17. Dependency graph

```text
Telegram
  -> api/app.py
  -> repositories/updates.py (enqueue_once)
  -> workers/tasks.py (Celery delivery)
  -> services/orchestrator.py
       -> input/telegram*.py / callbacks / visible actions
       -> services/input_recognition.py
       -> parsing/commands/api.py
       -> conversation/routing/state_compatibility.py
       -> integrations/openai_client.py
       -> integrations/cache.py / google_sheets.py / telegram.py
       -> services/venue_registration.py
       -> services/engine.py
            -> conversation/* core policies and handlers
            -> orders/catalog_resolution.py
       -> application/background_tasks.py
       -> repositories/sessions.py / order_events.py / updates.py
       -> presentation/telegram/* (reply construction)
```

`domain`/`conversation`/`parsing` pure modules не должны импортировать
`orchestrator` или Telegram transport. `orchestrator` — верхняя composition
boundary; `workers` и `api` знают его, нижние owners — нет.

## 18. Historical candidate seams и риск (Block 5Z)

Эта таблица описывает состояние до Block 6A и сохранена для трассировки решения;
она заменена актуальной score matrix в разделе `POST-6A REASSESSMENT / BLOCK 6B`.

| Candidate seam | Что переносится | Риск | Вердикт |
|---|---|---|---|
| Input parse pipeline | `_parse`, media/text contextual order, parse result logging hooks | высокий: меняет pre-modal ordering и voice/callback parity | единственная следующая campaign, строго coordinator-preserving |
| Analytics projection | scenario/count/status pure mapping | низкий-средний, но `_spoken_quantity` production caller | после input campaign, отдельно |
| Processing card helpers | text/voice/photo progress replies | низкий, но Telegram edit timing | можно позже, не следующий campaign |
| Catalog AI reconciliation | `_resolve_ai_pending` | очень высокий: safety gate, AI shortlist, engine re-entry | KEEP_TEMP; отдельный design review |
| Checkpoint protocol | claim/state/reply/tasks/finish | критический: lease/idempotency/transaction | не переносить механически |
| Registration completion | `_complete_registration` | высокий: state→reply→tasks order | protected |
| Review flow | `_handle_review_command` | высокий: token/fingerprint/venue access | protected, `OrderReviewService` уже owner effect |
| Side-effect dispatch | `_enqueue_side_effects` | высокий: lease fence and idempotency | protected |

### Ранжирование

1. Input parse pipeline — только после отдельного implementation plan;
2. pure analytics projection — после стабилизации input seam;
3. processing presentation adapters;
4. catalog AI reconciliation — после доказательного safety audit;
5. checkpoint/lease/registration/review — не переносить без новой campaign.

## 19. Historical implementation campaign (выполнена в Block 6A)

### `UpdateInputPipeline` — механическое выделение semantic input boundary

**Цель:** сделать видимым и тестируемым порядок

```text
normalized TelegramEvent + loaded ConversationState
  -> callback/text/voice/photo parse
  -> global ParsedCommand
  -> existing StateCompatibilityPolicy/contextual fallback
  -> parsed command + parse metadata
```

**Включить:**

- `_parse()`;
- `_parse_text_in_context()`;
- `_parse_sheet_review_command()`;
- `_parse_pending_comment_scope()`;
- `_recognizer()` как dependency factory/port;
- только pure selection helpers `_match_visible_action`,
  `_needs_visible_action_ai`, если их contracts можно сохранить;
- existing logging metadata as returned metadata, без изменения log schema.

**Оставить в `UpdateOrchestrator`:**

- `_claim`, sequence, stale processing, lease and all fences;
- registration/access and venue session transaction;
- catalog cache loading and invalidation;
- `ConversationEngine.handle` call;
- `_resolve_ai_pending` and engine re-entry;
- all checkpoints, order events, reply/task delivery and finish;
- analytics recording and error/retry protocol.

**Обязательные условия до кода:**

1. AST/caller inventory на исходном commit;
2. characterization tests для callback/text/voice/photo ordering;
3. exact before/after comparison of `ParsedCommand`, visible fallback and
   `StateCompatibilityPolicy` outcomes;
4. no prompt/parser/catalog threshold changes;
5. no new intent-specific rules;
6. focused routing/input tests, full pytest, ruff, mypy, markdown links,
   `git diff --check`;
7. rollback через один docs/implementation commit without touching protected
   checkpoint code.

**Ожидаемый результат:** `process()` читается как orchestration pipeline, но
durable safety protocol остаётся в одном owner и не появляется второй
orchestrator.

Никаких параллельных functional campaigns в этом аудите не назначается.

## 20. Что нельзя переносить в эту campaign

- `_checkpoint_state`, `_checkpoint_reply`, `_checkpoint_tasks`, `_finish`;
- `_claim` и `_defer_if_current_attempt`;
- `_enqueue_side_effects`;
- `_complete_registration` и `_ensure_venue_session`;
- `_handle_review_command` и `OrderReviewService.submit`;
- `_resolve_ai_pending`;
- catalog cache/evidence/safety gate;
- OpenAI prompts/schemas;
- SessionRepository/UpdateRepository/Redis lease;
- callback serialized protocol и UI revision format.

## 21. Channel neutrality

Потенциально channel-neutral:

- `ParsedCommand` и `StateCompatibilityPolicy`;
- contextual comment/candidate compatibility;
- catalog resolution/evidence;
- ConversationEngine state transitions;
- review snapshot/token/fingerprint contracts;
- analytics scenario projection (после удаления Telegram-specific fields).

Telegram-specific:

- update normalization;
- callback `v2:*` parsing and ACK;
- visible action labels/callback revision;
- Telegram processing cards and message editing;
- `TelegramClient` delivery;
- Telegram registration input/presentation.

Нельзя объявлять весь `UpdateInputPipeline` channel-neutral: он будет иметь
Telegram adapter на входе, а semantic parser can remain channel-neutral ниже.

## 22. Quality gates и результаты

В рамках forensic audit выполнен свежий полный run:

```text
.venv\Scripts\python.exe -m pytest -o addopts='' --basetemp=.tmp\pytest-5z-baseline -q --tb=short
1377 passed in 16.96s
```

До изменения docs-only файлов необходимо повторить стандартные проверки проекта:

```text
.venv\Scripts\ruff.exe check src tests scripts alembic
.venv\Scripts\ruff.exe format --check src tests scripts alembic
.venv\Scripts\python.exe -m mypy src
.venv\Scripts\python.exe scripts\check_markdown_links.py
git diff --check
```

Результат этих quality gates должен быть указан в commit report. Production
код и тесты в Block 5Z не меняются.

## 23. Git и безопасность

- стартовый и проверенный HEAD: `d1c5a2150e6a9c1749c10088847b1ce7c491a738`;
- branch: `decompose_bot`;
- origin: `https://github.com/Dzmitry-Radziuk/test_bot.git`;
- GitLab не использовался;
- `git ls-files .env` вернул пустой результат;
- `.env`, credentials и tokens не читались и не входят в audit doc;
- рабочее дерево до docs-only изменений было чистым.

## 24. Документы и текущие владельцы

Текущие owner-map документы остаются компактными. Этот документ является
подробным forensic приложением; в `PROJECT_HANDOFF.md`, `.agents/PROJECT_MAP.md`
и `docs/ARCHITECTURE_DECOMPOSITION.md` добавляется только ссылка на него, без
копирования таблиц и pipeline.

## 25. Итоговое решение

`UpdateOrchestrator` сейчас выполняет слишком много coordinator-level обязанностей,
но его самые опасные части связаны transaction/lease/checkpoint invariants.
Поэтому безопасный первый шаг — не дробить `process()` по строкам, а выделить
только input interpretation boundary с сохранением внешних contracts. Любой
перенос catalog AI, review, registration или durable protocol до отдельного
доказательного плана запрещён.
