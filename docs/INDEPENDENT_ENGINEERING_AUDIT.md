# Исторический независимый инженерный аудит — 13 августа 2026 года

Снимок относится к ветке `decompose_bot`; выводы и баллы ниже не описывают
текущую готовность ветки `develop`.

Дата: 2026-08-13

Ветка: `decompose_bot`

Исходный production HEAD: `4dc67430336bb90cfad731aca583f51a19948a48`
Режим: production-код заморожен; изменены только документация, scenario source,
generated scenario views и их генератор.

## Итог

Проект инженерно сильнее типичного pilot-stage Telegram-бота: здесь есть durable
inbox, сериализация чата, stale-owner fencing, проверяемая state policy, раздельная
provenance товара/количества/комментария, conservative catalog selection и явный
submission protocol с `uncertain` состояниями. Зелёный suite подтверждает большое
число негативных и failure-injection контрактов, а не только happy path.

Проект ещё не 10/10 и не доказан для широкого production. Главные ограничения —
неподтверждённая ротация ранее раскрытого Telegram token, отсутствие свежего live
preflight, точные окна между сетевым эффектом и checkpoint, broad retry Google read,
а также list-backed production catalog. Последнее является ограничением масштаба, но
не блокером малого контролируемого pilot.

**Overall engineering score: 8.4 / 10.**

**Controlled Telegram pilot readiness: 8.0 / 10, после обязательных preflight.**

**Production-at-scale readiness: 6.9 / 10.**

## Что означает 10/10 для этого проекта

10/10 не требует нулевого долга, одинаково маленьких файлов, 100% coverage или уже
реализованных будущих каналов. Для текущей стадии это означает:

1. Все риски неверной заявки, cross-venue доступа и повторного внешнего эффекта имеют
   доказанный owner, durable contract и наблюдаемое восстановление.
2. Репозиторные проверки зелёные, а критические provider paths подтверждены в
   изолированном live/staging окружении с fault injection.
3. Секреты отозваны/ротированы, эксплуатационные alerts, backup и restore drill
   подтверждены, а не только описаны.
4. Текущий объём каталога укладывается в измеренный latency/memory budget; перед 100k
   подключён bounded indexed provider.
5. Новый разработчик за 30 минут находит owners, safety invariants, test entrypoints и
   защищённый долг без чтения исторических кампаний.
6. Долг имеет измеримый trigger и не оплачивается заранее ценой риска стабильному
   Telegram runtime.

## Метод и свежие проверки

Аудит построен по runtime call graph, коду, миграциям, тестам и конфигурационным
контрактам. Старые verdict использовались только как указатели для повторной проверки.

| Проверка | Результат до обновления документации |
|---|---|
| `pytest` | `1391 collected / 1391 passed`, около 22.8 s; одно Windows cache warning |
| `pytest --collect-only` | `1391 tests collected in 1.99 s` |
| `mypy src` | `139 source files, no issues` |
| Ruff check | pass |
| Ruff format | `252 files already formatted` |
| Markdown links | `35 files, pass` |
| Scenario check | `34 scenarios, pass` |
| Compileall | pass |
| `git diff --check` | pass |

Финальные post-doc results приведены в конце документа после повторного полного
прогона. Hosted GitHub/GitLab CI в этой кампании не запускался и не считается
подтверждённым.

## Инвентаризация документации

`CURRENT_CANONICAL` — текущий источник истины; `CURRENT_SPECIALIZED` — актуальная
инструкция конкретной области; `HISTORICAL_ARCHIVE` — полезный снимок прошлого, но не
current state. Remove-кандидатов и настоящих дублей не найдено.

