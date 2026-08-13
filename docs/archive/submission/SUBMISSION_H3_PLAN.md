# BLOCK H3 — RECALC / CACHE / COMPLETION NOTIFICATION

## 1. Scope

Это production-safety анализ после завершённого H2. В H3 рассматриваются три
разные внешние операции:

1. удаление каталога из Redis-кэша;
2. вызов Apps Script для пересчёта листа `Заявка`;
3. финальное уведомление пользователя через Telegram.

Документ не меняет код, тесты, prompts, схему базы данных или пользовательские
тексты. H1/H2 остаются обязательными safety gates. Текущий baseline
**1285 collected / 1271 passed / 14 failed** не исправляется.

## 2. Current side-effect sequence

Фактическая последовательность в `SubmissionService.submit()`:

```text
catalog plan verified and catalog_updated checkpoint
    ↓
CatalogCache.invalidate(spreadsheet_id)
    ↓
GoogleSheetsGateway.trigger_recalculation(order_no, spreadsheet_id)
    ↓
_checkpoint(order_no, "recalc_done")
    ↓
dispatch_started → central Web App POST → dispatch_completed
    ↓
_finalize(...)
    ↓
Telegram send_reply(success/local-success)
    ↓
_checkpoint(order_no, "completion_notified")
```

Уточнение: cache invalidation сейчас выполняется только если в начале попытки
`catalog_was_completed == False`. Если запись каталога уже была завершена в
предыдущем запуске, cache шаг пропускается. Recalculation защищён только булевым
`recalc_done`; отдельного `started/uncertain` статуса нет. Центральный dispatch
уже имеет отдельный started/uncertain gate и в H3 не меняется.

Есть второй caller `CatalogCache.invalidate()` в
`UpdateOrchestrator.process()`, когда `EngineResult.invalidate_catalog` выставлен
для обычного изменения каталога. Он также не имеет отдельного durable checkpoint;
ниже основной анализ относится к submission pipeline, но H3a должен покрыть оба
вызова тестами операции удаления.

## 3. Cache invalidation analysis

### Фактическая операция

`CatalogCache.invalidate()` проверяет `spreadsheet_id` и вызывает один Redis
`DELETE` по детерминированному ключу. Удаление отсутствующего ключа не меняет
состояние и поэтому идемпотентно по смыслу. Redis-клиент всё же может выбросить
ошибку соединения, таймаут или ошибку инфраструктуры.

### Неверное окно текущего retry

```text
catalog_updated = true
↓
Redis DELETE применён или его результат неизвестен
↓
invalidate() выбрасывает ошибку до recalc
↓
следующий submit видит catalog_was_completed = true
↓
invalidate() пропускается
```

Следствие — устаревший каталог остаётся в Redis до TTL или до другой ручной
инвалидации. Внешняя каталожная мутация не повторяется, но repair-кэш теряется.
Это подтверждённый **MEDIUM operational/data-freshness risk**, не риск двойной
записи в таблицу.

Текущий тест
`test_catalog_cache_failure_after_completion_does_not_reapply` проверяет, что
каталоговая запись не повторяется, но вызывает `MagicMock.invalidate()` напрямую
и не моделирует `SubmissionService.submit()` с последующим retry. Поэтому он
**INCOMPLETE** для доказательства восстановления кэша.

### Рекомендация

Оставить `invalidate()` простой повторяемой операцией и выполнять её после
подтверждённого `catalog_updated` на каждой попытке, пока она не завершилась
успешно. Отдельная миграция для cache не нужна. Не связывать cache repair с
повторной записью каталога и не пересоздавать mutation plan.

Минимальный safety contract H3a:

- `catalog_updated=true` разрешает только cache delete, не новую Sheets mutation;
- ошибка delete останавливает дальнейшие стадии текущей попытки;
- следующий запуск снова делает delete;
- после успешного delete переходят к recalc;
- тесты покрывают submission caller и orchestrator caller.

## 4. Recalculation analysis

### Точный endpoint call

`GoogleSheetsGateway.trigger_recalculation()` выполняет:

```text
POST settings.google_recalc_url
JSON {
  token,
  sheetName: settings.google_recalc_sheet,
  spreadsheetId
}
timeout=45s, follow_redirects=True
```

`order_no` намеренно не отправляется: gateway-комментарий говорит, что внешний
скрипт пересчитывает весь лист `Заявка`. Успехом считается HTTP success и ответ
без `ok=false`, `success=false`, `error` или явного текста ошибки. После этого
локально ставится `recalc_done=true`.

### Доказательства и границы знания

Исходник Apps Script или endpoint-контракт в этом repository отсутствует. Есть
только URL/token настройки, gateway и тест тела запроса. Поэтому:

| Свойство | Статус | Основание |
|---|---|---|
| HTTP POST выполняется | PROVEN | `GoogleSheetsGateway.trigger_recalculation()` |
| формулы листа пересчитываются | LIKELY | имя/комментарий gateway, но нет endpoint source |
| только детерминированный recalc без других эффектов | UNKNOWN | нет Apps Script и контракта |
| append/history/notifications/automation отсутствуют | UNKNOWN | нет внешней реализации |
| повторный POST идемпотентен | **NOT PROVEN** | нет operation key или server-side ledger |

### Unknown-result window

```text
endpoint мог выполнить пересчёт
↓
ответ потерян / timeout / worker остановлен
↓
recalc_done остаётся false
↓
следующий submit повторяет POST
```

То же окно возникает, если POST успешно завершился, а DB checkpoint не записался.
Таким образом, blind retry сейчас возможен. В отличие от H2 catalog mutation здесь
нет детерминированного `before/expected_after` набора ячеек, по которому безопасно
доказать результат read-back.

### Варианты дизайна

| Вариант | Safety | Availability | Schema | External change | Recovery | Complexity |
|---|---|---|---|---|---|---|
| A. Оставить blind retry | Низкая, duplicate unknown | Высокая | Нет | Нет | Повтор POST | Низкая |
| B. `started/uncertain`, без blind retry | Высокая против duplicate | Средняя, возможен ручной recovery | **Нужна 0007** | Нет | Проверка/ручное продолжение | Средняя |
| C. Operation ID/idempotency key | Высокая при атомарной реализации | Высокая | Возможна | **Нужен Apps Script** | Повтор с тем же ID | Высокая |
| D. Read-back результата | Только при точном observable contract | Средняя | Возможно | Нужен контракт чтения | Сверка факта | Высокая и пока недоказуема |
| E. Доказать естественную идемпотентность | При полном внешнем доказательстве | Высокая | Нет | Нужен исходник/контракт | Blind retry допустим только после доказательства | Средняя |

### Рекомендация

Выбрать **B как текущий H3b**: перед POST сохранять `recalc_status=started`, при
неизвестном результате переводить в `uncertain` и не делать автоматический второй
POST. Для уверенного отказа до отправки можно оставить обычный retryable failure;
после начала внешнего вызова результат считать неопределённым. Это требует новой
миграции (например, typed status и timestamps; operation id полезен для аудита,
но сам по себе не делает Apps Script идемпотентным).

Долгосрочно вариант C возможен только после получения и проверки Apps Script:
один и тот же operation ID должен атомарно защищать внешний эффект. H3 не должен
утверждать безопасность повторного recalc до такого доказательства.

## 5. Telegram completion analysis

### Фактический flow

`_send_completion()` и `_send_local_completion()` вызывают
`TelegramClient.send_reply()`, затем `_checkpoint(..., "completion_notified")`.
`_load_unnotified_completion()` выбирает `finalized=true` и
`completion_notified=false`; следующий worker вызывает отправку ещё раз.

`TelegramClient` умеет редактировать сообщение по `edit_message_id` и возвращает
`message_id`. Однако completion presenter не задаёт `edit_message_id`, а
`SubmissionRecord` не сохраняет ID финального сообщения. `ConversationState.ui_message_id`
существует для UI-карточек, но не является durable completion message contract.

### Риск

