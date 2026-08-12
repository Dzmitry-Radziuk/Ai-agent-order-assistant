# Rapid input / button storm / concurrency safety

Статус: analysis-only, без исправлений production-кода, тестов и миграций.

Проверенный checkout: `736b04c460ca9458f1f8aa7e9f90b9977be55c35`, ветка
`decompose_bot`, remote только GitHub `origin/decompose_bot`.

Свежий полный запуск на этом checkout: **1316 collected / 1302 passed / 14
failed / 0 skipped / 0 xfailed / 0 errors**. Это тот же baseline, который был
зафиксирован после H3c; новые тесты и изменения в этом аудите не добавлялись.
Focused-набор H3c остаётся **122 passed**. Полный запуск не исправлялся: 14
падений относятся к существующему baseline.

## 1. Фактический pipeline

```text
Telegram webhook
  -> SessionLocal.begin + UpdateRepository.enqueue_once(update_id)
  -> commit
  -> process_telegram_update.delay(update_id)
  -> UpdateOrchestrator._claim (SELECT FOR UPDATE по update_id)
  -> callback ACK (только callback)
  -> Redis chat_lock(chat_id)
  -> state load / parsing / OpenAI / engine
  -> checkpoint state
  -> Telegram reply
  -> checkpoint reply
  -> enqueue side effects
  -> checkpoint tasks
  -> finish update=done
```

`SessionRepository.get_for_update()` защищает строку только внутри короткой
DB-транзакции. После завершения этой транзакции парсинг, OpenAI и engine
выполняются вне DB row lock; сериализация всей операции в текущем коде
осуществляется Redis `chat_lock`.

`UpdateRepository.enqueue_once()` даёт уникальность по `TelegramUpdate.update_id`.
`update_id` используется для deduplication и checkpoint-ов, но не как очередь
или sequence barrier для разных update одного чата.

## 2. Lock и Celery: доказанный контракт

В `src/restaurant_bot/integrations/cache.py`:

```python
redis.lock(
    f"restaurant-bot:chat-lock:{chat_id}",
    timeout=120,             # default
    blocking_timeout=15,
)
```

`SubmissionService.submit()`, `submit_product_add()` и
`OrderReviewService.submit()` передают `timeout=300`, но `blocking_timeout=15`
остаётся тем же. В `chat_lock` нет renewal/heartbeat. `lock.owned()` и Redis
token ownership защищают `release()` от освобождения чужого lock, но не
останавливают старого worker, который продолжил выполнять код после истечения
его TTL.

### A–H: ответы на критические вопросы

| Вопрос | Фактический результат |
|---|---|
| Первый запрос дольше 15 s, второй update | Второй worker ждёт до 15 s, затем `lock.acquire()` возвращает false и `chat_lock` поднимает встроенный `TimeoutError`. |
| Retry/requeue после lock contention | `process_telegram_update` autoretry-ит только `TELEGRAM_TRANSIENT_ERRORS`; `TimeoutError` туда не входит. Автоматического retry нет. |
| Потеря валидного сообщения | Webhook уже ответил 200 после постановки update. При contention задача падает до тела `try`, update остаётся `processing`; новый Celery task не создаётся. Сообщение может молча остаться необработанным до ручного/стороннего re-drive. |
| Callback ACK | `_answer_callback_best_effort()` вызывается до входа в `chat_lock`; ACK уже уйдёт в Telegram, хотя business action может затем не выполниться. |
| Истечение default TTL | TTL 120 s. Vision timeout в config допускает 180 s, поэтому photo request может пережить lock. |
| Истечение submission TTL | Submission lock получает TTL 300 s; длительный Sheets/App Script путь может пережить и его. |
| Renewal | В checkout renewal/heartbeat для chat lock не найден. |
| Новый owner после expiry | Да. Worker B может получить тот же Redis key, пока A ещё исполняется. `owned()` предотвращает ошибочный release, но не конкурентные state/Telegram side effects. |

## 3. Mutual exclusion не означает ordering

При действующем TTL lock два update одного чата не выполняют critical section
одновременно, но Redis lock не содержит Telegram sequence. Два Celery task могут
дойти до lock в порядке scheduler/worker, а не `update_id`. Поэтому сообщение
`101: «Добавь молоко»` может быть применено после `102: «Добавь сыр»`.

