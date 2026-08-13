# План декомпозиции архитектуры

## IMPLEMENTED AFTER 5Z / BLOCK 6A

Выполнено механическое выделение Telegram input interpretation из
`services/orchestrator.py`. Новый owner —
`input/telegram_interpretation.py:TelegramInputInterpreter`. В него перенесены
верхнеуровневый `_parse`, text contextual routing, sheet-review normalization,
pending-comment scope, visible-action matching и visible-action AI fallback.
`_recognizer()` и media provider остаются совместимым injected seam.

Семантика не менялась: сохранены точный порядок callback → text → voice/photo,
global parse до contextual fallback, StateCompatibilityPolicy и обработка
транзиентных OpenAI ошибок. Проверка: focused `444 passed`, полный suite
`1377 passed`. Block 6B reassessment выбрал
`ORCHESTRATOR_DECOMPOSITION_SUFFICIENT`; следующий шаг — `BLOCK 6C —
STABILIZATION / REALISTIC SMOKE / ACCEPTANCE PREP`, без нового production
переноса до отдельного одобрения. Детальная matrix находится в
[`UPDATE_ORCHESTRATOR_AUDIT.md`](UPDATE_ORCHESTRATOR_AUDIT.md).

## CURRENT ARCHITECTURE — Block 5Y

The current `UpdateOrchestrator` audit and the single candidate input-boundary
campaign are documented in [`UPDATE_ORCHESTRATOR_AUDIT.md`](UPDATE_ORCHESTRATOR_AUDIT.md).

Декомпозиция на этом этапе завершена. Block 6C занимается только стабилизацией
и readiness; текущая матрица каналов и acceptance находится в
[`TESTING_READINESS.md`](../../TESTING_READINESS.md). Новые слои и переносы не
создаются без доказанного P0/P1 blocker.

Block 5Y завершил поведенчески нейтральный перенос draft mutations, comment
operations, progression rendering и transient reset. Свежий полный baseline:
`1377 collected / 1377 passed`. Канонические owners: `conversation/draft_actions.py`
для skip/duplicate/remove, `conversation/comments.py` для comment mutations,
`conversation/progression.py` для state transition, `presentation/telegram/progression.py`
для read-only Telegram rendering и `conversation/state/transitions.py` для exact
transient reset. Engine уменьшен с `1613` до `1535` строк, с `76431` до `72673` байт
и с `33` до `30` методов; state-mutating methods: `18 → 13`; `MISMATCHES = 0`.
Порядок `handle()` и stale callback guard сохранены. `_build_item`,
`_spoken_quantity`, `_cart_page`, `_callback_item_index` оставлены как тонкие
adapters; quantity/new-order/product-add/submission seams защищены. Readiness:
`ENGINE_PHASE_ACCEPTABLE`; Block 6B завершил analysis-only reassessment
UpdateOrchestrator без нового implementation seam.

## CURRENT ARCHITECTURE — Block 5X (архив)

Block 5X завершён как поведенчески нейтральная доводка границ contextual routing
и catalog resolution. Свежий полный baseline: `1370 collected / 1370 passed`.
Публичный API `ContextualCommandPolicy` сохраняет порядок transforms, а
Telegram-specific page extraction находится в `input/telegram_visible_actions.py`.
`contextual_commands.py` не знает `TelegramEvent`, presentation/input/services или
callback-протокол `v2:*`; добавлен architectural import-boundary regression test.

`CatalogResolutionService` стал единственным production owner для
`match_item`, `apply_catalog` и `refresh_cart_order_values`; engine wrappers и
тестовые вызовы удалены/переведены на owner без изменения алгоритмов.
`CandidateSelectionHandler` оставлен `KEEP_TEMP` как presentation adapter,
а quantity action group — **NO SAFE QUANTITY ACTION SEAM**: текущая граница
смешивает state mutation, reply и Block C ordering. Следующий функциональный
этап не назначается этим рефакторингом. После удаления wrappers engine содержит
`1613` строк, `76431` байт и `33` метода.

## CURRENT ARCHITECTURE — Block 5W (архив текущего среза до 5X)

Актуальный baseline после Block 5V-A и до итогового commit Block 5W: `1369 collected /
1369 passed`. Block 5W-A закрепил нейтральные `venues/contracts.py` и `venues/codes.py`,
а провайдерский fallback моделей — в `integrations/openai_transcription_policy.py`.
В Block 5W-C контекстные команды вынесены в `conversation/routing/contextual_commands.py`,
в Block 5W-E сборка `CartItem` — в `conversation/item_intake.py`. Это механический перенос
границ: алгоритмы, public contracts, callback values, state serialization и порядок
`ConversationEngine.handle()` не менялись. Engine уменьшен с 2563 до 1680 строк и с 63
до 42 методов (`117719` → `79555` байт). New-order lifecycle и submission preparation оставлены в engine, потому
что безопасный отдельный owner не доказан; дополнительный seam: **NO SAFE EXTRA SEAM**.

## Block 5W-B — инвентарь ConversationEngine

