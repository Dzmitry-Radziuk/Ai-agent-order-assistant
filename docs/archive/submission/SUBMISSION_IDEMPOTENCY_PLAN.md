# Block H — идемпотентность побочных эффектов отправки

Статус: **анализ завершён, implementation pending**.

В Block H код приложения, тесты, prompts, схема базы данных и миграции не
изменялись. Ниже зафиксировано фактическое поведение на HEAD
`d2e00caa267d22da0f4a500b7d9a9fe6aa31fabf` (`decompose_bot`).

## 1. Scope and risk

Цель — не обещать невозможное «exactly once» между PostgreSQL и Google Sheets,
а исключить повторное применение изменения каталога после ответа Google Sheets,
который потерялся, или после падения процесса до локального checkpoint.

Главный подтверждённый риск: обновление количества в каталоге выполняется как
`current + increment`, а `catalog_updated` записывается только после успешного
возврата `batchUpdate`. При потере ответа локальный флаг остаётся `false`, и
повтор Celery может применить тот же increment ещё раз.

Текущий риск классифицирован как **P1 / production blocker для включения
автоматической отправки**, если каталог не имеет внешней идемпотентности.
В ручном режиме `GOOGLE_ORDER_SUBMISSION_ENABLED=false` центральная отправка
не вызывается, но запись каталога и перерасчёт всё равно являются внешними
побочными эффектами.

Текущие 14 падений полного pytest не входят в Block H и не исправляются.

## 2. Current submission side-effect graph

Фактическая последовательность `SubmissionService.submit()`:

```text
ConversationState.pending_submission (frozen rows/order_no)
  ↓
_record(): INSERT/lock SubmissionRecord + audit event
  ↓
Redis google_submission_lock_key(spreadsheet_id)
  ↓
if catalog_updated == false:
    GoogleSheetsGateway.increment_catalog_quantities()
    _checkpoint("catalog_updated")
    CatalogCache.invalidate()
  ↓
if recalc_done == false:
    GoogleSheetsGateway.trigger_recalculation()
    _checkpoint("recalc_done")
  ↓
if dispatch enabled:
    prepare_order_submission()  [read-only metadata]
    _mark_dispatch_started()    [local DB, before POST]
    send_order_submission()      [central Web App POST]
    _mark_dispatch_completed()   [local DB after successful response]
  ↓
_finalize(): clear pending/cart + finalized checkpoint
  ↓
Telegram success/local-success reply
  ↓
_checkpoint("completion_notified")
```

`PendingSubmission.rows`, `order_no`, `spreadsheet_id` и venue identity
создаются в `_prepare_submission()` до постановки Celery-задачи. При повторе
используется та же pending-заявка; редактировать draft между попытками нельзя.
Операционные поля `last_error`/`failed_stage` могут меняться для recovery, но
товарные строки и номер заявки должны оставаться неизменным snapshot.

`history_written` существует в `SubmissionRecord`, однако текущий submit-path
его не устанавливает: запись истории выполняется downstream-процессом таблицы/
перерасчёта и не является доказанным локальным checkpoint бота.

## 3. Confirmed catalog increment failure window

`GoogleSheetsGateway.increment_catalog_quantities()`:

1. читает каталог;
2. группирует `ID товара + department` и increment;
3. для каждой ячейки вычисляет `old = current quantity`, затем формирует
   абсолютное значение `old + increment`;
4. одним `values.batchUpdate(...).execute()` пишет значения количества и
   объединённые комментарии.

`SubmissionService.submit()` вызывает этот метод при `catalog_updated == false`,
а `_checkpoint(order_no, "catalog_updated")` — отдельной транзакцией после
возврата метода. Поэтому подтверждённо существует окно:

```text
batchUpdate применил old + increment
        ↓
ответ потерян / процесс завершился / DB checkpoint не закоммитился
        ↓
catalog_updated остаётся false
        ↓
retry снова читает уже увеличенное значение и пишет new_old + increment
```

Это не теоретическое различие `POST` и ответа: текущий код не передаёт
operation id в Sheets и не сохраняет before/after план, по которому можно было
бы отличить собственное предыдущее применение от внешнего изменения.