| Документ | Класс | Решение |
|---|---|---|
| `AGENTS.md` | CURRENT_CANONICAL | обязательный рабочий контракт |
| `README.md` | CURRENT_CANONICAL | обновлены owners, runtime, channels и catalog scale |
| `PROJECT_HANDOFF.md` | CURRENT_CANONICAL | заменён устаревший следующий refactor актуальным handoff |
| `.agents/PROJECT_MAP.md` | CURRENT_CANONICAL | обновлён текущий статус и переход к pilot |
| `.agents/DECISIONS.md` | CURRENT_CANONICAL | решения сверены; массовое переписывание не требуется |
| `.agents/DEVELOPMENT_PROCESS.md` | CURRENT_CANONICAL | текущие quality gates и процесс |
| `docs/CURRENT_ARCHITECTURE.md` | CURRENT_CANONICAL | переписан по фактическому call graph |
| `docs/TESTING_READINESS.md` | CURRENT_CANONICAL | удалены current claims `6299a84`/`1382`/freeze |
| `docs/README.md` | CURRENT_CANONICAL | навигация по актуальной документации |
| `docs/archive/README.md` | CURRENT_CANONICAL | навигация по историческому архиву |
| `docs/user-scenarios/scenarios.json` | CURRENT_CANONICAL | версия 2.1, 40 сценариев |
| `docs/USER_SCENARIOS.md` | CURRENT_CANONICAL | generated; вручную не редактируется |
| `docs/user-scenarios/index.html` | CURRENT_CANONICAL | generated HTML-каталог |
| `README-DEVOPS.md` | CURRENT_SPECIALIZED | deployment, backup, restore, rollback, alerts |
| `SECURITY.md` | CURRENT_SPECIALIZED | актуальный checklist; незакрытые пункты нельзя считать выполненными |
| `tests/README.md` | CURRENT_SPECIALIZED | структура и назначение test suite |
| `docs/CHANNEL_EXTENSION_GUIDE.md` | CURRENT_SPECIALIZED | расширение каналов без заявления об их реализации |
| `docs/ci/README.md` | CURRENT_SPECIALIZED | CI-контекст; hosted run здесь не подтверждён |
| `docs/archive/architecture/REFACTORING_CONTEXT_HANDOFF.md` | HISTORICAL_ARCHIVE | handoff завершённой декомпозиции |
| `docs/archive/architecture/ARCHITECTURE_DECOMPOSITION.md` | HISTORICAL_ARCHIVE | история structural campaign |
| `docs/archive/data-integrity/DATA_INTEGRITY_ANALYSIS.md` | HISTORICAL_ARCHIVE | исходный data-integrity audit |
| `docs/archive/data-integrity/DATA_INTEGRITY_BLOCK_A_PLAN.md` | HISTORICAL_ARCHIVE | план завершённого блока |
| `docs/archive/data-integrity/DATA_INTEGRITY_BLOCK_B_PLAN.md` | HISTORICAL_ARCHIVE | план завершённого блока |
| `docs/archive/data-integrity/DATA_INTEGRITY_BLOCK_C_PLAN.md` | HISTORICAL_ARCHIVE | план завершённого блока |
| `docs/archive/data-integrity/DATA_INTEGRITY_BLOCK_D_PLAN.md` | HISTORICAL_ARCHIVE | план завершённого блока |
| `docs/archive/data-integrity/DATA_INTEGRITY_BLOCK_E_PLAN.md` | HISTORICAL_ARCHIVE | план завершённого блока |
| `docs/archive/data-integrity/DATA_INTEGRITY_BLOCK_F_PLAN.md` | HISTORICAL_ARCHIVE | план завершённого блока |
| `docs/archive/data-integrity/DATA_INTEGRITY_BLOCK_G_PLAN.md` | HISTORICAL_ARCHIVE | план завершённого блока |
| `docs/archive/diagnostics/semantic_acceptance_round2.md` | HISTORICAL_ARCHIVE | forensic evidence прошлой приёмки |
| `docs/archive/regression/FULL_REGRESSION_AUDIT.md` | HISTORICAL_ARCHIVE | старый baseline не используется как current |
| `docs/archive/architecture/INPUT_RECOGNITION_AUDIT.md` | HISTORICAL_ARCHIVE | завершённый аудит границы ввода |
| `docs/archive/regression/RAPID_INPUT_CONCURRENCY_ANALYSIS.md` | HISTORICAL_ARCHIVE | исходный concurrency analysis |
| `docs/archive/architecture/SERVICES_TEXT_AUDIT.md` | HISTORICAL_ARCHIVE | завершённый targeted audit |
| `docs/archive/architecture/SERVICES_TRANSITION_AUDIT.md` | HISTORICAL_ARCHIVE | история перехода owners |
| `docs/archive/submission/SUBMISSION_H3_PLAN.md` | HISTORICAL_ARCHIVE | завершённый план submission safety |
| `docs/archive/submission/SUBMISSION_IDEMPOTENCY_PLAN.md` | HISTORICAL_ARCHIVE | завершённый protocol plan |
| `docs/archive/architecture/UPDATE_ORCHESTRATOR_AUDIT.md` | HISTORICAL_ARCHIVE | forensic snapshot до extraction |

`.agents/runtime/CURRENT_CONTEXT.md` — генерируемый локальный snapshot, а не
project-authored canonical document.

## Пользовательские сценарии

Старый каталог содержал 34 сценария; новый — 40. Удалено 0. Добавлены:

| ID | Поведение | Автоматическое доказательство | Live gap |
|---|---|---|---|
| REC-07 | удаление комментариев без изменения товаров | acceptance stabilization | общий voice smoke |
| REC-08 | точечная правка комментария | acceptance stabilization | общий voice smoke |
| REC-09 | отдельный запрос на отсутствующий товар | product-add lifecycle и uncertain gates | тестовая Google Sheets |
| REC-10 | расширение поиска на всех поставщиков без дубля | supplier lock + orchestrator progress | большой каталог отдельно |
| SUB-06 | at-most-once итоговое уведомление | submission notification lifecycle | real Telegram fault injection |
| SAF-05 | stale-owner fencing перед effect | lease и enqueue fencing | real Redis/Celery/PostgreSQL load |