Если Telegram уже принял сообщение, а ответ потерян или checkpoint завершения
упал, следующая попытка отправляет новую карточку. Заявка уже финализирована и
каталог/dispatch повторно не выполняются, поэтому это **MEDIUM/LOW UX risk**, а не
риск двойной заявки или двойного количества.

### Варианты доставки

| Вариант | Плюс | Минус |
|---|---|---|
| A. Текущий at-least-once | Пользователь почти всегда получает подтверждение | Возможна дублированная карточка |
| B. At-most-once: started до send | Нет автоматического дубля | Можно потерять подтверждение |
| C. Edit/upsert стабильного сообщения | Повтор может обновить ту же карточку | Нужен надёжный target message ID и fallback |
| D. Persist returned message ID | Улучшает повторную доставку | ID может быть потерян вместе с ответом; нужна схема |

### Рекомендация

Для H3c выбрать **at-most-once после неизвестного результата**: durable статус
`started/uncertain` запрещает автоматическую повторную отправку после начала вызова.
Это честно принимает неизбежный trade-off Telegram: при потерянном ответе нельзя
одновременно гарантировать отсутствие дубля и наличие сообщения. В отдельном UX
усилении можно добавить message ID и edit/upsert, но это не должно использоваться
как обещание exactly-once без доказанного Telegram operation key.

Минимальная схема для H3c — статус и timestamp notification; `message_id` нужен
только для выбранного будущего upsert-варианта. Текущий H3-анализ не меняет UX и
не добавляет миграцию.

## 6. Existing tests

| Область | Тесты | Классификация |
|---|---|---|
| Cache | `test_catalog_cache_failure_after_completion_does_not_reapply` | INCOMPLETE: не моделирует submit retry и repair |
| Recalc | `test_recalculation_uses_the_same_body_as_n8n`, `test_recalculation_without_token_cannot_be_marked_successful` | VALID для request/error до POST; MISSING для timeout/unknown/retry |
| Dispatch | `test_ambiguous_dispatch_failure_is_never_automatically_retried`, `test_redelivery_after_started_dispatch_does_not_send_second_post` | VALID |
| Completion order | `test_success_card_is_checkpointed_only_after_telegram_accepts_it`, `test_notification_failure_after_finalize_never_rolls_back_order` | VALID для порядка и no rollback |
| Completion recovery | `test_retry_delivers_success_card_after_order_was_already_finalized`, `test_retry_delivers_local_saved_card_without_claiming_dispatch` | INCOMPLETE: фиксируют текущий resend, но не защищают duplicate window |

Необходимые будущие regression tests:

- cache failure after catalog checkpoint → retry invalidates again and does not
  repeat Sheets mutation;
- recalc definite-before-POST failure, endpoint rejection, lost response, and
  checkpoint failure → unknown state never performs second POST;
- completion send failure before acceptance, accepted response, lost response,
  checkpoint failure → selected delivery policy is deterministic;
- local saved completion follows the same notification policy.

## 7. Failure-window matrix

### Cache

| Window | Current behavior | Safe? | Damage | Desired |
|---|---|---|---|---|
| C1 before delete | stage fails before invalidate | Да для каталога | stale cache remains | retry delete |
| C2 delete succeeds | next stages continue | Да | нет | unchanged |
| C3 delete raises | record catalog already completed, submission fails | Частично | stale cache | retry delete before recalc |
| C4 crash after delete | next run may call delete again | Да, delete idempotent | нет | repeatable delete |

### Recalculation

| Window | Current behavior | Safe? | Damage | Desired |
|---|---|---|---|---|
| R1 before POST | no `recalc_done`, retry possible | Да | нет | unchanged |
| R2 definite rejection | exception, no checkpoint | Да | no recalc | retry allowed only if definitely before effect |
| R3 effect + response | checkpoint true | Да | нет | unchanged |
| R4 effect + response lost | `recalc_done=false`, blind POST on retry | **Нет** | duplicate unknown effect | uncertain, no blind POST |
| R5 no effect + timeout | indistinguishable from R4 | Нет доказательства | possible missed recalc | uncertain/manual verification |
| R6 effect + DB failure | checkpoint false, blind POST | **Нет** | duplicate unknown effect | uncertain, no blind POST |

