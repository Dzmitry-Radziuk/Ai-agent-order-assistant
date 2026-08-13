# Текущая архитектура

Документ фиксирует фактическую структуру после финальной архитектурной кампании.
Он описывает владельцев и намеренно оставленный технический долг; сам по себе не
заменяет исполняемый код и тесты.

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
