# Передача проекта

## CURRENT STATUS — MULTI-CHANNEL ARCHITECTURE CAMPAIGN

Стартовый проверенный SHA: `1cabab401a1c8aa67c2ccddd7ded95b6c7689288`, ветка
`decompose_bot`, origin — GitHub. Свежий baseline до кампании:
`1382 collected / 1382 passed`; после добавления neutral-contract proof:
`1386 passed`. `.env` не tracked и не читался, GitLab не использовался.

Канально-нейтральная граница теперь представлена контрактами
`application/conversation/contracts.py` и use case
`application/conversation/use_case.py`. Telegram adapter находится в
`input/telegram.py`, а Telegram renderer — в
`presentation/telegram/conversation.py`. `UpdateOrchestrator` переводит Telegram
в `ConversationInput`, вызывает общий use case и сохраняет прежний durable
порядок claim → lease → state checkpoint → reply checkpoint → tasks.

Proof-тесты `tests/application/test_conversation_application.py` показывают
текстовый заказ и modal follow-up через fake-channel input без создания
`TelegramEvent`. Architecture guards дополнительно проверяют, что application
contracts не знают Telegram, presentation или infrastructure.

Текущий итоговый verdict: `MULTI_CHANNEL_ARCHITECTURE_READY_WITH_PROTECTED_DEBT`.
Защищены `ConversationEngine` (stateful routing и legacy `EngineResult`/`BotReply`),
`UpdateOrchestrator` (Telegram delivery protocol), submission/review/venue
координаторы и media recognition. Они не размножают business rules для будущих
каналов; их полный перенос требует отдельного proof checkpoint и не выполнялся.
Подробная инструкция расширения: [`docs/CHANNEL_EXTENSION_GUIDE.md`](docs/CHANNEL_EXTENSION_GUIDE.md).

Оценка готовности: Telegram isolation 3/5, MAX 3/5, Web 3/5, REST API 3/5,
third-party integrations 4/5, onboarding 4/5, testability 4/5, dependency clarity
4/5. Это readiness общей границы, а не утверждение, что новые каналы уже
реализованы.

## CURRENT STATUS — ARCHITECTURE FINALIZATION

Проверенный текущий HEAD: `6299a84`, ветка
`decompose_bot`, origin — GitHub. Свежий baseline до acceptance-тестов:
`1378 collected / 1378 passed`; финальный suite после двух новых характеристик:
`1382 collected / 1382 passed`. Baseline `1378/1378` снят на исходном Block 6D
HEAD `2fb1e6e03e5f889ba5034da013facd555fea138f4`. Ruff, format, mypy, markdown links, compileall и
`git diff --check` проходят. `.env` не tracked и не читался; GitLab не
использовался.

Block 6D закрыл acceptance gaps «удали все комментарии» и локальной коррекции
«не X, а Y». Финальная архитектурная кампания вынесла
`VenueContext`/`RegistrationResult` в application contract и добавила
architecture guards. Тесты подтверждают сохранность cart, identities,
quantity/unit, catalog bindings и независимых пожеланий. Архитектурный статус:
`ARCHITECTURE_FINALIZED_WITH_PROTECTED_DEBT`.

## NEXT STEP

Архитектурная кампания завершена. Live Telegram, ASR, vision и Google остаются
`MANUAL_LIVE_REQUIRED`; новые каналы, history implementation, DB migrations и
search redesign не начинать.

## IMPLEMENTED AFTER 5Z / BLOCK 6A

Выполнено поведенчески нейтральное выделение Telegram input interpretation.
Класс `TelegramInputInterpreter` в
`src/restaurant_bot/input/telegram_interpretation.py` теперь владеет выбором
пути для callback, text, voice и photo. Из `UpdateOrchestrator` вынесены
`_parse`, `_parse_text_in_context`, `_parse_sheet_review_command`,
`_parse_pending_comment_scope`, `_match_visible_action` и
`_needs_visible_action_ai`; lazy-фабрика `_recognizer()` оставлена в
orchestrator. Порядок callback/text/media, StateCompatibilityPolicy,
транзиентные ошибки провайдера и видимые действия не изменены.

Метрики после переноса: `orchestrator.py` — 1888 строк, 49 функций/методов;
новый интерпретатор — 330 строк, 3 класса, 11 функций/методов. Исторический
suite после переноса: `1377 collected / 1377 passed`; текущий Block 6D baseline и
финальный suite указаны в разделе `CURRENT STATUS — BLOCK 6D`.
Коммит extraction: `c6feeb7`.

### Block 6D — завершённая acceptance closure

Архитектурная декомпозиция и Block 6C cleanup завершены. `services/text.py` удалён
после механического переноса `to_float` в `parsing/numeric.py`; lower/core → services
edge из AI reconciliation устранён. Stateful/effectful services и handlers оставлены
защищёнными. Block 6D добавил только два acceptance-теста и минимальную
коррекцию comment mutation и два architecture guard-теста; финальный suite:
`1382 collected / 1382 passed`.
Подробный аудит: [`docs/SERVICES_TRANSITION_AUDIT.md`](docs/SERVICES_TRANSITION_AUDIT.md).
Решение: `SERVICES_CLEANUP_PARTIAL_WITH_PROTECTED_ADAPTERS`.
Следующий шаг: `CONTROLLED HUMAN PILOT`. Не начинать новую декомпозицию,
history implementation или search redesign.

## POST-6A REASSESSMENT / BLOCK 6B

