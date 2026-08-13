# Testing Readiness — Block 6D

Текущий документ готовности для ручного тестирования. Он не заменяет
исполняемый код и не переписывает исторический
[`FULL_REGRESSION_AUDIT.md`](FULL_REGRESSION_AUDIT.md).

## 1. Текущий снимок проверки

| Поле | Значение |
|---|---|
| Дата | 2026-08-13 |
| Ветка | `decompose_bot` |
| Проверенный HEAD | `c1cc92e697704d49e221eaed4ea3a07a0e3782a9` |
| Полный baseline | `1378 collected / 1378 passed` за `17.13 s` |
| Финальная проверка после acceptance tests | `1380 collected / 1380 passed` за `17.87 s` |
| Архитектурный статус | `SERVICES_FINAL_FREEZE`; Block 6C/6C.1 завершены |
| Реальные Telegram/Google/OpenAI/supplier effects | не выполнялись |
| `.env` | не tracked; значения не читались |

Baseline `1378/1378` снят на исходном Block 6D HEAD `2fb1e6e03e5f889ba5034da013facd555fea138f4`;
финальный документ и acceptance suite относятся к HEAD `c1cc92e697704d49e221eaed4ea3a07a0e3782a9`.

Архитектурная декомпозиция завершена решениями `ENGINE_PHASE_ACCEPTABLE`,
`ORCHESTRATOR_DECOMPOSITION_SUFFICIENT` и `SERVICES_FINAL_FREEZE`. Block 6D
закрыл два acceptance coverage gap и не начинал новый refactor.

Исторический snapshot на `669b5cff05ef959cc2ded5a21f14054f9aa737ab` сохранён
ниже как архивное сравнение и не является текущим baseline.

### Архивный snapshot Block 6C

| Поле | Историческое значение |
|---|---|
| HEAD | `669b5cff05ef959cc2ded5a21f14054f9aa737ab` |
| Full pytest | `1377 collected / 1377 passed` |
| Verdict | `READY_FOR_MANUAL_TESTING_WITH_KNOWN_NONBLOCKERS` |

Эти цифры относятся к завершённому Block 6C и не заменяют свежую проверку
текущего HEAD.

## 2. Автоматические проверки

| Проверка | Результат |
|---|---:|
| Full pytest | `1380 passed` |
| Acceptance stabilization tests | `2 passed` |
| Conversation/input/catalog/venue/callback focused set | `455 passed` |
| AI/voice/quantity/submission/concurrency safety set | `254 passed` |
| Representative historical HIGH recheck | `36 passed` |
| Explicit acceptance subset | `16 passed` |
| Ruff check | pass |
| Ruff format check (`src tests`) | `130 files already formatted` |
| Mypy | `129 source files, no issues` |
| Compileall (`src tests scripts alembic`) | pass |
| Markdown links | `33 files, pass` |
| Scenario catalog check | `34 scenarios, актуален` |
| `git diff --check` | pass |

Все smoke-наборы используют fake/mock/test settings. Никаких реальных заявок,
записей в production Sheets или сообщений пользователям не отправлялось.
Полная проверка `ruff format --check .` повторно показывает один исторический
P2-файл `docs/RAPID_INPUT_CONCURRENCY_ANALYSIS.md`; изменённые в Block 6D файлы
форматированы, исторический документ намеренно не переписывался.

## 3. Критические пользовательские сценарии

`AUTOMATED_PASS` означает проверенный fake/local path. `AUTOMATED_PARTIAL`
означает, что semantic path покрыт, но отсутствует реальный media/provider
прогон. `MANUAL_LIVE_REQUIRED` — операторская проверка с настроенным
окружением; секреты в этот документ не записываются.