Ниже зафиксирован фактический inventory после механического переноса. Диапазоны строк
относятся к текущему `services/engine.py`; почти все private-методы вызываются из
`handle()` или из соседнего state-action метода. `state` означает чтение или изменение
`ConversationState`, `reply` — выбор существующего Telegram presenter, а не новый
внешний эффект.

| Методы и диапазоны | Ответственность | State / mutation | Основные вызовы | Решение и риск |
|---|---|---|---|---|
| `__init__` (149–176) | Сборка зависимостей и handlers | Инициализирует engine | handlers, policies | KEEP: coordinator boundary |
| `handle` (178–909) | Полная лестница маршрутизации и переходов | Да / да | modal policy, handlers, catalog, progression, presenters | KEEP: authoritative coordinator; высокий риск изменения порядка |
| `_resolve_pending_comment_scope` (911–924), `_remove_navigation_command_items` (927–973) | Защита comment/navigation context | Да / да | comment policy, state queries | KEEP: state guard |
| `_build_item` (975–981), `_validate_supplier_hint` (983–993) | Intake adapter и проверка поставщика | Да / через результат | `conversation.item_intake`, parsing policy | SPLIT уже выполнен; supplier validation остаётся рядом с catalog boundary |
| `_spoken_quantity` (996–998), `_cart_page` (1001–1009), `_callback_item_index` (1084–1096), `_item_index` (1123–1125) | Compatibility, page/index queries | Читают state | quantity handler, state queries | KEEP тонкими; `_spoken_quantity` нужен production caller |
| `_fresh_order_state` (1012–1028), `_start_new_order` (1030–1034), `_resume_after_new_order_confirmation` (1223–1251) | Жизненный цикл нового заказа | Да / да | `deepcopy`, transient reset, existing replies | KEEP как единый lifecycle seam: безопасный отдельный owner не доказан |
| `_submit_product_add_description` (1036–1081), `_clear_transient_dialog_state` (1099–1111), `_repeat_add_more_prompt` (1114–1120) | Product-add и transient recovery | Да / да | product-add state, presenters | KEEP: stateful handlers требуют общего ordering |
| `_catalog_packaging_measurement` (1128–1133), `_match_item` (1135–1142), `_looks_like_supplier_comment_fragment` (1145–1147), `_remove_unanchored_supplier_hint` (1150–1152) | Catalog evidence и supplier/comment boundary | Да / да | catalog resolver, evidence, comment policy | KEEP: catalog mutation seam защищён Block D/E |
| `_sanitize_catalog_facts_before_resolution` (1154–1160), `_supplier_comment_start` (1163–1165), `_apply_catalog` (1167–1174), `_refresh_cart_order_values` (1176–1182), `_reconcile_quantity_with_catalog_name` (1185–1187) | Применение catalog facts и quantity reconciliation | Да / да | resolver, unit conversion | KEEP: source/catalog provenance; высокий риск |
| `_advance` (1193–1221), `_first_unresolved` (1189–1191) | Progression и поиск следующей проблемы | Да / да | progression policy, state queries | KEEP: один переходный coordinator |
| `_select_candidate` (1253–1279) | Выбор уже допущенного кандидата | Да / да | selection, catalog state | KEEP: candidate safety и callback revision |
| `_use_catalog_unit` (1281–1292), `_accept_suggested_quantity` (1294–1302), `_keep_current_quantity` (1304–1311), `_enter_other_quantity` (1313–1346), `_show_multiple_quantity_choice` (1348–1356), `_advance_multiple_quantity_choice` (1358–1368) | Quantity/unit modal actions | Да / да | quantity handlers, unit conversion, presenters | KEEP: Block C semantics и ordering |
| `_skip_current` (1370–1378), `_confirm_current` (1380–1396), `_remove_item` (1398–1433) | Item actions | Да / да | draft queries, comment cleanup | KEEP: shadow/duplicate cleanup invariants |
| `_edit_existing_comment` (1435–1489), `_edit_quantity` (1491–1524) | Изменение draft fields | Да / да | comment policy, unit conversion | KEEP: source ownership and comment provenance |
| `_prepare_submission` (1526–1609), `_handle_submission_failed_recovery` (1611–1665), `_submission_department_quantities` (1668–1680) | Submission preparation и recovery | Да / да | pending submission, Sheets-facing service contracts | PROTECTED: в Block 5W не переносить и не менять |

### Порядок `handle()` и dependency audit

Относительный порядок сохранён: dialogue enrichment → voice normalization → modal routing
→ stale callback rejection до mutation → `last_input_text` → failed-submission recovery
→ new-order confirmation → submit-confirm → add-more → comment scope → manual/not-found
ambiguity → navigation/shadow cleanup → contextual policy → duplicate/unit mismatch →
global routing → item mutation → progression. Новые conversation owners не импортируют
`presentation`, `input`, `integrations` или `services`; проверенный поиск таких импортов
для `conversation/` и `input/` дал 0 edges. Сам engine по-прежнему имеет существующие
presentation edges для `BotReply` и Telegram UX, что допустимо для Phase 1.

Архивная карта owners после Block 5U (для исторического сравнения) выглядит так:

| Область | Owner | Граница |
|---|---|---|
| Semantic command API | `parsing/commands/api.py` | Text parsing и enrichment |
| Callback decoding | `input/telegram_callbacks.py` | Telegram `v2:*` contract |
| Telegram rendering | `presentation/telegram/replies.py` | Read-only относительно state |
| Review contracts | `application/order_review/contracts.py` | Frozen snapshot types |
| Review pure snapshot | `application/order_review/snapshot.py` | Department aggregation и SHA256 fingerprint |
| Review token | `application/order_review/token.py` | Одноразовый токен длиной 20 hex |
| Review Telegram UI | `presentation/telegram/order_review.py` | Preview, truncation и submit replies |
| Review external effects | `services/order_review.py` | Lease/DB/Sheets/Telegram coordinator |
| Cart page state transition | `conversation/state/transitions.py` | Clamp выполняется до presentation |
| Telegram pagination constants | `presentation/telegram/pagination.py` | Page size belongs to Telegram presentation |
| Voice policy | `input/voice_policy.py` | Pure visible-action/prompt/model policy |
| Venue directory | `integrations/venue_directory.py` | GViz/cache and code primitives |
| Venue access registry | `integrations/venue_access_registry.py` | Access decision and cache |
| Venue registration input | `input/telegram_venue_registration.py` | Telegram text/callback parsing |
| Venue registration presentation | `presentation/telegram/venue_registration.py` | Pure BotReply builders |

`services/parser.py`, `services/replies.py` и `services/product_add_flow.py`
отсутствуют. Исторические таблицы и решения ниже помечены как snapshots и не
являются текущей картой owners.

## Block 5V — завершённые controlled seams

В 5V-A state page normalization получает явный `page_size`; Telegram constants
и page-count принадлежат `presentation/telegram/pagination.py`. В 5V-B чистые
voice-policy функции выделены в `input/voice_policy.py`, а media/provider/state
координатор сохранён в `services/input_recognition.py`. В 5V-C directory, access
registry, Telegram registration input и presentation разделены на отдельные
owners; `services/venue_registration.py` оставлен coordinator DB/Sheets/cache/
rollback с переходными `VenueContext` и `RegistrationResult`. Поведение,
callback values, prompts и внешние протоколы не менялись.

## HISTORICAL ARCHITECTURE SNAPSHOTS

## Block 5S — выполненная декомпозиция `services/text.py`

Четыре независимых seam-группы перенесены механически и проверены полным
regression suite. `domain/units.py` и `domain/unit_conversion.py` владеют
единицами и пересчётом; `domain/departments.py` — отделами;
`parsing/number_words.py` и `parsing/numeric_ranges.py` — словесными
числительными и диапазонами; `conversation/comments.py` и `catalog/evidence.py`
разделяют comment overlap и временный search overlap. `services/text.py`
сохраняет только `to_float`, поскольку его контракт одновременно нужен
Google Sheets и AI-reconciliation и безопасный отдельный owner не доказан.
Старые imports перенесённых символов отсутствуют. Baseline: `1366 collected /
1366 passed`; quality gates проходят. Следующий блок не начинается этим
изменением.

## Block 5R — Telegram presentation formatting

`escape` и `format_number` механически перенесены из `services/text.py` в
`presentation/telegram/formatting.py`. Новый owner зависит только от
`text_normalization` и stdlib; `presentation/telegram/submission.py` больше не
зависит от `services`. HTML, числовой формат, callback и UX-контракты не
изменялись; corpus comparison дал `MISMATCHES=0`.

## Block 5Q — выполненный перенос submission presenter

`services/submission_presenter.py` механически перенесён в
`presentation/telegram/submission.py`. Все 28 AST-функций и сигнатуры совпадают с
исходным модулем. `SubmissionService`, `ConversationEngine` и тесты используют
канонический путь; старый файл удалён. Presenter импортирует только domain-модели
и `presentation/telegram/formatting.py`, не выполняет внешних эффектов. Следующий seam
не назначен.

## Block 5P — выполненный перенос политики транскрипции

Из `services/input_recognition.py` механически вынесены только
`has_supported_voice_letters` и `select_transcription_result` в
`input/voice_transcript_policy.py`. Новый модуль не знает о Telegram, OpenAI,
state, parser или persistence. Остальные voice/photo responsibilities и
`services/input_recognition.py` не менялись; дальнейший seam не назначен.

## 1. Назначение и границы

Документ фиксирует безопасную поведенчески нейтральную декомпозицию. Каждый
migration block переносит существующий owner механически, сохраняет public
contracts и проходит focused/full regression до следующего блока.

Текущий checkout: `decompose_bot`, semantic baseline
`f9cbc3195c0eae843de3208e488c3f46baa5a5ec`. Accepted Block 3 code baseline:
`e4fb29e4d2d0ba906c91beef5c02d3e87d7a0b09`; Block 3C correction baseline:
`21358755ebbc36b95b9fb6b4799021027a2158e7`. Текущий Git HEAD определяется
через `git rev-parse HEAD`, автоматический baseline
`1366 collected / 1366 passed`. Локальный
`manual_smoke_forensic_logs.txt` не является частью проекта.