Анализ выполнен на HEAD `c3bde2ff1229d9850dcb8dd21543331956f20794`, ветка
`decompose_bot`, свежий полный suite: `1377 collected / 1377 passed` за `16.19 s`.
`UpdateOrchestrator` — `1888` строк, `49` функций/методов; input boundary
`TelegramInputInterpreter` подтверждён как отдельный owner без циклических
импортов и без DB/Redis/Telegram/Sheets/engine/checkpoint зависимостей.

Решение Block 6B: **ORCHESTRATOR_DECOMPOSITION_SUFFICIENT**. Полная inventory,
caller/effect/risk матрица, score matrix кандидатов A–I и сохранённый coordinator
pipeline находятся в [`docs/UPDATE_ORCHESTRATOR_AUDIT.md`](docs/UPDATE_ORCHESTRATOR_AUDIT.md).
Application code и tests в Block 6B не изменялись.

## CURRENT ARCHITECTURE — Block 5Y

### Block 5Z — forensic audit of UpdateOrchestrator

Detailed analysis-only inventory, pipeline, safety protocol and seam decision is
documented in [`docs/UPDATE_ORCHESTRATOR_AUDIT.md`](docs/UPDATE_ORCHESTRATOR_AUDIT.md).
The current baseline is `1377 collected / 1377 passed`; claim/lease/checkpoint
remain one durable coordinator boundary. The only next candidate campaign is
input interpretation extraction; checkpoint, lease, review, registration and
catalog AI remain protected.

Block 5Y завершён поверх опубликованного `fc39d6a13c6557f24816f9787ce3d14ee5b0985a`;
текущий полный regression suite: `1377 collected / 1377 passed`.
`ContextualCommandPolicy` теперь предоставляет публичные методы
`normalize_pre_modal_voice()`, `reinterpret_contextual_command()` и
`is_generic_show_products_command()`. Engine вызывает их в прежнем порядке:
отрицание → количество → страницы черновика → страницы финальной проверки →
статус заявки → voice. Telegram callback page parsing находится в
`input/telegram_visible_actions.py`; `contextual_commands.py` не импортирует
TelegramEvent, input/presentation/integrations/services и не содержит `v2:*`-парсинг.

`CatalogResolutionService` — единственный production owner `match_item`,
`apply_catalog` и `refresh_cart_order_values`; engine-фасады и тестовые обращения
переведены на canonical service. `CandidateSelectionHandler` оставлен `KEEP_TEMP`
как presentation adapter с callback/revision/reply контрактами. Quantity actions
не выделялись: подтверждено **NO SAFE QUANTITY ACTION SEAM** без риска изменить
Block C semantics. Алгоритмы catalog matching, provenance, callbacks, state
serialization и submission lifecycle не менялись.
После удаления obsolete catalog facades engine содержит `1613` строк, `76431` байт
и `33` метода; это уменьшение относится только к переходным wrappers, не к алгоритму.

В Block 5Y draft mutations перенесены в `conversation/draft_actions.py`, comment
mutations — в cohesive операции `conversation/comments.py`, progression state
transition остался в `conversation/progression.py`, а Telegram rendering вынесен в
`presentation/telegram/progression.py`. Exact transient reset теперь принадлежит
`conversation/state/transitions.py`. Engine уменьшен до `1535` строк, `72673` байт и
`30` методов; state-mutating methods: `18 → 13`. До/после state comparisons дали
`MISMATCHES = 0`; handle ordering и stale callback guard сохранены.

`_build_item`, `_spoken_quantity`, `_cart_page`, `_callback_item_index` оставлены
тонкими adapters; `_spoken_quantity` имеет production caller в orchestrator.
Quantity cluster, new-order lifecycle, product-add и submission остаются protected.

### Block 5Y — проверка границ и callers

Новый architectural regression test проверяет отсутствие Telegram transport/protocol
зависимостей в contextual policy. Полный suite и focused catalog/routing проверки
зелёные; targeted mypy и ruff проходят. Историческая глобальная проверка
`ruff format --check .` по-прежнему может показывать только ранее известный
`docs/RAPID_INPUT_CONCURRENCY_ANALYSIS.md`; изменённые файлы форматированы.

ENGINE_PHASE_ACCEPTABLE: ConversationEngine остаётся authoritative ordering
coordinator без внешних эффектов; следующий крупный target может быть
UpdateOrchestrator. Единственная следующая кампания после отдельного одобрения:
analysis-only audit UpdateOrchestrator перед любым переносом кода.

### Block 5X — завершённые seams

Block 5X опубликован коммитом `fc39d6a13c6557f24816f9787ce3d14ee5b0985a`; ниже
сохраняется его owner-map как историческая база Block 5Y.

### Block 5W — завершённые seams

Block 5W выполняется поверх `af78e13f3e8bac9eacc474e016ca3f7f083432fd` на ветке
`decompose_bot`. Базовая проверка до изменений: `1369 collected / 1369 passed`.
Историческое значение `1366 passed` относится к завершённому Block 5U и не является
текущим baseline.

### Block 5W-A — исправление владельцев Block 5V

Завершено и опубликовано коммитом `adf475128ad973dc6b97a3e37d741f6f50b078bf`.
Нейтральный контракт заведения находится в `venues/contracts.py`, нормализация кодов —
в `venues/codes.py`; Telegram input/presentation импортируют только эти примитивы,
а интеграционные directory/access registry остаются владельцами внешних источников.
Знание о fallback-моделях OpenAI перенесено в
`integrations/openai_transcription_policy.py`; input-policy больше не определяет
провайдерскую эвристику.