Все 40 карточек содержат priority, availability status, channel, precondition,
action, bot response, state/result, confirmation, recovery, automation status и
manual-live requirement. Все 40 имеют существующие pytest references; generator
проверяет путь и имя test function. Dangling mappings: 0.

Статусы после ревалидации: `AUTOMATED_PASS` для локально доказанных контрактов и
`AUTOMATED_PARTIAL` для регистрации через живой реестр, real voice/photo,
Google-backed submission/status/review, provider recovery, product-add effects и
real concurrency. `MANUAL_LIVE_REQUIRED` остаётся отдельным требованием в карточке;
`NOT_IMPLEMENTED` поддержан схемой, но текущих карточек с таким статусом нет.

## Архитектура и качество кода

### Сильные стороны

- Core package boundaries защищены architecture tests; направление зависимостей
  соответствует `domain/core → application ports → runtime coordinators`.
- State — контекст уже понятого сообщения; global parse и единая compatibility policy
  предшествуют contextual fallback.
- Domain-модели различают source query, catalog identity, quantity, packaging,
  comments, candidate evidence и lifecycle state.
- Durable orchestration не спрятан в HTTP request: webhook остаётся коротким, worker
  владеет claim/lease/checkpoint protocol.
- Защищённый долг описан с trigger, а не маскируется ложным «финалом».

### Реальные недостатки

- `ConversationApplication` всё ещё адаптирует строки legacy `BotReply`, поэтому
  channel-neutral output не полностью самостоятельный.
- `ConversationEngine.handle()` остаётся длинным авторитетным dispatcher и смешивает
  state ordering с частью presentation decisions.
- В adapters встречается `Any` и provider-specific dictionaries; strict mypy не
  устраняет semantic untyped boundaries.
- Несколько docstrings вроде «Инициализирует компонент» не добавляют контракт; это
  readability debt, но не production defect.

### Файлы больше 1000 строк

| Файл | Verdict сегодня | Конфликт ответственности | Safe seam |
|---|---|---|---|
| `integrations/openai_prompts.py`, 2378 | YES | text/photo/match/visible-action/comment-scope prompts меняются независимо | модули по request type с точными prompt contract tests |
| `services/submission.py`, 2053 | YES | order submission, status/history, product-add coordination | сначала status/history, затем product-add; основной protocol оставить целым |
| `services/orchestrator.py`, 1933 | YES | durable update protocol, AI candidate decision, analytics, progress UX | catalog-AI decision и analytics serialization |
| `services/engine.py`, 1517 | YES | общий state order и реализации многих flow | state-specific decision/result builders по одному flow |
| `tests/ai/test_ai_media.py`, около 1479 | УМЕРЕННО | много разных media contracts в одном test module | делить по контракту только при следующем изменении tests |

Они не являются pilot blocker только из-за LOC. Декомпозиция без feature trigger
сейчас повышает regression risk.

## Legacy bridge и новые каналы

| Вопрос | Ответ |
|---|---|
| Блокирует текущий Telegram production? | Нет. Он покрыт regression suite и сохраняет точный Telegram UX. |
| Блокирует MAX? | Не блокирует начало adapter, но блокирует честный полностью нейтральный output contract. |
| Блокирует Web? | Аналогично: domain decisions пригодны, renderer/delivery и output mapping нужны отдельно. |
| Удалять сейчас безопаснее? | Нет. Retain сейчас ниже риска, чем переписать доказанный state/output pipeline без второго consumer. |
| Точный trigger | Утверждённый вертикальный сценарий второго канала, которому недостаточно `reply.rows` и Telegram actions. |

## Catalog и масштаб

`CatalogSearch` — правильная insertion point. Но текущая production composition
получает полный каталог из Google Sheets/Redis, материализует tuple/list и применяет
O(N) retrieval/ranking/token-frequency. Возможны несколько одновременных копий и
рост latency/memory. Test на 100k проверяет interface, не p95/recall/RSS.

Перед реальным 100k deployment обязательны indexed venue/supplier-scoped backend,
runtime injection, ограниченная projection, benchmark representative queries и
план cache refresh/read-back. Core matcher/safety gate переписывать не нужно.

## Concurrency и idempotency

### Подтверждённая защита

- `update_id` — durable primary-key deduplication.
- Same-chat work сериализован renewable lease; different chats независимы.
- Claim повторно проверяется под DB lock; более ранний unfinished update имеет
  приоритет.
- Stale owner проверяется до важных state/task boundaries.
- Submission, product-add, recalculation, dispatch и completion notification имеют
  отдельные durable lifecycle gates.