## 2. Runtime boundary

| Точка | Текущий владелец | Контракт |
|---|---|---|
| `api/app.py:telegram_webhook` | FastAPI transport | Проверить Telegram secret, нормализовать update, поставить его в inbox один раз. |
| `workers/tasks.py:process_telegram_update` | Celery delivery | Retry и передача одного update orchestrator. |
| `application/background_tasks.py` | Application port | Описывает фоновые эффекты без зависимости от Celery или workers. |
| `services/orchestrator.py:UpdateOrchestrator.process` | Application pipeline | Claim, lease, venue access, parse, engine, checkpoint и reply. |
| `services/engine.py:ConversationEngine.handle` | State machine | Применить ParsedCommand к ConversationState и вернуть EngineResult. |
| `services/submission.py:SubmissionService` | Submission use case | Свежий доступ/снимок, запись, read-back, пересчёт, dispatch и уведомление. |
| `integrations/telegram.py` | Telegram adapter | Файлы, ответы и callback acknowledgement. |
| `integrations/google_sheets.py` | Sheets adapter | Каталог, статусы, заказ и операции пересчёта/мутации. |

## 3. Размеры и границы

| Модуль | Строк | Основная ответственность | Решение |
|---|---:|---|---|
| `services/engine.py` | 3353 | State routing, draft, catalog, review, submission preparation | Позже разделить по ответственности. |
| `integrations/openai_prompts.py` | 2378 | Prompt contracts | Переносить как единый внешний контракт. |
| `services/orchestrator.py` | 2152 | Transaction pipeline и scheduling | Позже выделить application pipeline. |
| `application/background_tasks.py` | 31 | Typed port фоновых эффектов | Реальная DI-граница между application и Celery adapter. |
| `services/submission.py` | 2047 | Submission use cases и checkpoints | Разделять по внешним контрактам. |
| `integrations/openai_parsing.py` | 39 | Совместимый re-export facade | Не содержит алгоритмов; удаление — отдельный шаг после audit callers. |
| `parsing/ai/schemas.py` | 67 | Declarative structured-output schemas | Не зависит от transport, state и внешних эффектов. |
| `parsing/ai/quantity_reconciliation.py` | 431 | Source quantity и packaging reconciliation | Использует существующие quantity/product primitives. |
| `parsing/ai/comment_reconciliation.py` | 616 | Comment provenance, scope и bindings | Не смешивается с command comment parsing. |
| `parsing/ai/item_reconciliation.py` | 332 | Source evidence, qualifier cleanup и item recovery | Не меняет state и persistence. |
| `parsing/ai/shadow_items.py` | 363 | Shadow projections, fragments и source variants | Только чистые преобразования AI payload. |
| `parsing/ai/reconciliation.py` | 89 | Порядок общей reconciliation pipeline | Единственная orchestration-точка AI postprocessing. |
| `parsing/commands/api.py` | 33 | Public text command API и enrichment | `services/parser.py` удалён в Block 5U. |
| `presentation/telegram/replies.py` | 947 | Cards, keyboards и UX contracts | Read-only presenter; делить только по доказанным seams. |
| `integrations/openai_client.py` | 927 | Transport и AI use cases | Разделять только после контрактов. |
| `integrations/google_sheets.py` | 884 | Несколько Sheets contracts | Сохранять единый gateway до доказанного split. |
| `catalog/evidence.py` | 323 | Канонизация, токены и evidence | Block 4 owner; чистые преобразования и доказательства. |
| `catalog/scoring.py` | 103 | Оценка одного товара | Block 4 owner; формулы сохранены 1:1. |
| `catalog/retrieval.py` | 53 | Bounded in-memory candidate retrieval | Block 4 owner; лимит и порядок сохранены. |
| `catalog/safety.py` | 385 | Safety gates, qualifiers и numeric compatibility | Block 4 owner; auto-select precision guard. |
| `catalog/resolver.py` | 199 | Supplier scope и CatalogDecision | Block 4 owner; state/persistence не импортирует. |
| `services/venue_registration.py` | 808 | Directory, access и registration | Разделять после базовых миграций. |
| `parsing/products.py` | 511 | Orchestration товарных строк | Текущий owner после Block 2A. |
| `parsing/quantities.py` | 57 | Quantity primitives | Самостоятельные короткие ответы количества. |
| `parsing/packaging.py` | 187 | Фасовка и catalog measurements | Отдельный measurement owner. |
| `parsing/comment_scope.py` | 52 | Explicit global comment scope | Только разбор области комментария. |
| `parsing/commands/patterns.py` | 329 | Статические шаблоны text-команд | Один owner таблиц команд. |
| `parsing/commands/normalization.py` | 85 | Нормализация и отрицание | Без state mutation. |
| `parsing/commands/navigation.py` | 496 | Свободная навигация и статусы | Не смешивается с callback. |
| `parsing/commands/item_commands.py` | 228 | Add/remove/edit item commands | Использует общие quantity primitives. |
| `parsing/commands/comment_commands.py` | 162 | Изменение комментариев черновика | Не владеет comment scope parsing. |
| `parsing/commands/dialogue.py` | 100 | Retry, quantity hint и dialogue metadata | Не меняет ConversationState. |
| `parsing/commands/router.py` | 208 | Text command routing | Не принимает callback data. |