## 4. Existing checkpoint semantics

Существующие boolean-поля — это **completed checkpoints**, а не журнал начала
операции:

| Поле | Что означает сейчас | Чего не означает |
|---|---|---|
| `catalog_updated` | метод increment вернулся без исключения | что Sheets точно не применил write при timeout |
| `recalc_done` | recalc HTTP вызов вернулся без ошибки | что Apps Script не выполнился при потерянном ответе |
| `dispatch_started` | перед central POST сохранено начало вызова | что POST не был выполнен |
| `dispatch_completed` | получен валидный `orderNumber` | что checkpoint не потеряется после внешнего успеха |
| `finalized` | локальный cart/state закрыт | что Telegram уже доставил подтверждение |
| `completion_notified` | Telegram send вернулся и checkpoint записан | что пользователь не получил сообщение при ошибке checkpoint |

Для dispatch уже есть отдельный safety protocol: `dispatch_started=true` и
`dispatch_completed=false` переводят заявку в `dispatch_uncertain`; повторный
worker не делает второй POST и не показывает кнопку опасного повтора. Этот
контракт менять нельзя.

## 5. Side-effect classification

| Step | Owner | External? | Reversible? | Idempotent? | Checkpoint | Unknown window | Current retry | Risk |
|---|---|---:|---:|---:|---|---|---|---:|
| `SubmissionRecord`/audit event | `submission.py`, repositories | PostgreSQL | Да | Да, по `order_no`/`idempotency_key` | транзакция до Sheets | commit/rollback известен БД | Celery повторяет безопасно | Низкий |
| `increment_catalog_quantities` | `google_sheets.py` | Google Sheets | Нет без доказанной компенсации | **Нет**: `old + increment` | after effect | timeout/crash/partial batch до `catalog_updated` | повторяет blind | **Высокий** |
| `CatalogCache.invalidate` | Redis | Да, локальная infra | Да | Да (`DELETE`) | после catalog checkpoint | Redis failure после checkpoint | retry пропускает catalog и может пропустить invalidate | Средний: stale cache до TTL |
| `trigger_recalculation` | Apps Script | Да | Не доказано | Вероятно convergent, но не подтверждено кодом endpoint | after effect | потерянный HTTP response до `recalc_done` | повторяет POST | Средний, зависит от script |
| `prepare_order_submission` | Google Sheets API GET | Да, read-only | Да | Да | нет | обычный transport error | повторяет GET | Низкий |
| `send_order_submission` | central Web App | Да | Нет | неизвестно извне | started before / completed after | POST applied + response lost | **не повторяет**, uncertain | Высокий, но уже защищён |
| `_finalize` | PostgreSQL | Нет | Да | практически да для того же order | transaction | DB commit boundary | повторяет state update | Низкий |
| `telegram.send_reply` success | Telegram | Да | Нет | нет стабильного message idempotency key | after send | Telegram accepted + checkpoint failed | может отправить duplicate card | Средний |
| `completion_notified` | PostgreSQL | Нет | Да | Да | after Telegram | checkpoint failure | resend completion | Средний UX-дубль |

Redis `chat_lock` сериализует операции одного чата, а
`google_submission_lock_key(spreadsheet_id)` — bot workers для одной таблицы.
Оба lock являются lease в Redis. Они не блокируют ручное редактирование
Google Sheets, другие Apps Script/n8n процессы или writers вне этого Redis и не
являются distributed lock для самой таблицы.

## 6. Failure window matrix