| ID / канал | Preconditions → действие → ожидаемый результат | Automated test / current result |
|---|---|---|
| REG-01 text/button | личный чат без venue → код → binding и collecting state | `tests/venue/test_venue_registration.py`; **AUTOMATED_PASS** |
| REG-02 text/button | активная привязка → отказ/смена → старый доступ не смешивается с новым | venue suite; **AUTOMATED_PASS** |
| REG-03 text/voice/photo/button | доступ отозван/восстановлен → запрос → access isolation | venue suite; **AUTOMATED_PASS**, live directory **MANUAL_LIVE_REQUIRED** |
| INP-01 text | collecting → товар с количеством → item в cart и reply | orchestrator/input suites; **AUTOMATED_PASS** |
| INP-02 voice | voice → transcript → тот же semantic pipeline → item/quantity | voice contract/media suites; **AUTOMATED_PARTIAL**, live audio **MANUAL_LIVE_REQUIRED** |
| INP-03 photo | фото оригинальной таблицы → structured items → catalog resolution | `test_ai_media.py`; **AUTOMATED_PARTIAL**, real image **MANUAL_LIVE_REQUIRED** |
| INP-04 photo | фото списка → позиции сохраняются без cross-row leakage | photo media suite; **AUTOMATED_PARTIAL** |
| INP-05 photo | зачёркнутое значение → handwritten replacement wins | `test_ai_media.py`; **AUTOMATED_PASS** на fake vision payload |
| REC-01 text/voice/photo | item без quantity → missing-quantity state; ответ числом завершает item | quantity/input suites; **AUTOMATED_PASS** |
| REC-02 text/voice/button | unit mismatch → correction card, quantity не теряется | unit/voice quantity suites; **AUTOMATED_PASS** |
| REC-03 text/voice/button | ambiguous candidates → explicit number/name выбирает только visible candidate | candidate suites; **AUTOMATED_PASS** |
| REC-04 text/voice/button | отсутствующий товар → NOT_FOUND/clarification, похожий товар не подставляется | not-found/candidate suites; **AUTOMATED_PASS** |
| REC-05 text/voice/button | duplicate pending → explicit merge/skip, независимая команда не подтверждает duplicate | duplicate suites; **AUTOMATED_PASS** |
| REC-06 text/voice/photo | comment scope item/order → comment binding без переноса на чужую позицию | comment scope/AI suites; **AUTOMATED_PASS** |
| DRF-01 text/voice/button | непустой cart → «черновик» → draft page без mutation | orchestrator/voice controls; **AUTOMATED_PASS** |
| DRF-02 text/voice/button | collecting/review → добавить ещё → collecting и сохранённый cart | add-more suites; **AUTOMATED_PASS** |
| DRF-03 text/voice/button | cart → очистить → пустой collecting state | engine/progression suites; **AUTOMATED_PASS** |
| DRF-04 text/voice/button | активный cart → новая заявка → confirmation, cart не уничтожается до подтверждения | new-order suites; **AUTOMATED_PASS** |
| DRF-05 text/voice/button | предыдущая заявка сохранена → новая → новый trace/state без leakage | order-events/submission suites; **AUTOMATED_PASS** |
| SUB-01 text/voice/button | готовый cart → финальная проверка → review state и revision | review/submission suites; **AUTOMATED_PASS** |
| SUB-02 text/voice/button | supplier minimum не достигнут → warning/chooser, отправка не запускается | multiple/supplier suites; **AUTOMATED_PASS** |
| SUB-03 text/voice/button | review → запись в order sheet → enqueue intent без реального dispatch | submission/mapping suites; **AUTOMATED_PASS** fake path |
| SUB-04 text/voice/button | history/status → выбранная заявка и pagination | telegram command suites; **AUTOMATED_PASS** |
| SUB-05 system | внешний dispatch failure → failure/uncertain state, не success | submission guard/lease suites; **AUTOMATED_PASS** fake path |
| SUB-07 text/voice/button | история → voice position/page → правильная detail page | telegram command suite; **AUTOMATED_PASS** |
| SUB-08 callback/Sheets | review link → token/venue/revision checks → safe review action | sheet review tests; **AUTOMATED_PASS** fake path |
| SAF-01 text/voice | modal item → «не добавляй» → item не добавлен | route safety suites; **AUTOMATED_PASS** |
| SAF-02 text/voice | submit confirmation → «не отправляй» → dispatch отменён | add-more/submit suites; **AUTOMATED_PASS** |
| SAF-03 text/voice | отрицательная фраза → не создаёт новую заявку | voice route safety; **AUTOMATED_PASS** |
| SAF-04 callback | старая revision → callback → текущий draft не меняется | callback contract; **AUTOMATED_PASS** |
| RCV-01 voice | пустой/неподдержанный transcript → safe recovery card | voice contract; **AUTOMATED_PASS** fake transcription |
| RCV-02 photo | фото без order quantity → позиции не отправляются как заказанные | AI media quantity tests; **AUTOMATED_PASS** fake vision |
| RCV-03 text | directory unavailable → безопасный отказ/предыдущий access не ломается | venue access tests; **AUTOMATED_PASS** |
| RCV-04 text/voice/photo | техническая ошибка → recovery reply, checkpoint semantics сохраняются | orchestrator pipeline; **AUTOMATED_PASS** |

## 4. Channel readiness

### TEXT

Готовность подтверждена fake-runtime suites: global parse, StateCompatibility,
engine, catalog, comments, quantity, draft, review и safe errors проходят.

### VOICE

Transcript после распознавания использует тот же text interpretation pipeline;
voice routing, quantity recovery, visible actions, retry и failure contracts
проходят. Отдельный live ASR прогон не выполнялся: **MANUAL_LIVE_REQUIRED**.

### PHOTO

Structured vision payload, quantity isolation, packaging/title separation и
handwritten replacement покрыты fake tests. Реальная фотография и качество OCR
не проверялись: **MANUAL_LIVE_REQUIRED**.

### CALLBACK

Fresh/stale revision, candidate, pagination, skip/back/confirm/comment scope и
review callbacks покрыты tests; stale callback не мутирует текущий draft.

## 5. Explicit acceptance regressions