DB `SELECT FOR UPDATE` не исправляет это: оно захватывается только на load и на
отдельных checkpoint-транзакциях. В `TelegramUpdate` нет поля predecessor,
sequence или per-chat queue cursor. Это доказанный пробел ordering, а не только
теоретическая перестановка в тестовом mock.

## 4. Callback inventory

| Семейство | Защита в основном orchestrator path | Прямые/background пути | Оценка |
|---|---|---|---|
| cart/draft, add/back/clear/submit | `_attach_ui_revision()` добавляет `:rN`; engine проверяет revision | raw callbacks из `replies.py` безопасны только если прошли orchestrator; `SubmissionService` использует revision для draft | REVISION PROTECTED в основном пути |
| quantity, candidate, duplicate, unit mismatch, manual, not-found | Формируются engine/replies и получают `:rN`; stale callback даёт no-op/recovery | legacy/unversioned callback может пройти guard | REVISION PROTECTED в основном пути; legacy gap |
| review submit/cancel | Основной путь получает revision; review token дополнительно проверяется | `OrderReviewService.submit()` и некоторые recovery/refresh replies создают token callbacks напрямую без revision | TOKEN PROTECTED для submit/cancel; refresh по venue code — OTHER PROTECTED |
| new-order confirmation | Основной reply получает revision | raw `v2:new`/`v2:clear` из registration/replies в основном пути | REVISION PROTECTED после attach |
| order history/status | Основной путь получает revision | `SubmissionService.send_status()` использует `presentation.telegram.submission` с raw `v2:order`, `v2:orderspage`, `v2:orderitems`; worker не инкрементирует revision | UNPROTECTED для stale status card; действие не является irreversible mutation |
| submission retry/product-add retry | Product-add worker явно добавляет revision; submit replies используют `_callback_with_revision` | uncertain completion может не иметь кнопки повторной отправки | REVISION PROTECTED / TOKEN-GATED |
| registration | Код venue bind/switch является target/registration context | callbacks не имеют ui revision | OTHER PROTECTED кодом/регистрационным состоянием, не modal revision |

Важное ограничение: `parse_callback()` оставляет `callback_revision=None`, если
callback не заканчивается `:rN`; engine stale guard проверяет только непустую
revision. Поэтому unversioned callbacks не становятся stale автоматически.

## 5. Сценарии rapid input и button storm