| Ситуация | Текущее поведение | Безопасно? | Возможный ущерб | Желаемое поведение |
|---|---|---:|---|---|
| A. crash до external call | checkpoint false, retry вызывает операцию | Да для первой попытки | Нет side effect | Отмечать plan/start; retry допустим только при доказанном `before` |
| B. external call rejected до apply | checkpoint false, retry | Только если rejection доказан | Обычно нет | `definitely_not_applied` → retry |
| C. effect applied + response success | checkpoint записывается | Да | Нет | completed checkpoint |
| D. effect applied + timeout/lost response | checkpoint false, blind retry | **Нет** | duplicate increment | verify before/after; unknown не повторять blind |
| E. effect not applied + timeout | checkpoint false, blind retry | Не доказано | либо пропуск, либо duplicate при ошибочной verify | read-back: `before` → один retry |
| F. effect success + DB checkpoint failure | checkpoint false, retry | **Нет** для increment | duplicate increment | persisted plan + read-back recovery |
| G. checkpoint success + cache invalidate failure | catalog true, retry skips invalidate | Частично | stale cache до TTL | invalidate как отдельный idempotent repair step |
| H. process crash after checkpoint | completed true, retry skips effect | Да | возможна stale cache/recalc gap | отдельные completed checkpoints и repair |
| I. retry after uncertain stage | catalog/recalc retry; dispatch no POST | Нет для catalog без verify | duplicate/unknown | uncertain state, manual/verified recovery |

## 7. Recalculation, dispatch and completion assessment

### Recalculation

`trigger_recalculation()` отправляет `spreadsheetId`, `sheetName` и token в
Apps Script; endpoint в этом repository отсутствует. Комментарий в gateway
говорит, что скрипт пересчитывает весь лист `Заявка`, что делает повторный
пересчёт похожим на convergent operation. Но отсутствие исходника/контракта
не позволяет доказать, что скрипт не добавляет строки, события или другие
эффекты. Поэтому `recalc_done` сегодня не считается полноценной
идемпотентностью. В отдельной фазе нужно подтвердить endpoint или перевести
потерянный ответ в `recalc_uncertain` без blind retry.

### Центральная dispatch

Текущий `dispatch_started → POST → dispatch_completed` — правильный reference
protocol. При timeout или падении после `dispatch_started` второй POST не
делается; пользователь получает uncertain-reply без кнопки повтора. Этот
механизм оставить без изменений и не копировать его в каталог без read-back
проверки, потому что dispatch имеет внешний order number, а catalog update —
несколько абсолютных ячеек.

### Completion notification

`Telegram.send_reply()` выполняется до `completion_notified`. Если Telegram
принял сообщение, а локальный checkpoint не записался, следующий worker может
отправить ту же карточку ещё раз. Это **MEDIUM** риск UX, не риск двойной
заявки/количества. Его следует закрывать отдельной Telegram delivery policy,
не смешивая с первым catalog correction.

## 8. Existing tests and false assumptions

Focused submission suite на текущем HEAD: **58 passed** (`test_submission.py`
и `test_google_sheets_mapping.py`). Полный свежий run: **1254 collected / 1240
passed / 14 failed / 0 skipped / 0 xfailed / 0 errors**. Эти 14 failures
остаются без изменений и относятся к baseline Block G.

`tests/submission/test_submission.py::test_transient_pre_dispatch_error_can_be_retried_safely`
сейчас green, но классифицируется как **DANGEROUS ASSUMPTION / INCOMPLETE**.
Он моделирует `TimeoutError` из `increment_catalog_quantities()` и проверяет,
что ошибка не попадает в final failure и может быть повторена. Однако слово
«pre-dispatch» означает только «до central Web App POST»; оно не доказывает,
что Google Sheets не применил `batchUpdate`. Следовательно, тест валиден для
маршрута до dispatch, но не должен утверждать, что любой catalog timeout
безопасно повторять.

Уже существующие product-add тесты используют более безопасный шаблон:
после retryable write error они читают лист и проверяют, появился ли точный
текст (`append_product_request`). Этот шаблон полезен как аналог read-back,
но его нельзя копировать без изменений: для catalog increment нужно сравнивать
числовые before/after cells и учитывать concurrent writers.

Недостающие тесты перечислены в разделе 13; production code и тесты в Block H
не изменялись.

## 9. Candidate designs