### Точные остаточные interleavings

1. Worker отправил Telegram reply, но умер до `reply_sent` checkpoint. Retry видит
   сохранённый result и может отправить тот же reply повторно. State/effect не
   дублируется, но UX at-most-once не доказан.
2. Worker опубликовал Celery task, но умер до `tasks_enqueued`. Retry может опубликовать
   task повторно. Submission/product-add подавят опасный эффект durable gate, однако
   status/reply task может повторно ответить.
3. Lease потерян сразу после последнего `ensure_owned`, пока Telegram HTTP request уже
   в сети. Новый owner может продолжить, а старый ответ всё же дойти. Это частный случай
   первого окна.
4. Внешний dispatch завершился у provider, но ответ потерян. Durable stage становится
   `uncertain`; auto-retry подавлен. Дубль предотвращён ценой возможного lost success,
   который требует ручной сверки.

Первое и второе окно требуют transactional outbox/delivery identity, если pilot
покажет реальные дубли или перед широким production. Они не доказывают риск двойной
заявки поставщику в текущем protocol.

## Submission protocol

Проверены fresh state/access reread, fingerprint/revision, per-sheet lock, catalog
mutation plan/read-back, recalculation checkpoint, dispatch lifecycle, finalization и
notification gate.

Evidence поддерживает отсутствие автоматического duplicate external dispatch на
проверенных путях. Возможные исходы:

- **duplicate effect:** не найден подтверждённый путь через guarded submission;
- **false success:** не показывается после exception/lease loss; используется
  `uncertain`;
- **lost success:** возможен при успешном provider effect и потерянном response;
  осознанно требует ручной reconciliation;
- **unsafe retry:** начатые recalc/dispatch/notification автоматически не повторяются.

Это сильный safety-first protocol. Его монолитность — цена protocol locality, а не
основание для срочного переписывания.

## AI safety boundary

- OpenAI возвращает structured schemas, но явная source evidence имеет приоритет для
  order quantity, packaging role и product identity.
- Числа title/фасовки/диапазона не должны становиться количеством заказа.
- Comment bindings проверяются по source anchors; product facts не обязаны становиться
  комментариями.
- Candidate AI видит deterministic shortlist и может выбрать только его ID; выбор ещё
  проходит confidence, contradiction, numeric, qualifier и packaging gates.
- NOT_FOUND и похожий товар не обходят auto-select safety.
- Global intent проходит StateCompatibilityPolicy до candidate/comment/quantity
  fallback; visible actions не подменяют сильную команду.

Путь прямого обхода deterministic authorization AI payload не найден. Риск остаётся в
сложности prompt/reconciliation и качестве live model, а не в выдаче AI права на
внешний эффект.

## Security и privacy

### Подтверждено

- Webhook secret сравнивается constant-time; production config требует HTTPS и
  достаточную длину secret.
- `.env` не tracked; repository scan не нашёл строк, похожих на Telegram/OpenAI keys.
- Идентификаторы и user content хэшируются/скрываются по умолчанию; token patterns
  очищаются, а `httpx` INFO подавлен из-за token в Telegram URL.
- Доступ проверяется по текущему venue и повторно до order/status/product-add effects.
- SQL строится SQLAlchemy/repositories, а не из пользовательских строк.
- Callback revision блокирует старую кнопку. Подпись callback отсутствует, но webhook
  auth, chat ownership, state и revision ограничивают воздействие текущим чатом.
- External URLs берутся из config, не из пользовательского ввода; явный SSRF path не
  найден.

### Gap

`SECURITY.md` всё ещё содержит незакрытые пункты об отзыве token из старого n8n export
и выпуске нового. Репозиторий не может доказать внешнюю ротацию. Это обязательное
операционное подтверждение до pilot. Также нужны реальные secret storage, rate limits,
backup и alerts по checklist. Exception/URL fields вне известного набора content keys
могут передать лишнюю provider detail в лог; это следует сузить перед широким
production.

## Observability

Structured events содержат анонимный chat/venue context, update ID, intent, stage,
timings, catalog decision, submission lifecycle, uncertainty и retry suppression.
Оператор по логам обычно может определить запрос, этап, dependency и outcome.
Langfuse опционален и получает метаданные/счётчики, а не обязательный raw content.

Не доказаны production dashboards/alerts, end-to-end external operation correlation и
retention. Lifecycle Langfuse client/flush явно не оформлен как shutdown contract.

## Внешние интеграции