| Scenario | Current protection | Actual result | Data safe? | User action preserved? | Ordering preserved? | Duplicate side effect possible? | Severity | Evidence | Recommended action |
|---|---|---|---|---|---|---|---|---|---|
| Exact same `update_id` доставлен повторно | PK + `enqueue_once` + `_claim` | Одна DB row и обычно одна `.delay`; duplicate webhook получает 200 без второй задачи | Да при нормальном enqueue | Да, если task был поставлен | Не применимо | Нет для одной row; crash window после commit остаётся | P2 | `updates.py`, `api/app.py`, `test_duplicate_webhook_is_acknowledged_without_a_second_task` | Transactional outbox/re-drive для commit→delay window |
| Crash после DB commit до `.delay` | Dedupe row уже committed | Row остаётся queued, task отсутствует; повтор webhook не создаёт task из-за PK | Данные не дублируются | Нет гарантии доставки | Нет | Нет | P1 | webhook вызывает `.delay` после `SessionLocal.begin()` | Durable enqueue/outbox или sweeper queued rows |
| Первый запрос >15 s, второй text | Per-chat lock, 15 s wait | Второй task получает `TimeoutError` до orchestration try; `process_telegram_update` не retry-ит | Первый может быть safe; второй не применён | **Нет** | Нет | Нет | **P1** | `cache.py`, `tasks.py`, `_claim/process` | Обработать contention как durable retry/requeue, не терять update |
| Первый запрос >15 s, второй callback | ACK до lock + тот же timeout | Telegram spinner закрыт, business action может исчезнуть | Состояние второго не меняется | **Нет** | Нет | Нет | **P1** | `_answer_callback_best_effort()` перед `chat_lock` | То же; ACK не считать success |
| Два разных update одного chat | Redis lock без sequence | Кто первым получил lock, тот и выполняется; #102 может опередить #101 | При коротких операциях обычно цело | Да, но в неправильном порядке | **Нет** | Возможны неправильные переходы/side effects из-за порядка | **P1** | no sequence field/cursor; independent Celery tasks | Один per-chat sequencing owner |
| Двойной клик одной кнопки `r15` | Revision attached в main path | Если первый завершил checkpoint, второй видит stale `r15` и no-op. Если lock contention/expiry, второй может потеряться или overlap | Да при валидном lease; нет гарантии при expiry | Иногда нет | Scheduler order only | H3 gates защищают submit, но не все UX/replay | P1 (expiry), P2 (normal) | `ui_revision`, engine stale guard, H3 gates | Sequencing + lease fencing; оставить revision source of truth |
| Две разные кнопки старой карточки `r15` | Те же revision checks | При последовательной обработке вторая stale после первой; при обратном scheduling может исполниться первой; при unversioned — обе текущие | Обычно да при lease; overlap опасен | Может выполниться не выбранная последовательность | **Нет** | Submit gates safe, state transitions may be wrong | P1 | callbacks are independent update IDs | Sequencing owner, не random debounce |
| Старый card после `r16/r17` | `:rN` только в основном пути | Versioned callback даёт stale safe reply/no mutation; raw callback bypasses revision | Versioned да; raw status/legacy может менять current view | Versioned action отклонён намеренно | Да не требуется | Не для versioned, возможны current-state действия raw | P2 (legacy), P1 если destructive raw найден | `parse_callback`, `_attach_ui_revision` | Инвентаризировать и закрыть legacy destructive paths |
| OpenAI 20–40 s + новый текст | lock 15 s; no process-task retry for TimeoutError | B ждёт, затем падает и остаётся `processing`; не обрабатывается позже | A да; B не применён | **Нет** | Нет | Нет | **P1** | text timeout 30 s, vision 180 s; `chat_lock` | Durable per-chat queue/retry |
| OpenAI + callback | ACK before lock; old keyboard disabled only after A enters lock | ACK/keyboard UX не означают выполнение; B может исчезнуть через 15 s | При valid lease да | **Нет** при contention | Нет | Expiry can overlap | **P1** | orchestrator lines around ACK/processing/lock | Same as above |
| Voice/photo then text | Processing card + best-effort keyboard disable | Disable/send failure only logged; text is separate queued update and subject to same lock timeout/order | Usually A state safe; B may be lost | **Нет гарантии** | Нет | No direct duplicate, overlap after expiry | P1 | `_send_processing_best_effort`, `_disable_keyboard_best_effort` | Durable queue; keep processing card best-effort |
| Two voice/photo quickly | Same chat lock, vision max 180 s vs TTL 120 s | Second can timeout at 15 s; after TTL it can overlap first | **Нет гарантии** due expiry | No for timeout case | No | State/UI overwrite possible | **P0** | config vision timeout 180, lock TTL 120, no renewal | Lease renewal/fencing; sequencing separately |
| Submission running + Submit again | Submission `chat_lock(timeout=300)` but wait 15 s | Second normal update can fail after 15 s; submit task itself retries exceptions, user update does not | H1/H2/H3 gates protect irreversible submission | New command can be lost | No | Irreversible duplicate blocked by gates; duplicate UX possible | P1 (loss), P2 duplicate UX | `submission.py`, `tasks.py` | Do not drop user update; sequence and explicit contention handling |
| Submission + Back/New order/Add product/status | Same per-chat key | All normal updates contend; status background task retries on Exception, user update does not; ordering unspecified | Submission data guarded; view may be overwritten | **Not guaranteed** for user update | No | Status/reply duplication possible | P1 | `send_status` autoretry vs `process_telegram_update` | Queue normal updates behind submission or durable retry |
| Task duplicated after `tasks_enqueued` checkpoint failure | checkpoint flags + downstream gates | Background task may be enqueued twice | H1/H2/H3a/b/c and dispatch gates prevent irreversible repeat; status/replies can repeat | User request remains | No strict order | Irreversible no; UX yes | P2 | `_enqueue_side_effects` then `_checkpoint_tasks`; H3 tests | Durable task outbox/idempotency per side effect |
| Worker crash while chat lock held | Redis TTL releases key; Celery late ack/reject requeues worker-lost task | Chat blocked up to TTL (120/300); requeued task can hit 15 s timeout; order can reverse after release | H3 side effects guarded; state overlap/order not fenced | May be delayed/lost | No | Possible only after duplicate task reaches unguarded path | P1 (lease), P2 otherwise | Celery config + `chat_lock` | Lease/fencing and retry policy |
| State checkpoint succeeds, Telegram reply fails | `state_applied` + result persisted before reply; Telegram transient retry path | Retry reloads result, skips parsing/engine, retries delivery | Yes | Usually yes | N/A | No business duplicate | P2 | `test_checkpointed_result_retries_only_telegram_delivery` | Keep checkpoint contract |
| Telegram accepted reply, reply checkpoint fails | `reply_sent` remains false | Retry may send the same card again | State safe | Reply duplicated, not lost | N/A | No irreversible business duplicate | P2 | `_checkpoint_reply` after `send_reply`; no message id dedupe | Optional Telegram message upsert/checkpoint |
| Different chats concurrently | Lock key includes chat_id | Critical sections independent | Yes | Yes | Per chat only | No global duplicate from lock | P3/OK | key `restaurant-bot:chat-lock:{chat_id}` | Preserve per-chat, never global lock |
| DB row `get_for_update` during slow work | Short transaction only | Row lock ends before AI/engine; does not serialize request | No guarantee by itself | No guarantee | No | No direct duplicate by row lock | P1 as false safety assumption | `SessionRepository`, orchestrator transactions | Treat Redis/sequencer as owner; do not rely on DB row lock |
| UI revision N→N+1 window | Increment/attach before state checkpoint, reply after checkpoint | State may be N+1 while old card visible; old versioned callback becomes stale; unversioned callback remains executable | Versioned safe | Stale action intentionally rejected | No for independent updates | No under valid lease | P2, P1 with expiry | `_attach_ui_revision`, checkpoint order | Keep revision; fence old owners |