| Option | Safety guarantee | DB schema | Sheets change | Apps Script change | Lost response | Checkpoint failure | Concurrent writer risk | Complexity | Recommendation |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| A. started/uncertain gate | Не делает второй write после unknown | Нужны started/uncertain fields или metadata | Нет | Нет | Не подтверждает effect | Не устраняет duplicate без verify | Средний | Средняя | Нужен как safety gate, но недостаточен один |
| B. persisted mutation plan + verify | `before/expected-after` разрешает prove-applied / prove-not-applied / conflict | Небольшая migration для явного plan/status; временно возможно JSONB | Нет | Нет | Да, через read-back | Да, plan остаётся после crash | Конфликт явно останавливается | Средняя | **Да, основной дизайн** |
| C. Sheets idempotency marker/ledger | Effect once по operation id при атомарном ledger | Возможно отдельный DB status | Нужен ledger/колонки | Проверить атомарность двух writes | Только если marker и effect атомарны | Да при атомарности | Зависит от ledger lock | Высокая | Нет доказанной атомарности в текущем API |
| D. Apps Script idempotent mutation | Один server-side operation с LockService/ledger | Возможно минимальная DB state | Нет | **Требует нового endpoint** | Да, если endpoint реализован idempotently | Да | Защищает writers на стороне script | Высокая | Долгосрочное усиление, недоступно в текущем repo |
| E. absolute writes без plan | Повтор пишет ожидаемое значение | Нет | Нет | Нет | Только частично | Нет | Может затереть чужое изменение | Низкая | **Не рекомендовать** без ownership/compare-and-set |

## 10. Recommended design

Рекомендуется **Option B: persisted mutation plan + read-back verification**,
дополненный Option A как gate состояний.

Будущий протокол:

1. До первого Sheets write построить deterministic plan для каждой затронутой
   ячейки: product id, department, range, before, increment, expected after,
   order number и operation id. План сохранить транзакционно рядом с
   `SubmissionRecord`; `PendingSubmission.rows` остаётся неизменным.
2. Локально зафиксировать `started` до внешнего вызова.
3. Выполнить batchUpdate. При обычном ответе перечитать затронутые ячейки и
   записать `completed` только после проверки expected-after.
4. При timeout/connection reset/ошибке checkpoint не считать операцию
   failed-before-apply. Перейти в `uncertain` и перечитать cells:
   - все expected-after → effect подтверждён, отметить completed без write;
   - все before → один безопасный retry по тому же plan;
   - смешанные/другие значения → conflict/uncertain, не перезаписывать и не
     компенсировать автоматически.
5. Для комментариев в том же batch сохранить ожидаемое значение или выделить
   их в отдельный проверяемый mutation step; silent overwrite чужого изменения
   запрещён.
6. После verified catalog completion отдельно invalidation cache; эта операция
   идемпотентна и может быть repairable.

Почему это подходит фактической инфраструктуре: текущий gateway уже умеет
читать каталог и ячейки через Sheets API, но в repository нет Apps Script
endpoint для catalog mutation и нет доказанного технического ledger. Option B
не требует менять пользовательский лист или центральный Web App и честно
останавливается на conflict.

### Fallback, если schema migration временно недоступна

Сохранить plan/status в отдельном JSONB execution metadata поле
`SubmissionRecord` (не менять frozen `payload.rows`) и использовать тот же
read-back protocol. Это допустимый промежуточный rollout, но не конечный
контракт: typed columns/индексы и явный status предпочтительнее для recovery,
операторских запросов и мониторинга.

Не выбирать fallback «checkpoint before write»: он создаёт обратную дыру,
в которой `done=true`, а effect не выполнялся.

## 11. Data/schema and migration implications

Сейчас `SubmissionRecord` содержит только completed booleans и `last_error`.
Для production-варианта нужны минимум:

- `catalog_update_started` или единый enum status;
- `catalog_update_uncertain`/`catalog_update_conflict` либо явный status;
- persisted immutable mutation plan (JSONB) и operation id;
- timestamps/last verification details для recovery.

Текущий `payload` JSONB хранит snapshot заявки и может временно вместить
execution metadata, но смешение mutable status с frozen business payload
ухудшит контракт. Поэтому для постоянного решения **DB migration required**;
сейчас миграция не создаётся.

Миграционный footprint: новая Alembic revision после `0005_order_dispatch.py`,
upgrade с безопасными defaults для старых записей и downgrade без переписывания
существующих миграций. Перед rollout нужны `alembic heads`, `alembic check` и
ручная проверка upgrade на копии БД.

Google Sheets schema change: **не требуется** для Option B.