| Сценарий | Current result |
|---|---|
| Новый товар во время quantity modal | **PASS**: `test_quantity_answer_completes_current_missing_item_without_new_cart_line`, `test_voice_product_list_is_not_consumed_by_open_quantity_card`; сильная новая команда прерывает modal и не наследует старый quantity |
| «Удали все комментарии» | **PASS**: `test_remove_all_comments_keeps_cart_and_catalog_bindings`; комментарии очищены по scope, cart, identities, quantity, units и catalog bindings не изменились |
| «Не X, а Y» в комментарии | **PASS**: `test_comment_correction_replaces_only_corrected_fact`; заменён только совпавший фрагмент, независимые пожелания сохранены |
| Quantity provenance | **PASS**: explicit order quantity не переписывается packaging/title/catalog number; catalog tests и AI media tests green |
| NOT_FOUND | **PASS**: safe clarification/alternatives, похожий товар не auto-select |
| Supplier-comment provenance | **PASS**: representative catalog/AI provenance tests green |
| Text/voice parity | **PASS** для transcript/stub path; live ASR parity остаётся manual-only |

## 6. Historical HIGH recheck

Исторические `45/52` из `FULL_REGRESSION_AUDIT.md` не используются как текущий
baseline. Representative groups на текущем HEAD дали `36 passed`, включая AI
comment provenance, spoken quantity, title/packaging numbers, voice shadow and
comment recovery, visible-action fallback и photo quantity isolation.

Текущий full suite также зелёный (`1380/1380`), поэтому подтверждённых P0/P1
регрессий по историческому списку нет. Два ранее открытых acceptance gap закрыты
автоматическими характеристиками на уровне ConversationEngine.

## 7. Search readiness

| Слой | Current result |
|---|---|
| Retrieval | exact/normalized/short/broad/supplier-scoped/search-all paths покрыты catalog resolver/matching tests |
| Ranking | deterministic scores и shortlist тестируются отдельно |
| Candidate presentation | ambiguous/not-found/candidate callback tests green |
| Auto-selection | safety gates, evidence, numeric/packaging compatibility и similar-but-not-same veto green |

Высокий score не считается достаточным для auto-select. Если identity товара не
доказана, текущий код оставляет candidate/clarification.

## 8. Safety and external effects

- Registration/access, venue isolation: fake suite green.
- Same-chat sequencing, different-chat lease independence and stale-owner fencing:
  `171` safety tests green.
- State/reply/task checkpoint replay: repository/orchestrator/submission suites green.
- Submission disabled/uncertain paths: fake tests green; real dispatch не выполнялся.
- Required invariant `GOOGLE_ORDER_SUBMISSION_ENABLED=false` для ручной проверки
  сохраняется; реальный supplier dispatch запрещён.

## 9. Local startup path

Официальный локальный путь находится в `README.md`:

```text
docker compose config --quiet
docker compose up -d --build
docker compose ps
curl --fail http://127.0.0.1:8000/health/live
curl --fail http://127.0.0.1:8000/health/ready
docker compose logs --tail 100 api worker
```

Альтернатива для изолированного development процесса: `APP_ENV=local`,
`uvicorn restaurant_bot.api.app:app` и отдельный Celery worker. Для ручного
Telegram smoke нужны только названия настроек из `config.py`/`.env.example`:
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `OPENAI_API_KEY`,
`GOOGLE_SERVICE_ACCOUNT_FILE`, `GOOGLE_RECALC_URL`,
`GOOGLE_ORDER_SUBMISSION_URL`, `GOOGLE_ORDER_SUBMISSION_SECRET`, database и
Redis/Celery URLs. Значения секретов здесь не приводятся и не читались.

В Block 6C Docker не запускался: это не требуется для fake readiness и могло бы
создать внешние/локальные side effects. Для human Telegram pilot оператор должен
сам подтвердить изолированное test окружение и отключённую отправку.

## 10. Manual smoke checklist

1. Подтвердить отдельные test bot, venue и test database/Redis.
2. Убедиться, что `GOOGLE_ORDER_SUBMISSION_ENABLED=false`.
3. Проверить `/health/live` и `/health/ready`.
4. Registration: valid/invalid/decline/switch/unauthorized.
5. Text: item, quantity, missing quantity, comment, cart, remove, new order.
6. Voice: same semantic phrases, visible action, quantity, failed transcription.
7. Photo: list, order quantity, packaging/title numbers, empty quantity.
8. Callback: fresh/stale revision, candidate, pagination, review, confirm/cancel.
9. NOT_FOUND, duplicate, unit mismatch и ambiguous candidate.
10. Проверить логи без token/key/secret и без реальной отправки поставщику.

## 11. Verdict

**READY_FOR_MANUAL_TESTING_WITH_KNOWN_NONBLOCKERS**

Основание: full suite и critical fake smoke зелёные, P0/P1 blocker на текущем
HEAD не воспроизведён, search safety и replay/lease protections подтверждены.
Known nonblockers — отсутствие live ASR/vision/Telegram smoke и внешних
провайдерских эффектов в тестовом окружении. Следующая единственная кампания:
`CONTROLLED HUMAN PILOT` по этому checklist; новую архитектурную декомпозицию и
history implementation не начинать.