## 6. Findings by severity

### P0

**P0-1 — expiring chat lock does not fence the owner.** Default TTL 120 s is
shorter than the allowed 180 s vision timeout. Worker A may continue mutating
state, sending/editing cards and checkpointing after worker B acquired the same
chat key. Redis ownership tokens prevent A from releasing B's lock, but they do
not prevent stale A writes. This is a real data/order-corruption path, not a
sleep-based theoretical race. H3 gates reduce duplicate irreversible submission
risk, but do not protect ordinary ConversationState/UI checkpoints.

### P1

**P1-1 — lock contention loses valid updates.** `TimeoutError` is outside
`process_telegram_update.autoretry_for`, is raised before the inner `try`, and
the webhook has already returned 200. The row can remain `processing`; the
user's second message or callback has no durable retry.

**P1-2 — distinct updates have no defined order.** `enqueue_once` uniqueness is
not sequencing. Celery pickup and Redis lock acquisition may reverse update
order, which can produce materially wrong draft transitions even when there is
no lock expiry.

**P1-3 — submission/background work blocks normal user input with the same 15 s
wait.** The H1–H3c gates protect irreversible work, but they do not preserve a
new text/callback that times out waiting for the submission lock.

**P1-4 — crash/commit→delay gap has no durable re-drive.** A queued DB row can
exist without a Celery task; subsequent duplicate webhook delivery is deduped
and cannot repair the missing task.

### P2

- Reply accepted by Telegram before `reply_sent` checkpoint can be delivered
  twice after a retry.
- `_enqueue_side_effects` before `tasks_enqueued` checkpoint can duplicate
  status/review/product-add user messages even though H3/dispatch gates prevent
  duplicate irreversible submission.
- Background status presenter emits unversioned pagination/order callbacks;
  these are not destructive but can address a newer current view.
- Best-effort processing-card/keyboard operations may fail with only a log.

### P3 / backlog

- No production metric currently proves lock wait distribution or expiry.
- Registration callbacks intentionally use venue context rather than UI
  revision; this is low risk while code/token is validated.
- Unit tests that replace `chat_lock` with `nullcontext()` cannot expose Redis
  lease or cross-worker races.

## 7. Required invariants and current status