| Adapter | Сильная сторона | Gap |
|---|---|---|
| Telegram | явные HTTP timeout, retry 5xx/connect, callback revision, reply checkpoints | неопределённая доставка reply может дублироваться; rate-limit/live 429 не проверен |
| OpenAI | structured output, timeout/retry, trace, deterministic post-validation | live model/ASR/vision не подтверждены; prompts собраны в одном огромном модуле |
| Google Sheets | mapping, access, read-back, Apps Script response validation | `_get_values` retry ловит любой `Exception`; API transport timeout не задан явно |
| Redis/Celery | renewable lease, token ownership, fencing, retry config | network partition и process-kill не проверены live; общего outbox нет |
| PostgreSQL | short session scopes, `FOR UPDATE`, indexes и migrations | redrive читает коллекции целиком; production-like load/restore drill не доказаны |
| Apps Script | prepared request, explicit timeout, uncertainty owned submission | реальный recalc/dispatch failure timing не проверен |
| Langfuse | optional tracing без обязательного raw content | нет доказанного production alert/flush lifecycle |

Broad retry Google read следует заменить классификацией transient 429/5xx/transport и
fail-fast для 4xx/schema/config, плюс зафиксировать transport deadline.

## Database

Модели и migrations задают PK `update_id`, session state JSONB, submission lifecycle,
order-event idempotency, venue binding uniqueness и необходимые одиночные индексы.
Repositories держат транзакции короткими и применяют `FOR UPDATE` в критических
местах. Явный N+1 в safety-critical path не найден.

Future gaps: redrive загружает recoverable/unfinished rows в память; при большом backlog
нужны keyset/batch processing и измерения. Composite `(chat_id, status, update_id)`
следует добавлять только после query-plan evidence. Retention task есть, но production
retention/backup ещё нужно подтвердить операционно.

## Test quality

1391 — не самостоятельная оценка, однако состав suite сильный:

- behavior и cross-channel transcript parity;
- негативные routing/modal cases;
- source provenance и catalog safety;
- failure injection submission/concurrency;
- architecture/cycle/import guards;
- stale callback, access revocation и venue isolation;
- generated scenario reference validation.

Fakes качественно моделируют contract outcomes и checkpoint failures, но не заменяют
provider timing. Meaningful gaps: real Telegram/ASR/vision/Sheets/Redis fault runs,
property/stateful fuzzing modal transitions, workload benchmark 100k и outbox crash
reproduction. Большой `test_ai_media.py` снижает навигацию, хотя тематическое покрытие
остаётся полезным.

## Operational readiness

Repository содержит liveness/readiness, Alembic migration service, Celery cleanup,
config validation, Docker/startup, backup/restore, rollback и alert runbook. Этот аудит
не менял DevOps и не запускал hosted CI или production compose.

До pilot нужно выполнить preflight из `TESTING_READINESS.md`; до broad production —
подтвердить ежедневный backup и restore drill, rate limits, alerts, token rotation,
telemetry и rollback rehearsal.

## Score table

| Dimension | Score | Evidence / причина ниже 9 |
|---|---:|---|
| Architecture | 8.6 | сильные owners и safety seams; legacy output и крупные coordinators |
| Package/module cohesion | 8.0 | core когезивен, четыре production-модуля смешивают независимые обязанности |
| Dependency direction | 8.8 | core guards сильны; engine/presentation compatibility seam остаётся |
| Domain modeling | 8.7 | богатая state/provenance model; часть provider payload остаётся словарями |
| Application/use-case design | 7.8 | use case нейтрален на входе, но legacy `reply.rows` на выходе |
| Channel isolation | 7.7 | contracts готовы, реализован только Telegram и legacy bridge |
| Persistence design | 8.6 | durable records/indexes/JSONB; redrive materializes backlog |
| Transaction boundaries | 8.8 | claim/state/finalization границы сильны; network/checkpoint gap неизбежен без outbox |
| Concurrency correctness | 8.7 | lease/fencing/sequencing доказаны; live partitions не проверены |
| Idempotency | 8.5 | business effects gated; reply/task publish windows остаются |
| Failure recovery | 8.6 | explicit uncertain и safe retry; operator reconciliation/live drill не доказаны |
| External-effect safety | 9.1 | submission/read-back/fencing unusually strong |
| Catalog/search design | 8.0 | evidence/safety хороши; production всё ещё list-backed O(N) |
| 100k+ scalability architecture | 6.8 | порт есть, production indexed backend и benchmark отсутствуют |
| Performance awareness | 7.5 | bounded boundary и timings есть; нет budgets/load profiles |
| OpenAI/AI boundary design | 8.8 | structured schemas и reconciliation; prompt/recovery complexity высока |
| Prompt safety / deterministic ownership | 9.2 | AI не авторизует опасные действия |
| Telegram integration | 8.4 | auth/timeouts/callback/checkpoints; duplicate reply window и live 429 gap |
| Google Sheets integration | 7.7 | read-back и mappings; broad retry и неявный API timeout |
| Third-party extensibility | 7.8 | adapters/ports есть, некоторые provider dictionaries протекают внутрь |
| Error handling | 8.1 | хорошая uncertainty taxonomy; broad `Exception` в Google read |
| Logging/observability | 8.5 | структурированные lifecycle/timings; dashboards/alerts не доказаны |
| Privacy/security | 8.1 | redaction/access сильны; token rotation и exception-field gap |
| Secrets handling | 8.4 | env untracked/scan clean; external rotation и secret manager не доказаны |
| Configuration | 8.8 | строгая production validation; реальная config matrix не запускалась |
| Typing | 8.9 | strict mypy green; adapter `Any` escape hatches остаются |
| Code readability | 8.0 | понятные owners; длинные orchestrators и часть слабых docstrings |
| Function/class cohesion | 7.7 | `handle/process/submission` несут несколько причин изменения |
| Documentation | 9.0 | после refresh current/history разделены; live facts зависят от operator proof |
| Developer onboarding | 8.8 | README/map/guide/tests ясны; protected seams всё ещё требуют глубокого чтения |
| Testing strategy | 9.1 | сильные behavior/layer/failure suites |
| Regression protection | 9.4 | широкое negative/provenance/state/concurrency покрытие |
| Architecture tests | 8.8 | boundaries/cycles/contracts; не доказывают runtime performance |
| Test maintainability | 8.2 | консолидация выполнена; один крупный media module и intentional overlap |
| Deployment/operations readiness | 7.5 | runbook полный; live backup/alerts/restore/hosted CI не доказаны |
| Maintainability | 8.1 | owners улучшены; четыре крупных coordination/prompt hotspots |
| Change safety | 9.1 | AGENTS process, strict gates, regression и architecture checks |
| Production readiness | 7.8 | code safety сильна; обязательные live/security/ops proofs не закрыты |