## 4. Карта владельцев

### Domain и persistence

- `domain/models.py`, `db.py`, `db_models.py`, repositories и миграции —
  **KEEP**; serialized contracts и схема не меняются.
- `repositories/updates.py` отвечает за inbox/sequencing, `sessions.py` —
  за состояние, `submissions.py` — за checkpoints/idempotency.

### Ввод и разбор

| Текущий owner | Действие |
|---|---|
| `input/telegram.py` | Block 5N: канонический Telegram raw-update adapter; старый `services/input_normalizer.py` удалён после нулевого caller-аудита. |
| `services/input_recognition.py` | Block 5O полный MOVE в `input/recognition.py` отклонён как смешение transport/provider/policy/presentation; Block 5P завершил перенос `has_supported_voice_letters` и `select_transcription_result` в `input/voice_transcript_policy.py`. |
| `parsing/commands/api.py` | **Block 2B/5U: CANONICAL OWNER** text command API; callback owner — `input/telegram_callbacks.py`. |
| `parsing/commands/` | **Block 2B: CREATE** owners text command parsing по responsibility. |
| `parsing/products.py` | **Block 1/2A: MOVE** orchestration в parsing package; после extraction остаётся центральным entry point. |
| `parsing/quantities.py` | **Block 2A: CREATE** quantity primitives. |
| `parsing/packaging.py` | **Block 2A: CREATE** фасовка и каталожные измерения. |
| `parsing/comment_scope.py` | **Block 2A: CREATE** явная область общего комментария. |
| `services/text.py` | Block 5S: сохранён только `to_float`; units, departments, number words, ranges и overlap имеют канонические owners в domain/parsing/catalog/conversation. |
| `integrations/openai_parsing.py` | Compatibility re-export facade; алгоритмов нет. |
| `integrations/openai_prompts.py` | Позже MOVE prompt contract без изменения текста. |
| `integrations/openai_client.py` | Позже SPLIT transport и AI facade в `application/ai_service.py`. |

### Каталог

- `catalog/{evidence,scoring,retrieval,safety}.py` — **Block 4 CREATE/MOVE**;
  каждый модуль имеет одну каталожную ответственность.
- `catalog/resolver.py` — **Block 4 MOVE** из `services/catalog_resolver.py`;
  resolver не меняет state.
- `conversation/quantity_resolution.py` — **Block 5E owner** channel-neutral
  политики кратности, рекомендации количества и предупреждений.
- `catalog/evidence.py` владеет canonical representation, tokens,
  query/catalog evidence и supplier hint matching; `catalog/safety.py` владеет
  qualifier conflicts, numeric compatibility, safe equivalence, broad-category
  policy и auto-select safety.
- `orders/catalog_resolution.py` владеет channel-neutral применением результата
  каталога к `CartItem`: каталожные поля, quantity reconciliation, comment
  provenance, статусы и обновление уже выбранных позиций.
- `services/engine.py` остаётся transitional orchestration/state-machine caller:
  он делегирует catalog-to-draft resolution новому orders owner и сохраняет
  только совместимые тонкие вызовы для доказанных legacy callers.
- Полный audit оставшегося `services/` и доказанный порядок демонтажа находятся
  в `docs/archive/architecture/SERVICES_TRANSITION_AUDIT.md`; этот документ не переносит production-код.

### Conversation

- Block 5A: channel-neutral routing policy находится в
  `conversation/routing/`; список сильных intent не дублируется в
  engine/orchestrator/handlers.
- `conversation/selection.py` — channel-neutral item/candidate targeting core:
  score, stem compatibility, однозначный поиск позиции и выбор кандидата.
  Модуль не импортирует `ParsedCommand` и не знает о callback semantics.
- `services/conversation_handlers/candidate_selection.py` — presentation-only
  adapter, преобразующий `ParsedCommand` в нейтральные аргументы selection core.
- `conversation/progression.py` — channel-neutral owner перехода к следующей
  нерешённой позиции, issue mapping и progression stage; presentation replies
  остаются в `ConversationEngine`.
- `conversation/quantity_resolution.py` — channel-neutral owner политики
  кратности: расчёт рекомендации, канонические предупреждения и выбор позиции
  без зависимости от канала или presentation.
- `orders/supplier_minimums.py` — channel-neutral owner агрегации минимальных
  сумм поставщиков; возвращает структурированные warnings без мутации state.
- `orders/catalog_resolution.py` — channel-neutral owner применения каталожного
  результата к позиции заказа и refresh каталожных значений черновика; не знает
  Telegram, ParsedCommand, engine или persistence.
- После Block 5J канонические routing/state owners используются напрямую;
  obsolete compatibility facades `services/conversation_handlers/state_compatibility.py`,
  `modal_routing.py`, `state.py`, а также `services/matching.py` и
  `services/catalog_resolver.py` удалены после нулевого caller-аудита.