| Invariant | Current status |
|---|---|
| I1. One `update_id` mutates business state at most once | **Mostly enforced** by unique row + state checkpoint; stale reclaim after 5 min remains an edge case. |
| I2. At most one active business mutation per chat | **Not proven** after TTL expiry; valid while lease is alive. |
| I3. Distinct valid updates are not silently lost | **Violated** by 15 s contention and commit→delay gap. |
| I4. One-chat updates execute in a defined order | **Violated**; no sequence owner/cursor. Product decision: strict Telegram arrival order is required for draft commands. |
| I5. Stale callback cannot mutate current modal state | **Mostly enforced** for versioned main-path callbacks; unversioned callbacks bypass revision. |
| I6. Rapid submit cannot duplicate irreversible work | **Enforced for known H1–H3c paths** by durable submission/dispatch gates. |
| I7. Lock contention is normal, retryable state | **Violated**; built-in `TimeoutError` is not process-task retryable. |
| I8. Expired lease cannot permit stale owner mutation | **Violated**; no renewal/fencing. |
| I9. Slow chat does not block unrelated chats | **Satisfied**; key is per chat, no global chat lock. |
| I10. UX duplicate/lost replies are classified separately | **Partially satisfied**; H3c covers completion, ordinary reply checkpoint window remains P2. |

## 8. Existing test coverage

| Area | Coverage |
|---|---|
| `enqueue_once`/duplicate webhook | EXISTS AND STRONG for sequential repository/API dedupe; no commit→delay crash test |
| `_claim` stale/reclaim | EXISTS BUT INSUFFICIENT; pipeline tests use mocked claims and do not run concurrent DB rows |
| Multiple workers / Redis lock | MISSING; service tests patch `chat_lock` to `nullcontext()` |
| Stale `ui_revision` callback | EXISTS AND STRONG for a versioned callback (`test_stale_callback_cannot_mutate_current_draft`) |
| Rapid double callback / different buttons | MISSING |
| Lock contention / lock expiry / renewal | MISSING |
| Task enqueue replay | EXISTS BUT INSUFFICIENT; H3 side-effect gates are focused, orchestration checkpoint race is not |
| Reply replay after transient Telegram error | EXISTS AND STRONG for state checkpoint before reply |
| Reply checkpoint failure after Telegram acceptance | EXISTS BUT INSUFFICIENT; no duplicate-delivery assertion |
| Slow OpenAI/voice/photo against a second input | MISSING |
| Submission double invocation | EXISTS AND STRONG for H1–H3c irreversible gates; MISSING for competing normal user update |
| Different chats remain concurrent | MISSING integration/concurrency test |

The relevant read-only validation command passed:

```text
.venv\Scripts\python.exe -m pytest -q \
  tests/api/test_health_and_webhook.py tests/workers/test_tasks.py \
  tests/repositories/test_repositories.py tests/telegram/test_callback_contract.py \
  tests/telegram/test_telegram_transport.py tests/input/test_voice_processing_card.py \
  tests/conversation/test_ui_revision.py tests/submission/test_submission_guards.py
```

Result: **all selected tests passed**. No tests were added or changed.

## 9. Proposed regression matrix for a future implementation block

These are test cases for a later, separately approved implementation; they are
not added in this analysis:

1. Two distinct updates for one chat with reversed worker pickup: assert
   `update_id` order, one state mutation each, and no cross-chat blocking.
2. Lock held >15 s: second update remains queued/retryable and eventually
   executes exactly once; no error-only terminal state.
3. Photo/vision work >120 s: old owner cannot checkpoint after lease handoff;
   either lease is renewed or fenced.
4. Same callback `r15` five times: one business transition, remaining four
   stale/no-op, one task enqueue.
5. Two different `r15` callbacks: deterministic order and stale behavior.
6. Submission running while Add/Back/New/Status arrives: all valid updates are
   preserved and ordered; H3 gates still allow at most one irreversible effect.
7. Commit succeeds and Celery `.delay()` fails: a re-drive finds and enqueues
   the queued row without creating a duplicate.
8. Telegram accepts reply and reply checkpoint fails: recovery policy is
   explicit and duplicate UX is measured separately from state safety.

## 10. Bounded implementation scope (not implemented here)

Two bounded blocks are required because they protect independent invariants:

1. **Per-chat durable sequencing and contention handling** — owner at the
   webhook/queue boundary must preserve every accepted update, define order for
   one chat, and retry/re-drive normal updates when a worker is busy. It must
   not be a global lock and must not drop later valid input.
2. **Lease expiry fencing for long-running work** — renew or fence the per-chat
   lease so an expired worker cannot write state/UI after a new owner starts.
   This cannot be safely replaced by block 1 alone: a perfect queue still has a
   stale-owner race if the lease expires, while fencing alone cannot restore
   messages lost by the current 15-second contention path or define FIFO.

