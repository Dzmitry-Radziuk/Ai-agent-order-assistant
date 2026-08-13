# Журнал подтверждённых решений

В журнал попадают только решения, которые должны пережить отдельную задачу или сессию. Новая запись не переписывает историю: при изменении решения добавляется следующая запись со ссылкой на заменённую.

## Действующие решения

### ADR-001 — DevOps-контракт отделён от прикладной разработки

- Статус: действует.
- Решение: CI-шаблоны, Compose, Dockerfile, серверные worker-настройки и `README-DEVOPS.md` не меняются без прямого согласования с пользователем и DevOps.
- Причина: pipeline и заливка уже организованы корпоративными шаблонами; прикладное исправление не должно незаметно менять эксплуатационный контракт.

### ADR-002 — Внешняя отправка заявки управляется переключателем

- Статус: действует.
- Решение: при `GOOGLE_ORDER_SUBMISSION_ENABLED=false` бот записывает количества и комментарии в таблицу текущего заведения, запускает пересчёт и завершает черновик, но не вызывает центральный Apps Script отправки.
- Быстрое будущее включение: только после готовности процесса и проверки переменных значение меняется на `true`; бизнес-код не переписывается.
- Реализация: `src/restaurant_bot/services/submission.py` и настройки в `src/restaurant_bot/config.py`.

### ADR-003 — Реальные статусы читаются из истории заведения

- Статус: действует.
- Решение: экран «Мои заявки» читает реальный лист истории таблицы, связанной с текущим заведением. Данные группируются по заявке и поставщикам, выдаются страницами; просмотр ничего не записывает.
- Реализация: `src/restaurant_bot/integrations/google_sheets.py`, `services/submission.py`, `services/submission_presenter.py`.

### ADR-004 — Доступ задаётся центральным реестром заведений

- Статус: действует.
- Решение: авторизация использует центральную таблицу заведений/пользователей, а не лист поставщика. Отзыв активной отметки блокирует работу; старый invite-код не обходит отзыв.
- Реализация: `src/restaurant_bot/services/venue_registration.py` и `repositories/venue_bindings.py`.

### ADR-005 — AI понимает речь, state machine разрешает действие

- Статус: действует.
- Решение: текст, голос и фото могут проходить через AI для понимания свободной речи. Авторизация, отрицание, навигация, выбор кандидата, изменение количества и внешние эффекты дополнительно защищаются детерминированными правилами состояния.
- Причина: невозможно перечислить все человеческие формулировки, но нельзя позволять вероятностной модели выполнять опасное действие без контекста.

### ADR-006 — Похожий товар не равен заказанному

- Статус: действует.
- Решение: низкая уверенность, широкий категорийный запрос или несовпадающие существенные признаки приводят к вариантам/уточнению, а не к автоматической подстановке единственного похожего товара.
- Примеры риска: кукуруза не заменяется кукурузной крупой; свинина без костей не заменяется салом.
- Реализация: `src/restaurant_bot/services/matching.py` и защитные проверки `services/orchestrator.py`/`services/engine.py`.

### ADR-007 — Пользовательские сценарии имеют один источник

- Статус: действует.
- Решение: `docs/user-scenarios/scenarios.json` — канонический источник. `docs/USER_SCENARIOS.md` и `docs/user-scenarios/index.html` генерируются скриптом и связаны с pytest-тестами.
- Проверка: `scripts/generate_user_scenarios.py --check` и `tests/docs/test_user_scenarios.py`.

### ADR-008 — Комментарий поставщику имеет подтверждённое происхождение

- Дата: 2026-08-08.
- Статус: действует.
- Контекст: остаток товарного названия и справочное примечание каталога могли ошибочно попасть в новую заявку как инструкция поставщику.
- Решение: комментарий хранит provenance `semantic` или `explicit_marker`; `catalog` хранится отдельно. Необозначенный остаток фразы остаётся частью `product_query`, а не восстанавливается вычитанием слов.
- Последствия: неоднозначный признак товара может привести к уточнению каталога, но не станет скрытой инструкцией поставщику. Старые сериализованные черновики поддерживаются и очищаются при обновлении данных каталога.
- Реализация: `domain/models.py`, `parsing/comment_policy.py`, `parsing/products.py`, `parsing/comment_scope.py`, `integrations/openai_parsing.py`, `services/engine.py`, `tests/conversation/test_comment_handling.py` и `tests/input/test_voice_input_contract.py`.