## Findings

### BLOCKER

Не найдено подтверждённых repository blockers: путь к cross-venue доступу, двойному
supplier dispatch, silent incorrect order или unrecoverable state не доказан.

### MUST_FIX_BEFORE_PILOT

1. **Подтвердить ротацию Telegram token.** Evidence: незакрытые пункты
   `SECURITY.md`. Риск: доступ к test/production bot при ранее опубликованном token.
   Сложность: XS, внешняя операция. Code change не нужен.
2. **Выполнить изолированный live preflight.** Evidence: все текущие provider tests
   fake/stub; hosted runtime в аудите не запускался. Проверить test bot, access,
   ASR/photo, Sheets write/read-back/recalc, redaction и disabled supplier dispatch.
   Сложность: M. Это proof, а не исправление architecture.

### SHOULD_FIX_SOON

1. Transactional outbox/delivery identity для reply/task checkpoint windows перед
   broad production (`services/orchestrator.py`, `repositories/updates.py`, workers), M/L.
2. Классификация transient Google errors и явный API transport deadline
   (`integrations/google_sheets.py`), S/M.
3. Production alerts и dashboard для `uncertain`, queue age, retries, backup age и
   provider latency, M.
4. Ограничить/санитизировать произвольные exception/URL fields логов, S.

### IMPROVEMENT_WHEN_TRIGGERED

- Indexed `CatalogSearch` backend и 100k benchmark — до реального роста каталога.
- Удаление legacy reply bridge — при первом втором канале.
- Разделение prompt/submission/orchestrator/engine — при изменении соответствующего
  use case, не отдельной кампанией.
- Batch/keyset redrive и DB composite indexes — при измеренном backlog/query cost.
- Property/stateful model testing — при росте числа modal states.

### INTENTIONAL_TECHNICAL_DEBT

- Legacy `EngineResult`/`BotReply` bridge.
- Protocol-local submission coordinator.
- List-backed catalog adapter для текущего малого каталога.
- At-most-once notification, допускающее lost notification вместо дубля.

### NON_ISSUE

- LOC сам по себе не defect.
- Наличие AI не нарушает безопасность: deterministic code остаётся authorizer.
- Неподписанный callback не является cross-user authorization bypass при текущих
  webhook/chat/revision checks.
- Отсутствие MAX/Web не дефект текущего Telegram продукта.
- 100k characterization не провал производительности; это честно ограниченное
  доказательство интерфейса.

## Top 10 улучшений по ценности

1. Подтвердить token rotation и изолировать pilot bot.
2. Выполнить live pilot preflight без supplier dispatch.
3. Настроить alerts и операторский runbook для `uncertain`.
4. Сузить Google retry taxonomy и задать deadline.
5. Спроектировать outbox для reply/task delivery до широкого production.
6. Провести backup/restore и rollback drill.
7. Добавить real Redis/process-kill fault run.
8. Подключить indexed catalog provider перед 100k.
9. Провести benchmark latency/memory/recall реального каталога.
10. Платить legacy bridge/large-file debt только по feature trigger.

## Gap to 10/10

