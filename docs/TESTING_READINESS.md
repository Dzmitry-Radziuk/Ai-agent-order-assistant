# Готовность к тестированию

Текущий документ относится к ветке `decompose_bot` и независимому аудиту
2026-08-13. Актуальная Git-ревизия — commit, содержащий этот файл; её нужно получать
командой `git rev-parse HEAD`. Production-код аудита совпадает с исходным SHA
`4dc67430336bb90cfad731aca583f51a19948a48`.

Документ не заменяет исполняемый код, каталог сценариев или исторический
[`FULL_REGRESSION_AUDIT.md`](archive/regression/FULL_REGRESSION_AUDIT.md).

## Текущий baseline

| Проверка | Свежий результат |
|---|---|
| Full pytest до обновления docs | `1391 collected / 1391 passed` |
| Full pytest после обновления docs | `1391 passed` за `18.78 s` |
| Mypy | `139 source files, no issues` |
| Ruff check | pass |
| Ruff format check | `252 files already formatted` |
| Compileall | pass |
| Markdown links после новых docs | `36 files, pass` |
| Scenario source/generation до обновления | `34 scenarios, pass` |
| Scenario source/generation после обновления | `40 scenarios, pass` |
| `git diff --check` | pass |
| Реальные Telegram/OpenAI/Google/Redis effects | не выполнялись |
| `.env` | не tracked; значения не читались |

После документных изменений весь набор повторно выполнен. Documentation impact
checker запускается после commit, потому что сравнивает Git-объекты. Итоговые
результаты также зафиксированы в
[`INDEPENDENT_ENGINEERING_AUDIT.md`](INDEPENDENT_ENGINEERING_AUDIT.md) и
`PROJECT_HANDOFF.md`.

## Что доказывает automation

- Все 1391 теста проходят локально на Python 3.12.
- Architecture guards проверяют направление core-зависимостей, отсутствие циклов,
  нейтральный conversation contract и разрешённые compatibility seams.
- Text, расшифрованный voice и semantic callback используют общий state policy.
- Негативные и modal-routing тесты защищают от ошибочного quantity, comment,
  candidate и independent-intent перехвата.
- Concurrency/failure-injection тесты покрывают inbox deduplication, renewable lease,
  stale-owner fencing, submission lifecycle и at-most-once notification gates.
- Catalog test с 100 000 строк доказывает возможность bounded provider на границе
  `CatalogSearch`, но не измеряет production performance.
- После консолидации intentional overlap оставлен только там, где один invariant
  доказывается на разных слоях или каналах.

Локальные fake/stub тесты не подтверждают доступность реальных провайдеров, качество
ASR/OCR и фактический сетевой timing.

## Каталог пользовательских сценариев

Канонический источник —
[`user-scenarios/scenarios.json`](user-scenarios/scenarios.json), представление —
[`USER_SCENARIOS.md`](USER_SCENARIOS.md). В каждой из 40 карточек явно указаны:

- `AUTOMATED_PASS` — локальный контракт подтверждён связанными тестами;
- `AUTOMATED_PARTIAL` — основной контракт проверен, внешний live-контур нет;
- `MANUAL_LIVE_REQUIRED` — подтверждение возможно только в живом окружении;
- `NOT_IMPLEMENTED` — функция ещё отсутствует.

Текущий каталог не выдаёт fake provider за live-доказательство. Все 40 сценариев
связаны с существующими и содержательно релевантными pytest-функциями; dangling
references отсутствуют.

## Готовность каналов

### Текст

Parsing, global intent, modal compatibility, catalog resolution, draft и submission
decisions полностью покрыты локальным suite. Для pilot остаётся end-to-end Telegram
smoke в изолированном окружении.

### Голос

После транскрипции используется общий semantic pipeline; quantity/comment recovery и
route parity покрыты fake/stub тестами. Реальные аудио, основная и резервная ASR-модели
требуют `MANUAL_LIVE_REQUIRED`.

### Фото

Structured vision payload, отделы, зачёркивание, packaging/title numbers и отсутствие
заказанного количества покрыты тестами. Реальные печатные и рукописные фотографии
требуют `MANUAL_LIVE_REQUIRED`.

### Callback

Revision, candidate, pagination, comment scope, review и confirmation callbacks
проверены автоматически. Старый callback не мутирует новый черновик.

### Другие каналы

Нейтральные contracts и fake-channel proof существуют, но MAX/Web/REST adapters не
реализованы. Их статус нельзя считать `IMPLEMENTED AND TESTED`.

## Готовность внешних контуров

| Контур | Репозиторное доказательство | Что ещё нужно live |
|---|---|---|
| Telegram | transport tests, callback revision, reply checkpoints | webhook, send/edit, сетевые сбои и повтор ответа |
| OpenAI text/voice/vision | structured schemas, retries, reconciliation, fake payloads | реальная модель, ASR и фотографии |
| Google Sheets | mapping, read-back, access и failure tests | тестовая таблица, latency, quota, 403/429 и перерасчёт |
| Redis/Celery | lease/fencing и task lifecycle tests | process kill, network partition и нагрузка |
| PostgreSQL | migrations, locks, repositories и transaction tests | backup/restore drill и production-like load |
| Catalog 100k | bounded interface characterization | indexed provider, memory/latency/recall benchmark |
| Supplier dispatch | durable uncertainty protocol | только отдельный безопасный environment; для pilot выключен |

## Условия контролируемого Telegram pilot

До pilot обязательно:

1. Подтвердить отзыв старого Telegram token, упомянутого в `SECURITY.md`, и выпуск
   отдельного test token; значения в репозиторий не записывать.
2. Использовать отдельные test bot, venue, PostgreSQL, Redis и Google Sheets.
3. Подтвердить `GOOGLE_ORDER_SUBMISSION_ENABLED=false`.
4. Применить Alembic migrations и проверить `/health/live` и `/health/ready`.
5. Выполнить live smoke регистрации, текста, голоса, фото, callback, записи и
   перерасчёта без отправки поставщику.
6. Проверить redaction логов и доступ оператора к `uncertain` lifecycle events.
7. Назначить ответственного за остановку pilot и ручную сверку неизвестного результата.

## Архивные snapshots

| Кампания | HEAD | Исторический результат |
|---|---|---|
| Block 6C | `669b5cff05ef959cc2ded5a21f14054f9aa737ab` | `1377/1377`, `READY_FOR_MANUAL_TESTING_WITH_KNOWN_NONBLOCKERS` |
| Block 6D | `6299a84` → `c1cc92e6` | `1378/1378` → `1382/1382`, `SERVICES_FINAL_FREEZE` |

Эти строки сохраняют историю и не являются текущим baseline или планом работ.