### ADR-009 — Каталоговый resolver не изменяет состояние диалога

- Дата: 2026-08-08.
- Статус: действует.
- Контекст: поиск кандидатов, фильтр поставщика и hard veto были перемешаны с изменением `CartItem` внутри `ConversationEngine`.
- Решение: `CatalogResolver` владеет областью поиска, ранжированием и решением `auto_select`/`clarify`, но не изменяет `ConversationState` и не выполняет внешние эффекты. Engine применяет выбранную строку и управляет карточкой пользователя.
- Последствия: policy сопоставления тестируется независимо, а PostgreSQL search в будущем сможет заменить только реализацию поиска без переписывания state machine.
- Реализация: `services/catalog_resolver.py`, `services/engine.py` и `tests/catalog/test_catalog_resolver.py`.

### ADR-010 — Непустой ответ AI не переписывается детерминированным восстановлением

- Дата: 2026-08-08.
- Статус: действует.
- Контекст: повторный разбор полного исходного текста после ответа AI создавал
  фантомные товары и затирал комментарии к уже распознанной позиции.
- Решение: если AI вернул хотя бы один товар, сохраняем его товары, количества и
  комментарии; детерминированный `parse_product_lines()` используется только при
  полностью пустом ответе AI. Самообучение и автоматическое переписывание каталожных
  названий в этом маршруте не выполняются.
- Реализация: `integrations/openai_parsing.py` и
  `tests/input/test_ai_result_integrity.py`.

### ADR-011 — Каналы используют единое прикладное ядро

- Дата: 2026-08-11.
- Статус: действует.
- Контекст: продукт должен развиваться за пределами одного Telegram-интерфейса.
- Решение: Telegram Bot, Mini App, MAX и Web считаются внешними адаптерами одного
  application/domain core. Бизнес-правила, черновик, каталог, доступ, история и
  отправка заявки не дублируются по каналам.
- Последствия: новые parsing, routing, catalog, conversation и submission owners
  не должны зависеть от Telegram-specific объектов. Пустые будущие packages сейчас
  не создаются.
- Реализация: границы модулей фиксируются в `docs/ARCHITECTURE_DECOMPOSITION.md`.

### ADR-012 — Свободные запросы истории проходят через отдельный use case

- Дата: 2026-08-11.
- Статус: действует как целевое направление.
- Контекст: будущие вопросы о прошлых заявках не должны превращаться в набор
  разрозненных regex внутри Telegram parser.
- Решение: natural-language запрос преобразуется в структурированный
  `HistoryQuery`, затем обрабатывается history use case и repository. AI может
  помогать понять фразу, но scope, чтение, безопасность и результат определяет
  детерминированный код.
- Последствия: историю не реализуем в Block 2B и не добавляем новые history intents
  в механический перенос parser.
- Реализация: будущий владелец будет определён после фактического аудита history
  маршрутов.

### ADR-013 — Архитектурный перенос начинается с доказанной границы

- Дата: 2026-08-11.
- Статус: действует.
- Контекст: крупные модули нельзя безопасно переносить целиком без понимания
  callers, aliases и скрытых зависимостей.
- Решение: перед `MOVE`, `MERGE`, `DELETE` или `SPLIT` выполняется полный
  usage/duplicate/dead-code audit; после переноса audit повторяется. Один
  behavior/responsibility имеет одного owner. Facade допустим только как простой
  re-export при подтверждённых callers и с заранее понятным удалением.
- Последствия: `services/parser.py` не переносится целиком в `commands.py`; callback
  parsing не смешивается с channel-agnostic text parsing.
- Реализация: `AGENTS.md`, `.agents/DEVELOPMENT_PROCESS.md` и отчёт каждого блока.

### ADR-014 — Каталог масштабируется через searchable projection

- Дата: 2026-08-11.
- Статус: действует как целевое направление.
- Контекст: текущий каталог хранится в Google Sheets, а будущий объём потребует
  ограниченного venue-scoped retrieval вместо передачи полного каталога в AI.