В столбце «Риск» указан риск самого изменения, а не риск бездействия.

| Dimension | Сейчас → цель | Gap | Evidence | Действие | Риск | Польза | Trigger | Приоритет | Effort |
|---|---:|---:|---|---|---|---|---|---|---|
| Architecture | 8.6 → 10 | 1.4 | legacy output, крупные coordinators | убрать seams по feature trigger | высокий regression | яснее evolution | второй канал/изменение flow | triggered | L |
| Package cohesion | 8.0 → 10 | 2.0 | четыре mixed-responsibility модуля | поэтапные extractions | средний/высокий | дешевле изменения | реальная change pressure | triggered | XL |
| Dependency direction | 8.8 → 10 | 1.2 | engine знает Telegram presentation | нейтральный output port | высокий | чистая channel boundary | второй канал | triggered | L |
| Domain modeling | 8.7 → 10 | 1.3 | provider payload dictionaries | typed boundary DTO | средний | меньше implicit contracts | изменение adapter | soon | M |
| Application design | 7.8 → 10 | 2.2 | mapper legacy rows | first-class result model | высокий | независимые channels | второй канал | triggered | L |
| Channel isolation | 7.7 → 10 | 2.3 | только Telegram adapter | vertical second-channel proof | средний | доказанная расширяемость | утверждённый канал | triggered | L |
| Persistence | 8.6 → 10 | 1.4 | full redrive lists | batch/keyset queries | низкий | bounded memory | backlog растёт | triggered | M |
| Transactions | 8.8 → 10 | 1.2 | publish/checkpoint gap | outbox/inbox dispatcher | высокий | atomic delivery intent | broad production | soon | L |
| Concurrency | 8.7 → 10 | 1.3 | нет live partition proof | fault-injection environment | средний | confidence under failure | before scale | soon | M |
| Idempotency | 8.5 → 10 | 1.5 | reply/task duplicates possible | delivery IDs + outbox | высокий | end-to-end replay safety | observed duplicate/broad prod | soon | L |
| Failure recovery | 8.6 → 10 | 1.4 | manual uncertain reconciliation | operator workflow/alerts | низкий | shorter recovery | before broad prod | soon | M |
| External effects | 9.1 → 10 | 0.9 | real providers not fault-tested | controlled live faults | средний | operational proof | before enable dispatch | soon | M |
| Catalog design | 8.0 → 10 | 2.0 | list adapter in runtime | indexed scoped provider | средний | bounded latency/memory | large catalog | triggered | L |
| 100k scalability | 6.8 → 10 | 3.2 | no production backend/benchmark | provider + load/recall suite | средний | real scale readiness | before 100k | triggered | XL |
| Performance | 7.5 → 10 | 2.5 | no SLO/budgets/profile | p95/RSS/query benchmarks | низкий | evidence-based capacity | before scale | soon | M |
| AI boundary | 8.8 → 10 | 1.2 | reconciliation complexity | contract tracing/fuzz corpus | средний | safer model upgrades | model/prompt change | triggered | M |
| Deterministic ownership | 9.2 → 10 | 0.8 | live model not proven | adversarial live eval | низкий | stronger evidence | before model upgrade | soon | M |
| Telegram | 8.4 → 10 | 1.6 | delivery gap/live 429 | delivery identity + smoke | средний | predictable UX | pilot/broad prod | soon | M |
| Google Sheets | 7.7 → 10 | 2.3 | broad retry/no explicit deadline | taxonomy, timeout, live quota tests | низкий/средний | bounded failures | before broad prod | soon | M |
| Third-party extensibility | 7.8 → 10 | 2.2 | provider shapes leak | typed ports/contract suites | средний | replaceable adapters | new provider | triggered | M |
| Error handling | 8.1 → 10 | 1.9 | broad adapter exception | error taxonomy | низкий | correct retry/recovery | next Sheets change | soon | S |
| Observability | 8.5 → 10 | 1.5 | no live dashboards/alerts | SLO dashboard + correlation | низкий | faster diagnosis | before broad prod | soon | M |
| Privacy/security | 8.1 → 10 | 1.9 | rotation unproven/log exception gap | rotate + tighten fields + review | низкий | lower breach/privacy risk | before pilot/prod | must | S |
| Secrets | 8.4 → 10 | 1.6 | secret manager/rotation unproven | documented verified rotation | низкий | operational assurance | before pilot | must | XS/S |
| Configuration | 8.8 → 10 | 1.2 | real matrix not run | staging config validation | низкий | fewer deploy failures | pilot deploy | must | S |
| Typing | 8.9 → 10 | 1.1 | adapter Any | typed DTO/protocol response | средний | safer changes | adapter edit | triggered | M |
| Readability | 8.0 → 10 | 2.0 | large handle/process, weak docstrings | targeted extraction/text cleanup | средний | faster review | touched area | triggered | L |
| Function/class cohesion | 7.7 → 10 | 2.3 | multiple reasons to change | characterization + one seam at a time | высокий | local reasoning | feature work | triggered | XL |
| Documentation | 9.0 → 10 | 1.0 | live state cannot be repo-proven | periodic verified refresh | низкий | trustworthy onboarding | each release | soon | S |
| Onboarding | 8.8 → 10 | 1.2 | protected protocol still complex | guided walkthrough/runbook | низкий | faster contribution | team growth | soon | S |
| Testing strategy | 9.1 → 10 | 0.9 | real provider gaps | staging contract/fault suite | средний | end-to-end confidence | before broad prod | soon | L |
| Regression protection | 9.4 → 10 | 0.6 | no formal stateful fuzz | model/property tests | средний | novel sequence discovery | modal growth | triggered | M |
| Architecture tests | 8.8 → 10 | 1.2 | structure ≠ runtime perf | composition/load assertions | низкий | honest scale proof | new backend | triggered | M |
| Test maintainability | 8.2 → 10 | 1.8 | large media module/overlap | split by contracts when touched | средний | faster diagnosis | test change | triggered | M |
| Deployment/ops | 7.5 → 10 | 2.5 | runbook, no executed proof | backup/restore/alerts/rollback drill | низкий/средний | recoverable operations | before broad prod | must/soon | M |
| Maintainability | 8.1 → 10 | 1.9 | coordination hotspots | trigger-based decomposition | высокий | lower change cost | measured pain | triggered | XL |
| Change safety | 9.1 → 10 | 0.9 | hosted CI not verified here | branch protection + CI evidence | низкий | independent enforcement | team/pilot | soon | S |
| Production readiness | 7.8 → 10 | 2.2 | live/security/ops proof missing | close pilot and ops gates | средний | responsible launch | before production | must | L |

