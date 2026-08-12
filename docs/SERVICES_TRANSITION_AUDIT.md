# Аудит переходного слоя services/ и результат Block 5K

## Block 5R — presentation formatting вынесен

`escape` и `format_number` удалены из transitional `services/text.py` и
переведены на `presentation/telegram/formatting.py`. Caller audit подтвердил
нулевые старые imports; `presentation/telegram/submission.py` больше не
зависит от `services`. Остальные measurement и parsing symbols `services.text`
не переносились.

## Block 5Q — выполненный перенос Telegram submission presenter

`submission_presenter.py` удалён из `services/` после repository-wide caller-аудита и
перенесён без изменения тела функций в `presentation/telegram/submission.py`.
Канонический модуль содержит только presentation/read-model formatting и callback
строки; его formatting dependency — `presentation/telegram/formatting.py`.
`SubmissionService`, `ConversationEngine` и тесты импортируют новый путь.

## Block 5P — выполненный pure voice transcript seam

Проверенный перенос завершён: два pure-алгоритма `has_supported_voice_letters` и
`select_transcription_result` теперь находятся только в
`input/voice_transcript_policy.py`. `services/input_recognition.py` импортирует их
как канонический owner и сохраняет все provider, Telegram, state-aware retry и
progress обязанности. Динамических или production callers старых class methods
не найдено; orchestrator wrappers и тестовые вызовы переведены на новый импорт.
Поведенческое сравнение с исходным SHA дало `MISMATCHES=0`.

## Границы

Аудит выполнен на ветке decompose_bot, исходный SHA аудита:
d1eb9981e91a75a70931b4a3fd5b5e1dc6a8eec6. Рабочий код Python в этом блоке не
изменялся. Пакет services/ рассматривается по ADR-015 как переходный слой,
а не как целевая архитектура. Вызовы проверены по src, tests, api, workers и
cli.py; динамические границы проверены по orchestrator/tasks и callback-фасадам.
Этот SHA обозначает исходную ревизию аудита. d8fbced775aeb0685a49e2ae52be53e74c1ecaf8
остаётся принятым audit baseline; фактический starting SHA Block 5I:
1a6bf0da753976303b196c57f65a36316bc15685.
Документ уточнён в Block 5NC после Block 5N; starting SHA этой коррекции:
c2119d1adcad543d3833f3818e3cf9c9442ebcbc. Python-код и тесты в коррекции не
изменяются.

## Состав пакета

Файлы верхнего уровня внутри `src/restaurant_bot/services/`:
engine.py, input_recognition.py, orchestrator.py, order_review.py, parser.py,
product_add_flow.py, replies.py, submission_presenter.py, submission.py, text.py,
venue_registration.py.

`src/restaurant_bot/services/conversation_handlers/`:
__init__.py, candidate_selection.py, comment_scope.py, final_review.py,
navigation.py, pending_quantity.py.

Вне `services/` уже находятся канонические владельцы `input/telegram.py` и
`parsing/comment_policy.py`; `catalog/resolver.py` и остальные модули `catalog/`
также являются действующими owners своих catalog-ответственностей. В бывших
переходных путях obsolete facades удалены: `services/comment_policy.py`,
`services/catalog_resolver.py`, `services/matching.py` и старые
`services/conversation_handlers/{modal_routing,state,state_compatibility}.py`.

## Классификация файлов

