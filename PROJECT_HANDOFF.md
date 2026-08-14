# PROJECT HANDOFF

Актуально для ветки `decompose_bot` после добавления запросов к истории заявок
2026-08-14. Git SHA текущей версии документа нужно получать командой
`git rev-parse HEAD`.

## Текущая задача

Завершить проверенный функциональный этап запросов к истории заявок и перейти
только к подготовке контролируемого Telegram pilot. Новую общую декомпозицию и
production refactor не начинать.

Подробный verdict и scores:
[`docs/INDEPENDENT_ENGINEERING_AUDIT.md`](docs/INDEPENDENT_ENGINEERING_AUDIT.md).

## Подтверждённые факты

- Ветка: `decompose_bot`.
- Remote: только GitHub `origin` → `Dzmitry-Radziuk/test_bot`.
- Полный suite после HISTORY-02: `1444 passed` (без падений; запуск с
  локальным `--basetemp`, один предупреждающий `PytestCacheWarning` не связан с
  приложением).
- Mypy: `155 source files, no issues`.
- Ruff check: pass; Ruff format: `306 files already formatted`.
- Compileall и `git diff --check`: pass.
- После refresh Markdown checker проверил 38 файлов.
- Scenario catalog вырос до 41 содержательного сценария; все mappings
  проверяются официальным generator.
- `.env` не tracked; значения не читались.
- Repository scan не нашёл строк, похожих на Telegram/OpenAI keys.
- Изменения текущего этапа ограничены маршрутизацией прерываний, presentation и
  связанными regression-тестами; схема БД, callbacks, отправка заявок и поставщики
  не менялись.
- Для прерванных modal-вопросов состояние хранит стек контекстов: новый
  независимый товар получает приоритет, вложенные прерывания возвращаются в порядке
  `C → B → A`, а товары одного сообщения сохраняют исходный порядок. Черновик и
  порядок позиций не переставляются.
- При возобновлении вопроса Telegram показывает реальное число активных
  нерешённых позиций с правильной русской формой слова и текущим выделенным
  названием; для проблем, не связанных с количеством, вопрос о количестве не
  добавляется.
- Граница товара и комментария уточняется по однозначному совпадению с активным
  черновиком. Неоднозначная граница приводит к безопасному уточнению; текст и
  голос используют один и тот же результат.
- Декоративные emoji удаляются только из подписей Telegram-кнопок; callback-данные,
  порядок строк и разрешённые стрелки не меняются. Мягкие предупреждения используют
  `🔸`, а `⛔` сохранён только для действительно блокирующего отказа в доступе.
- Telegram UX polish: названия товаров в карточках и списках экранируются и
  выделяются жирным, первые заголовки карточек — жирным подчёркиванием; порядок
  кнопок, callback-протокол, state-machine, база данных и семантика отправки не
  изменены.
- Для текста добавлен короткий промежуточный статус перед разбором; голос, фото и
  callback используют специализированные статусы. Callback сначала подтверждается
  и получает отключённую клавиатуру, после чего временная карточка заменяется
  результатом. Существующие revision/idempotency/lease-проверки сохранены.
- Добавлены channel-neutral contracts для вопросов о поставках: детерминированный
  parser распознаёт высокоуверенные русские формулировки, свободные варианты
  проходят существующий структурированный AI fallback через `ParsedInputSchema`,
  а text и voice используют один маршрут. Запрос истории не превращается в
  ADD_ITEMS и не меняет modal-контекст; местоименный товар принимается только при
  единственном безопасном товарном контексте.
- Источник истории строго ограничен листом Google Sheets с точным именем
  `История`. `GoogleHistoryRepository` выполняет один venue-scoped read за запрос;
  каталог, черновик, база данных и лист `История товары(API)` не используются.
- Историческая строка сохраняет evidence заявки, поставщика, стадии и даты для
  каждого товара из поля `Список товаров`. Для вопроса о том, приехал ли товар,
  остаются видимыми доставленные и завершённые строки; для будущей даты они
  исключаются. Стадия `Доставлено` классифицируется отдельно от `Завершена`,
  отмена остаётся доступной для текущего статуса, а неизвестные стадии не
  угадываются. Год в дате без года берётся из переданных часов приложения и его
  часового пояса.
- Запросы истории обслуживаются `HistoryQueryService` и отдельным Telegram
  presenter; существующая команда «Мои заявки» и её путь не заменены.

Финальные post-doc проверки зелёные. Documentation impact checker после commit
также прошёл; код истории сопровождается обновлённым каталогом сценариев.

## Что сейчас работает

- Telegram webhook authentication и durable inbox deduplication.
- Same-chat renewable lease, update sequencing и stale-owner fencing.
- Общий text/transcribed-voice semantic pipeline.
- Единая `StateCompatibilityPolicy` перед contextual modal fallback.
- Quantity, unit mismatch, ambiguous candidate, not-found, duplicate, comment,
  manual/product-add, review, submit, failure и new-order modal contexts.
- Source-evidence reconciliation для product identity, order quantity, packaging и
  comments.