## Что улучшается в репозитории, а что требует среды

### Можно улучшить в коде/репозитории

- Google retry taxonomy и deadline.
- Outbox/delivery identity design и crash tests.
- Typed provider DTO и log-field policy.
- Targeted module extraction по реальному feature trigger.
- Batch redrive, indexed catalog adapter и benchmark harness.
- Stateful/property modal tests.

### Требует реальной среды/эксплуатации

- token rotation, secret manager и credential permissions;
- live Telegram webhook/send/edit/429;
- real ASR и vision photographs;
- Google write/read-back/recalc/quota;
- Redis partition/process kill и PostgreSQL load;
- 100k catalog latency/memory/recall;
- backup/restore/rollback drills;
- dashboards, alerts, retention и operator response;
- supplier dispatch fault tests в отдельном безопасном окружении.

## Ответы для решения

**Продолжать ли общий архитектурный refactor сейчас? — НЕТ.** Owners и safety
границы достаточны для pilot; новый общий refactor даст больше regression risk, чем
пользы. Платить конкретный долг нужно при втором канале, росте каталога или изменении
соответствующего flow.

**Готов ли проект к контролируемому human Telegram pilot? — ДА, условно.** Сначала
обязательны подтверждение token rotation, отдельное test environment,
`GOOGLE_ORDER_SUBMISSION_ENABLED=false`, migrations/health и live preflight из
`TESTING_READINESS.md`.

**Что senior-разработчик раскритикует первым?** Несоответствие зрелого core тонкому
application output: `ConversationEngine`/`BotReply` по-прежнему удерживают Telegram UX,
а orchestrator/submission/engine велики и требуют глубокого чтения protocol order.

**Что необычно сильно?** Системная защита от неверной заявки: source provenance,
StateCompatibilityPolicy, conservative catalog gates, access reread, lease fencing,
durable uncertainty и тесты конкретных crash/interleaving случаев.

**Единственная самая рискованная production-область:** внешняя отправка/перерасчёт в
неопределённом сетевом исходе. Код правильно предпочитает `uncertain` вместо дубля, но
без live provider tests, alerts и ручной reconciliation оператор может не заметить
lost success. Для текущего pilot supplier dispatch должен оставаться выключенным.

## Финальные проверки

| Проверка | Финальный результат |
|---|---|
| Full pytest | `1391 passed in 18.78 s` |
| Mypy | `139 source files, no issues` |
| Ruff check | pass |
| Ruff format | `286 files already formatted` после исправления одного пробела в архивном code sample |
| Markdown links | `36 files, pass` |
| Scenario generator/check | `40 scenarios, pass` |
| Documentation update check | выполняется после commit, потому что checker сравнивает Git-объекты |
| Compileall | pass |
| `git diff --check` | pass |
| Production executable diff | 0 |

Финальный audit verdict после закрытия preflight:
`PROJECT_ENGINEERING_GOOD_WITH_PRE_PILOT_FIXES`.