### Block 5W-C/E — первые безопасные seams ConversationEngine

Контекстные команды принадлежат `conversation/routing/contextual_commands.py`.
`ConversationEngine` только вызывает эту policy в прежнем порядке и не передаёт ей
Telegram presentation, provider или infrastructure зависимости. Построение
`CartItem` принадлежит чистому seam `conversation/item_intake.py`; engine сохраняет
тонкий `_build_item`-адаптер для существующего внутреннего контракта.

Размер engine уменьшен с `117719` до `79555` байт и с `2563` до `1680` строк, число методов
`ConversationEngine` — с `63` до `42`. Сохранён порядок `handle()`: enrichment,
voice normalization, modal/stale-callback guards, recovery и confirmation flows,
contextual reinterpretation, global routing, mutation и progression. `_prepare_submission`
и внешние submission-эффекты не переносились.

### Block 5W-D/F/G — границы и нерешённые seams

New-order lifecycle (`_fresh_order_state`, `_start_new_order`,
`_resume_after_new_order_confirmation`) оставлен в engine как единый stateful seam:
отдельный перенос не доказал бы безопасного нового владельца сериализованного state.
`_spoken_quantity` сохранён как compatibility facade, потому что его вызывает
production analytics path в `services/orchestrator.py`; wrappers без production callers
удалены после caller-аудита. Дополнительный seam, кроме contextual policy и item intake,
признан небезопасным: **NO SAFE EXTRA SEAM**.

Block 5W опубликован коммитами `adf475128ad973dc6b97a3e37d741f6f50b078bf` и
`5cabc5bd56ffc1f1884860a877493dc81d590e3c`; его seams являются базой для Block 5X.

## ARCHIVED ARCHITECTURE SNAPSHOT — Block 5V

Последний завершённый блок — Block 5U. Актуальные владельцы:

| Ответственность | Канонический модуль |
|---|---|
| Text semantic parser | `parsing/commands/api.py` |
| Telegram callback parser | `input/telegram_callbacks.py` |
| Основные Telegram replies | `presentation/telegram/replies.py` |
| Review contracts | `application/order_review/contracts.py` |
| Review snapshot и fingerprint | `application/order_review/snapshot.py` |
| Review token | `application/order_review/token.py` |
| Review preview и submission replies | `presentation/telegram/order_review.py` |
| Review side-effect coordinator | `services/order_review.py` |
| Telegram page-size constants | `presentation/telegram/pagination.py` |
| Pure voice recognition policy | `input/voice_policy.py` |
| Venue directory adapter | `integrations/venue_directory.py` |
| Venue access registry adapter | `integrations/venue_access_registry.py` |
| Venue registration input | `input/telegram_venue_registration.py` |
| Venue registration presentation | `presentation/telegram/venue_registration.py` |
| Venue registration coordinator | `services/venue_registration.py` |

`services/parser.py`, `services/replies.py` и `services/product_add_flow.py`
удалены после caller-аудита. `presentation/telegram/*` только читает
`ConversationState`; onboarding и нормализация страниц выполняются в
`conversation/state/transitions.py` и conversation/engine handlers. Исторические блоки ниже помечаются как архивные и
не являются текущей картой владельцев.

## Block 5V — pagination, input recognition и venue registration

Block 5V-A завершён: размеры страниц Telegram (`20`) принадлежат
`presentation/telegram/pagination.py`; state transitions получают явный
`page_size` и больше не знают Telegram-значение. Cart и final-review callbacks
сохранили прежнюю арифметику и callback-контракт.

Block 5V-B завершён: pure policy `match_visible_action`,
`voice_transcription_prompt`, `requires_high_accuracy_transcription`,
`has_distinct_models` вынесены в `input/voice_policy.py`. Сервис
`InputRecognitionService` сохранён как media/provider/state-aware coordinator;
visible actions остаются contextual fallback и не стали глобальным parser-ом.
Prompt-тексты и voice/photo pipeline не менялись.

Block 5V-C завершён: `VenueDirectory` и его GViz/cache primitives находятся в
`integrations/venue_directory.py`, `VenueAccessRegistry` — в
`integrations/venue_access_registry.py`, Telegram registration input — в
`input/telegram_venue_registration.py`, а чистые ответы — в
`presentation/telegram/venue_registration.py`. `VenueRegistrationService`
сохраняет DB/Sheets/cache/rollback coordinator; `VenueContext` и
`RegistrationResult` намеренно остаются его переходным контрактом. Service
re-exports сохранены для совместимости старых callers.

Focused проверка Block 5V-C: `63 passed`; полные quality gates выполняются после
обновления owner-документации. БД, Alembic, DevOps, prompts, OpenAI schemas,
catalog thresholds, callbacks, serialized state и submission protocol не
изменялись.

## Block 5U — выполненная декомпозиция

5U-A удалил obsolete `services/parser.py`; все 28 тестовых импортных групп
переведены на canonical parsing/callback owners, полный suite остался зелёным.
5U-B убрал три мутации из Telegram presentation и добавил regression-тест
read-only контракта. 5U-C вынес frozen review contracts, чистый snapshot и token
в `application/order_review/`, а preview и submission reply builders — в
`presentation/telegram/order_review.py`. `OrderReviewService.submit()` сохранил
lease/DB/Sheets/Telegram порядок и остался координатором внешних эффектов.
5U-D подтвердил, что candidate selection и comment scope — thin adapters к
`conversation/` core, final review владеет page transition, а pending quantity
сохраняется без переноса из-за риска изменить Block C quantity semantics.
Отдельная state-boundary функция `normalize_cart_page` теперь сохраняет clamp
страницы до вызова Telegram presenter; presenter больше не исправляет state.