- Решение: Google Sheets остаётся source of truth. Будущая синхронизация создаёт
  в PostgreSQL локальную searchable/indexed projection с безопасным upsert.
  Retrieval начинается с venue scope и может объединять exact, normalized,
  lexical, trigram/full-text и только при доказанной необходимости vector search.
  В AI передаётся bounded top-N shortlist; catalog evidence, scoring и
  deterministic safety gate остаются обязательными до AUTO_SELECT/CLARIFY.
- Последствия: PostgreSQL не становится source of truth автоматически. Индексы,
  таблицы, embeddings и pgvector выбираются только после benchmark реального
  объёма, запросов, EXPLAIN/ANALYZE, latency и recall.
- Реализация: будущие design blocks для catalog sync и searchable projection;
  Block 3 не меняет БД, migrations, Docker или Sheets sync.

### ADR-015 — `services/` остаётся transitional compatibility layer

- Дата: 2026-08-12.
- Статус: действует.
- Контекст: исторический пакет `services/` содержит orchestration, handlers,
  presentation и остаточные core-алгоритмы, поэтому его нельзя считать целевой
  архитектурой или безопасно переносить целиком.
- Решение: каждый оставшийся модуль `services/` проходит отдельный
  responsibility/caller/duplicate audit. Core переносится по одному доказанному
  seam в именованный owner (`conversation/`, `orders/`, `catalog/`, `parsing/`,
  `submission/`, `venues/` или `application/`). Compatibility facade допустим
  только при подтверждённых старых callers, без собственной реализации и с
  понятным путём удаления.
- Последствия: массовой механической миграции и generic-свалок не создаём;
  `services/engine.py` может временно координировать state machine, а новые
  channel-neutral owners должны зависеть от domain/core, а не от engine или
  transport.
- Реализация: `orders/catalog_resolution.py`, owner maps в
  `.agents/PROJECT_MAP.md`, `docs/ARCHITECTURE_DECOMPOSITION.md` и текущие
  compatibility wrappers в `services/engine.py`.

### ADR-016 — Общий conversation вход и результат

- Дата: 2026-08-13.
- Статус: действует.
- Контекст: будущие MAX/Web/REST адаптеры не должны конструировать Telegram
  payload для запуска общего диалога.
- Решение: `application/conversation/contracts.py` владеет
  `ConversationInput`, `ConversationResult`, `ConversationView`, семантическими
  действиями и планом эффектов. Канальный adapter переводит свой payload в этот
  вход, а renderer переводит результат в UI канала. Telegram callback encoding,
  durable update delivery и side-effect checkpoints остаются на Telegram boundary.
- Последствия: новый канал может переиспользовать parsing/state/catalog/order
  правила через `ConversationApplication`; legacy `ConversationEngine` и
  `BotReply` остаются protected compatibility bridge до отдельного proof
  checkpoint, без копирования бизнес-правил.
- Реализация: `application/conversation/`, `input/telegram.py`,
  `presentation/telegram/conversation.py`,
  `tests/application/test_conversation_application.py`.

### ADR-017 — Нейтральное действие и защищённый engine bridge

- Дата: 2026-08-13.
- Статус: действует.
- Контекст: общий диалог должен принимать text/voice/web input без чтения
  Telegram callback-полей, но существующий engine всё ещё владеет безопасным
  порядком state transitions и legacy reply contract.
- Решение: `SemanticAction` и его codec находятся в
  `application/conversation/actions.py`; Telegram mapper внедряется в
  `ConversationApplication` снаружи. `ConversationEngine` принимает
  `ConversationInteraction`, а `TelegramEvent` сохраняет явные adapter aliases
  для старых callers. Полный перенос engine и renderer не выполнять без
  отдельного proof checkpoint.
- Последствия: новые каналы не должны создавать `callback_data`, `BotReply` или
  Telegram identity; presentation отвечает за mapping. Переходный долг виден и
  тестируется, но не размножает business rules.
- Реализация: `application/conversation/contracts.py`,
  `application/conversation/actions.py`, `application/conversation/use_case.py`,
  `presentation/telegram/conversation.py`, `services/engine.py` и
  `tests/application/test_conversation_application.py`.

## Шаблон новой записи

```markdown
### ADR-NNN — Короткое название

- Дата: YYYY-MM-DD.
- Статус: предложено | действует | заменено ADR-NNN.
- Контекст: почему понадобилось решение.
- Решение: что именно принято.
- Последствия: что становится проще, сложнее или запрещено.
- Реализация: проверяемые пути к коду и тестам.
```