- Удаление комментариев всей заявки одинаково распознаётся из текста и голосовой
  транскрипции, очищает только комментарии активных позиций и сохраняет товары и
  связи с каталогом.
- Новые regression-тесты покрывают вложенные прерывания, динамическое количество
  уточнений, границу комментария для текста/голоса и очистку подписей кнопок.
- Conservative catalog shortlist и deterministic auto-select safety gates.
- Venue isolation и повторная проверка доступа до внешних effects.
- Submission lifecycle с read-back, recalc/dispatch uncertainty и at-most-once
  completion notification gate.
- Neutral conversation input/action contracts и architecture guards.

## Что не доказано или требует среды

- Реальный Telegram/ASR/vision/Google/Redis/PostgreSQL fault run в этой кампании не
  выполнялся.
- Hosted CI не проверялся.
- Production performance на 100 000 товаров не измерена; тест подтверждает только
  bounded `CatalogSearch` interface.
- DEPLOY-01 подтверждён локально 2026-08-14: `alembic/env.py` загружает модели
  через `restaurant_bot.persistence.alembic`, а не через отсутствующий
  `restaurant_bot.db_models`. Цепочка ревизий непрерывна до `0008`, `alembic check`
  сообщает об отсутствии новых операций. Существующая БД успешно прошла два
  последовательных `upgrade head`; обычный `docker compose up -d --build`
  завершил `migrate` с кодом 0, после чего `api` и `worker` стали healthy.
  Отдельная временная PostgreSQL без общего volume также прошла два `upgrade head`
  и содержит ровно пять текущих ORM-таблиц. Production и внешние записи не
  проверялись.
- Внешняя supplier dispatch должна оставаться выключенной для pilot.
- Token rotation из незакрытого `SECURITY.md` checklist не подтверждена репозиторием.
- Production alerts, backup/restore и rollback drill описаны, но не доказаны текущим
  запуском.

## Критические архитектурные правила

1. AI предлагает структуру; source text подтверждает факты; catalog уточняет identity;
   deterministic code разрешает действие.
2. State хранит контекст уже понятого сообщения и не определяет смысл следующего.
3. Text и voice проходят global interpretation до contextual fallback.
4. Новый сильный intent может прервать modal flow, не наследуя quantity/comment/
   candidate старого item.
5. Callback остаётся явным UI-путём с проверкой revision.
6. Доступ к venue повторно проверяется перед order/status/product-add effects.
7. Неизвестный результат внешнего POST не повторяется автоматически.
8. `services` содержит защищённые effect/runtime coordinators, а не общий core.
9. Production catalog пока list-backed; перед 100k нужен indexed scoped provider и
   benchmark.
10. Legacy `EngineResult`/`BotReply` bridge сохраняется до реального второго канала.

## PRIORITY ROADMAP — НЕ ПОТЕРЯТЬ

State-machine migration по принципу

```text
TEXT / VOICE
→ GLOBAL PARSING
→ STATE COMPATIBILITY POLICY
→ CONTINUE / INTERRUPT / AMBIGUOUS / REJECT
→ state handler или обычный routing
```

завершена для запланированных modal contexts и защищена regression suite. Следующий
приоритет — не новая policy и не декомпозиция, а эксплуатационное подтверждение
получившейся системы.

## NEXT FUNCTIONAL STEP

**Одна следующая задача: CONTROLLED HUMAN TELEGRAM PILOT PREPARATION.**

До pilot:

1. подтвердить отзыв старого Telegram token и отдельный test bot;
2. подготовить отдельные test venue, PostgreSQL, Redis и Google Sheets;
3. проверить `GOOGLE_ORDER_SUBMISSION_ENABLED=false`;
4. применить migrations и проверить health;
5. пройти live text/voice/photo/callback/Sheets smoke;
6. проверить redaction, `uncertain` events и операторскую остановку.

## ARCHITECTURAL REFACTOR STATUS

Общая декомпозиция остановлена. Следующие долги оплачиваются только по trigger:

- legacy reply bridge — при втором канале;
- indexed catalog backend — перед большим каталогом;
- engine/orchestrator/submission/prompt extraction — при изменении соответствующего
  flow и отдельном characterization proof;
- transactional outbox — перед broad production или при наблюдаемом duplicate reply/
  task delivery.

## Источники истины

- [`README.md`](README.md) — назначение, запуск, owners и runtime.
- [`docs/CURRENT_ARCHITECTURE.md`](docs/CURRENT_ARCHITECTURE.md) — текущие границы.
- [`docs/TESTING_READINESS.md`](docs/TESTING_READINESS.md) — automation/live readiness.
- [`docs/user-scenarios/scenarios.json`](docs/user-scenarios/scenarios.json) —
  канонический каталог сценариев.
- [`docs/INDEPENDENT_ENGINEERING_AUDIT.md`](docs/INDEPENDENT_ENGINEERING_AUDIT.md) —
  независимые scores, gaps и findings.
- `docs/archive/architecture/ARCHITECTURE_DECOMPOSITION.md`,
  `docs/archive/data-integrity/` и остальные block
  reports — исторические snapshots, не текущий roadmap.