Do not add a generic busy flag, arbitrary sleeps or a global serialization lock.
Reuse `ui_revision` and the existing H1–H3c side-effect gates. Keep Telegram
transport concerns at the adapter boundary and leave parser/engine
decomposition for the planned behavior-preserving refactor.

## Final verdict

**VERDICT C: IMPLEMENT TWO BOUNDED CONCURRENCY FIX BLOCKS**

The P0 lease-expiry race and P1 lost-input/ordering failures block safe
decomposition. The two blocks above are the minimum separable scope: durable
per-chat sequencing preserves input and order; lease fencing prevents an old
worker from corrupting state after ownership expiry.

Application code, tests and migrations were not changed in this analysis.
`.env` was not read and is not tracked (`git ls-files .env` returned empty).
GitLab was not used.

## CONCURRENCY BLOCK 2 — IMPLEMENTATION RESULT

Block 2 is implemented without changing parser, engine business rules, UX,
database schema, or the existing Block 1 sequencing contract.

\`chat_lock()\` now returns a renewable \`ChatLease\`. Redis locks use
\`thread_local=False\`, so the heartbeat thread and the worker share the same
redis-py ownership token. Renewal runs at \`timeout / 3\` (or a stricter
validated interval), and release checks ownership after the heartbeat stops;
an expired or replaced token is never released by the old worker.

\`ChatLeaseLostError\` is an explicit retryable signal. Orchestrator checkpoints,
Telegram replies, background task publication, catalog invalidation, and
completion fencing check ownership before committing the next step. Submission
and review services use the same lease and check it around state writes and
external Google Sheets/Telegram boundaries. A lost lease is not converted into
a generic failed update: the current \`processing\` row is returned to
\`queued\` only when its \`attempts\` value still matches the worker's claimed attempt.
Product-add writes are conservatively marked \`write_uncertain\` before the
external append, so a worker losing ownership cannot mark the request submitted
or blindly repeat the append.

Regression coverage includes lease acquisition, heartbeat renewal and stop,
renewal failure, changed-owner fencing, safe release, per-chat isolation,
task-publication fencing, attempt-safe deferral, and the Celery retry signal.

Validation:

- Block 2 focused tests: **42 passed**.
- H1–H3c submission safety focused tests: **122 passed**.
- Fresh full suite: **1337 collected / 1323 passed / 14 failed / 0 skipped /
  0 xfailed / 0 errors**. The 14 failures are the known pre-existing baseline;
  no new failure nodeids appeared.
- Ruff and \`git diff --check\` pass. No migration was added; Alembic remains at
  \`0008\`.

\`.env\` was not read and is not tracked. GitLab was not used.

## CONCURRENCY BLOCK 1 — IMPLEMENTATION RESULT

Block 1 is implemented at the webhook/repository/worker boundary. The durable
`TelegramUpdate` row remains the source of accepted work; `UpdateRepository`
enforces the smallest unfinished `update_id` per chat before a row is claimed.
The row stays `queued` until the per-chat Redis lock is acquired, so ordinary
lock contention cannot strand fresh work as `processing`.

`ChatLockBusyError` and `UpdateSequenceDeferred` are explicit retry signals.
Celery retries both signals, while `redrive_telegram_updates` runs every 60
seconds and enqueues the oldest recoverable row per chat. Recoverable rows are
`queued` rows and `processing` rows older than five minutes; fresh processing
rows are not stolen. Duplicate webhooks re-drive only existing `queued` or
`processing` rows; `done` and `ignored` rows are not replayed.

The database row is committed before Celery dispatch and remains durable if the
dispatch call fails, closing the commit-to-task gap through periodic re-drive.
Existing state, reply, task, and side-effect checkpoints remain authoritative.
No migration was required because the existing `TelegramUpdate` fields and
indexes are sufficient; Alembic remains at `0008`.

Block 2 is intentionally open: Redis lease expiry fencing/heartbeat is not
implemented in this block.

## CONCURRENCY BLOCK 2.1 — DONE

The catalog submission subtree now carries the same `ChatLease` through
prepare/apply/read-back, controlled recovery, catalog checkpoints and result
replies. A stale owner cannot continue verification, controlled apply or
conflict/uncertain mutation after lease loss. Finalization fences state save
and `SubmissionRecord.finalized` internally. Deterministic submission tests
cover catalog apply/read-back loss, controlled-apply blocking, finalize,
recalculation, dispatch and H3c notification behavior. No migration was
added; Alembic remains at `0008`.