## Block 5S — контролируемая декомпозиция `services/text.py`

Block 5S завершён как поведенчески нейтральный перенос четырёх независимых
seam-групп. Единицы и пересчёт находятся в `domain/units.py` и
`domain/unit_conversion.py`; отделы — в `domain/departments.py`; словесные
числительные и диапазоны — в `parsing/number_words.py` и
`parsing/numeric_ranges.py`. Политики overlap разделены по владельцам:
`conversation/comments.py` отвечает за комментарии, а `catalog/evidence.py` —
за временную поисковую копию. `source_query`, `product_query`, `comment` и
`comment_source` при этом не изменяются поисковыми функциями.

В `services/text.py` оставлен только `to_float`. Он используется одновременно
Google Sheets и AI-reconciliation и допускает форматы внешнего листа; безопасный
единый новый owner для этого смешанного контракта не доказан, поэтому файл не
удалялся. Старых импортов перенесённых символов не осталось. Полный regression
baseline после переноса: `1366 collected / 1366 passed`; `ruff check`, формат,
`mypy`, Markdown links, `compileall` и `git diff --check` проходят. БД, Alembic,
Docker, prompts, state-machine semantics и тестовые assertions не менялись.
Следующий блок автоматически не назначается.

## Block 5R — Telegram presentation formatting

Block 5R завершён механическим переносом `escape` и `format_number` из
`services/text.py` в канонический `presentation/telegram/formatting.py`.
Новый модуль зависит только от `text_normalization.clean_text` и стандартных
библиотек. Все production callers переведены, старых импортов нет; остальные
symbols `services.text` не менялись. Сравнение с исходными реализациями:
`escape` — 14 случаев, `format_number` — 16 случаев, `MISMATCHES=0`.

## Block 5Q — перенос Telegram submission presenter

Block 5Q завершён механическим переносом `services/submission_presenter.py` в
`presentation/telegram/submission.py`. Все 28 функций и их сигнатуры сохранены;
production и test callers переведены на новый путь, старый services-файл удалён.
Presenter остаётся чистым presentation/read-model слоем: он возвращает `BotReply`,
`Button` и текст, не выполняет DB, Redis, Sheets, OpenAI, TelegramClient или
submission side effects. Formatting dependency направлена на
`presentation/telegram/formatting.py`; временной зависимости от `services` нет.
Сравнение старого и нового модуля на 23 representative cases дало `MISMATCHES=0`.
Следующий seam после Block 5Q не назначается автоматически.

## Block 5P — чистая политика выбора транскрипции

Block 5P завершён поведенчески нейтрально. Функции `has_supported_voice_letters` и
`select_transcription_result` имеют единственного владельца
`src/restaurant_bot/input/voice_transcript_policy.py` и зависят только от `re` и
`text_normalization.normalize_text`. `InputRecognitionService` сохранил orchestration
голоса, фото, retry, visible actions и progress; он вызывает новый pure-модуль.
Тесты переведены на канонический импорт, старые test-only wrappers удалены.
Сравнение старой и новой реализации на corpus дало `MISMATCHES=0`; полный baseline
после переноса: `1366 collected / 1366 passed`. Следующий seam после Block 5P не
назначается до отдельного review.

## 1. Назначение

Это production Python-бот для закупок ресторана в Telegram. Он принимает текст,
голос, фото и callback, ведёт редактируемый черновик, сопоставляет товары с
каталогом выбранного заведения и после подтверждения записывает заявку в Google
Sheets.

AI помогает понять свободную речь и найти кандидатов. Детерминированный код
владеет состоянием, проверками безопасности, идемпотентностью, хранением и
внешними эффектами.

## 2. Текущий checkout

- Репозиторий: `Dzmitry-Radziuk/test_bot`.
- Ветка: `decompose_bot`.
- Semantic baseline: `f9cbc3195c0eae843de3208e488c3f46baa5a5ec`.
- Accepted Block 3 code baseline: `e4fb29e4d2d0ba906c91beef5c02d3e87d7a0b09`.
- Block 3C correction baseline: `21358755ebbc36b95b9fb6b4799021027a2158e7`.
- Accepted Block 4 code baseline: `1583d0ee17f94948f6bc110ebcc03a5463a18af9`.
- Текущий Git HEAD всегда определяется командой `git rev-parse HEAD`, а не
  фиксируется в handoff после каждого commit.
- Единственный рабочий remote: GitHub `origin/decompose_bot`.
- Текущий полный baseline: `1369 collected / 1369 passed` после трёх
  архитектурных regression-тестов Block 5U.
- `manual_smoke_forensic_logs.txt` — необязательный локальный diagnostic artifact,
  не tracked-файл репозитория. Если он существует локально, его нельзя менять,
  удалять или добавлять в commit; отсутствие файла нормально.

## 3. Основной pipeline

```text
Telegram update
  -> webhook и inbox
  -> Celery delivery
  -> UpdateOrchestrator
  -> авторизация и lease чата
  -> нормализация text/voice/photo
  -> глобальный ParsedCommand
  -> reconciliation исходного текста
  -> StateCompatibilityPolicy
  -> ConversationEngine и CatalogResolver
  -> изменение ConversationState
  -> checkpoint сессии и ответ Telegram
```