### Telegram

| Window | Current behavior | Safe? | Damage | Desired |
|---|---|---|---|---|
| T1 before send | `completion_notified=false` | Да | missing card until retry | policy-specific retry |
| T2 definite API failure | no checkpoint, retry path | Обычно | no card | classify only definitely-before-accept |
| T3 delivered + response | checkpoint true | Да | нет | unchanged |
| T4 delivered + response lost | resend on next worker | Нет для UX | duplicate card | uncertain policy |
| T5 delivered + DB checkpoint failure | resend on next worker | Нет для UX | duplicate card | uncertain policy |
| T6 checkpoint before send | not current design | Trade-off | missing card | only if at-most-once chosen |

## 8. Design options

Сравнение вариантов по трём операциям:

| Область | Минимальный безопасный вариант | Более сильный вариант | Почему не выбираем сразу |
|---|---|---|---|
| Cache | Повторяемый Redis delete после `catalog_updated` | Отдельный durable cache checkpoint | Для idempotent delete отдельная схема избыточна |
| Recalc | `started/uncertain`, без blind POST | Apps Script operation key или доказанный read-back | Endpoint source отсутствует; безопасность не доказана |
| Telegram | At-most-once после uncertain | Persisted message ID + edit/upsert | Message ID не гарантирует exactly-once при потерянном ответе |

## 9. Recommended implementation

### Priority and phases

1. **H3a — cache repair**: small, no schema; make delete retryable after a
   completed catalog checkpoint and cover both callers.
2. **H3b — recalc uncertainty gate**: highest severity; add durable started/uncertain
   status (migration 0007), forbid blind POST after unknown, then document manual
   verification. Do not claim Apps Script idempotency.
3. **H3c — completion notification policy**: add durable notification uncertainty
   status and choose at-most-once after unknown; consider message-ID upsert later.

The first implementation phase should be **H3a cache repair**, because it is a
local, idempotent operation with a confirmed bug and no external schema change.
H3b remains the highest-severity production risk and should follow immediately,
with its migration and tests explicitly separated from H3a.

## 10. Schema and migration implications

- Cache: no schema change required.
- Recalc: migration 0007 is required for durable `started/uncertain/completed`
  status and timestamps; do not add it in this analysis task.
- Telegram: a status/timestamp is required for at-most-once recovery. A persisted
  completion `message_id` is additionally required only for edit/upsert design.
- H1 migration 0006 remains unchanged. No 0007 was created here.

## 11. User-facing implications

Никакие тексты в H3 analysis не меняются. Future implementation must explain
only that the request is saved and needs a responsible employee/check, without
words such as `checkpoint`, `timeout`, `state`, `dispatch`, `idempotency`, `API`
or `retry loop`. A user must never be offered a button that can repeat an
uncertain external side effect.

## 12. Implementation phases

1. H3a: repairable cache invalidation, без миграции.
2. H3b: recalc uncertainty gate и migration `0007` после отдельного согласования.
3. H3c: notification uncertainty policy; message-ID upsert только отдельным
   решением после проверки Telegram UX.

## 13. Success criteria

- Cache delete can be retried after a completed catalog stage without repeating
  catalog mutation or recalculation.
- Recalc unknown result never causes an automatic second POST without proven
  idempotency or a verified external operation key.
- Completion notification policy explicitly chooses duplicate-vs-missing trade-off
  and tests both accepted-message/checkpoint-failure windows.
- H1/H2 tests remain green; current 14 baseline failures remain untouched.
- No parser, engine, orchestrator, matching, prompt, decomposition or MAX work is
  mixed into H3.

## 14. Out of scope

- H3 implementation, migration 0007, Apps Script changes and live endpoint checks;
- fixing the 14 current full-suite failures;
- parser/OpenAI/matching/state-machine changes;
- changing user-facing text or Telegram UX in this analysis;
- decomposition and MAX.
