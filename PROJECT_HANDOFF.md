# PROJECT HANDOFF

Актуально для ветки `decompose_bot` после исправления границы сложных каталожных
позиций 2026-08-16. Git SHA текущей версии документа нужно получать командой
`git rev-parse HEAD`.

## Текущая задача

Завершить проверенный функциональный этап PRODUCT-BOUNDARY-01: отделить границы
сложной каталожной позиции, фасовку, количество заказа и комментарий. После этого
пересобрать контейнеры и перейти только к подготовке контролируемого Telegram
pilot. Новую общую декомпозицию и production refactor не начинать.

Подробный verdict и scores:
[`docs/INDEPENDENT_ENGINEERING_AUDIT.md`](docs/INDEPENDENT_ENGINEERING_AUDIT.md).

## Подтверждённые факты

- Ветка: `decompose_bot`.
- Remote: только GitHub `origin` → `Dzmitry-Radziuk/test_bot`.
- Полный suite после PRODUCT-BOUNDARY-01: `1533 passed` (без падений; запуск с
  локальным `--basetemp`, один предупреждающий `PytestCacheWarning` не связан с
  приложением).
- Mypy: `159 source files, no issues`.
- Ruff check: pass; Ruff format: `316 files already formatted`.
- Compileall и `git diff --check`: pass.
- После refresh Markdown checker проверил 38 файлов.
- Scenario catalog вырос до 41 содержательного сценария; все mappings
  проверяются официальным generator.
- `.env` не tracked; значения не читались.
- Repository scan не нашёл строк, похожих на Telegram/OpenAI keys.
- Изменения текущего этапа ограничены venue-level маршрутизацией истории,
  channel-neutral ответом и связанными regression-тестами; схема БД, callbacks,
  отправка заявок и поставщики не менялись.
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
- LIVE-HISTORY-03A: в обычном интерфейсе «Мои заявки» технический номер заявки
  скрыт в списке, кнопке и заголовке деталей; вместо него показывается дата
  создания. История и статусы заявок используют общий человекочитаемый formatter,
  прямой ответ по истории начинается с товара, а прошедшая плановая дата явно
  помечается как прошедшая без вывода о фактической доставке. Внутренний поиск по
  номеру заявки, callback-протокол и исходные доказательства не изменены.
- LIVE-HISTORY-04: общие вопросы о поставках текущего заведения распознаются как
  `VENUE_DELIVERIES` с пустым `product_queries`, не попадают в каталог или not-found
  и не изменяют черновик. Формулировка с человеком или ролью не создаёт товар или
  поставщика; ответ показывает только доказанные строки листа «История» и отдельно
  сообщает, если источник не подтверждает, кто физически привезёт поставку.
- PROD-SEMANTICS-01: явные комментарии существующего товара распознаются как
  `EDIT_COMMENT` без обращения к каталогу; общая конструкция «товар и товар всё по
  N единиц» создаёт отдельные позиции с общей quantity и хвостовым пожеланием;
  ответы области комментария для текста и голоса проходят детерминированную
  проверку до AI; отмена уточнения сохраняет ожидающие товары и продолжает их
  добавление; `clear_all` удаляет пользовательские комментарии активных позиций,
  сохраняя каталожные данные; bare quantity разрешён только в modal
  `AWAIT_UNIT_QUANTITY`, а выбор кандидата остаётся отдельным контекстом.
- После PROD-SEMANTICS-01 полный suite: `1477 passed`; mypy, Ruff, форматирование,
  Markdown links, каталог сценариев, compileall и `git diff --check` прошли.
- PRODUCT-BOUNDARY-01: запятая больше не считается границей товара без отдельного
  доказательства; цепочки `число единица/число единица` и `число единица/кор` остаются
  признаками каталога. Полная исходная строка сохраняется в `source_line`, а каталог
  участвует в окончательной сверке количества и комментария. Неподтверждённое
  `quantity_source` не авторизует заказ автоматически; независимые photo/order-entry
  источники сохраняются. Добавлены шесть regression-тестов для pepper, mustard,
  повторяющихся чисел и смешанных комментариев.