Подтверждение заявки — отдельная граница: свежий снимок, запись в лист, read-back,
пересчёт, optional dispatch и контрольная точка уведомления.

## 4. Владельцы текущих модулей

### Domain и persistence

- `domain/models.py` — Intent, ParsedCommand, ExtractedItem, CartItem,
  ConversationState, ответы, submission-контракты и сериализованные enum.
- `db.py`, `db_models.py` — SQLAlchemy и схема.
- `repositories/updates.py` — inbox и идемпотентная последовательность update.
- `repositories/sessions.py` — версионированное состояние диалога.
- `repositories/submissions.py` — checkpoints и idempotency отправки.
- `repositories/order_events.py`, `venue_bindings.py` — аудит и привязки
  пользователей/чатов к заведению.

### Ввод и разбор

- `input/telegram.py` — канонический адаптер Telegram raw update -> `TelegramEvent`.
- `services/input_recognition.py` — transitional voice/photo recognition; Block 5O
  подтвердил смешение transport/provider/policy/presentation и назначил единственный
  следующий seam `input/voice_transcript_policy.py` для transcript selection.
- `parsing/commands/api.py` — глобальный intent parser и enrichment; callback
  contract находится в `input/telegram_callbacks.py`.
- `parsing/products.py` — orchestration разбора товарных строк и сборка
  итогового списка `ExtractedItem`.
- `parsing/quantities.py` — короткие ответы количества и quantity primitives.
- `parsing/packaging.py` — фасовка, диапазоны и catalog measurement parsing.
- `parsing/comment_scope.py` — явная область общего комментария.
- `services/text.py` — transitional `to_float`; единицы, отделы, числительные,
  диапазоны и overlap находятся в канонических domain/parsing/catalog/conversation owners.
- `parsing/ai/schemas.py` — structured AI schemas без алгоритмов.
- `parsing/ai/quantity_reconciliation.py` — reconciliation количеств, фасовки и диапазонов.
- `parsing/ai/comment_reconciliation.py` — provenance и comment bindings.
- `parsing/ai/item_reconciliation.py` — source qualifier cleanup и восстановление позиций.
- `parsing/ai/shadow_items.py` — shadow projections и ссылочные дубли.
- `parsing/ai/reconciliation.py` — единый порядок AI reconciliation.
- `integrations/openai_parsing.py` — compatibility re-export facade без алгоритмов.
- `integrations/openai_prompts.py` — неизменяемые prompt-контракты.

### Каталог и диалог

- `catalog/evidence.py` — каноническое представление, токены, query/catalog
  evidence и сопоставление supplier hint.
- `catalog/scoring.py` — детерминированная оценка одного товара.
- `catalog/retrieval.py` — bounded in-memory enumeration, admission и порядок
  кандидатов.
- `catalog/safety.py` — конфликты квалификаторов, numeric compatibility, safe
  equivalence, broad-category policy и auto-select safety.
- `catalog/resolver.py` — поиск в supplier scope и чистое решение
  `CatalogDecision` без изменения `ConversationState`.
- `conversation/quantity_resolution.py` — единый channel-neutral владелец
  кратности заказа: `nearest_valid_multiple`, расчёт рекомендации и
  предупреждения без изменения черновика.
- `orders/supplier_minimums.py` — единый channel-neutral владелец агрегации
  минимальных сумм поставщиков и структурированных предупреждений без мутации
  состояния.
- `orders/catalog_resolution.py` — единый channel-neutral владелец применения
  результата `CatalogResolver` к `CartItem`: каталожные поля, quantity
  reconciliation, comment provenance, статусы и refresh черновика.
- `catalog/evidence.py`, `catalog/retrieval.py`, `catalog/safety.py` и
  `catalog/scoring.py` — канонические владельцы catalog matching; прежние
  `services/matching.py` и `services/catalog_resolver.py` удалены в Block 5J.
- `conversation/selection.py` — channel-neutral score, targeting и выбор
  кандидата; не знает о ParsedCommand или callback semantics.
- `conversation/progression.py` — channel-neutral progression core: выбор
  следующей нерешённой позиции, issue mapping и смена stage без presentation.
- `services/conversation_handlers/` — quantity, candidate, comment scope,
  review, navigation и единая StateCompatibilityPolicy.
- `services/engine.py` — transitional state machine и orchestration caller;
  применение решения каталога делегируется `orders/catalog_resolution.py`, а
  остальные переходы черновика, comments, quantity, duplicate flow и подготовка
  submission остаются в engine.
- Полный audit оставшегося transitional `services/`, dependency direction и
  caller-backed roadmap находится в `docs/SERVICES_TRANSITION_AUDIT.md`.
- `presentation/telegram/replies.py` — карточки, клавиатуры и пользовательские
  тексты без мутации `ConversationState`.

### Application и фоновые задачи

- `application/background_tasks.py` — типизированный порт четырёх фоновых
  эффектов без зависимости от Celery.
- `services/orchestrator.py` — использует этот порт через явную constructor
  dependency; worker-модули больше не импортируются из orchestration-кода.

### Внешние эффекты