- TelegramEvent-зависимые handlers остаются в legacy-пакете до отдельного
  input/application блока; переносить их ради дерева нельзя.
- `engine.py` в финале должен координировать pipeline, а не владеть каждым
  сценарием.

### Orders, submission и adapters

- review/product-add, submission stages и venue registration переносятся по
  внешним контрактам, сохраняя facade и checkpoints.
- Telegram, OpenAI, Sheets и cache остаются adapters; нижние слои не импортируют
  orchestrator, workers или API.

## 5. Целевое дерево

```text
restaurant_bot/
  parsing/
    text.py
    commands/
      patterns.py
      normalization.py
      navigation.py
      item_commands.py
      comment_commands.py
      dialogue.py
      router.py
    products.py
    ai/
      schemas.py
      quantity_reconciliation.py
      comment_reconciliation.py
      item_reconciliation.py
      shadow_items.py
      reconciliation.py
  catalog/{evidence.py,scoring.py,retrieval.py,safety.py,resolver.py}
  conversation/
    selection.py
    progression.py
    quantity_resolution.py
    routing/
      contracts.py
      state_compatibility.py
      item_resolution.py
      order_flow.py
      comment_scope.py
      modal_routing.py
    state/queries.py
    handlers/
  orders/{review.py,product_add.py}
  submission/{service.py,checkpoints.py,catalog.py,status.py,presenter.py}
  venues/{directory.py,access.py,registration.py}
  application/{update_pipeline.py,ai_service.py}
  application/background_tasks.py
  input/{telegram.py,recognition.py}
  integrations/{telegram,openai,google_sheets}
```

После Block 2A пакет `parsing/` содержит `products.py`, `quantities.py`,
`packaging.py` и `comment_scope.py`. После Block 2B text command parsing
находится в `parsing/commands/`. После Block 3 structured AI contracts и
reconciliation находятся в `parsing/ai/`; transport остаётся в integrations.

## 6. Правила зависимостей

```text
domain
  <- parsing, catalog, conversation
  <- orders, venues, submission, input
  <- application
  <- api, workers, integrations, repositories, infrastructure
```

`parsing` импортирует только domain и допустимые лексические service
primitives; `catalog` не меняет state; `conversation` не импортирует
Celery/DB/Sheets; `repositories` не импортируют services/integrations.
`application` зависит от domain и собственных портов, но не импортирует
`workers`, Celery или channel adapters. `workers` и API являются внешними
адаптерами и могут импортировать application use cases/порты.
В текущем runtime допустимы `workers/tasks → services/orchestrator` и
`orchestrator → application/background_tasks`; обратного импорта
`services.orchestrator → workers.tasks` быть не должно.
В migration period facade допускается только если это явно предусмотрено
конкретным блоком и имеет записанный шаг удаления. Block 5M является исключением:
для Group N compatibility facade не создаётся.

## 7. Compatibility strategy

Каждый перенос проходит четыре фазы:

1. Создать нового owner механически, без изменения поведения.
2. Перевести внутренние imports и, только если это предусмотрено контрактом блока,
   оставить старый путь re-export facade. Для Group N Block 5M старый public path
   намеренно не сохраняется.
3. Просканировать source/tests/scripts/workers/packaging и прогнать gates.
4. Удалить facade только отдельным блоком после доказанного отсутствия callers.

Facade не содержит второго алгоритма, не меняет serialization, prompt, UX или
внешний контракт.

Перед каждым переносом, слиянием или удалением выполняется repository-wide
usage/duplicate audit по исходникам, тестам, scripts, entrypoints и aliases.

## 8. Block 1 и Block 2A — выполненные механические переносы

До кода нужно было подтвердить реальные callers product parser:

- единственный production-import implementation был `services/parser.py`;
- application и tests используют публичные aliases `services.parser`;
- отдельные private symbols были доступны через старый facade, но repository-wide
  audit не нашёл оправданных callers старого пути.

Block 1 перенёс цельный owner без изменения алгоритма, а Block 2A разделил
внутри него независимые responsibilities:

```text
services/product_parser.py
  -> удалён после import audit
parsing/products.py
  -> orchestration и порядок parsing rules (511 строк)
parsing/quantities.py
  -> _is_standalone_quantity, parse_quantity_unit
parsing/packaging.py
  -> _PACKAGING_REFERENCE_PREFIX_RE, _spoken_measurement_pair,
     _is_packaging_reference_prefix, _is_compact_catalog_measurement,
     _single_product_packaging_item
parsing/comment_scope.py
  -> _extract_global_comment, has_explicit_global_comment_scope
services/parser.py
  -> imports из реальных owners; command parsing не переносился
```

В `products.py` остались `_query_with_unmarked_tail`,
`parse_product_lines`, product-line splitting, границы позиций, порядок правил
и координация leaf primitives. `_query_with_unmarked_tail` сознательно не
переносился в comment scope: он собирает product query из неподтверждённого
хвоста. Новые `helpers.py`, `utils.py` и service-классы не создавались.