- После коммита PRODUCT-BOUNDARY-01 выполнен `docker compose up -d --build`:
  `migrate` завершился с кодом 0, `api` и `worker` имеют состояние healthy,
  `beat` запущен, PostgreSQL и Redis healthy. Объёмы и Docker-конфигурация не
  изменялись.

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
- COMMENT-01: локальные и общие комментарии разделены через provenance
  `CartItem.order_comment_fragments`. Удаление общих комментариев удаляет только
  подтверждённые общие фрагменты и сохраняет локальные пожелания; старые состояния
  без provenance обрабатываются без разрушительных догадок. Явная цель товара не
  может незаметно превратиться в общий комментарий, а операции комментариев всегда
  возвращают обычный `Черновик заявки` с кнопками.
- ROUTING-01: слова о доставке сами по себе не определяют `history_query`.
  История требует семантики вопроса о существующей поставке; доказанное добавление
  товара и явное изменение комментария имеют приоритет. Даты и слова «привезти»/
  «доставить» в пожелании являются данными команды, а не самостоятельным intent.
  Это правило подтверждено для текста и транскрипции голоса; покрытие всех
  возможных разговорных формулировок русского языка не заявляется.
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

## PROD-SEMANTICS-01 — CORRECTIVE BLOCK

Исправлены оставшиеся границы семантики комментариев и количества:

- формы удаления комментария с предлогами «о», «об» и «про» остаются `EDIT_COMMENT`;
- явный общий комментарий без товаров проходит как `EDIT_COMMENT` области `order`, а не как `ADD_ITEMS`;
- область комментария разрешается только среди позиций текущего состояния: поддержаны named, ordinal, all, order и cancel; неоднозначный выбор безопасно приводит к уточнению;
- голосовой транскрипт использует тот же `TelegramInputInterpreter.interpret_text`, что и текст;
- bare quantity в `AWAIT_UNIT_QUANTITY` поддерживает `MISSING_QTY`, `UNIT_MISMATCH` и `DUPLICATE_PENDING`, применяя ожидаемую единицу каталога; явная единица в `UNIT_MISMATCH` сохраняет путь high-accuracy разбора;
- candidate modal по-прежнему владеет числовым выбором вне quantity-modal;
- комментарии, область комментария, история и quantity-modal завершаются до catalog resolution.

Проверка после corrective-block: `1489 passed`, focused semantic/comment/voice набор — `89 passed`, mypy — `158 source files, no issues`, Ruff check и format — pass, Markdown links — `38 files`, scenario catalog — `41 сценарий`, compileall и `git diff --check` — pass.

Изменения ограничены parsing, input interpretation, semantic routing, comment scope и regression tests. Схема базы данных, Alembic, callback protocol и Docker-конфигурация не менялись.

## COMMENT-SCOPE-02 — EXISTING-ITEM COMMENT SCOPE

Исправлен жизненный цикл уточнения комментария для уже существующих позиций:

- активный scope определяется единым channel-neutral `has_pending_comment_scope`;
  обязательны стадия `AWAIT_COMMENT_SCOPE`, текст комментария и хотя бы одна
  действующая цель — активный ID существующей позиции или ожидающая новая позиция;
- пустой `pending_comment_items` больше не закрывает scope, если в состоянии
  сохранены действующие existing item IDs; пропущенные и устаревшие IDs не считаются
  целью;
- ответы `для всех товаров`, named и `только для последнего` применяются к
  существующим товарам без повторного `ADD_ITEMS` и без обращения к каталогу;
  `для всей заявки` сохраняет `order_comment_fragments`, а отмена не меняет корзину;