- `services/submission.py` — запись, checkpoints, пересчёт, dispatch и статусы.
- `services/venue_registration.py` — каталог заведений, доступ и регистрацию.
- `integrations/telegram.py` — Telegram transport.
- `integrations/google_sheets.py` — каталог и листы заведения.
- `integrations/cache.py` — catalog cache и Redis locks.
- `workers/tasks.py` — Celery tasks и adapter реализации application-порта.

## 5. Неприкосновенные инварианты

1. AI предлагает структуру, а источник пользователя и каталог подтверждают её.
2. `product_query` и supplier `comment` могут намеренно содержать одну
   характеристику одновременно.
3. Количество заказа отдельно от фасовки, веса упаковки, диапазона и размеров.
4. Retrieval, ranking и auto-select — разные решения; слабый токен не даёт
   право выбрать другой товар.
5. Текст и голос после транскрибации используют один semantic pipeline.
6. Сначала выполняется global parsing, затем contextual modal fallback.
7. Сильное независимое намерение может прервать старый modal state, не перенося
   quantity/comment в новый товар.
8. Комментарий поставщику требует подтверждённого происхождения.
9. Draft mutation и submission идемпотентны и привязаны к заведению.
10. Старый callback revision не изменяет новый черновик.
11. Ненайденный товар не заменяется похожим автоматически.

## 6. Защищённые границы

Без отдельного разрешения не менять Docker, deployment, CI/CD, `.env`,
секреты, миграции, схему БД, serialized state, Telegram UX/callback,
prompts, matching thresholds, submission contracts, Google Sheets,
Redis locking, workers и API entrypoints.

GitLab не используется. В этой задаче разрешён только GitHub.

## 7. Известные ручные acceptance-проблемы

Это текущие нерешённые acceptance-проблемы:

- после вопроса о количестве для «хлеб» фраза «новый товар» не должна стать
  количеством хлеба;
- «удали все комментарии» не должна очищать товары;
- исправление комментария «не X, а Y» должно менять только исправленную часть.

## HISTORICAL ARCHITECTURE TIMELINE

Разделы ниже сохраняют историю завершённых блоков. Текущие owners и текущий
статус Block 5U указаны выше и имеют приоритет.

## 8. Статус декомпозиции

Block 0 завершён: зафиксированы владельцы, dependency rules, compatibility
facades и порядок миграции в `docs/ARCHITECTURE_DECOMPOSITION.md`.

Block 1 завершён механически: реализация product parser находится в
`parsing/products.py`; позднее text command API переехал в
`parsing/commands/api.py`.
Поведение, prompts, state machine, matching, UX, persistence и deployment не
менялись. Focused и полный regression baseline проходят.

Block 2A завершён: из `products.py` вынесены три доказанных независимых
кластера — quantities, packaging и explicit global comment scope. Старый
`services/product_parser.py` удалён после полного import audit: callers старого
пути не найдены.

Block 2B завершён механически: text command parsing разделён по ответственностям
в `parsing/commands/`; Block 5U позже удалил obsolete parser facade после caller-аудита.
`parse_callback()` оставлен отдельным channel contract. Поведение подтверждено
сравнением на 62 существующих случаях и полным baseline `1362 passed`.

Block 3 завершён механически: structured AI schemas и reconciliation разделены
по ответственности в `parsing/ai/`. `integrations/openai_parsing.py` оставлен
совместимым re-export facade на 39 строк; OpenAI transport остаётся в
`integrations/openai_client.py`. Поведение подтверждено focused AI suite
`220 passed` и полным baseline `1362 passed`.

Сохранены инварианты: Google Sheets остаётся source of truth; PostgreSQL,
pgvector, catalog migrations и Sheets sync в Block 3 не реализовывались.
AI предлагает структуру, source phrase подтверждает, catalog уточняет,
детерминированный код выполняет действие.

Block 4 завершён механически: смешанный catalog matching разделён на owners
`catalog/evidence.py` (323 строки), `catalog/scoring.py` (103),
`catalog/retrieval.py` (53), `catalog/safety.py` (385) и
`catalog/resolver.py` (199). После Block 5J канонические catalog owners и
`conversation/quantity_resolution.py` используются напрямую; старые
compatibility facades `services/matching.py` и `services/catalog_resolver.py`
удалены после caller-аудита. Production callers переведены на новые owners.
Сравнение старого и нового pipeline на 18
представительных corpus-классах дало `0 mismatches`; полный baseline —
`1362 collected / 1362 passed`. Retrieval остался bounded in-memory; PostgreSQL,
pgvector, embeddings, catalog migrations и Sheets sync в Block 4 не добавлялись.

Block 4C review подтвердил: `catalog/retrieval.py` — текущий in-memory retrieval
seam, который позднее можно заменить searchable projection PostgreSQL с
лексическими/full-text или `pg_trgm` индексами и, только после benchmark,
`pgvector`, не меняя evidence, safety, ConversationEngine и channel adapters.
Реализация PostgreSQL, индексов, embeddings, миграций и Sheets sync не входит
в этот блок. `catalog/resolver.py` сохраняет transitional imports
`parsing/comment_policy` и `text_normalization`; это остаточная зависимость будущего
parsing/conversation cleanup, а не дублирующая реализация.
Repository-wide audit подтвердил одного owner для catalog responsibilities,
отсутствие циклов и workflow-изменений в engine/orchestrator; единственный
политика кратности вынесена в `conversation/quantity_resolution.py`.
Conversation Block не начинался.