Побайтное сравнение реализации после нормализации переводов строк с исходным
файлом из baseline для каждой функции совпало. `products.py` уменьшился с 673
до 511 строк; сама функция `parse_product_lines` занимает 11 строк, а
`_parse_product_line` — 84 строки как orchestration одной исходной строки.
Focused и полный regression baseline проходят.

## 9. Долговременная архитектурная цель

Проект развивается как production-grade платформа закупок, а не как набор
Telegram-specific сценариев. Telegram Bot, Telegram Mini App, MAX и Web должны
оставаться внешними adapters одного application/domain core. Новые владельцы
parsing, routing, catalog, conversation, history и submission не импортируют
объекты конкретного канала.

Будущие свободные вопросы о прошлых заявках проходят через структурированный
`HistoryQuery` и отдельный history use case. Этот migration block историю не
реализует и не добавляет новые history intents.

Перед каждым `MOVE`, `MERGE`, `DELETE` или `SPLIT` выполняется repository-wide
usage/duplicate/dead-code audit до и после изменения. Для одной ответственности
остаётся один owner; facade возможен только как простой re-export при доказанных
callers.

## 10. Block 2B — выполненный механический перенос text command parsing

`services/parser.py` уменьшился с 1687 до 224 строк. В нём остались только
public `infer_intent`, callback `parse_callback` и compatibility exports.
Свободный текст теперь маршрутизируется через `parsing/commands/router.py`.

```text
parsing/commands/patterns.py
  -> статические _COMMANDS и _NATURAL_COMMANDS
parsing/commands/normalization.py
  -> normalize_command_text и guards отрицания
parsing/commands/navigation.py
  -> свободная навигация и order-status navigation
parsing/commands/item_commands.py
  -> add/remove/edit item commands и target cleaning
parsing/commands/comment_commands.py
  -> edit comment command без мутации состояния
parsing/commands/dialogue.py
  -> retry, quantity hint и dialogue response
parsing/commands/router.py
  -> порядок text-only правил и ParsedCommand
services/parser.py
  -> text/callback dispatcher и обратная совместимость imports
```

`parse_callback()` намеренно не переносился: его mapping и revision являются
контрактом кнопок канального адаптера. Product parsing Block 2A не менялся.
Сравнение старой и новой реализации на 62 существующих тестовых строках дало
нулевые расхождения по полной модели `ParsedCommand`.

## 11. Проверки Block 2A и Block 2B

Focused:

```text
tests/input/test_parser.py
tests/input/test_input_edge_cases.py
tests/input/test_voice_quantity_recovery.py
tests/conversation/test_comment_handling.py
tests/quantity/test_quantity_contract.py
tests/conversation/test_quantity_state_preemption.py
```

Full и quality gates:

```text
python -m pytest -q --tb=short
ruff check src tests
ruff format --check <изменённые Python-файлы>
mypy src
python scripts/check_markdown_links.py
git diff --check
```

Дополнительно импортируются `restaurant_bot.parsing.products`,
`restaurant_bot.parsing.quantities`, `restaurant_bot.parsing.packaging`,
`restaurant_bot.parsing.comment_scope` и `restaurant_bot.services.parser`;
repository-wide scan подтверждает отсутствие старого facade и второй
реализации каждой перенесённой функции.

### Block 4C review

`catalog/retrieval.py` — текущий in-memory retrieval seam. В будущем его можно
заменить searchable projection PostgreSQL с lexical/full-text или `pg_trgm`
индексами и, после benchmark, `pgvector`, сохранив контракты evidence, safety,
ConversationEngine и channel adapters. PostgreSQL, embeddings, индексы,
миграции и Sheets sync в этом блоке не реализуются.

`catalog/resolver.py` импортирует `parsing/comment_policy` и `text_normalization`.
Это transitional residue для будущей очистки
parsing/conversation, а не дублирование алгоритмов. Audit catalog-модулей
подтвердил отсутствие циклов, dead duplicate algorithms и зависимостей от
engine/orchestrator/Telegram/Celery/Sheets. `engine.py` и `orchestrator.py`
сохраняют workflow; engine использует catalog owners и
`conversation/quantity_resolution.py`. Старый compatibility facade
`services/matching.py` удалён в Block 5J, поэтому quantity owner используется
напрямую.

### Block 5A review

Routing policy механически разделена по связным ответственностям без изменения
контрактов и порядка переходов:

```text
conversation/routing/contracts.py
  -> CompatibilityAction, CompatibilityContext, CompatibilityDecision
conversation/routing/item_resolution.py
  -> чистые quantity, manual/product-add details, candidate, not-found,
     duplicate и unit-mismatch functions; общий predicate товарной позиции
conversation/routing/order_flow.py
  -> чистые sheet review, new-order, add-more, submit-confirm и
     submission-failed functions
conversation/routing/comment_scope.py
  -> чистая pending comment scope function
conversation/routing/state_compatibility.py
  -> единый public StateCompatibilityPolicy, context_for и явный dispatch
conversation/routing/modal_routing.py
  -> channel-neutral агрегатор ModalRoutingDecision
conversation/state/queries.py
  -> canonical unresolved membership/priority, first_unresolved и item_index
```

