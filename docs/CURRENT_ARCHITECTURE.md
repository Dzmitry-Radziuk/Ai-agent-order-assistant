# Текущая архитектура

## Финальная проверка структуры и масштаба

Проверка выполнена на checkout кампании, начатой с SHA
`1232bc62ca776dae5cc7cacf3765322288e214e1`. Production-код не переносился
между пакетами ради размера файла, не менялись БД, миграции, DevOps и
пользовательские алгоритмы. Целью были явные владельцы, нейтральный вход и
доказательство границ для будущих каналов.

### Контракты каналов

| Область | Владелец | Зафиксированное правило |
|---|---|---|
| Нейтральный вход | `application/conversation/contracts.py` | `ConversationInteraction` содержит только conversation/actor/channel/kind/text/action/media/metadata. |
| Семантическая кнопка | `application/conversation/actions.py` | Кодек не знает Telegram и сохраняет namespace, target, value и revision. |
| Telegram mapping | `presentation/telegram/conversation.py` | Только presentation переводит кнопку Telegram в `SemanticAction` и обратно в callback. |
| Общий use case | `application/conversation/use_case.py` | Вызывает processor и возвращает нейтральный результат; mapper внедряется снаружи. |
| Legacy bridge | `services/engine.py` | Принимает нейтральный протокол, но временно отдаёт `EngineResult`/`BotReply`. |

Callback round-trip проверен для `skip`, `sel`, `qty`, `cartpage` и
`review_submit`; fake-channel тесты покрывают простой товар, missing quantity,
comment edit и candidate selection. Callback, state serialization и durable
claim/lease/checkpoint не переписывались.

### Инвентаризация крупных файлов

| Файл | Строки | Классы | Функции | Ответственность и решение |
|---|---:|---:|---:|---|
| `integrations/openai_prompts.py` | 2378 | 0 | 0 | Единый текстовый контракт AI; оставлен целиком, дробление изменило бы prompt API. |
| `services/submission.py` | 2051 | 1 | 53 | Безопасный протокол записи/повторной проверки/отправки; защищён до отдельного effect proof. |
| `services/orchestrator.py` | 1932 | 2 | 50 | Claim, lease, state/reply checkpoints и запуск задач; это один durable coordinator. |
| `services/engine.py` | 1515 | 1 | 31 | Авторитетный порядок state machine и legacy reply bridge; перенос требует отдельного proof. |
| `presentation/telegram/replies.py` | 950 | 0 | 30 | Когезивные пользовательские ответы и кнопки Telegram. |
| `integrations/openai_client.py` | 935 | 1 | 21 | Транспорт и retry/trace policy провайдера OpenAI. |
| `conversation/routing/contextual_commands.py` | 930 | 1 | 25 | Policy contextual fallback и modal routing без transport. |
| `integrations/google_sheets.py` | 881 | 6 | 33 | Внешний каталог, кеш и таблицы; materialized list оставлена до search projection. |
| `presentation/telegram/submission.py` | 698 | 0 | 28 | Telegram-представление submission/review. |
| `parsing/ai/comment_reconciliation.py` | 588 | 0 | 17 | AI/source comment reconciliation; выделен тематически, дальнейшее дробление не нужно. |
| `domain/models.py` | 530 | 20 | 15 | Стабильные модели домена и совместимый TelegramEvent adapter. |
| `parsing/products.py` | 506 | 0 | 13 | Orchestration deterministic product-line parsing. |

Файлы крупнее 1000 строк имеют конкретного владельца, а не являются свалками:
prompts — единый контракт, submission — safety protocol, orchestrator — durable
pipeline, engine — stateful compatibility coordinator. Удаление этих границ
без caller/effect proof опаснее, чем текущий размер.

### Зависимости и масштабирование

Направление модулей: `input/presentation/api/workers → application →
conversation/orders/catalog/parsing/domain`; `repositories/integrations` —
адаптеры внешних эффектов, `services` — защищённые координаторы. Точный AST-граф
модулей текущего checkout не содержит циклов. Внутри `services` остаются
переходные зависимости на legacy presentation, что отмечено выше и не
распространяется на нейтральный application слой.

Каталог сейчас передаётся как materialized `list[CatalogProduct]` через
`CatalogResolver` и Google Sheets cache. Следующая безопасная граница масштаба —
порт `CatalogSearch` с bounded venue-scoped shortlist; deterministic evidence,
ranking и safety gate должны остаться до AI auto-select. В этой кампании не
добавлялись таблицы, индексы, миграции или vector search.

### Защищённый долг

Оставлены `UpdateOrchestrator`, `SubmissionService`, `OrderReviewService`,
`VenueRegistrationService`, `InputRecognitionService` и Telegram presentation:
они совмещают внешние эффекты с координацией и требуют отдельного отказоустойчивого
proof. Не создавать для них `engine_part*.py`, `helpers.py` или параллельные
state rules. Следующий функциональный шаг проекта — строго
`TEST SUITE CONSOLIDATION / DEDUPLICATION`, а не новая декомпозиция.

Документ фиксирует фактическую структуру после финальной архитектурной кампании.
Он описывает владельцев и намеренно оставленный технический долг; сам по себе не
заменяет исполняемый код и тесты.

## Multi-channel boundary — текущий срез

Финальная кампания добавила общий прикладной контракт в
`application/conversation/`: `ConversationInput`, `ConversationResult`,
`ConversationView`, `SemanticAction` и `ConversationEffectPlan`. Канальный адаптер
`input/telegram.to_conversation_input()` переводит `TelegramEvent` в этот вход,
`ConversationApplication` вызывает существующий stateful processor, а
`presentation/telegram/conversation.py` кодирует смысловые действия обратно в
совместимые Telegram-кнопки. `UpdateOrchestrator` использует этот use case, не
меняя durable claim/lease/checkpoint порядок.