Block 5A выполнен как поведенчески нейтральная декомпозиция conversation
routing/state policy. Новые channel-neutral owners находятся в
`conversation/routing/`: `contracts.py`, `item_resolution.py`, `order_flow.py`,
`comment_scope.py`, `state_compatibility.py` и `modal_routing.py`. Чистые
canonical unresolved membership/priority и state-query функции находятся в
`conversation/state/queries.py`. Production
imports переведены на новые owners; старые
`services/conversation_handlers/state_compatibility.py`, `modal_routing.py` и
`state.py` удалены в Block 5J после нулевого caller-аудита.

`StateCompatibilityPolicy` сохранила public methods `evaluate`, `context_for`,
`should_try_contextual_fallback` и `submission_failure_mode`, все enum/decision
contracts, context priority и mode values. `has_named_product_items` имеет
одного channel-neutral owner в `conversation/routing/item_resolution.py`;
`PendingQuantityHandler` делегирует ему, поэтому routing больше не импортирует
handler. `PendingQuantityHandler.handle` и `OrderStatusHandler.handle` намеренно
остались в legacy services-пакете: они принимают TelegramEvent или
presentation-зависимые ответы и будут перенесены только вместе с input/
application boundary.

После Block 5AC `StateCompatibilityPolicy` остаётся единым public coordinator,
но больше не наследует routing mixin-классы. `item_resolution.py`,
`order_flow.py` и `comment_scope.py` содержат явные чистые module-level policy
functions; coordinator передаёт им необходимые зависимости. Скрытая связь
через members будущего subclass и `type: ignore[attr-defined]` устранена без
изменения context priority, action/mode contracts или engine workflow.

Block 5A не меняет engine workflow, parser, catalog, prompts, comments,
quantity semantics, database, Docker, Sheets или Telegram UX. Focused modal
suite, полный baseline и quality gates подтверждают сохранение routing behavior.
После review этой границы выполнен отдельный Block 5B по core-операциям draft и
comments; Telegram/presentation handlers в нём не переносились. Block 5C также
завершён: `conversation/selection.py` владеет channel-neutral selection core,
а `CandidateSelectionHandler` остаётся presentation-only адаптером. Engine и
input recognition используют этот общий owner.
Block 5D также завершает поведенчески нейтральное выделение progression core:
`conversation/progression.py` владеет переходом к следующей нерешённой позиции,
а engine сохраняет только presentation adapter.

Постоянное правило: перед каждым `MOVE`/`MERGE`/`DELETE` выполняются
repository-wide usage и duplicate audit. Мёртвый или дублирующий код не
переносится; для одной ответственности остаётся одна реализация. Временный
facade допустим только как явный re-export при подтверждённых callers и явном контракте
конкретного блока. Для Group N Block 5M такой facade намеренно не создаётся.

## HISTORICAL SNAPSHOT — Block 2B

Документационный commit с постоянными правилами создан отдельно. Text command
parsing механически разделён по доказанным ответственностям в `parsing/commands/`.
На момент Block 2B `services/parser.py` оставался компактным facade/dispatcher:
`infer_intent`, `parse_callback` и public compatibility exports. В Block 5U этот
facade удалён; текущие owners перечислены в начале handoff.

До code commit выполнены повторные usage/duplicate/dead-code audit и semantic
comparison на 62 существующих тестовых строках: расхождений нет. Полный
regression baseline остаётся `1362 passed`; code migration прошёл все quality
gates.

## HISTORICAL SNAPSHOT — Block 5B и Block 5T owner table

Block 5B выполняется как поведенчески нейтральное выделение channel-neutral
операций из `services/engine.py`. Владельцами становятся:

- `conversation/comments.py` — подтверждённые комментарии, comment scope,
  provenance-нормализация и удаление comment shadows;
- `conversation/draft.py` — наличие активного черновика, поиск дублей и
  слияние только подтверждённых одинаковых строк.

`parsing/comment_policy.py` теперь является единым владельцем лексических и
provenance-примитивов; старый `services/comment_policy.py` удалён после
подтверждённого отсутствия callers. `services/conversation_handlers/comment_scope.py`
сохраняет presentation/state handler и делегирует core-операции новым
модулям. Две исторически разные семантики объединения комментариев сохранены:
`merge_comments()` нормализует внутренние пробелы как прежний engine, а
`merge_scope_comments()` сохраняет пробелы как прежний CommentScopeHandler.
Ни один обработчик Telegram, prompt, parser, matching, state-machine workflow
или внешний контракт в этом блоке не изменяется.

Сравнение старой и новой реализаций на corpus состояний и строк дало
`MISMATCHES=0`; полный regression baseline — `1362 collected / 1362 passed`.

Текущий selection core не импортирует `ParsedCommand`: handler преобразует
callback target и поля команды в нейтральные аргументы core. Это сохраняет
channel boundary без изменения callback format и пользовательского поведения.

## 11. Следующий функциональный блок

Block 5I завершил механический перенос `services/comment_policy.py` в
`parsing/comment_policy.py`: перенесены только три pure supplier-comment функции
и их private regex/constants. Starting SHA блока:
`1a6bf0da753976303b196c57f65a36316bc15685`.
Старый модуль удалён после repository-wide audit; production/test imports старого
пути отсутствуют. Следующий code seam до external review не назначать;
Block 5L завершил read-only audit `services/text.py`, а Block 5M механически перенёс
`clean_text` и `normalize_text` в `restaurant_bot.text_normalization`. Production-код
поведения и тесты не менялись. Полный symbol/caller/duplicate audit находится в
[`docs/SERVICES_TEXT_AUDIT.md`](docs/SERVICES_TEXT_AUDIT.md). Единственный
завершённый seam — этот перенос; Block 5O отдельно назначил следующий доказанный
transcript-policy seam после audit `services/input_recognition.py`.