Production imports переведены на новых owners. После Block 5AC leaf policy
модули не содержат mixin-классов: `StateCompatibilityPolicy` явно вызывает
их module-level functions и передаёт необходимые зависимости. После Block 5J
старые test-only re-export facades удалены. `PendingQuantityHandler.handle` и
`OrderStatusHandler.handle` не переносились: они принимают TelegramEvent или
presentation-зависимые ответы. Policy больше не импортирует
`PendingQuantityHandler`; чистый `has_named_product_items` имеет одного owner в
`conversation/routing/item_resolution.py`, а legacy handler делегирует ему.
Focused modal suite и полный baseline подтверждают нулевые поведенческие
расхождения; следующий блок — только после отдельного review этого routing
boundary.

### Block 5B — выделение conversation draft и comments

`services/engine.py` остаётся владельцем orchestration state machine, а
channel-neutral операции черновика и комментариев вынесены без изменения
контрактов:

```text
conversation/comments.py
  -> merge_comments и merge_scope_comments с сохранением двух прежних семантик
  -> comment scope, provenance cleanup, catalog-fact cleanup и comment shadows
conversation/draft.py
  -> active draft predicate, duplicate lookup и слияние подтверждённых дублей
services/conversation_handlers/comment_scope.py
  -> presentation/state handler, делегирующий core-операции
```

В Block 5I `services/comment_policy.py` механически перенесён в
`parsing/comment_policy.py`: туда перемещены три regex-константы и три public
функции. Telegram-зависимые handlers, parser, matching, prompts и state-machine
workflow не менялись. Все production callers переведены на новый owner, старый
модуль удалён после повторного audit. Сравнение старой и новой реализации на
лексическом corpus дало `MISMATCHES=0`, полный baseline —
`1362 collected / 1362 passed`.

В Block 5J удалены пять test-only compatibility facades после проверки
production, dynamic и test callers. Тестовые imports переведены на канонические
owners без изменения assertions и поведения; полный regression baseline
сохранён.

## 12. Порядок следующих миграций

1. Conversation routing/state policy — Block 5A выполнен; граница проверена.
2. Conversation draft и comments — Block 5B выполнен с caller/dead-code audit.
3. Comment policy — Block 5I выполнен.
4. Compatibility cleanup — Block 5J выполнен; после external review выбрать
   следующий доказанный seam; `services/text.py` —
   только кандидат, не начатый этап.
5. Engine decomposition без изменения state-machine semantics.
6. Input/channel-neutral boundary, orders, submission, venues и внешние adapters.
7. Только после контрактов уменьшать `engine.py` и `orchestrator.py`.

Порядок ориентировочный: фактические зависимости и подтверждённые контракты
имеют приоритет. Catalog Block 4 уже принят и не является следующим этапом.

## 13. Запреты текущего блока

Не менять prompts, state machine, matching, quantity/comment semantics, UX,
persistence, migrations, DB schema, Docker/deploy, workers, API entrypoints,
tests только ради нового пути импорта или внешние сервисы. Известные manual
acceptance issues остаются в `PROJECT_HANDOFF.md`.
## Historical snapshot — Block 5T controlled cleanup leaf boundaries

Block 5T завершён механически в `9456e79` от `227ba6e`. Канонические владельцы:

- `parsing/commands/api.py` — semantic text command API;
- `input/telegram_callbacks.py` — Telegram callback parsing;
- `orders/product_add.py`, `conversation/product_add.py`,
  `presentation/telegram/product_add.py` — product-add contracts;
- `presentation/telegram/replies.py` — Telegram replies и keyboards;
- `orders/package_suggestions.py` — чистая подсказка фасовки;
- `services/text.py` — только `to_float`, оставленный из-за смешанного Sheets и
  AI-reconciliation контракта.

`services/parser.py` был сохранён как 50-строчный compatibility facade на момент
Block 5T; в Block 5U он удалён после миграции тестовых imports.
`services/replies.py` и `services/product_add_flow.py` удалены. State-machine,
matching, prompts, schemas, persistence и DevOps не затрагивались.
Единственная оставшаяся нижнеуровневая зависимость от `services` — вызов
`to_float` из четырёх AI reconciliation-модулей; она покрыта решением 5T-A.
Исторические строки таблиц ниже описывают состояние до Block 5T; текущими
считаются владельцы, перечисленные в этой секции.
# Актуальный статус финальной архитектуры

Текущая карта владельцев и dependency direction зафиксирована в
[`CURRENT_ARCHITECTURE.md`](../../CURRENT_ARCHITECTURE.md). Финальная кампания не
переносит защищённые effect coordinators (`engine`, `orchestrator`, `submission`,
`order_review`, `venue_registration`, `input_recognition`) без отдельного proof
checkpoint protocol. Контракты `VenueContext` и `RegistrationResult` теперь
принадлежат `application/venue_registration/contracts.py`; старый service import
сохранён для совместимости. Архитектурные границы core закреплены тестами.