Apps Script change: **не требуется** для первого варианта, но Option D остаётся
долгосрочным усилением. В repository нет исходника Apps Script, поэтому его
идемпотентность нельзя считать доступной.

## 12. Recovery and UX semantics

При unknown/conflict бот не должен сообщать «ничего не произошло» и не должен
предлагать blind retry. Безопасный recovery должен показать, что обновление
таблицы не удалось подтвердить, сохранить тот же order/plan и направить на
проверку оператору или на отдельную verified recovery action.

`dispatch_uncertain` уже следует этому принципу и служит UX-reference. Для
catalog uncertainty потребуется отдельное состояние/текст; его реализация не
входит в Block H plan.

Автоматическая компенсация (`-increment`) запрещена: без proof она может
исправить корректное первое применение и испортить каталог.

## 13. Regression test matrix for implementation

Будущая implementation должна добавить минимум:

1. success + checkpoint success → одна прибавка;
2. failure доказанно до apply → retry разрешён;
3. effect applied + response lost → retry не делает вторую прибавку;
4. effect applied + checkpoint failure → read-back восстанавливает completed;
5. started, но outcome не проверяем → uncertain, blind retry запрещён;
6. read-back expected-after → checkpoint recovered без write;
7. read-back before → ровно один retry;
8. conflicting value → no overwrite, uncertain/conflict;
9. dispatch uncertain protocol остаётся без второго POST;
10. retry использует тот же `PendingSubmission` snapshot/order_no;
11. comments in batch не теряются и не затирают concurrent update;
12. cache invalidate failure не повторяет catalog mutation;
13. Telegram accepted + notification checkpoint failure классифицируется как
    duplicate-UX recovery, а не повтор заявки.

Отдельно переименовать/уточнить смысл
`test_transient_pre_dispatch_error_can_be_retried_safely`: его следует
разделить на definitely-before-apply и unknown-outcome cases, а не считать любой
`TimeoutError` безопасным.

## 14. Implementation phases

### H1 — catalog mutation plan and state boundary

- Владелец: `SubmissionService` + `GoogleSheetsGateway` + submission model/
  repository.
- Создать deterministic plan и explicit started/uncertain/completed contract.
- Не менять engine, parser, matching, state compatibility или UX до отдельного
  согласования.
- Добавить migration только после подтверждения footprint.

### H2 — read-back recovery

- Владелец: Google Sheets adapter/recovery service.
- Реализовать compare `before / expected-after / conflict` для всех затронутых
  cells и запрет blind retry.
- Добавить crash-window regression tests и operator-safe recovery result.

### H3 — recalc/cache/notification hardening

- Подтвердить idempotence Apps Script перерасчёта или ввести его uncertain gate.
- Сделать cache repair отдельным completed step.
- Отдельно решить duplicate Telegram completion delivery.

### H4 — optional Apps Script ledger

- Только при наличии владельца endpoint: operation id, LockService и атомарный
  ledger + mutation.
- Сравнить с Option B на staging; не включать `GOOGLE_ORDER_SUBMISSION_ENABLED`
  автоматически.

## 15. Out of scope

- любые изменения `engine.py`, `orchestrator.py`, parser, prompts, matching и
  state compatibility;
- исправление текущих 14 pytest failures;
- изменение `PendingSubmission` пользовательского поведения;
- Apps Script/Web App implementation без исходника и отдельного разрешения;
- удаление или переписывание истории миграций;
- автоматическая компенсация или force retry;
- decomposition и MAX.

## 16. Success criteria

Block H implementation можно считать готовой только когда:

- duplicate catalog increment невозможен после lost response/checkpoint failure
  либо операция переводится в доказуемый uncertain/conflict без blind retry;
- before/after plan сохраняется для того же frozen order snapshot;
- concurrent writer не перезаписывается молча;
- dispatch safety (`started` без второго POST) не регрессирует;
- recalc и Telegram notification имеют явно классифицированные uncertainty
  semantics;
- crash-window matrix покрыта тестами;
- оператору доступен safe recovery без автоматической компенсации;
- schema/migration и внешние изменения явно подтверждены до rollout.