- смешанный existing + pending-new поток сохраняет прежнее добавление новых позиций:
  локальный групповой комментарий применяется до обычного catalog resolution,
  order provenance не создаётся для групповой области;
- прямые команды комментария разделяют GROUP и ORDER: групповой суффикс удаляется
  в parsing owner и не создаёт order provenance, заявочный суффикс сохраняет
  provenance всей заявки;
- text и voice используют один deterministic scope resolver, поэтому ответы области
  комментария не вызывают дополнительный AI scope call.

После COMMENT-SCOPE-02 полный suite: `1501 passed`; новые regression-тесты — `12`.

## MODAL-AUTHORITY-01 — GENERAL STATE-AWARE MODAL ROUTING

Исправлена граница между предложением глобального parser и авторизацией
действия активным modal-контекстом:

- quantity modal (`MISSING_QTY`, `UNIT_MISMATCH`, `DUPLICATE_PENDING`) сначала
  проверяет строгую форму ответа: короткое число, число с единицей, разговорную
  оболочку и контейнерную единицу. Произвольное числительное внутри фразы больше
  не меняет количество; неполное числительное остаётся безопасным уточнением.
- текст и транскрибированный голос используют один `PendingQuantityHandler`;
  «пять», «пусть будет пять», «три штуки» и «пять коробок» относятся к текущей
  позиции, а явные независимые команды могут прервать modal.
- `SELECT_CANDIDATE` разрешён только при текущем `AMBIGUOUS` item; вне candidate
  context голое число не создаёт выбор кандидата.
- `StateCompatibilityPolicy` принимает единственное решение о `CONTINUE`,
  `INTERRUPT` и `AMBIGUOUS` для quantity/duplicate/unit-mismatch контекстов.
- доказанный независимый ADD_ITEMS в `AWAIT_ADD_MORE_CONFIRM` не теряется из-за
  отсутствия совпавшей visible action; неуверенный no-op не показывает сообщение
  об успешном добавлении.
- provenance каталожной фасовки и количества заказа сохранён: regression для
  названия с `180 г` и отдельного заказа `500 г` проходит.

Проверки после этапа: полный pytest `1527 passed`, mypy `159 source files, no
issues`, Ruff check/format, Markdown links (`38 файлов`), каталог сценариев (`41`),
compileall и `git diff --check` — успешно. Изменены только маршрутизация modal,
quantity provenance/reconciliation и regression-тесты; DB/Alembic/Docker/History
source/callback protocol не менялись, новых AI-вызовов не добавлено.
Mypy, Ruff check/format, Markdown links, каталог сценариев, compileall и
`git diff --check` прошли. База данных, Alembic, Docker, History и callback protocol
в этом этапе не менялись.

## PRODUCT-QUANTITY-PROVENANCE-01 — КОЛИЧЕСТВО ЗАКАЗА И ЧИСЛА КАТАЛОГА

- Единый владелец `reconcile_order_quantity_evidence` отделяет числовые признаки
  identity каталога от подтверждённого количества заказа.
- Размеры, фасовка, диапазоны и дроби из имени товара не создают количество
  `CartItem`; остаётся только остаточное, подтверждённое исходной фразой количество.
- Text и voice сходятся после разбора. Выбор кандидата сначала проверяется в
  текущем списке кандидатов; явное независимое добавление прерывает modal fallback,
  а данные старой позиции не переносятся в новую.
- Подтверждены сценарии горчицы с `450 мл` и `1/6`, а также огурцов с размерами,
  фасовкой и остаточным `5 шт`; ранее доказанное количество и единица сохраняются.
- Регрессионные проверки provenance и candidate routing проходят. Полный suite
  собран на `1510` тестах: `1509 passed`, один известный baseline failure —
  `tests/quantity/test_unit_handling.py::test_packaging_in_name_and_order_weight_are_kept_separate`.
  База данных, Alembic, Docker, History и дополнительные AI-вызовы в этом этапе
  не менялись.