Block 5J завершил удаление пяти obsolete test-only compatibility facades:
`services/catalog_resolver.py`, `services/matching.py` и трёх старых
`conversation_handlers` путей. Все тестовые imports переведены на канонические
owners (`catalog/*`, `conversation/routing/*`, `conversation/state/queries.py`);
production и dynamic caller-ы отсутствовали. Поведение и assertions не менялись,
полный baseline сохранён: `1362 collected / 1362 passed`.

Block 5N завершил механический перенос Telegram raw-update adapter из
`services/input_normalizer.py` в `input/telegram.py`. Перенесены только
`_clean_message_text` и `normalize_telegram_update`; API, orchestrator, tests и
`TelegramEvent` contract используют новый owner. Старый services-модуль удалён
после repository-wide caller-аудита; `input_recognition.py` и остальные services
не изменялись.

Block 5S завершил controlled multi-seam decomposition `services/text.py`; текущий
полный baseline на момент Block 5S — `1366 collected / 1366 passed`. `to_float` оставлен после
отдельного caller-аудита, остальные symbols переведены в канонические owners,
описанные в начале этого handoff и [`docs/SERVICES_TEXT_AUDIT.md`](docs/SERVICES_TEXT_AUDIT.md).
Следующий блок не назначается автоматически.

Block 5O выполнен как audit-only проверка `services/input_recognition.py` на SHA
`9c19f784ef88a0721c8be99bb8cece80ae4ebfe4`. Полный MOVE класса в
`input/recognition.py` не принят: внутри смешаны Telegram download/cleanup,
OpenAI voice/photo, state-aware retry, visible actions и progress presentation.
Единственный следующий code seam — чистая transcript policy
`has_supported_voice_letters` + `select_transcription_result` в
`input/voice_transcript_policy.py`; сам перенос выполнен в Block 5P, а production
voice orchestration и tests сохранены по контракту. Полный отчёт:
[`docs/INPUT_RECOGNITION_AUDIT.md`](docs/INPUT_RECOGNITION_AUDIT.md).

## 12. Проверки

Для Block 5S подтверждены focused seam-проверки и полный baseline
`1366 collected / 1366 passed`. Также пройдены:

```text
ruff check src tests
ruff format --check <изменённые Python-файлы>
mypy src
python scripts/check_markdown_links.py
python -m compileall -q src/restaurant_bot
git diff --check
```

Импортированы канонические catalog/routing owners и application task port.
Цикл `services.orchestrator ↔ workers.tasks` устранён: допустимое направление
остаётся `workers.tasks → services.orchestrator`, обратных imports нет.
`.env` и `manual_smoke_forensic_logs.txt` в commit не входят.

## 13. Правила передачи

Текущий код, тесты и Git-diff важнее старых заметок. Не удалять пользовательские
файлы, не использовать destructive Git commands и force push. Не читать и не
выводить `.env`. После каждого блока обновлять этот короткий handoff одной
актуальной записью и отдельно указывать подтверждённые факты, ограничения и
следующий блок.

Критическое состояние проекта не должно существовать только в истории ChatGPT
или Codex-сессии. Для восстановления работы достаточно AGENTS.md,
PROJECT_HANDOFF.md, `.agents/DECISIONS.md`, `.agents/PROJECT_MAP.md`,
архитектурной документации, тестов и текущего Git state. История обсуждений
в handoff не копируется.
## HISTORICAL SNAPSHOT — Block 5T

Block 5T завершён поведенчески нейтрально в коммите `9456e79` от исходного
`227ba6e`. Изменения ограничены переносом владельцев и импортов; database,
Alembic, DevOps, prompts, schemas, state-machine, matching и serialized state
contracts не менялись.

### Решения по этапам

- **5T-A — `to_float`: AUDITED/LEFT.** `services/text.py` оставлен с единственной
  функцией `to_float`: её одновременно используют Google Sheets и AI
  reconciliation, а безопасный общий owner для обоих контрактов не доказан.
- **5T-B — parser boundary: MOVED.** Semantic text API находится в
  `parsing/commands/api.py`, callback parsing — в `input/telegram_callbacks.py`.
  В Block 5U временный compatibility facade `services/parser.py` удалён после
  миграции 28 тестовых импортных групп; callback values и revision semantics
  сохранены.
- **5T-C — product-add boundary: MOVED.** Request id перенесён в
  `orders/product_add.py`, очистка pending state — в `conversation/product_add.py`,
  prompt — в `presentation/telegram/product_add.py`; старый facade удалён.
- **5T-D — replies boundary: MOVED.** Replies и keyboards находятся в
  `presentation/telegram/replies.py`; `services/replies.py` удалён. Чистая
  подсказка фасовки вынесена в `orders/package_suggestions.py`.
- **5T-E — dependency audit: DONE.** Новых циклов нет. Единственные
  lower/core → `services` edges — четыре AI reconciliation-модуля к
  `services/text.py:to_float`; это осознанно оставленная граница 5T-A. Остальные
  services отвечают за application adapters и координацию.

Baseline до и после: `1366 collected / 1366 passed`. Focused parser, callback,
product-add, replies, supplier и submission tests зелёные. Следующий block не
назначается автоматически: Block 5U не начинался.