| МОДУЛЬ | ТЕКУЩИЙ ВЛАДЕЛЕЦ / ВЫЗОВЫ | ЭФФЕКТЫ / СВЯЗНОСТЬ | СТАТУС | ЦЕЛЬ / ДЕЙСТВИЕ | ПРИОРИТЕТ |
|---|---|---|---|---|---|
| services/catalog_resolver.py | Удалён в Block 5J после нулевого caller-аудита | Только catalog; внешних эффектов не было | Удалённый compatibility facade | catalog.resolver / DONE | P2 |
| services/matching.py | Удалён в Block 5J после нулевого caller-аудита | Только core; мутаций не было | Удалённый compatibility facade | catalog/* и conversation quantity / DONE | P2 |
| services/comment_policy.py | Удалён в Block 5I; до переноса callers: catalog, conversation, parsing, AI, engine | Чистая parsing/evidence policy, внешних эффектов нет | Удалён после audit | parsing/comment_policy.py / DONE | P1 |
| catalog/resolver.py | Канонический resolver; callers: engine и catalog pipeline | Чистый catalog resolution, без Telegram effects | DONE вне services | Сохранять owner; не возвращать facade | P2 |
| catalog/evidence.py, retrieval.py, scoring.py, safety.py | Канонические catalog-этапы retrieval/evidence/scoring/safety | Чистый catalog core | DONE вне services | Сохранять разделение этапов | P2 |
| parsing/comment_policy.py | Каноническая supplier-comment policy; callers parsing, catalog, conversation, AI, engine | Чистый parsing/evidence policy | DONE вне services | Сохранять единственного owner | P1 |
| engine.py | ConversationEngine: handle, routing, modal actions, duplicate/comment/product-add/submission preparation, catalog callers | Изменяет ConversationState/CartItem; строит replies | Смешанный координатор state-machine; catalog core перенесён в 5G | application/conversation и owners conversation / SPLIT | P4 |
| input/telegram.py (вне services/) | Telegram raw payload в TelegramEvent; callers: api и orchestrator | Связь с Telegram/raw update; внешних эффектов нет | Канонический input adapter | DONE | P3 |
| input_recognition.py | Скачивание файла, OpenAI voice/photo recognition, visible actions, progress | Telegram + provider + state-aware prompts и побочные progress effects | Смешанный input adapter | input/recognition и channel progress / SPLIT | P3 |
| orchestrator.py | Claim/lease, access, session, recognition, parsing, catalog, engine, checkpoints, delivery, review/analytics | DB/Redis/Sheets/OpenAI/Telegram/Celery effects | Координатор application смешан с use cases | application/update_pipeline и use cases / SPLIT | P5 |
| order_review.py | Review snapshot, stale token, preview, submit handoff | Redis/DB/Sheets/Telegram | Review use case смешан с presentation | submission/review и presentation / SPLIT | P4 |
| parser.py | infer_intent и parse_callback; imports владельцев parsing command | Чистый parsing; callback — channel contract | Частичный facade и реальный public owner | parsing/commands и input/callback / SPLIT | P3 |
| product_add_flow.py | Request ID, prompt, clear pending; callers: engine | Небольшой state helper и presentation text | Связный временный владелец | orders/product_add и presentation / SPLIT | P4 |
| replies.py | BotReply renderers, cards, keyboards, issue/candidate/status text | Читает state, агрегирует display данные, строит callbacks | Presentation с остаточными расчётами | presentation/telegram replies / SPLIT | P4 |
| presentation/telegram/submission.py | Submission/status/recovery/history rendering | Чистая presentation и callbacks | Владелец presentation | DONE в Block 5Q | P3 |
| submission.py | Submit, read-back, checkpoints, catalog/recalc, dispatch fencing, completion, product-add write | DB/Redis/Sheets/Telegram effects | Смешанный сервис с safety-критичными операциями | submission/service, catalog, dispatch / SPLIT | P5 |
| text.py | Cleanup, normalization, units/departments, ranges, number words, conversion, numeric parse, HTML/number formatting | Pure, но с большим fan-in в lower layers и presentation | Смешанный core/presentation primitive owner | parsing text, domain units, presentation formatting / SPLIT | P2 |
| venue_registration.py | Directory, invite, access registry, binding, context, replies | HTTP/Redis/DB/Sheets и access mutation | Смешанный venue service | venues/directory, access, registration / SPLIT | P5 |
| services/conversation_handlers/candidate_selection.py | Adapter к conversation.selection; callers engine/tests | Читает state, возвращает EngineResult/reply | Корректный adapter | conversation routing / KEEP_TEMP | P3 |
| services/conversation_handlers/comment_scope.py | Проверка и применение pending scope; callers engine/tests | Мутирует comments/stage, строит replies | State/presentation adapter; core в conversation/comments | conversation routing / KEEP_TEMP | P3 |
| services/conversation_handlers/final_review.py | Final guards и подготовка submission | Мутирует stage/issue, строит reply | Адаптер review | conversation/review / MOVE later | P4 |
| services/conversation_handlers/modal_routing.py | Удалён в Block 5J после перевода тестового import | Эффектов не было | Удалённый compatibility facade | conversation.routing / DONE | P2 |
| services/conversation_handlers/navigation.py | Passive replies и paging истории | Мутирует history view state, ставит async request | Смешанный navigation/history adapter | conversation navigation + history use case / SPLIT | P4 |
| services/conversation_handlers/pending_quantity.py | Parser ответа количеством и мутация CartItem | Мутация modal state, зависимости parser/text | Адаптер state количества | conversation quantity flow / MOVE later | P3 |
| services/conversation_handlers/state.py | Удалён в Block 5J после нулевого caller-аудита | Эффектов не было | Удалённый compatibility facade | conversation.state.queries / DONE | P2 |
| services/conversation_handlers/state_compatibility.py | Удалён в Block 5J после перевода тестовых imports | Эффектов не было | Удалённый compatibility facade | conversation.routing / DONE | P2 |
| services/conversation_handlers/__init__.py | Маркер действующего handlers-пакета | Нет | Маркер пакета | Оставить для действующих handlers / KEEP | P4 |

## Граф зависимостей

Production-импорты из `services/` (точные прямые edges текущего дерева):

- `workers/tasks.py` → `services/orchestrator.py`, `services/submission.py`;
- `cli.py` → `services/venue_registration.py`;
- `integrations/openai_client.py` → `services/parser.py`, `services/text.py`;
- `services/engine.py` → `services/conversation_handlers/*`, `parser.py`,
  `product_add_flow.py`, `replies.py`, `submission_presenter.py`, `text.py`;
- `services/input_recognition.py` → `services/parser.py`, `services/text.py`;
- `services/orchestrator.py` → `services/engine.py`, `input_recognition.py`,
  `order_review.py`, `parser.py`, `text.py`, `venue_registration.py`;
- `services/order_review.py` → `services/replies.py`, `text.py`,
  `venue_registration.py`;
- `services/replies.py` → `services/text.py`;
- `services/submission.py` → `services/replies.py`,
  `submission_presenter.py`, `text.py`, `venue_registration.py`;
- `services/submission_presenter.py` → `services/text.py`;
- `services/venue_registration.py` → `services/text.py`;
- `services/conversation_handlers/*` → соответствующие `services/replies.py`,
  `services/parser.py`, `services/text.py`.

Отдельно от этого графа: `api/app.py` → `input/telegram.py`. Это внешний
канонический input-адаптер и не зависимость `api` от переходного пакета
`services/`. `services/orchestrator.py` также импортирует `input.telegram` для
общего TelegramEvent-контракта; этот edge не считается импортом из `services/`.

Фактические прямые зависимости lower/core → `services/`:

- `catalog/evidence.py`, `catalog/safety.py` → `services/text.py`;
- `conversation/comments.py`, `conversation/draft.py` → `services/text.py`;
- `orders/catalog_resolution.py` → `services/text.py`;
- `parsing/ai/{comment_reconciliation,item_reconciliation,quantity_reconciliation,shadow_items}.py`
  → `services/text.py`;
- `parsing/commands/{dialogue,item_commands,navigation,router}.py` →
  `services/text.py`;
- `parsing/{packaging,products,quantities}.py` → `services/text.py`;
- `integrations/google_sheets.py` → `services/text.py`;
- `integrations/openai_client.py` → `services/parser.py`, `services/text.py`.

`catalog/resolver.py` импортирует `parsing/comment_policy.py` и
`text_normalization.py`, а `parsing/comment_policy.py` импортирует
`restaurant_bot.text_normalization`; утверждение `parsing/comment_policy →
services/text` для текущего дерева ложно. Остаточная нижнеуровневая зависимость
от `services` сохраняется через перечисленные callers `services/text.py` и
требует отдельного audit. `services/orchestrator.py` не импортирует
`workers.tasks`; AST-проверка подтверждает `services.orchestrator →
workers.tasks = 0`.
Зависимости presentation от старых services use cases допустимы до отдельного
блока application/presentation.

`application/background_tasks.py` содержит только Protocol фоновых эффектов.
`services/orchestrator.py` зависит от этого порта и не импортирует workers.
`workers/tasks.py` реализует порт через Celery `.delay`; поэтому внешнее
направление `workers/tasks → orchestrator` сохраняется, а обратный импорт
устранён.

## Карта ответственности text.py

Полный symbol-level caller/duplicate audit Block 5L вынесен в
[`docs/SERVICES_TEXT_AUDIT.md`](SERVICES_TEXT_AUDIT.md). Он не переносит production-код
и назначает ровно один следующий seam только для `clean_text` и `normalize_text`.

- Общая очистка: clean_text, normalize_text; lower/core callers в parsing/catalog/conversation.
- Очистка поиска и комментариев: remove_phrase_overlap, remove_global_comment_overlap; catalog и conversation evidence.
- Единицы: UNIT_ALIASES, normalize_unit, convert_quantity; primitives количества в domain/parsing.
- Отделы: DEPARTMENT_ALIASES, normalize_department; сопоставление заказа и Sheets.
- Числовой разбор: numeric_range_spans, to_float, parse_number_words, NUMBER_WORDS; parsing, AI, catalog и Sheets.
- Представление: format_number и escape; replies, submission, review и venue.
Не переименовывать файл целиком в common/text.py: кластеры имеют разные owners и
требуют отдельного caller audit.

## Карта ответственности parsing/comment_policy.py

explicit_supplier_comment распознаёт явный маркер пожелания; supplier_comment_start
находит начало подтверждённой инструкции; comment_semantic_key удаляет дубли
инструкций. Это чистая parsing/evidence policy, не state и не UI. Дубликата тех же
regex в conversation/comments, parsing/comment_scope и AI reconciliation не найдено.
Текущий implementation owner — parsing/comment_policy.py; собственного
implementation в services больше нет.

### Точный caller-аудит до и после Block 5I

До MOVE прямые production-импорты символов `services.comment_policy` были
проверены по рабочему дереву. Найдены пять lower/core-модулей:

- `src/restaurant_bot/catalog/resolver.py` — `supplier_comment_start`;
- `src/restaurant_bot/parsing/products.py` — `explicit_supplier_comment`;
- `src/restaurant_bot/parsing/packaging.py` — `explicit_supplier_comment`;
- `src/restaurant_bot/conversation/comments.py` — `comment_semantic_key`;
- `src/restaurant_bot/parsing/ai/comment_reconciliation.py` —
  `explicit_supplier_comment`, `supplier_comment_start`.

Отдельно `src/restaurant_bot/services/engine.py` импортирует
`supplier_comment_start`. Это transitional coordinator, поэтому он не считается
lower/core caller, но входит в общий итог: шесть production-модулей с прямым
импортом. Прямых тестовых импортов этих символов нет; тест
`tests/catalog/test_catalog_resolver.py` использует метод
`CatalogResolver.split_explicit_supplier_comment`, а не `comment_policy`.
Re-export и динамических callers для этого модуля не найдено.

После MOVE повторный repository-wide поиск
`restaurant_bot.services.comment_policy` по `src` и `tests` дал `0` импортов.
Символы `explicit_supplier_comment`, `supplier_comment_start` и
`comment_semantic_key` имеют единственного implementation owner:
`src/restaurant_bot/parsing/comment_policy.py`. Все шесть production callers
переведены на новый путь; test, dynamic и re-export callers старого пути равны
нулю.

## Карта ответственности на уровне символов

| КЛАСТЕР | ТЕКУЩЕЕ СОСТОЯНИЕ | ЦЕЛЬ / ДЕЙСТВИЕ | ПОЧЕМУ |
|---|---|---|---|
| ConversationEngine.handle и contextual routing | services/engine.py | application/conversation + conversation/routing / SPLIT | Одна state-machine entry координирует много modal flows |
| Engine quantity/unit и duplicate/comment/product-add actions | engine и handlers | conversation quantity/draft/comments, orders/product_add / SPLIT | Изменение state остаётся в coordinator |
| Engine catalog methods | engine wrappers | orders/catalog_resolution / KEEP_TEMP facade | Владелец подтверждён Block 5G |
| Orchestrator process, claim, checkpoint | orchestrator.py | application/update_pipeline + repositories / SPLIT | Надёжность и порядок side effects |
| Orchestrator review/analytics | orchestrator.py | application use cases / SPLIT | Независимые use cases |
| text core normalization | text.py | parsing/domain/catalog / SPLIT | Большой fan-in, алгоритмы сохраняются |
| text presentation formatting | text.py | presentation/formatting / MOVE later | HTML и числовой вывод |
| SubmissionService.submit | submission.py | submission/service.py / SPLIT | Порядок checkpoints |
| Submission catalog/recalc | submission.py | submission/catalog.py | Read-back и uncertainty |
| Submission dispatch/completion | submission.py | submission/dispatch.py | At-most-once delivery |
| Venue directory/access/registration | venue_registration.py | venues/directory, access, registration / SPLIT | Разные security contracts |
| Input voice/photo | input_recognition.py | input/recognition + channel progress / SPLIT | Смешаны provider и Telegram effects |
| Replies renderers | replies.py | presentation/telegram / SPLIT | UX/callback contract |

## Block 5O — результат targeted audit input recognition

Полный symbol/caller/effect audit `services/input_recognition.py` зафиксирован в
[`docs/INPUT_RECOGNITION_AUDIT.md`](INPUT_RECOGNITION_AUDIT.md). Весь класс не
переносится целиком: он одновременно содержит Telegram download/cleanup, OpenAI
voice/photo, state-aware retry, visible actions и progress presentation.

Ровно один следующий code seam — чистая voice transcript acceptance policy:
`has_supported_voice_letters` и `select_transcription_result` в
`input/voice_transcript_policy.py`. `requires_high_accuracy_transcription`,
`voice_transcription_prompt`, visible actions, progress и provider calls в этот
seam не входят. Production code в Block 5O не изменялся.

## Результат удаления obsolete facades в Block 5J

До удаления все пять кандидатов были проверены repository-wide: production и
dynamic/importlib callers отсутствуют, callbacks и monkeypatch paths к ним не
обращаются. Тестовые imports переведены на реальные owners, assertions не
изменялись. Удалены `catalog_resolver.py`, `matching.py`,
`conversation_handlers/modal_routing.py`, `state.py` и
`state_compatibility.py`. `parser.py` не является facade целиком: `infer_intent`
и `parse_callback` остаются public owners до отдельного callback/text contract
block.

## План следующих переносов

### СЕЙЧАС

Сохранить текущие services adapters и owners Block 5G. Block 5I выполнил
comment-policy seam, Block 5J — только удаление доказанных test-only facades;
другие production-переносы не выполняются.

### NEXT — только после external review

Block 5L завершил отдельный caller/duplicate audit, а Block 5M завершил его единственный
следующий seam: `clean_text` и `normalize_text` перенесены в semantic-neutral
`text_normalization.py`; старые callers переведены, а оставшиеся функции `services/text.py`
используют private imports. Полная таблица callers, риски и исключение старого варианта
`parsing/text.py` зафиксированы в [`docs/SERVICES_TEXT_AUDIT.md`](SERVICES_TEXT_AUDIT.md).
Block 5I стартовал с SHA 1a6bf0da753976303b196c57f65a36316bc15685.

### ПОЗДНЕЕ

Точечный split text.py (сначала core normalization/unit tables, затем presentation);
input boundary; parser callback/text; conversation handlers; review/product-add;
reliability-sensitive orchestrator/submission/venue seams.

### ФИНАЛЬНАЯ ОЧИСТКА

Obsolete facades catalog_resolver.py, matching.py и modal/state facades удалены
в Block 5J после нулевого caller-аудита. `services/comment_policy.py` удалён
ранее в Block 5I. Не удалять services искусственно: application coordinator,
adapters и доказанные public facades могут временно оставаться.

## Dependency inversion фоновых задач

До Block 5K `workers/tasks.py → services/orchestrator.py` и локальные imports в
`UpdateOrchestrator._enqueue_side_effects()` создавали обратный цикл. Теперь
`application/background_tasks.py` владеет минимальным Protocol, orchestrator
получает его через constructor dependency, а `CeleryBackgroundTaskDispatcher`
в workers вызывает неизменённые Celery tasks. Repository-wide проверка после
изменения даёт `services.orchestrator → workers.tasks = 0` и сохраняет
`workers.tasks → services.orchestrator`.

## Что намеренно не делалось

В Block 5J изменены только тестовые imports, удалены пять re-export модулей и
обновлены audit-документы. В Block 5K добавлены application task port,
минимальный Celery adapter, constructor wiring и regression tests dispatch/lease;
алгоритмы callers, services/text.py, prompts, catalog thresholds, AI, callbacks,
DB, Sheets, Docker, task bodies, decorators, names, retry settings и UX не
менялись. Block 5I и Block 5G не переоткрывались; новый ADR не добавлялся.