Проверяемый fake-channel proof находится в
`tests/application/test_conversation_application.py`: текстовый заказ и ответ на
ожидаемое количество проходят без создания `TelegramEvent`. Архитектурные guards
проверяют отсутствие Telegram/presentation/infrastructure imports в нейтральном
application contract.

Оставшийся защищённый долг: `ConversationEngine` по-прежнему возвращает
совместимый `EngineResult`/`BotReply` и содержит stateful Telegram UX adapters;
`UpdateOrchestrator`, `SubmissionService`, `OrderReviewService`, регистрация и
media recognition сохраняют effectful Telegram/DB/Sheets протоколы. Это не
создаёт отдельные правила для MAX/Web, но полный перенос renderer/stateful
coordinators потребует отдельного proof checkpoint и не выполнялся в этом run.

## Слои и направление зависимостей

```text
api / input / workers / presentation
              ↓
application use cases и контракты
              ↓
conversation / orders / catalog / parsing / domain
              ↓
чистые модели и правила

repositories / integrations реализуют внешние эффекты,
а composition root связывает concrete adapters с координаторами.
```

Фактический граф верхнего уровня:

| Пакет | Назначение | Основные зависимости | Внешние эффекты |
|---|---|---|---|
| `domain` | модели команд, состояния и каталога | нормализация текста | нет |
| `parsing` | deterministic/AI parsing и reconciliation | `domain`, `conversation` для comment contract | нет |
| `catalog` | evidence, retrieval, ranking и safety gates | `domain`, `parsing` | нет |
| `conversation` | routing, state transitions и draft mutations | `domain`, `orders`, `parsing` | нет |
| `orders` | catalog-to-draft resolution и supplier policies | `catalog`, `conversation`, `domain`, `parsing` | нет |
| `application` | review/venue contracts и фоновые порты | `domain` | нет |
| `presentation` | Telegram replies, buttons и formatting | `application`, `conversation`, `domain`, `orders` | формирование ответа |
| `input` | Telegram input adapters и voice/photo interpretation | `conversation`, `domain`, `integrations`, `parsing`, `venues` | получение input через adapters |
| `repositories` | DB persistence | `domain`, DB models | БД |
| `integrations` | Telegram/OpenAI/Redis/Sheets/provider adapters | `domain`, `catalog`, `parsing`, `venues` | внешние API и cache |
| `workers` | Celery delivery adapters | `services`, repositories, integrations | фоновые задачи |
| `services` | защищённые effectful coordinators | все runtime-слои | orchestration, DB, lease, Sheets, Telegram |

`domain`, `conversation`, `catalog`, `orders` и `parsing` не импортируют
`services`, `workers`, `api`, `repositories`, `integrations` или
`presentation`. Это закреплено архитектурным тестом.

## Runtime-потоки

### Telegram update

```text
Telegram webhook → UpdateOrchestrator
  → TelegramInputInterpreter
  → global parsing/reconciliation
  → StateCompatibilityPolicy
  → ConversationEngine
  → catalog resolution / state checkpoint
  → Telegram presentation reply
  → reply checkpoint → background task, если нужен
```

### Отправка заявки

```text
review callback → OrderReviewService
  → lease + DB lock
  → свежий venue access и snapshot каталога
  → fingerprint/revision check
  → prepare/readback/recalc protocol
  → внешний dispatch или disabled/uncertain recovery
  → idempotent completion notification
```

### Order review

Чистые `ReviewItem`, `ReviewSnapshot`, fingerprint и token принадлежат
`application/order_review`. `services/order_review.py` остаётся effect coordinator:
он объединяет lease, DB session, venue access, Sheets и Telegram notification.
Разделение на application use case и concrete adapters сейчас не выполняется,
поскольку требует перепроверки durable checkpoint protocol и не даёт безопасного
механического переноса.

### Venue registration

`application/venue_registration/contracts.py` владеет
`VenueContext` и `RegistrationResult`. `services/venue_registration.py` остаётся
координатором транзакции привязки, доступа, directory, Sheets и rollback; старый
импорт контрактов сохранён как совместимый re-export через module namespace.

## Разрешённые и запрещённые зависимости

- `presentation` читает state и строит `BotReply`, но не мутирует cart или submission.
- `repositories` и `integrations` владеют внешними эффектами.
- `workers` не содержат бизнес-решений и не импортируются из core/application.
- `services` не является core-слоем; оставшиеся файлы — явные coordinators.
- Core не знает Telegram, DB, Redis, Sheets, Celery и concrete services.
- `application` содержит только контракты/порты и не импортирует concrete
  Telegram, Sheets, Redis или DB.

## Оставшийся защищённый долг

| Область | Почему оставлена |
|---|---|
| `services/engine.py` | единый порядок stateful routing и modal safety |
| `services/orchestrator.py` | durable claim/lease/checkpoint/reply/task protocol |
| `services/submission.py` | критический protocol с uncertainty, fencing и replay |
| `services/order_review.py` | lease/DB/Sheets/Telegram side effects в одной транзакционной границе |
| `services/venue_registration.py` | access rollback и sync semantics требуют общего effect coordinator |
| `services/input_recognition.py` | Telegram download, provider retry и media progress связаны runtime-контрактом |
| `services/conversation_handlers/*` | handlers адаптируют pure decisions к `EngineResult` и Telegram UX без доказанного безопасного нового owner |

Эти границы классифицированы как `PROTECTED_BY_SAFETY`, а не как забытые
transitional facades. Новые каналы, history use case, DB migrations и search
redesign в кампанию не входят.
