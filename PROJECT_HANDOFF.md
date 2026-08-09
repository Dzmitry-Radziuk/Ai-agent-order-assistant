# PROJECT HANDOFF

> Актуальный статус: этап `NOT_FOUND` завершён. Следующий функциональный этап — `DUPLICATE_PENDING`.

## CURRENT ROADMAP OVERRIDE

- Завершено: `MISSING_QTY`, `AWAIT_COMMENT_SCOPE`, `AMBIGUOUS / candidate selection`, `NOT_FOUND`.
- Следующий и единственный функциональный этап: `DUPLICATE_PENDING`.
- Декомпозицию не продолжать; функциональный roadmap остаётся главным приоритетом.

## ARCHITECTURAL REFACTOR STATUS (CURRENT)

Выполнен один поведенчески нейтральный перенос: сравнение кандидатов остаётся ответственностью
`CandidateSelectionHandler.contains_score()`, а `ConversationEngine` и `InputRecognitionService`
обращаются к нему напрямую. Алгоритмы, контракты, state machine, parser, prompts, matching и UX
не менялись; proxy-методы `ConversationEngine._contains_score()` и `_tokens_share_stem()` сохранены
как временный re-export для совместимости.

Будущие каналы, включая MAX, в этот этап не входят и будут рассматриваться отдельно.

## NOT_FOUND — АНАЛИЗ ПЕРЕД РЕАЛИЗАЦИЕЙ

Этап `NOT_FOUND` ещё не реализован. Ниже зафиксирован текущий pipeline и граница
следующего минимального изменения; код приложения, parser, prompts, matching,
comment logic и UX на этапе анализа не менялись.

### Текущий pipeline

```text
TEXT/VOICE
  -> UpdateOrchestrator._parse()
  -> OpenAI/global parser (для voice через тот же callback после transcription)
  -> contextual comment/visible-action fallback, если policy его разрешает
  -> ConversationEngine.handle()
  -> state-specific contextual rewrites
  -> обычный intent routing / NOT_FOUND transition
  -> SessionRepository save + reply
```

Для `NOT_FOUND` global parser выполняется первым. Однако в начале
`ConversationEngine.handle()` пока нет решения `StateCompatibilityPolicy` для
этого контекста. После global parse на команду могут повлиять:

- общий guard `is_product_add_request_phrase()` для открытых `NOT_FOUND`/`AMBIGUOUS` карточек;
- `_contextual_negative_command()` с веткой `current.status == NOT_FOUND`;
- `_contextual_voice_command()` с аналогичной веткой для voice;
- `_remove_navigation_command_items()`, который может удалить unresolved item,
  если его исходная строка сама похожа на навигацию.

Таким образом, первый архитектурный дефект — не ранний перехват до parser, а
отсутствие единого решения `NOT_FOUND context + ParsedCommand` перед этими
локальными rewrite-правилами.

### Ответственный код и состояние

Отдельного `NotFoundHandler` нет. Карточку `NOT_FOUND` рендерит
`services/replies.py::issue_reply()`, а переходы выполняет `ConversationEngine`:
`PRODUCT_ADD`, `MANUAL_CURRENT`, `SKIP_CURRENT`, `REMOVE_ITEM`, повторное
сопоставление и `_advance()`.

Старый контекст хранится в самом `ConversationState.cart[*]` (`CartItem.status`,
`source_query`, `quantity`, `unit`, `comment`, `candidates` и product-add fields),
а активная ссылка — в `current_issue_item_id`/`current_issue_kind` и `stage`.
Отдельный suspended context не нужен: обычный `ADD_ITEMS`, `SHOW_CART`, `THANKS`
не должны перезаписывать старый item. `REMOVE_ITEM` обязан очистить активные
ссылки, если удаляется именно текущий item. `START_NEW_ORDER`/`CLEAR_CART` —
явные reset-команды и намеренно создают свежий state.

### Предлагаемая точка подключения policy

Расширить существующие `CompatibilityContext` и `StateCompatibilityPolicy`,
не создавая нового списка независимых intent в `orchestrator.py` или handler.
В `ConversationEngine.handle()` policy должна быть вызвана сразу после global
parse и до `_contextual_negative_command()`, `_contextual_voice_command()` и
общего product-add guard:

| NOT_FOUND + ParsedCommand | Решение |
|---|---|
| конкретный `ADD_ITEMS` | `INTERRUPT`, normal routing, старый item сохраняется |
| `SHOW_CART`, `REMOVE_ITEM`, `THANKS`, `START_NEW_ORDER`, `CLEAR_CART`, `ORDER_STATUS` и другие самостоятельные действия | `INTERRUPT` |
| существующее уточнение/ручное продолжение текущей карточки | `CONTINUE` |
| неполное или неразличимое уточнение | `AMBIGUOUS`, без изменения draft |
| явно невалидный ответ | `REJECT`, существующее безопасное уточнение |

Для `ADD_ITEMS` следует использовать уже существующий структурный критерий
`_has_concrete_new_items()`, а не добавлять товарные regex/blacklist. При
`INTERRUPT` contextual NOT_FOUND rewrite не вызывается. При `CONTINUE` остаются
только существующие пути ручного уточнения/product-add. Это сохраняет единый
маршрут для текста и голоса.

### Regression scenarios перед реализацией

Нужно зафиксировать для text и voice:

1. `NOT_FOUND("манго") + "манго тайское"` → `CONTINUE`, существующее уточнение.
2. `+ "пармезан 3 кг"` → `INTERRUPT/ADD_ITEMS`, старый манго остаётся `NOT_FOUND`.
3. `+ "добавь укроп 2 кг"` → `INTERRUPT/ADD_ITEMS`.
4. `+ "покажи черновик"` → `INTERRUPT/SHOW_CART`, pending context не теряется.
5. `+ "убери манго"` → `INTERRUPT/REMOVE_ITEM`, ссылки на удалённый item очищены.
6. `+ "спасибо"` и явные reset/status-команды → `INTERRUPT`, без мутации старого item.
7. `+ "ну ладно потом"` → `AMBIGUOUS`, draft и NOT_FOUND context без изменений.
8. Новый item не получает quantity/comment/source metadata старого `NOT_FOUND` item.

## FUTURE CHANNELS / MAX

MAX — будущая продуктовая задача; сейчас основной transport — Telegram. Код под
MAX на этом этапе не реализуется.

Целевая граница:

```text
Telegram adapter ─┐
                  ├─> normalized IncomingEvent -> Conversation Core
MAX adapter ──────┘                              -> normalized Reply
```

`ConversationEngine`, `StateCompatibilityPolicy`, handlers, catalog/matching,
draft/cart и OpenAI parsing должны оставаться общими. MAX позже подключается
через adapter/renderer; копию Telegram business logic создавать нельзя.

### Telegram-specific зависимости, обнаруженные аудитом

- Transport/API: `api/app.py` (`/webhooks/telegram`, webhook secret),
  `integrations/telegram.py` (`TelegramClient`, Telegram API methods и file URLs).
- Нормализация входа: `services/input_normalizer.py` читает `update`,
  `callback_query`, `callback_data`, Telegram `file_id`, `chat_id` и sender fields.
- Domain/state: `domain/models.py` содержит `TelegramEvent`, Telegram user/chat IDs,
  `file_id`, callback fields и `Button.callback_data`; persistence использует
  `telegram_updates` и `telegram_chat_id`.
- Recognition/submission: `services/input_recognition.py` и
  `services/submission.py` напрямую вызывают Telegram client, редактируют/отправляют
  сообщения и используют `chat_id`.
- UI contracts: `engine.py`, `replies.py`, `submission_presenter.py` формируют
  Telegram HTML, InlineKeyboard-like `Button` rows и revision-bearing callback data.
- Application leakage: `TelegramEvent` передаётся ниже transport boundary в
  `ConversationEngine`, handlers и registration/submission services; Telegram IDs
  используются как ключи состояния и доступа.

Это только карта зависимостей. В рамках текущего `NOT_FOUND` этапа их не менять.

## ONE MECHANICAL REFACTOR — COMPLETED

План и фактический перенос одного небольшого слоя:

| Было | Стало | Зависимости | Проверка | Риск |
|---|---|---|---|---|
| `ConversationEngine._contains_score()` и вызов из `InputRecognitionService` | Прямые вызовы `CandidateSelectionHandler.contains_score()` | только существующий handler; временные proxy сохранены | candidate 54, regression 10, baseline 115, Ruff, `git diff --check` | низкий |

Перенесена только точка использования уже существующего алгоритма сравнения кандидатов.
`CandidateSelectionHandler` не менялся алгоритмически, публичные contracts и state transitions сохранены.
В `engine.py` осталось две временные proxy-функции для обратной совместимости; их удаление — отдельный cleanup после проверки callers.
Размер `engine.py` существенно не уменьшился: это намеренный call-site перенос без массового рефакторинга.

Актуальная передача контекста проекта между разработчиками и AI-агентами.
Это не журнал всех действий. Устаревшие сведения заменяются актуальными.

## 1. Текущее состояние

Проект — Python Telegram-бот для заявок ресторана. Он принимает текст, голос,
фото и callback-действия, распознаёт товарные позиции, сопоставляет их с
каталогом заведения, ведёт черновик и записывает подтверждённые данные в Google
Sheets через существующий слой интеграции.

Основной runtime: FastAPI, Celery, Redis и PostgreSQL. AI используется для
транскрипции, структурированного разбора и проверки сопоставления с каталогом.
Локальные Docker-контейнеры после последней пересборки работают: `api` и
`worker` healthy, `beat` запущен, PostgreSQL и Redis healthy; миграционный
контейнер завершился успешно.

Рабочая ветка — `decompose_bot`. Рабочее дерево содержит незакоммиченные
изменения пользователя и текущей разработки; перед любыми правками сначала
нужно изучить полный diff и не удалять чужие изменения.

## 2. Текущая задача

Исследуется системная проблема приоритета state над новым сообщением. Требуется
единый слой `global message interpretation -> state compatibility -> continue /
interrupt / reject / ambiguous`, чтобы новый однозначный товар или команда могли
прервать открытый вопрос, а незавершённая позиция и модальный контекст не терялись.

На этом шаге выполнен только анализ; код приложения, prompts и Docker
конфигурация не менялись.

## 3. Последняя подтверждённая рабочая логика

### Parser и комментарии

`product_query` и `comment` не являются взаимоисключающими. Для фразы
`свиная шея 5 кг без костей без кожи без хрящиков` допустимо и ожидаемо:

- `product_query` содержит полное название вместе с признаками;
- `comment` содержит те же требования поставщику;
- `quantity=5`, `unit=кг`.

Это намеренное дублирование сохраняется в черновике.

### Поиск кандидатов

Для поиска создаётся только временная копия:

```text
search_query = remove_phrase_overlap(source_query, comment)
```

Для указанного примера результат — `свиная шея`. Исходные `source_query`,
`product_query`, `comment`, `quantity` и `unit` не изменяются. Поэтому кандидат
`Шея свиная, кг` может пройти первичный поиск, а `Вишня без косточки`, найденная
только по общему ограничению, не должна показываться.

`CatalogResolver` передаёт полные данные позиции существующему AI matcher;
временный поисковый запрос не сохраняется в item.

### Сообщения действий

В `cart_reply()` строки вроде `Комментарий добавлен` и `Не нашёл товар ...`
выводятся курсивом, без галочки/предупреждающего эмодзи и без жирного текста.
Заголовки карточек, например `🧾 <b>Черновик заявки</b>`, сохраняют эмодзи и
жирное оформление. Кнопка действия `✅ Отправить заявку` не является статусным
сообщением и не относится к этому правилу.

## 4. Критические архитектурные правила

- `product_query`/`source_query` и `comment` могут содержать одну информацию
  одновременно.
- Candidate generation имеет право создавать только локальные поисковые строки;
  он не меняет item и не удаляет комментарий.
- AI matcher получает полный product query, comment и кандидата.
- Одно случайное совпадение общего токена не является достаточным evidence для
  кандидата. Нельзя заменять это правило простым `matched_tokens >= 2`.
- Сохраняются fuzzy-, морфологическое и voice-transcription-сопоставление.
- Нельзя добавлять специальные regex/blacklist под один товар без анализа
  ответственности слоя.
- Текст, голос, фото и callback должны проходить одну безопасную state-machine.
- Внешняя отправка заявки и запись в таблицы не должны включаться или менять
  credentials без отдельного запроса.
- Секреты, токены, пароли и полные connection strings в handoff не записываются.

## 5. Pipeline

```text
Telegram update
  -> API / inbox
  -> Celery task / orchestrator
  -> input normalization and recognition
  -> parser or OpenAI structured parsing
  -> postprocessing
  -> product_query + comment in ConversationState
  -> remove_phrase_overlap() for a temporary search_query
  -> CatalogResolver / rank_candidates()
  -> has_catalog_search_evidence()
  -> existing AI matcher
  -> ConversationEngine state transition
  -> draft reply or Google Sheets submission flow
```

`engine.py` координирует state machine, `matching.py` отвечает за ranking и
evidence, а `catalog_resolver.py` изолирует поиск кандидатов и решение
`auto_select`/`clarify` без прямого изменения состояния диалога.

## 6. Что было сделано последним

- В `src/restaurant_bot/services/engine.py` поиск выполняется по временной
  копии, очищенной от точного overlap комментария.
- В `src/restaurant_bot/services/matching.py` усилен candidate evidence gate:
  одиночные сильные товарные слова поддерживаются, но совпадение только общей
  характеристики не проходит.
- В `tests/catalog/test_matching.py` добавлены проверки сохранения overlap,
  отрицательных межкатегорийных совпадений и однословных товаров.
- В `tests/input/test_ai_result_integrity.py` зафиксировано намеренное хранение
  товарных требований одновременно в query и comment и защита от выдуманного
  комментария.
- В `src/restaurant_bot/services/replies.py` notices действий переведены в
  единый курсивный стиль без иконок; заголовки оставлены прежними.
- Вокруг `engine.py`, parser, input recognition, conversation handlers и
  integrations присутствуют текущие изменения декомпозиции. Их нельзя
  автоматически считать завершёнными только по наличию новых файлов.

## 7. Что сейчас работает

Подтверждены следующие сценарии:

- `remove_phrase_overlap()` для `свиная шея ...` возвращает `свиная шея` и не
  изменяет исходные строки.
- В regression-проверке `Вишня без косточки` не проходит как кандидат для
  `свиная шея без костей`; `Шея свиная` допускается.
- Однословные `пармезан`, `укроп`, `картофель` и базовый `лук порей` сохраняют
  поиск.
- Сохранённые комментарии отображаются в карточке черновика, а action notices
  отображаются под заголовком карточки.
- После последней локальной пересборки Docker-сервисы находятся в рабочем
  состоянии, указанном в разделе 1.

## 8. Что не работает / открытые проблемы

- Полный набор тестов после текущей пересборки контейнеров в этом handoff не
  запускался.
- В последнем зафиксированном широком прогоне оставались 4 известных сбоя в
  `tests/catalog/test_product_matching.py`, связанных с прежней логикой
  санитаризации комментария/количества каталога; они не относились к candidate
  gate и не были исправлены в рамках этой задачи.
- `python scripts/build_agent_context.py` в текущем Windows checkout не смог
  обновить `.agents/runtime/CURRENT_CONTEXT.md` из-за `PermissionError`. Перед
  следующей разработкой нужно проверить права/владельца этого файла, не удаляя
  его и не обходя защиту случайной перезаписью.
- В рабочем дереве есть большой незакоммиченный diff. Его происхождение и
  границы нужно разобрать до следующего крупного изменения или commit.
- State-preemption остаётся незавершённой проблемой. Первый подтверждённый
  перехват до глобального разбора — `UpdateOrchestrator._parse()` и
  `_parse_text_in_context()`: при `pending_comment_items` они сразу вызывают
  `_parse_pending_comment_scope()`. В `ConversationEngine.handle()` такой же
  pending-comment guard стоит до любых остальных маршрутов.
- Дополнительные state-specific переписывания выполняются в engine до обычной
  обработки intent: contextual quantity/negative/voice handlers, открытая
  карточка кандидатов, pending new-order confirmation и этапы manual/product-add.
  Из-за этого один и тот же `ADD_ITEMS` может стать выбором кандидата,
  переименованием текущей позиции или описанием запроса снабженцу.

## 9. Последняя диагностика

```text
source_query:
  свиная шея без костей без кожи без хрящиков

comment:
  без костей без кожи без хрящиков

remove_phrase_overlap:
  свиная шея

ожидаемый кандидат:
  Шея свиная, кг

недопустимый случайный кандидат:
  Вишня без косточки
```

Эти временные значения относятся только к поиску. В item и в draft должны
остаться полные query и comment.

## 10. Изменённые / важные файлы

- `src/restaurant_bot/services/engine.py` — orchestration state machine и
  формирование временного search query.
- `src/restaurant_bot/services/matching.py` — ranking и
  `has_catalog_search_evidence()`.
- `src/restaurant_bot/services/catalog_resolver.py` — поиск кандидатов,
  supplier scope и решение auto-select/clarify.
- `src/restaurant_bot/services/text.py` — нормализация и
  `remove_phrase_overlap()`.
- `src/restaurant_bot/integrations/openai_parsing.py` — схемы и postprocessing
  structured AI output.
- `src/restaurant_bot/integrations/openai_prompts.py` — системные prompts.
- `src/restaurant_bot/services/replies.py` — карточки черновика и action notices.
- `tests/catalog/test_matching.py` — candidate regression tests.
- `tests/input/test_ai_result_integrity.py` — parser/comment invariants.
- `AGENTS.md` — обязательный workflow и правило handoff.

## 11. Тесты

По последней подтверждённой проверке запускались:

- `tests/catalog/test_matching.py` — 14 passed;
- `tests/input/test_ai_result_integrity.py tests/conversation/test_ui_replies.py`
  — 14 passed;
- связанная выборка parser/UI/catalog resolver/submission/product-add — 56 passed;
- `ruff` для изменённых областей — passed;
- `git diff --check` — passed.

Полный suite после последней пересборки контейнеров ещё нужно запустить.
Нельзя писать `tests passed` для полного suite без фактического запуска.

## 12. Последнее принятое решение

Не вводить второй механизм `build_core_search_query()`: существующий
`remove_phrase_overlap()` уже корректно строит временное поисковое ядро из
полного query и сохранённого comment. Слой поиска исправляется через временную
строку и evidence gate, а не через изменение parser/comment или новый список
характеристик.

UI-решение: эмодзи и жирное оформление остаются у заголовков; только action и
success notices унифицируются курсивом без иконок.

## 13. Чего не делать

- Не удалять признаки из `product_query` или `comment` из-за их overlap.
- Не сохранять `search_query` обратно в item.
- Не менять prompts/parser/postprocessing при работе только над candidate search.
- Не возвращать случайных кандидатов по словам `без`, `не`, цвету или другой
  общей характеристике.
- Не убирать эмодзи из заголовков карточек ради изменения action notices.
- Не добавлять новые исключения под отдельную фразу без анализа pipeline.
- Не менять Docker, webhook, secrets, Google Sheets или production без прямого
  разрешения.
- Не делать commit или force push без прямого запроса.

## 14. Текущий результат и следующий шаг

1. Следующий агент должен прочитать `AGENTS.md` и этот файл, затем проверить
   текущий `git diff`.
2. Для первого этапа `MISSING_QTY / AWAIT_UNIT_QUANTITY` уже добавлена единая
   `StateCompatibilityPolicy` после global parse и до contextual handlers.
   Конкретный новый `ADD_ITEMS` и сильные навигационные intent прерывают quantity
   modal, а короткий ответ количества продолжает его.
3. `suspended_interaction` пока не добавлялся: незавершённая позиция уже остаётся
   в `state.cart`, поэтому для этого этапа отдельное хранилище не требуется.
4. Visible-action matching для text/voice оставлен contextual fallback после
   global parse; concrete `ADD_ITEMS` им не заменяется.
5. Этап `AWAIT_COMMENT_SCOPE` реализован отдельно: global parse выполняется
   первым, а `resolve_comment_scope()` вызывается только через централизованный
   contextual fallback policy. Независимые intent проходят обычный routing,
   pending context сохраняется; удалённые позиции вычищаются из pending IDs.
6. Следующий этап — отдельная policy для `AMBIGUOUS`, `NOT_FOUND`,
   `UNIT_MISMATCH` и других modal states. Не расширять текущий патч без
   отдельного анализа и regression matrix.
7. Исправить причину `PermissionError` для runtime context snapshot безопасным
   способом или явно зафиксировать, почему это невозможно.

## 15. Последняя проверенная версия

```text
branch: decompose_bot
commit: 3b888a4 (Поправил нормализацию комментариев и обработку голосовых запросов)
working tree: dirty; есть незакоммиченные изменения
date: 2026-08-09
```

Проверки текущих этапов: 115 связанных тестов passed, ruff и `git diff --check`
passed. Полный `tests/conversation` дал 180 passed и 3 failures, все в ранее
изменённых UI/catalog-сценариях, не связанных с comment-scope preemption.

Этот файл обновлён вместе с добавлением обязательного handoff-workflow в
`AGENTS.md`.

## 16. Baseline перед декомпозицией (2026-08-09)

Снимок `.agents/runtime/CURRENT_CONTEXT.md` перед аудитом не был пересоздан:
`python scripts/build_agent_context.py` завершился `PermissionError` при записи
`.agents/runtime/CURRENT_CONTEXT.md`. Рабочие файлы при этом читались напрямую;
ошибка снимка не является результатом изменения прикладного кода в этом этапе.

Текущий baseline нового функционала:

```text
tests/conversation/test_comment_scope_preemption.py
tests/conversation/test_comment_scope_clarification.py
tests/conversation/test_conversation_handlers.py
tests/conversation/test_orchestrator_pipeline.py
tests/input/test_input_routing.py
tests/input/test_voice_routing_contract.py
tests/quantity/test_voice_quantity_context.py
=> 115 passed in 1.92s
```

Полный `tests/conversation`:

```text
180 passed, 3 failed in 2.57s
```

Оставшиеся падения:

| тест | ожидалось | фактически | связь с текущим этапом |
|---|---|---|---|
| `test_product_sent_from_add_more_prompt_opens_duplicate_in_collecting_stage` | в ответе строка `Товар уже в черновике` | `⚠️ <b>Товар уже есть в черновике</b>...` | Не AWAIT_COMMENT_SCOPE. Формулировка изменена в текущем dirty diff `services/replies.py`; это более раннее UI-изменение. |
| `test_late_global_comment_applies_to_existing_and_new_items_without_overlap` | `cart[2].catalog_comment == "тест"` | `cart[2].catalog_comment == ""` | Не AWAIT_COMMENT_SCOPE: в сценарии нет `pending_comment_items`, поэтому policy не вызывается. Похоже на отдельную регрессию предыдущего catalog/comment refactor (кандидат не применил catalog reference); считать её доказанно pre-existing относительно branch нельзя, исправлять заодно не следует. |
| `test_manual_action_without_an_open_item_uses_source_recovery_card` | кнопки `📦 Показать черновик`, `➕ Добавить еще товары` | `Показать черновик`, ` Добавить еще товары` | Не AWAIT_COMMENT_SCOPE. Эмодзи удалены из action-кнопок в более раннем UI-diff; заголовки при этом не затронуты. |

Вывод: 1 и 3 относятся к ранее принятому изменению оформления сообщений; 2 —
отдельная незакрытая регрессия каталожного комментария в общем dirty tree, а не
ошибка preemption. Ни один тест не исправлялся в рамках аудита.

## 17. Инвентаризация крупных модулей

Подсчёт сделан AST по `src/` (функции включают методы классов; строки — физические
строки файла). Модули меньше 700 строк, но явно входящие в запрос, указаны также.

| файл | строк | классов | функций/методов | основные responsibilities | основные зависимости | признаки смешения |
|---|---:|---:|---:|---|---|---|
| `services/engine.py` | 3005 | 1 | 81 | state-machine, intent routing, quantity/comment flows, candidate application, duplicate/status handling, review, submission preparation | domain; handlers; parser/text; matching; catalog resolver; replies; submission presenter | один класс меняет state, принимает решения каталога и формирует UI/rows; `handle()` dispatcher множества сценариев |
| `services/orchestrator.py` | 1787 | 2 | 53 | claim/lock, registration, input parse, visible-action fallback, catalog AI resolution, analytics, checkpoints, Telegram/Celery effects | DB/repositories; Telegram/Sheets/OpenAI/cache; engine; workers | транспорт, транзакции, semantic routing, observability и side effects в одном `process()` |
| `integrations/openai_parsing.py` | 1455 | 5 | 43 | Pydantic schemas, quantity/range repair, comment binding/scope, product recovery, packaging, duplicate/shadow cleanup | domain; parser; comment policy; text | схемы и пять независимых групп чистого postprocessing связаны порядком вызовов |
| `services/parser.py` | 1505 | 0 | 17 | command normalization, negation, navigation, edit commands, callback parsing, facade to product parser | domain; product_parser; text | команды навигации, отрицания, редактирование и фасад товарного parser-а в одном модуле |
| `integrations/openai_prompts.py` | 2378 | 0 | 0 | пять системных prompt-контрактов: text/photo/match/visible-action/comment-scope | нет импортов | runtime-кода нет, но независимые prompt-контракты невозможно просматривать отдельно |
| `services/replies.py` | 1017 | 1 | 32 | cart, issues, comment notices, review, product-add, warnings, onboarding, action keyboards | domain; text | форматирование разных UI-состояний и общий action/success style в одном файле |
| `services/submission.py` | 1009 | 1 | 31 | order write/checkpoints/retry, order status, product-add write, access checks, Telegram completion | DB/repositories; Sheets/Telegram/cache; replies/presenter | внешний write lifecycle, history read и product-add flow объединены с UX-ответами |
| `integrations/openai_client.py` | 904 | 1 | 21 | OpenAI transport, text/photo parsing, transcription, deterministic bypasses, catalog matcher, visible action, comment scope | OpenAI; parsing/prompts; matching/parser/text; observability | один adapter владеет шестью AI-контрактами и fallback-правилами |
| `services/venue_registration.py` | 808 | 8 | 33 | central directory, invite/access registry, binding, onboarding, HTTP/Redis integration, registration replies | DB models; Redis; Sheets/httpx; domain/text | каталог заведений, authorization state и onboarding в одном service |
| `services/matching.py` | 657 | 0 | 26 | tokenization, identity/qualifier evidence, fuzzy matching, numeric compatibility, ranking and auto-select guards | domain; text | меньше порога, но уже смешивает evidence, constraints, score и policy безопасности |
| `domain/models.py` | 466 | 16 | 24 | enums плюс Telegram event, extracted/cart/catalog, command, conversation state, reply and submission models | только Pydantic/stdlib | не явная ошибка: общий контрактный модуль; дробление раньше стабилизации сериализации рискованно |

Статический граф внутренних импортов не содержит циклов. При этом границы всё
ещё не идеальны: `orchestrator` импортирует `engine`, repositories, integrations и
workers; `engine` импортирует handlers, replies и submission presenter. Это не цикл,
но признак того, что верхний orchestration слой знает слишком много деталей.

## 18. Текущий pipeline и dependency map

Для TEXT/VOICE фактический порядок сейчас такой:

```text
Telegram update
  -> normalize_telegram_update / InputRecognitionService (voice/photo)
  -> UpdateOrchestrator._parse()
  -> global parser/OpenAI ParsedCommand
  -> StateCompatibilityPolicy (quantity/comment-scope)
  -> contextual fallback только если policy разрешила
  -> ConversationEngine.handle()
  -> handler/state transition, CatalogResolver, reply
  -> orchestrator checkpoint (session/update/audit)
  -> Telegram reply и отложенные worker effects
```

Для callback comment-scope сохраняется отдельный явный путь до contextual
parser-а. Это соответствует принятому контракту и не является кандидатом на
изменение в декомпозиции.

Слои, подтверждённые импортами:

```text
domain.models, services.text
  -> product_parser / matching / comment_policy
  -> parser / catalog_resolver / conversation_handlers
  -> engine
  -> orchestrator
  -> workers / Telegram / persistence / external effects

openai_parsing -> domain + parser + comment_policy + text
openai_client  -> openai_parsing + prompts + parser/matching/text
submission     -> repositories + Sheets/Telegram/cache + replies/presenter
```

`domain` и чистые функции matching/parser не импортируют engine/orchestrator.
Динамические импорты worker tasks остаются в orchestrator только в момент enqueue,
что предотвращает статический цикл, но должно остаться проверяемым контрактом.

## 19. Decomposition maps (без переноса кода на этом этапе)

### `engine.py`

```text
OLD ConversationEngine
  - dispatch и state guards
  - quantity / duplicate / unit flows
  - comment and comment-scope application
  - catalog match/apply/reconcile
  - candidate selection and not-found/product-add
  - review/navigation/status
  - submission preparation

NEW (целевое)
  services/conversation/engine.py              # только orchestration pipeline
  services/conversation/routing/intent.py      # dispatch order and intent routing
  services/conversation/state/transitions.py   # stage/current-item transitions
  services/conversation/handlers/quantity.py  # existing PendingQuantityHandler
  services/conversation/handlers/comments.py   # existing CommentScopeHandler + edits
  services/conversation/handlers/candidates.py # existing CandidateSelectionHandler
  services/conversation/handlers/review.py     # existing FinalReviewHandler/navigation
  services/catalog/cart_application.py         # apply CatalogProduct to CartItem
  services/conversation/submission_rows.py     # pure row preparation
  services/engine.py                            # temporary compatibility facade
```

### `orchestrator.py`

```text
OLD UpdateOrchestrator
  - inbox claim/lock/checkpoint
  - registration/access
  - text/voice/photo parsing and contextual fallback
  - candidate AI resolution
  - review deep-link
  - analytics/logging
  - Telegram/Celery delivery

NEW
  services/conversation/routing/input_router.py
  services/conversation/routing/visible_actions.py
  services/conversation/review_orchestrator.py
  services/observability/request_analytics.py
  services/persistence/update_checkpoint.py
  services/orchestrator.py                  # thin transaction coordinator
```

### `openai_parsing.py`

```text
OLD schemas + quantities + comments + packaging + recovery in one module
NEW integrations/openai/parsing/
  schemas.py
  quantities.py
  comments.py
  packaging.py
  postprocessing.py
  __init__.py                                # stable re-exports
```

### `matching.py`

```text
OLD token/evidence/qualifier/numeric/ranking/auto-select functions
NEW services/catalog/
  evidence.py        # identity evidence and query_evidence_tokens
  qualifiers.py      # qualifier conflicts and variant constraints
  numeric.py         # numeric characteristic compatibility
  ranking.py         # match_score, rank_candidates, auto-select
  resolver.py        # existing CatalogResolver facade
  matching.py        # temporary re-export facade
```

### `openai_client.py`

```text
OLD one OpenAIService for parse/transcribe/photo/match/action/comment scope
NEW integrations/openai/
  transport.py       # client construction, retry/usage/trace boundary
  text.py            # parse_text and deterministic skip rules
  media.py           # transcribe and parse_photo
  matching.py        # choose_catalog_candidate
  actions.py         # choose_visible_action and resolve_comment_scope
  client.py          # facade preserving OpenAIService contract
```

### `openai_prompts.py`

```text
OLD five prompt constants in one 2378-line file
NEW integrations/openai/prompts/
  text.py, photo.py, match.py, visible_actions.py, comment_scope.py
  __init__.py
OLD integrations/openai_prompts.py -> compatibility re-export only
```

### `replies.py`

```text
OLD all Telegram cards/buttons
NEW services/conversation/replies/
  cart.py, quantity.py, comments.py, review.py,
  product_add.py, onboarding.py, notices.py
services/replies.py -> stable re-export facade initially
```

### `submission.py`

```text
OLD order submission + status read + product-add write + completion UX
NEW services/submission/
  order.py, status.py, product_add.py, checkpoints.py, completion.py
services/submission.py -> service facade with same public methods
```

### `venue_registration.py`

```text
OLD directory parsing + registry + registration flow + replies
NEW services/venue/
  directory.py, access_registry.py, registration_flow.py, replies.py
```

`domain/models.py` пока не дробить. Сначала стабилизировать persistence and
serialization contracts; затем, если импорт-граф подтвердит необходимость,
выделять `commands.py`, `cart.py`, `conversation.py`, `transport.py` с обратными
реэкспортами.

## 20. Безопасный порядок декомпозиции

Оценки размера старого файла после шага являются ориентировочными и означают
остаток фасада/координатора, а не обещание изменения поведения.

| этап | механический перенос | imports/re-export | проверки | риск | ожидаемый старый файл |
|---|---|---|---|---|---:|
| 0. baseline | ничего; зафиксировать 115 passing и 3 failures | нет | targeted conversation, полный conversation, diff-check | нулевой | без изменения |
| 1. routing contract | не переносить policy: проверить `StateCompatibilityPolicy`, `CompatibilityContext`, `ConversationEngine.handle`, `UpdateOrchestrator._parse_text_in_context` | нет; один policy остаётся источником истины | quantity/comment preemption и voice routing | низкий | engine 3005 / orchestrator 1787 |
| 2. prompt sets (первый механический перенос) | константы пяти prompt-ов в prompt-модули | `openai_prompts.py` только реэкспортирует прежние имена | input/AI contract/media tests, import smoke, diff-check | самый низкий: нет runtime-логики | 30–60 строк фасада вместо 2378 |
| 3. существующие handlers | механически сгруппировать уже выделенные `pending_quantity`, `comment_scope`, `candidate_selection`, `final_review`, `navigation` | временные re-export из старых путей | handler, quantity/comment preemption | низкий/средний | engine уменьшится только после переноса |
| 4. catalog pure layer | `matching.py`: сначала `evidence/qualifiers/numeric/ranking`; `CatalogResolver` остаётся фасадом | `services/matching.py` реэкспортирует публичные функции | `tests/catalog/*`, resolver, voice/fuzzy/negative cases | средний | matching 80–140 строк фасада |
| 5. OpenAI parsing | schemas → quantities/comments/packaging/postprocessing без изменения порядка recovery | `openai_parsing.py` реэкспортирует схемы и функции | AI integrity, voice/parser suites | средний/высокий из-за order-sensitive postprocessing | 120–220 строк фасада |
| 6. engine internals | сначала pure `cart_application` и `submission_rows`, затем routing/state transitions | `services/engine.py` сохраняет `ConversationEngine` import | conversation, quantity, catalog, submission, product-add | высокий | 3005 → ориентировочно 500–900 |
| 7. orchestrator | analytics/checkpoints/input routing/review вынести после стабилизации engine | фасад `UpdateOrchestrator` | orchestrator pipeline, repositories, workers, API smoke | самый высокий: транзакции/side effects | 1787 → ориентировочно 300–500 |

## 21. Самый безопасный первый механический перенос

Первым переносом считаю разбиение `openai_prompts.py`: файл не содержит классов,
функций, состояния или внешних вызовов; его пять констант уже имеют независимые
контракты, а `OpenAIService` обращается к ним по именам. Сначала новые модули
экспортируют байт-в-байт те же строки, затем старый `openai_prompts.py` делает
обратный реэкспорт. После import-smoke и AI contract tests поведение runtime
должно быть идентичным.

Это не означает, что prompts следует менять. Меняется только физическое место
хранения; prompt text, parser, state machine, matcher и UX остаются прежними.
Декомпозицию `engine.py` до этого шага не начинать: его методы имеют больше всего
скрытых связей с `ConversationState`, handlers, replies и submission.

На текущем этапе код приложения не переносился и не менялся; обновлён только этот
handoff-документ результатами аудита.

# PRIORITY ROADMAP — НЕ ПОТЕРЯТЬ

Декомпозиция является вспомогательной задачей и не меняет основной
функциональный roadmap.

Главная задача проекта — перевести state machine на единый принцип:

```text
TEXT / VOICE
  ↓
GLOBAL PARSING
  ↓
STATE COMPATIBILITY POLICY
  ↓
CONTINUE / INTERRUPT / AMBIGUOUS / REJECT
  ↓
state handler или обычный routing
```

State является контекстом уже понятого сообщения, но не определяет смысл
следующего сообщения пользователя.

Уже реализовано:

1. `MISSING_QTY / AWAIT_UNIT_QUANTITY`:
   - новый `ADD_ITEMS` прерывает ожидание количества;
   - старый incomplete item остаётся в черновике;
   - навигация, `thanks` и `remove_item` не превращаются в quantity;
   - text и voice используют один маршрут.
2. `AWAIT_COMMENT_SCOPE`:
   - global parsing выполняется раньше contextual comment scope;
   - новый независимый intent прерывает modal flow;
   - старый pending comment не применяется к новому товару;
   - pending context сохраняется;
   - удалённые item IDs очищаются корректно;
   - callback остаётся отдельным явным путём.

Следующие функциональные этапы распространения той же policy:

3. `AMBIGUOUS / candidate selection`;
4. `NOT_FOUND`;
5. `DUPLICATE_PENDING`;
6. `UNIT_MISMATCH`;
7. `AWAIT_MANUAL_DETAILS`;
8. `AWAIT_PRODUCT_ADD_DETAILS`;
9. `AWAIT_ADD_MORE_CONFIRM`;
10. `AWAIT_SUBMIT_CONFIRM`;
11. `SUBMISSION_FAILED`;
12. review/modal contexts;
13. `pending_new_order_confirmation`.

Порядок может быть уточнён после проверки зависимостей, но ни одно состояние
из списка не должно исчезнуть из roadmap.

Глобальные инварианты:

- сильное новое намерение может прервать старый modal state;
- данные нового товара никогда не применяются к старому pending item;
- случайная разговорная фраза не становится товаром, quantity, comment или
  candidate selection только из-за текущего state;
- `StateCompatibilityPolicy` остаётся единой точкой принятия решения, без
  параллельных списков правил в orchestrator, engine и handlers.

Во время механической декомпозиции поведение и алгоритмы не меняются. После
каждого переноса запускается regression suite. Если начат атомарный этап
декомпозиции, он завершается до baseline и на этом работа останавливается;
следующий большой этап автоматически не начинается.

## NEXT FUNCTIONAL STEP

> Актуальный источник истины: следующий функциональный этап — `NOT_FOUND`; этап `AMBIGUOUS / candidate selection` уже завершён.

Следующая и только одна функциональная задача: распространить
`StateCompatibilityPolicy` на `AMBIGUOUS / candidate selection`.

Граница этапа:

```text
global ParsedCommand
  → policy для открытого выбора кандидата
  → CONTINUE: выбор кандидата
  → INTERRUPT: обычный независимый intent
  → AMBIGUOUS/REJECT: сохранить карточку и pending context
```

До изменения нужно отдельно проверить text и voice, сохранение pending
кандидата при interrupt и отсутствие переноса данных в новый товар. Состояния
`NOT_FOUND` и последующие этапы в этот шаг не входят.

## ARCHITECTURAL REFACTOR STATUS

Архитектурный аудит выполнен только документально. Код приложения не переносился,
поведение не менялось. Зафиксирован один безопасный кандидат для будущего
механического переноса — разбиение `openai_prompts.py` на prompt-модули с
обратным реэкспортом старых имён. Этот перенос разрешён только после отдельного
подтверждения и не является следующей функциональной задачей.

Текущее правило приоритета: функциональный этап `AMBIGUOUS / candidate
selection` выполняется раньше следующего крупного decomposition-шага, если только
пользователь явно не изменит приоритет.

## 22. Аудит AMBIGUOUS / candidate selection (анализ без изменения кода)

### Где сейчас перехватывается TEXT/VOICE

Для текста и голоса global parsing действительно выполняется первым:

```text
UpdateOrchestrator._parse()
  -> _parse_text_in_context()
  -> OpenAIService.parse_text() / voice transcription + тот же callback parser
  -> ParsedCommand
  -> ConversationEngine.handle()
```

После этого в `ConversationEngine.handle()` есть отдельный contextual block
(примерно после quantity/comment compatibility):

```text
current.status == AMBIGUOUS
and command.intent in {UNKNOWN, ADD_ITEMS, MANUAL_CURRENT}
  -> сравнить event.text с current.candidates
  -> если один лучший score: заменить command на SELECT_CANDIDATE
```

Именно это первая неверная точка. Она не использует `StateCompatibilityPolicy`.
Поэтому global command `ADD_ITEMS` с реальным товаром может быть заменён на
выбор старого кандидата до обычной ветки добавления товара.

Фактическая локальная репродукция на текущем коде:

```text
current item: сыр, status=AMBIGUOUS
candidates: Сыр Пармезан, Сыр Гауда
global command: ADD_ITEMS, item=пармезан, quantity=3, unit=кг

ожидалось: новый item «пармезан 3 кг», старый «сыр» остаётся AMBIGUOUS
фактически: старый «сыр» получил catalog_product_id=parmesan и status=MISSING_QTY;
новая позиция не добавилась
```

Для `гауда` без количества текущая эвристика выбирает старого кандидата — это
допустимый contextual case, который должен сохраниться. Для `покажи черновик`,
`спасибо` и неизвестной фразы текущий candidate block не выбирает товар, и
старый item остаётся в состоянии `AMBIGUOUS`.

### Какой handler отвечает за AMBIGUOUS

За применение выбранного варианта отвечает
`services/conversation_handlers/candidate_selection.py`:

- `CandidateSelectionHandler.resolve()` проверяет текущий item, callback index,
  selected index/selection query и возвращает `CandidateSelectionOutcome`;
- `ConversationEngine._select_candidate()` применяет каталог, проверяет duplicate
  и вызывает `_advance()`;
- `replies.issue_reply()` показывает список кандидатов и callback-кнопки.

Важно: handler сам по себе не должен определять, является ли вход новым
намерением. Это должен решить policy до contextual candidate fallback.

### Где хранятся кандидаты

Кандидаты не лежат в отдельном transient state. Они хранятся в самой позиции:

```text
ConversationState.cart[*].candidates: list[Candidate]
ConversationState.cart[*].status: ItemStatus.AMBIGUOUS
ConversationState.current_issue_item_id: str
ConversationState.current_issue_kind: IssueKind.CANDIDATE
```

`Candidate` содержит `product_id`, `name`, `supplier`, `unit`, `score`, `reason`.
`SessionRepository` сохраняет весь `ConversationState.model_dump(mode="json")`,
поэтому кандидаты переживают следующий update, перезапуск worker и повторную
загрузку сессии.

### Что происходит при другом intent сейчас

- `ADD_ITEMS`: старый `AMBIGUOUS` item обычно остаётся в `cart`, но до этого
  может быть ошибочно изменён candidate block-ом. Если block не сработал, новый
  item добавляется отдельно; `_advance()` снова выбирает первый unresolved item.
- `SHOW_CART`, `THANKS`, `SMALL_TALK`, `ORDER_STATUS`: обычные handlers возвращают
  ответ и не очищают `candidates` или `current_issue_item_id`.
- `REMOVE_ITEM`: целевая позиция помечается `SKIPPED`; это ожидаемое явное удаление,
  а не потеря контекста из-за preemption.
- `MANUAL_CURRENT`, `SKIP_CURRENT`, `PRODUCT_ADD` и callbacks обслуживаются
  существующими state-specific ветками; callback candidate selection остаётся
  явным UI-путём.

Следовательно, отдельный `suspended_interaction` для кандидатов сейчас не нужен:
модель уже сохраняет старую позицию и её shortlist. Требуется не хранение, а
запрет неправильного переписывания старого item.

### Единственная точка подключения policy

Расширять существующую `StateCompatibilityPolicy`, а не добавлять новый набор
правил в engine:

1. добавить контекст `CANDIDATE_SELECTION`;
2. распознавать его в `context_for()` по `current_item.status == AMBIGUOUS` и
   непустому `current_item.candidates`;
3. в начале `ConversationEngine.handle()` получить decision policy до текущего
   candidate contextual block;
4. разрешать текущий block только для `CONTINUE` или разрешённого contextual
   fallback; при `INTERRUPT` пропускать block и продолжать обычный routing;
5. при `AMBIGUOUS` не менять item/command, вернуть текущую карточку безопасного
   уточнения; при `REJECT` также не выбирать и не менять draft.

Policy получает уже созданный `ParsedCommand` и структурированные поля выбора;
она не разбирает natural language и не дублирует visible-action matcher.

Минимальная семантика решения:

| вход в candidate context | policy |
|---|---|
| callback `SELECT_CANDIDATE` с валидным target/index | `CONTINUE` |
| global `SELECT_CANDIDATE` с явным selected index/query | `CONTINUE` |
| `ADD_ITEMS` с quantity, несколькими items или явной вводной добавления | `INTERRUPT` |
| `SHOW_CART`, `REMOVE_ITEM`, `THANKS`, `START_NEW_ORDER`, `ORDER_STATUS` и другие самостоятельные intent | `INTERRUPT` |
| `ADD_ITEMS` без concrete quantity/вводной, `UNKNOWN` — кандидатный contextual fallback | `AMBIGUOUS` до проверки существующего candidate evidence |
| некорректный index/пустой selection в уже объявленном SELECT | `REJECT` |

Существующая проверка уникального candidate evidence может остаться механизмом
contextual fallback после policy. Если единственного подтверждённого кандидата
нет, карточка остаётся без изменения; blacklist разговорных фраз и товарные
исключения не нужны.

### Visible actions

`UpdateOrchestrator._parse_text_in_context()` уже применяет visible-action matching
после global parse и не вызывает его для `ADD_ITEMS` с реальными items. Это
сохраняет новый товар от подмены кнопкой. Для candidate card короткое `второй`
может быть преобразовано в callback/`SELECT_CANDIDATE` contextual fallback, а
сильные `ADD_ITEMS`, `SHOW_CART`, `REMOVE_ITEM` и `THANKS` должны пройти дальше
как global intent.

Callback `v2:sel:*` остаётся отдельным явным путём и не должен проходить через
текстовую эвристику.

### Baseline этого аудита

Связанный candidate baseline дополнительно проверен:

```text
tests/catalog/test_candidate_selection.py
tests/input/test_voice_controls.py
tests/conversation/test_voice_route_safety.py
=> 54 passed in 0.26s
```

## 23. Реализация AMBIGUOUS / candidate selection

Этап реализован без изменения parser, prompts, matching, comment logic,
структуры `Candidate` и `SessionRepository`.

Изменения:

- В `StateCompatibilityPolicy` добавлен контекст `CANDIDATE_SELECTION`.
- Policy проверяет открытый `AMBIGUOUS` item с непустым списком кандидатов.
- Валидный `SELECT_CANDIDATE` продолжает contextual flow; неверный индекс получает `REJECT`.
- Конкретный `ADD_ITEMS` с количеством, несколькими позициями или явной вводной добавления получает `INTERRUPT`.
- Независимые intent (`SHOW_CART`, `REMOVE_ITEM`, `THANKS`, `START_NEW_ORDER`, `EDIT_QUANTITY`, `CLEAR_CART`, `ORDER_STATUS` и другие) не проходят candidate fallback.
- Для `UNKNOWN` и неконкретного однотоварного `ADD_ITEMS` сохранён существующий безопасный contextual fallback: выбор выполняется только при единственном лучшем совпадении.
- Для voice с новым конкретным товаром contextual candidate transformations пропускаются после решения policy, поэтому числительное в количестве не воспринимается как номер старого кандидата.
- При удалении текущего `AMBIGUOUS` item очищаются `current_issue_item_id` и `current_issue_kind`; старые candidates не остаются битой активной ссылкой.
- `suspended_interaction` не добавлялся: item и его candidates уже хранятся в `ConversationState.cart`.

Добавлены regression tests в `tests/conversation/test_ambiguous_candidate_preemption.py`:

- новый `пармезан 3 кг` и voice `пармезан три килограмма` добавляются отдельной позицией;
- старый `сыр` остаётся `AMBIGUOUS` с исходными candidates;
- `гауда`, `первый/второй`, навигация, удаление, благодарность и случайная фраза не смешивают контексты.

Проверки:

```text
54 существующих candidate/voice tests — passed
10 новых AMBIGUOUS preemption tests — passed
ruff check изменённых файлов — passed
git diff --check — passed
```

Зафиксированный baseline нового функционала из раздела 16 повторно пройден:
115 passed. Три известных падения полного `tests/conversation` остаются
отдельным ранее зафиксированным состоянием и в candidate policy не входят.

`NEXT FUNCTIONAL STEP` не изменён: распространение StateCompatibilityPolicy
на следующий modal state выполняется только после отдельного подтверждения.

## 24. Реализация NOT_FOUND

Этап выполнен минимальным diff без изменений parser, prompts, matching,
candidate flow, comment flow, quantity flow, persistence schema и MAX.

Изменения:

- В `CompatibilityContext` добавлен `NOT_FOUND`.
- `StateCompatibilityPolicy.context_for()` распознаёт активную карточку
  `ItemStatus.NOT_FOUND`.
- Конкретный `ADD_ITEMS` определяется существующим `_has_concrete_new_items()`
  и получает `INTERRUPT`.
- Независимые действия проходят в обычный routing, не переписывая старый item.
- Продолжение разрешено только для уже открытого `AWAIT_MANUAL_DETAILS` или
  `AWAIT_PRODUCT_ADD_DETAILS`; отмена product-add сохраняет прежний безопасный
  flow.
- Неконкретный `ADD_ITEMS` в обычном NOT_FOUND-контексте получает `AMBIGUOUS` и
  не создаёт случайный товар.
- Contextual NOT_FOUND rewrites пропускаются после `INTERRUPT`.
- `suspended_interaction` не добавлялся: старый `CartItem` уже сохраняется в
  корзине.

Добавлены regression tests в
`tests/conversation/test_not_found_preemption.py`:

- manual clarification;
- новый товар для text и voice;
- сохранение старого `NOT_FOUND` item и data isolation;
- SHOW_CART, THANKS и REMOVE_ITEM;
- очистка current issue references;
- безопасная обработка случайной фразы;
- сохранение существующего product-add cancellation flow;
- фактический результат `_advance()` после независимого добавления.

Проверки:

```text
NOT_FOUND regression + related modal tests: 64 passed
baseline из раздела 16: 115 passed
полный tests/conversation: 180 passed, 3 ранее известных failures
Ruff: passed
git diff --check: passed
```

После `ADD_ITEMS("пармезан 3 кг")` старый `манго` остаётся в корзине со
статусом `NOT_FOUND`, а новый item получает собственные `source_query`,
quantity/unit и пустой comment. `_advance()` оставляет
`current_issue_item_id="mango"` при `stage=COLLECTING`; это зафиксировано как
текущее поведение и не изменялось в данном этапе.

## NEXT FUNCTIONAL STEP

Следующая функциональная задача — `DUPLICATE_PENDING`: применить тот же
глобальный parse → StateCompatibilityPolicy → continue/interrupt принцип к
открытому подтверждению дубликата. Декомпозиция остаётся отдельным разделом
`ARCHITECTURAL REFACTOR STATUS` и не заменяет roadmap.

## OPEN UX QUESTION — MODAL RESUME / FOCUS

После независимого `ADD_ITEMS` старый unresolved item сохраняется корректно,
но `_advance()` может снова оставить его в `current_issue_item_id`. Сейчас это
не считается ошибкой сохранности данных: временная парковка modal context и
новая resume policy не вводились. Вопрос — возвращать ли старый modal item в
фокус сразу после interrupt или явно парковать его — остаётся отдельной UX-задачей.

## 25. Один механический перенос после NOT_FOUND

Выполнен ровно один перенос без изменения поведения:

```text
ConversationEngine.handle()
  modal policy evaluations and interruption flags
        ↓
conversation_handlers/modal_routing.py
  ModalRoutingDecision
  evaluate_modal_routing()
```

Новый модуль зависит только от domain-моделей и
`StateCompatibilityPolicy`; зависимости `routing -> engine` нет. Policy,
intent priority, state transitions, callbacks, prompts, parser, matching и UX
не изменялись. Compatibility proxy не потребовался. Из `engine.py` вынесено
примерно 25 строк orchestration-кода.

Проверки механического переноса:

```text
MISSING_QTY / COMMENT_SCOPE / CANDIDATE_SELECTION / NOT_FOUND regression: passed
baseline: 115 passed
full tests/conversation: 180 passed, 3 ранее известных failures
Ruff: passed
git diff --check: passed
```

`NEXT FUNCTIONAL STEP` остаётся `DUPLICATE_PENDING`; следующий этап снова будет
функциональным, а не декомпозицией.

## 26. Анализ DUPLICATE_PENDING (код приложения не менялся)

### Текущий pipeline

1. Глобальный parse выполняется в `UpdateOrchestrator._parse_text_in_context()`:
   сначала `self.openai.parse_text(text)`, затем contextual fallback для pending comment
   и visible actions. Для voice после transcription используется тот же callback
   `_parse_text_in_context()`.
2. `ConversationEngine.handle()` первым вызывает `evaluate_modal_routing()`, но сейчас
   policy оценивает только `QUANTITY`, `COMMENT_SCOPE`, `CANDIDATE_SELECTION` и `NOT_FOUND`.
   Для `DUPLICATE_PENDING` отдельного решения нет.
3. Затем engine выполняет state-aware rewrites (`_contextual_negative_command`,
   `_contextual_quantity_command`, `_contextual_voice_command`) и только после них
   вызывает `PendingQuantityHandler`.
4. `PendingQuantityHandler` принимает `DUPLICATE_PENDING` как один из статусов
   quantity-flow. Короткое количество записывается в текущий duplicate item, а затем
   возвращается `CONFIRM_CURRENT`; `_confirm_current()` складывает quantity с существующей
   строкой и переводит duplicate в `SKIPPED`.
5. Независимый `ADD_ITEMS` проходит обычную ветку добавления, а `_advance()` снова выбирает
   unresolved item по приоритету `DUPLICATE_PENDING`.

### Где создаётся duplicate

- При обычном `ADD_ITEMS` в `ConversationEngine.handle()` после `_find_duplicate()`:
  новый item получает `status=DUPLICATE_PENDING`, `issue_message=existing.id`,
  `duplicate_existing_quantity` и `duplicate_existing_unit`.
- При `_select_candidate()` после применения выбранного кандидата выполняется тот же
  `_find_duplicate()` и заполняются те же поля.
- Обычно stage остаётся `COLLECTING`: `_advance()` при наличии unresolved card возвращает
  issue reply и не переводит stage. Если duplicate появился после add-more prompt, engine
  перед добавлением явно переводит stage из `AWAIT_ADD_MORE_CONFIRM` в `COLLECTING`.
  Отдельного `DUPLICATE_PENDING` stage нет.

### Поля состояния

`CartItem` хранит:

- `status=DUPLICATE_PENDING`;
- `issue_message` — id существующей строки, с которой требуется merge;
- `duplicate_existing_quantity` и `duplicate_existing_unit` — снимок данных существующей
  строки для карточки подтверждения;
- обычные `source_query`, quantity/unit, catalog metadata и comment.

`ConversationState` хранит общий modal focus:
`current_issue_item_id`, `current_issue_kind=IssueKind.DUPLICATE`, `stage` и
`pending_added_items_count`. Отдельного pending duplicate объекта или suspended context нет.
`_UNRESOLVED_PRIORITY` ставит `DUPLICATE_PENDING` первым, поэтому старый duplicate остаётся
активным после добавления другой позиции.

### Существующие ответы duplicate

Текст/голос:

- короткое количество обрабатывает `PendingQuantityHandler`;
- `_contextual_voice_command()` распознаёт подтверждение merge и возвращает
  `MERGE_DUPLICATE`, а отказ — `SKIP_CURRENT`;
- `_contextual_negative_command()` превращает отмену/отрицание merge в `SKIP_CURRENT`.

Callback:

- `v2:dupmerge:<index>:<revision>` → `MERGE_DUPLICATE` → `_confirm_current()`;
- `v2:skip:<index>` → `SKIP_CURRENT` → `_skip_current()`;
- `v2:unitedit:<index>` → `UNIT_EDIT` и существующий ввод quantity в catalog unit.

Другие используемые engine intents — `CONFIRM`, `CANCEL`, `ENTER_OTHER_QUANTITY`,
`KEEP_CURRENT_QUANTITY`/`KEEP_MULTIPLE`, `UNIT_OK` — должны быть разделены policy на
действительно совместимые ответы и независимые команды; отдельный новый intent не нужен.

### Фактическая проверка независимого intent

Воспроизведение через `ConversationEngine.handle()` с существующей строкой quantity=2,
старым duplicate quantity=3 и новым `ADD_ITEMS` quantity=2 показало:

```text
до interrupt: MATCHED(2), DUPLICATE_PENDING(3), current_issue=duplicate, stage=COLLECTING
после ADD_ITEMS нового товара: MATCHED(2), DUPLICATE_PENDING(3), MATCHED(2)
current_issue по-прежнему у старого duplicate, current_issue_kind=duplicate, stage=COLLECTING
```

То есть данные нового товара не перенеслись в старый duplicate и старый context не был
удалён. Одновременно `_advance()` немедленно возвращает фокус к старому duplicate — это
зафиксированный открытый UX-вопрос modal resume/focus, а не потеря данных.

Для прямых `SHOW_CART`, `THANKS` и неизвестной фразы на том же состоянии проверка также
не изменила cart и сохранила `current_issue_item_id`. `REMOVE_ITEM` без target снимает
текущий duplicate через `_skip_current`; `REMOVE_ITEM` с названием, совпадающим сразу с
существующей и duplicate строкой, может не выбрать строку: `_find_cart_item()` возвращает
ничего при равном score. Это существующее ограничение адресного удаления, которое нужно
проверить отдельным regression test до изменения policy.

### Возможные места подмены global command

- Главный риск — duplicate не участвует в `StateCompatibilityPolicy`, поэтому до
  `PendingQuantityHandler` нет единого `CONTINUE/INTERRUPT/AMBIGUOUS/REJECT` решения.
- При отсутствии policy-флага duplicate проходит через state-aware voice/negative/quantity
  rewrites. Они безопасны для проверенных merge/skip фраз, но могут переписать command до
  общей маршрутизации.
- Candidate fallback относится только к `AMBIGUOUS`; duplicate его не вызывает.
- Visible-action matching в orchestrator вызывается после global parse для `UNKNOWN` или
  `ADD_ITEMS` без items. Для сильного concrete `ADD_ITEMS`, `SHOW_CART`, `REMOVE_ITEM` и
  `THANKS` global command обычно сохраняется, но duplicate-specific policy в этой точке
  отсутствует. Это нужно покрыть тестом, а не решать новым списком фраз.

### Предлагаемая точка подключения

Добавить `CompatibilityContext.DUPLICATE_PENDING` в уже существующую
`StateCompatibilityPolicy`, распознавать его в `context_for()` и добавить одно поле в
`ModalRoutingDecision`/`evaluate_modal_routing()`. `ConversationEngine.handle()` должен
использовать это решение перед duplicate-specific rewrites и `PendingQuantityHandler`:

```text
global ParsedCommand
↓
evaluate_modal_routing() / StateCompatibilityPolicy(DUPLICATE_PENDING)
↓
INTERRUPT → обычный routing
CONTINUE  → существующий duplicate/quantity handler
AMBIGUOUS → безопасное уточнение без mutation
REJECT    → безопасный отказ/повтор
```

Семантическое правило должно остаться только в policy; orchestrator и engine не должны
получить второй список independent intents. Callback остаётся явным UI-путём, но его
валидность также проходит через тот же state context на уровне engine.

### Матрица совместимости (для следующего этапа)

| Incoming ParsedCommand | DUPLICATE_PENDING |
|---|---|
| `MERGE_DUPLICATE`, валидный `CONFIRM`, существующее короткое quantity | CONTINUE |
| `SKIP_CURRENT`, валидный отказ/`CANCEL` по текущему duplicate | CONTINUE |
| `UNIT_EDIT`/`UNIT_OK`/`ENTER_OTHER_QUANTITY`, если это текущий unit-flow | CONTINUE |
| concrete `ADD_ITEMS` с новой товарной позицией | INTERRUPT |
| `SHOW_CART`, `REMOVE_ITEM`, `THANKS`, `START_NEW_ORDER`, `CLEAR_CART`, `ORDER_STATUS` и другие сильные независимые intent | INTERRUPT |
| `UNKNOWN`/случайная фраза без надёжного duplicate ответа | AMBIGUOUS |
| явно невалидный duplicate callback/ответ | REJECT |

При `INTERRUPT` старый `CartItem` и его `issue_message` должны сохраняться; новый item не
получает quantity, unit, comment, candidates или duplicate metadata старого item. При
удалении старого item нужно отдельно проверить очистку `current_issue_item_id`,
`current_issue_kind` и отсутствие битой ссылки `issue_message`.

### Suspended context

Дополнительный `suspended_interaction` сейчас не нужен: `CartItem` и его duplicate metadata
уже сериализуются внутри `ConversationState.cart`, а independent `ADD_ITEMS` фактически
сохранил старую карточку. Следует сначала подключить policy и покрыть сохранение/удаление
ссылок тестами. Вопрос возврата фокуса после interrupt остаётся отдельным UX-решением;
`_advance()` в этом этапе менять нельзя.

### План regression-проверок после подтверждения

- duplicate + `MERGE_DUPLICATE`/короткое количество → merge;
- duplicate + `SKIP_CURRENT`/voice negative → skip;
- duplicate + concrete `ADD_ITEMS` text и voice → новый item, старый duplicate/context без изменений;
- duplicate + `SHOW_CART`, `REMOVE_ITEM`, `THANKS`, `START_NEW_ORDER`, `CLEAR_CART`,
  `ORDER_STATUS` → interrupt без применения duplicate ответа;
- duplicate + случайная фраза → ambiguous, draft и context без mutation;
- callback `dupmerge`, `skip`, `unitedit` → прежний explicit path;
- удаление duplicate по current id и по имени при равных строках — отдельно зафиксировать
  текущую адресность.

`NEXT FUNCTIONAL STEP` остаётся `DUPLICATE_PENDING`. Код приложения, prompts, parser,
matching, `_advance()` и state policy в рамках анализа не изменялись.

## 27. Реализация DUPLICATE_PENDING

Этап реализован отдельным функциональным diff без продолжения декомпозиции.

Изменения:

- `state_compatibility.py` получил контекст `DUPLICATE_PENDING` и использует
  существующую `StateCompatibilityPolicy` для решения `CONTINUE`, `INTERRUPT` или
  `AMBIGUOUS` до duplicate-handler.
- конкретный независимый `ADD_ITEMS` прерывает duplicate-flow и проходит обычный
  маршрут добавления товара; старый item со статусом `DUPLICATE_PENDING`, его
  `issue_message` и duplicate-данные сохраняются;
- `SHOW_CART`, `REMOVE_ITEM`, `THANKS`, `START_NEW_ORDER`, `CLEAR_CART`,
  `ORDER_STATUS` и другие сильные независимые intent не подменяются duplicate
  fallback-логикой;
- короткие ответы текущего duplicate-flow по-прежнему продолжают обработку;
- случайная фраза не меняет draft и получает безопасное уточнение;
- text и voice используют один и тот же global-parse → policy → handler/routing путь;
- `_find_cart_item()` и `_advance()` не изменялись. `suspended_interaction` не добавлялся.

Для нового товара не переносятся quantity, unit, comment, candidates или duplicate
metadata старого item. При удалении current duplicate существующая очистка ссылок
сохраняется.

Добавлен regression-файл
`tests/conversation/test_duplicate_pending_preemption.py` (9 тестов): независимое
добавление text/voice, продолжение коротким количеством, SHOW_CART, THANKS, случайная
фраза и удаление duplicate-контекста.

Проверки:

- новые duplicate-тесты: 9 passed;
- baseline-набор: 115 passed;
- связанный modal/voice/candidate набор: passed;
- полный `tests/conversation`: 180 passed и 3 известных ранее существовавших падения;
- полный репозиторный suite: 45 падений в текущем dirty checkout; они сосредоточены
  в ранее изменённых AI/parser/photo/voice/catalog/supplier тестах и не воспроизводятся
  в целевом modal baseline. Отдельное duplicate-падение — только старое ожидание текста
  «Товар уже в черновике» вместо текущего «Товар уже есть в черновике»;
- Ruff для изменённых модулей: passed;
- `git diff --check`: passed.

Известные baseline-падения не относятся к этому diff:

1. `test_product_sent_from_add_more_prompt_opens_duplicate_in_collecting_stage` —
   различие текста «уже в черновике»/«уже есть в черновике»;
2. `test_late_global_comment_applies_to_existing_and_new_items_without_overlap` —
   старое ожидание `catalog_comment="тест"` при фактическом пустом значении;
3. `test_manual_action_without_an_open_item_uses_source_recovery_card` — старое
   ожидание emoji в подписях кнопок при текущем стиле без emoji.

## NEXT FUNCTIONAL STEP

Следующий функциональный этап roadmap: `UNIT_MISMATCH` с тем же единым порядком
`GLOBAL PARSING → StateCompatibilityPolicy → CONTINUE / INTERRUPT / AMBIGUOUS / REJECT`.

## ARCHITECTURAL REFACTOR STATUS

Декомпозиция приостановлена. В рамках текущего этапа переноса файлов и изменения
публичных контрактов не выполнялось.

## LARGE MODULE DEBT

Размеры по текущему checkout; это только зафиксированный долг и не заменяет
функциональный roadmap:

| Модуль | Строк | Статус |
|---|---:|---|
| `services/engine.py` | 3056 | ACTIVE DECOMPOSITION |
| `services/orchestrator.py` | 1787 | TODO |
| `services/parser.py` | 1505 | TODO |
| `integrations/openai_parsing.py` | 1455 | TODO |
| `services/submission.py` | 1009 | TODO |
| `integrations/openai_client.py` | 904 | TODO |
| `services/venue_registration.py` | 808 | TODO |
| `integrations/google_sheets.py` | 674 | WATCH |
| `services/matching.py` | 657 | WATCH |
| `services/product_parser.py` | 593 | OK |
| `integrations/openai_prompts.py` | 2378 | INTENTIONALLY LARGE |

## OPEN ISSUE — AMBIGUOUS CART TARGET RESOLUTION

Если несколько строк имеют одинаковый score для `REMOVE_ITEM` по названию,
текущий `_find_cart_item()` не выбирает произвольную строку. Исправление адресности
оставлено отдельной задачей и в этап `DUPLICATE_PENDING` не входит.

## 28. Анализ UNIT_MISMATCH (код приложения не менялся)

### Текущий pipeline

`CartItem` получает `status=UNIT_MISMATCH` в трёх рабочих местах:

1. `_apply_catalog()` — после сопоставления товара, если одновременно заданы
   пользовательские `item.unit` и `catalog_unit`, но их нормализованные значения
   различаются. Это сравнение единиц, а не количества.
2. `_edit_quantity()` — если при редактировании уже существующей позиции
   `command.edit_unit` отличается от `item.catalog_unit`.
3. `PendingQuantityHandler` — когда короткий ответ на `MISSING_QTY` содержит
   единицу, отличную от каталожной.

После выбора каталога сохраняются `quantity`, пользовательская `unit`,
`catalog_unit`, `catalog_product_id`, `catalog_name`, `comment` и обычные
каталожные metadata. Состояние диалога хранит ссылку через
`current_issue_item_id`, а `_advance()` выставляет `current_issue_kind=UNIT`.
Отдельное поле `unit_item_index` существует в модели, но в текущем коде не
устанавливается и используется только при очистке transient-состояния.

`UNIT_MISMATCH` не имеет собственного обязательного stage. При обычном добавлении
позиции stage остаётся `COLLECTING`; callback `UNIT_EDIT` переводит его в
`AWAIT_UNIT_QUANTITY`; при редактировании количества из review stage может остаться
`REVIEW`. `_advance()` не меняет stage, если находит unresolved item.

### Текущий handler и действия

Следующий ввод обрабатывается `ConversationEngine.handle()`. До обычного intent
routing он вызывает `_contextual_negative_command()`,
`_contextual_quantity_command()` и для voice `_contextual_voice_command()`.
Именно эти функции уже понимают свободные ответы unit-flow и переписывают их в
существующие intent:

| ParsedCommand/контекстный результат | Текущее действие |
|---|---|
| `USE_CATALOG_UNIT` / `UNIT_OK` | `_use_catalog_unit()`; при необходимости конвертирует количество, ставит `MATCHED` |
| `UNIT_EDIT` / `ENTER_OTHER_QUANTITY` | `_enter_other_quantity()`; открывает ввод количества в `catalog_unit` |
| `EDIT_QUANTITY` | `_edit_quantity()`; при несовпадающей единице оставляет `UNIT_MISMATCH` |
| `SKIP_CURRENT` | `_skip_current()` |
| короткое число/единица после contextual recovery | `PendingQuantityHandler`; он уже умеет `UNIT_MISMATCH` |

Для обычной карточки `issue_reply()` показывает «Ввести количество в …»
(`v2:unitedit:<index>`) и «Не добавлять» (`v2:skip:<index>`). При наличии
расчётной фасовки есть `v2:qty:<item_id>:<count>`. Парсер также сохраняет
явные callback actions `unitok`, `use_catalog_unit`, `enter_quantity` и
`unitedit`; callback остаётся отдельным UI-путём.

### Найденное место hijack

`evaluate_modal_routing()` сейчас не содержит `UNIT_MISMATCH`, а
`StateCompatibilityPolicy.context_for()` его не обнаруживает. Поэтому в
`ConversationEngine.handle()` отсутствует `unit_interrupted`-guard, и перед
обычным routing выполняются contextual-переписывания.

Фактическая локальная репродукция:

```text
старый item: Chicken, quantity=4 pcs, catalog_unit=kg, status=UNIT_MISMATCH
global ParsedCommand: ADD_ITEMS, Parmesan, quantity=3 kg
результат текущего handle(): один item Chicken, status=MATCHED, quantity=3 kg
```

То есть `Пармезан 3 кг` не создаётся как новый item: `_contextual_quantity_command()`
видит текущий `UNIT_MISMATCH`, извлекает `3 kg` и возвращает `EDIT_QUANTITY` для
старой позиции. Это первый неверный переход. Parser и callback здесь не являются
источником ошибки.

Для `SHOW_CART`, `THANKS`, `START_NEW_ORDER`, `CLEAR_CART`, `ORDER_STATUS` и
прочих независимых intent часть текущих contextual-функций обычно уже оставляет
команду без переписывания, но это не формализовано общей policy и не защищает
`REMOVE_ITEM`/новый `ADD_ITEMS` от state-specific перехвата.

### Предлагаемое подключение

Добавить только следующий слой, без нового intent и без изменения parser/prompts:

```text
global ParsedCommand
  ↓
evaluate_modal_routing()
  ↓
StateCompatibilityPolicy(UNIT_MISMATCH)
  ↓
CONTINUE / INTERRUPT / AMBIGUOUS / REJECT
  ↓
existing unit handler или обычный routing
```

`context_for()` должен проверять реальный `current_item.status` и возвращать
`UNIT_MISMATCH` независимо от того, был ли stage `COLLECTING`, `REVIEW` или
`AWAIT_UNIT_QUANTITY`; duplicate-контекст, если он уже возник, должен сохранять
свой более высокий приоритет.

Policy не должна разбирать natural language. Для коротких числовых ответов и
voice-фраз без самостоятельного товара нужно сохранить существующий
`PendingQuantityHandler`/contextual recovery. Конкретный `ADD_ITEMS` с реальным
названием и количеством определяется через уже существующий helper
`PendingQuantityHandler.has_named_product_items()` и получает `INTERRUPT`.

### Compatibility matrix

| Incoming ParsedCommand | UNIT_MISMATCH |
|---|---|
| `USE_CATALOG_UNIT`, `UNIT_OK` для текущей позиции | CONTINUE |
| `UNIT_EDIT`, `ENTER_OTHER_QUANTITY` для текущей unit-карточки | CONTINUE |
| `EDIT_QUANTITY`/короткое количество, относящееся к текущей карточке | CONTINUE |
| `SKIP_CURRENT`/явная отмена текущей позиции | CONTINUE |
| concrete `ADD_ITEMS` (`пармезан 3 кг`, `добавь укроп 2 кг`) | INTERRUPT |
| `SHOW_CART`, `REMOVE_ITEM`, `THANKS`, `START_NEW_ORDER`, `CLEAR_CART`, `ORDER_STATUS` и другие independent intent | INTERRUPT |
| случайная/неконкретная фраза без доказанного unit-ответа | AMBIGUOUS |
| явно невалидный unit callback/ответ | REJECT или существующий safe retry |

При `INTERRUPT` старый item, его quantity/unit/catalog_unit/comment и issue-ссылка
не меняются. Новый item получает только данные нового `ParsedCommand`. При
`AMBIGUOUS` draft и unit-контекст не мутируют.

### Text, voice, callback и resume

Text проходит `orchestrator._parse_text_in_context()` → global OpenAI/deterministic
parse → `engine.handle()`. Voice сначала транскрибируется в
`InputRecognitionService`, затем передаёт тот же `ParsedCommand` в этот же engine;
отдельного voice unit-flow нет. Callback `v2:unitedit`, `v2:qty`, `v2:skip` остаётся
явным путём и не должен проходить через natural-language policy.

Дополнительный `suspended_interaction` не нужен: весь unit-контекст уже находится
в `CartItem` и `ConversationState`. После независимого добавления `_advance()`
сохраняет старый `UNIT_MISMATCH` как current issue по приоритету
`DUPLICATE_PENDING → UNIT_MISMATCH → MISSING_QTY ...`; stage не переключается
автоматически. Для `COLLECTING` это оставляет `COLLECTING`, а если до interrupt
был `AWAIT_UNIT_QUANTITY`, он также сохраняется. Это существующий
`OPEN UX QUESTION — MODAL RESUME / FOCUS`, менять его в следующем patch нельзя.

Удаление текущей unit-позиции через `_remove_item()` очищает
`current_issue_item_id`/`current_issue_kind`, после чего `_advance()` выбирает
следующую unresolved-позицию. `unit_item_index` отдельно очищается общим
transient cleanup и сейчас не содержит активной ссылки.

### Regression plan (до реализации)

- valid `USE_CATALOG_UNIT`/`UNIT_EDIT`/quantity response → CONTINUE;
- concrete text и voice `ADD_ITEMS` → INTERRUPT, новый item отдельно;
- `SHOW_CART`, `REMOVE_ITEM`, `THANKS`, `START_NEW_ORDER`, `CLEAR_CART`,
  `ORDER_STATUS` → INTERRUPT;
- random phrase → AMBIGUOUS без изменения draft;
- data isolation старого и нового item;
- удаление текущего `UNIT_MISMATCH` очищает references;
- callback contract остаётся прежним;
- после independent `ADD_ITEMS` фиксируется фактический `_advance()`/resume.

Код приложения, parser, prompts, matching, duplicate flow, candidate flow,
NOT_FOUND, comments, quantity semantics, `_find_cart_item()` и `_advance()` в
рамках этого анализа не изменялись. 45 failures полного dirty checkout остаются
отдельным `GLOBAL TEST BASELINE / TEST DEBT` и не исправляются в этом этапе.

`NEXT FUNCTIONAL STEP` остаётся `UNIT_MISMATCH`; реализация ожидает отдельного
подтверждения.

## 29. Реализация UNIT_MISMATCH

Этап `UNIT_MISMATCH` реализован минимальным функциональным diff и завершён.

### Что изменено

- `StateCompatibilityPolicy` получил `CompatibilityContext.UNIT_MISMATCH`.
- `context_for()` распознаёт активную позицию со статусом `UNIT_MISMATCH`.
- `evaluate_modal_routing()` возвращает решение для unit mismatch одним общим policy-вызовом.
- `ConversationEngine.handle()` не передаёт независимую команду в contextual quantity/voice recovery,
  если policy вернула `INTERRUPT`.
- При `AMBIGUOUS` сохраняется текущая unit-карточка без изменения draft.
- Существующие unit callbacks и обработчики продолжения остаются прежними.

### Контракт поведения

- Конкретный `ADD_ITEMS` с новым товаром прерывает unit flow; старый item сохраняет quantity, unit,
  catalog_unit, comment, status и current issue reference.
- Новый item обрабатывается только из своего `ParsedCommand` и не получает данные старого item.
- `USE_CATALOG_UNIT`, `UNIT_OK`, `UNIT_EDIT`, `ENTER_OTHER_QUANTITY`, допустимое короткое количество
  и `SKIP_CURRENT` продолжают текущий unit flow.
- `SHOW_CART`, `REMOVE_ITEM`, `THANKS`, `START_NEW_ORDER`, `CLEAR_CART`, `ORDER_STATUS` и другие
  независимые intent не перехватываются unit handler.
- Случайная фраза даёт безопасное уточнение и не меняет draft.
- Text и voice используют один и тот же policy path; callback остаётся отдельным явным UI-путём.
- `suspended_interaction` не добавлялся; текущие `CartItem` и `ConversationState` сохраняют контекст.

### Изменённые файлы

- `src/restaurant_bot/services/conversation_handlers/state_compatibility.py` — policy и контекст.
- `src/restaurant_bot/services/conversation_handlers/modal_routing.py` — агрегирование решения.
- `src/restaurant_bot/services/engine.py` — единая блокировка contextual preemption для unit mismatch.
- `tests/conversation/test_unit_mismatch_preemption.py` — regression tests для text/voice, unit actions,
  независимых intent, случайной фразы и очистки issue reference.

Парсер, prompts, matching, каталоговые semantics, comments, quantity semantics, другие modal states,
`_find_cart_item()` и `_advance()` в этом этапе не изменялись.

### Проверки

- UNIT и связанные modal regression tests: **126 passed**.
- `ruff check` для изменённых модулей и теста: **passed**.
- `git diff --check`: **passed**.
- `tests/conversation`: **180 passed, 3 failures**; это известные ранее существовавшие падения,
  не связанные с UNIT_MISMATCH: текст duplicate prompt, каталоговый comment в late global comment,
  emoji в manual-action button.
- Текущий dirty baseline voice-contract: **9 известных failures** в
  `tests/input/test_voice_input_contract.py`; они относятся к ранее изменённым parser/postprocessing/
  UX-контрактам и не затрагивают этот diff. Полный dirty checkout ранее фиксировал 45 failures как
  отдельный test debt.

`NEXT FUNCTIONAL STEP`: **AWAIT_MANUAL_DETAILS**.

## 30. AWAIT_MANUAL_DETAILS (implemented)

`UNIT_MISMATCH` подтверждён как DONE. Этап **AWAIT_MANUAL_DETAILS — DONE**.

### 1. Реальный flow и call sites

`SessionStage.AWAIT_MANUAL_DETAILS` объявлен в `src/restaurant_bot/domain/models.py:125`.
Единственное присваивание stage находится в `ConversationEngine.handle()` (`src/restaurant_bot/services/engine.py:580-584`) при `Intent.MANUAL_CURRENT`:
выбирается callback-target (если он передан), сохраняется `current_issue_item_id`, затем stage переводится в ожидание нового названия.

В этот flow пользователь попадает из двух карточек unresolved товара:

- `AMBIGUOUS`: кнопка `Изменить название` (`replies.py:731-735`) или contextual voice rewrite (`engine.py:1452-1460`);
- `NOT_FOUND`: кнопка `Изменить название` (`replies.py:797-805`) или contextual voice rewrite (`engine.py:1475-1484`).

Также callback `v2:rename`/`v2:manual` маппится в `MANUAL_CURRENT` в `services/parser.py:1367-1368`, поэтому технически callback может открыть manual flow для любого явно переданного item index.

Отдельного присваивания `manual_item_index` нет: поле объявлено в `ConversationState`, но в рабочем коде только сбрасывается в `_clear_transient_dialog_state()` (`engine.py:1964`). Реальная привязка manual flow — `current_issue_item_id`; `current_issue_kind` сохраняет прежний `IssueKind` и при входе не меняется.

### 2. Смысл stage и UI-контракт

На входе пользователь получает из `ConversationEngine.handle()` сообщение:

`✏️ Измените название` → `Текущий запрос: ...` → `Отправьте новое название товара.`

Кнопки: `Не добавлять` (`v2:skip:<index>`) и `К черновику` (`v2:back`). Текстовый и голосовой ответ — новое название товара; отдельного manual parser нет. Callback-пути остаются явными: `skip` идёт в `SKIP_CURRENT`, `back` — в `BACK`.

Обычно current item имеет `NOT_FOUND` или `AMBIGUOUS`; исходные `source_query`, quantity/unit, comment, supplier/catalog metadata и issue data уже находятся в `CartItem`. Для `ConversationState` реально используются `current_issue_item_id`, сохранённые `current_issue_kind`, `stage` и общий transient state. `pending_comment_*`, product-add refs и другие pending-поля manual flow сам не создаёт.

### 3. Текущий handler и мутации

Отдельного `ManualDetailsHandler` нет. Путь такой:

`ConversationEngine.handle()` → initial `evaluate_modal_routing()` → общие contextual rewrites → ветка `Intent.ADD_ITEMS` → `engine.py:750-761`.

Эта ветка получает уже готовый `ParsedCommand`, но при `state.stage == AWAIT_MANUAL_DETAILS` и `current_issue_item_id` изменяет существующий item:

- `source_query` заменяется на `command.items[0].product_query`;
- quantity/unit заменяются только если новые значения truthy;
- status становится `NEW`;
- candidates очищаются;
- `rename_attempted=True`;
- stage становится `COLLECTING`;
- выполняются `_match_item(current, catalog)` и `_advance(state)`.

`comment`, supplier и catalog metadata этой веткой напрямую не переносятся, но старые значения остаются на перезаписанном item. Новая позиция не строится через `_build_item()` и не добавляется в `cart`.

### 4. Граница global parsing и первый hijack

Для TEXT `UpdateOrchestrator._parse_text_in_context()` (`orchestrator.py:1121-1172`) сначала вызывает `self.openai.parse_text(text)`, затем только при необходимости применяет pending-comment и visible-action contextual fallback. Для VOICE транскрипция (`input_recognition.py:120-130`) вызывает тот же callback `parse_text`, поэтому отдельного manual voice bypass нет. Callback обрабатывается отдельным явным путём.

До реализации policy вычислялась до contextual rewrites (`engine.py:153-163`), но `ModalRoutingDecision`/`CompatibilityContext` не имели manual context. Поэтому concrete `ADD_ITEMS` не блокировал manual branch. Теперь этот контекст вычисляется до status-based fallback и блокирует hijack.

Фактическая проверка с `AWAIT_MANUAL_DETAILS` и старым `NOT_FOUND` item:

| Вход | Фактический результат |
|---|---|
| `пармезан 3 кг` | cart остаётся из одной строки; старый item получает `source_query=пармезан`, quantity `3`, unit `кг`; новая строка не создаётся |
| `добавь укроп 2 кг` | аналогично перезаписывает старый item значениями укропа |
| voice после транскрипции `пармезан три килограмма` | тот же результат, что и TEXT |
| `ну потом` / `не знаю` / `как-нибудь` при искусственно сформированном `ADD_ITEMS` без quantity | старый item также перезаписывается новым текстом; это показывает защитный пробел, если upstream нарушил контракт `UNKNOWN` для случайной фразы |

Первый неверный переход был в `engine.py:750`, где manual branch игнорировал уже рассчитанное решение `StateCompatibilityPolicy(NOT_FOUND)=INTERRUPT` и забирал concrete `ADD_ITEMS` себе. Теперь branch выполняется только при `MANUAL_DETAILS=CONTINUE`; concrete команда проходит обычный ADD_ITEMS routing.

### 5. Уже существующие intents и фактические переходы

| Intent | Текущий путь | Наблюдаемая мутация |
|---|---|---|
| `ADD_ITEMS` с одним названием без количества | manual branch, продолжение rename | заменяет source query старого item, stage `COLLECTING`, затем match |
| concrete `ADD_ITEMS` | должен быть независимым, но manual branch его сейчас перехватывает | старый item портится, новый не создаётся |
| `MANUAL_CURRENT` | устанавливает stage и prompt | CartItem не меняется |
| `SKIP_CURRENT` | `_skip_current()` | item `SKIPPED`, issue refs очищаются, `_advance()` выбирает следующий unresolved/review |
| `SHOW_CART`, `THANKS`, `ORDER_STATUS` | обычные passive/navigation handlers | manual item и refs сохраняются; stage обычно остаётся manual |
| `START_NEW_ORDER` | обычный routing | при активном draft создаётся pending new-order confirmation, старый item сохраняется |
| `CLEAR_CART` | обычный routing | draft очищается, transient refs сбрасываются, stage `COLLECTING` |
| `REMOVE_ITEM` по текущему item | `_remove_item()` | item `SKIPPED`, `current_issue_item_id` и `current_issue_kind` очищаются, pending comment IDs pruning сохраняется |
| `CLARIFY_CURRENT`, `CONTINUE_CURRENT` | `_advance()` | при unresolved item снова показывается его issue card; manual stage сам по себе не восстанавливается отдельным prompt |
| `EDIT_QUANTITY` | обычная quantity/edit ветка | может менять quantity текущего item и перевести его в `UNIT_MISMATCH`; это не manual answer |
| `SELECT_CANDIDATE` | candidate handler | для manual item без candidates безопасно не выбирает старый вариант |

Таким образом, strong independent intents маршрутизируются централизованно, а manual stage получил отдельный policy-gate перед `ADD_ITEMS` branch.

### 6. Random input, REJECT и data isolation

При корректном global parse случайные фразы должны быть `UNKNOWN`; engine возвращает существующую unresolved-карточку без изменения draft. Policy не разбирает raw natural language. `UNKNOWN` получает `AMBIGUOUS`, а concrete `ADD_ITEMS` — `INTERRUPT`.

Текущий concrete `ADD_ITEMS` нарушает изоляцию: новый product name/quantity/unit записываются в старый item, а новый item не получает собственного `CartItem`, candidates, catalog metadata или issue context. Старый item теряет исходный source query и получает чужие quantity/unit. Это именно тот data leak, который должен закрыть следующий функциональный diff.

### 7. Нужен ли отдельный CompatibilityContext

Отдельный `CompatibilityContext.MANUAL_DETAILS` оправдан. `AWAIT_MANUAL_DETAILS` — semantic modal prompt, но stage используется поверх как минимум `NOT_FOUND` и `AMBIGUOUS`, а callback технически может выбрать item другого status. Оставлять manual semantics внутри `NOT_FOUND` недостаточно: `context_for()` сейчас отдаёт `CANDIDATE_SELECTION` для ambiguous item и `NOT_FOUND` для not-found item, а прямой stage-based manual flow остаётся без policy.

Минимальная архитектурная точка подключения:

`CompatibilityContext.MANUAL_DETAILS` → `StateCompatibilityPolicy` → поле решения в `ModalRoutingDecision` → `evaluate_modal_routing()` → единая проверка в `ConversationEngine` перед manual `ADD_ITEMS` branch.

Policy должна принимать готовый `ParsedCommand` и state, не иметь локального списка natural-language правил. Она разрешает только неполный manual answer как `CONTINUE`; concrete independent `ADD_ITEMS` и централизованные strong intents дают `INTERRUPT`; `UNKNOWN`/неразрешимый ответ — `AMBIGUOUS`/safe retry; явный skip/cancel — существующий безопасный путь `REJECT`/continue по действующему handler.

### 8. TEXT, VOICE, callbacks, resume и suspended context

TEXT и VOICE используют один global parse → policy → engine маршрут; отдельного manual voice bypass нет. Callback contract не меняется.

`suspended_interaction` не нужен: незавершённый item и его candidates/status уже сохраняются в `ConversationState.cart`, а focus — в `current_issue_item_id/current_issue_kind`. После реализации independent add выполняется normal append. `_advance()` выбирает первый unresolved item, оставляет stage `COLLECTING` и возвращает его issue card; автоматического возврата в отдельный manual prompt нет. Это наблюдение относится к сохранённому `OPEN UX QUESTION — MODAL RESUME / FOCUS` и в этом этапе не менялось.

Удаление текущего manual item через `REMOVE_ITEM` очищает `current_issue_item_id/current_issue_kind`, помечает item `SKIPPED` и вызывает pruning pending comment IDs. `manual_item_index` не является рабочей ссылкой и очищается общим transient reset. `OPEN ISSUE — AMBIGUOUS CART TARGET RESOLUTION` не затрагивался.

### 9. Regression plan перед implementation

Добавлены regression tests:

1. валидный manual answer без quantity → `CONTINUE`, меняется только выбранный item;
2. второй допустимый manual action, если он подтверждён текущим flow → `CONTINUE`;
3. `AWAIT_MANUAL_DETAILS + пармезан 3 кг` → `INTERRUPT`, новый item, старый item и refs без изменений;
4. `+ добавь укроп 2 кг` → тот же interrupt и isolation;
5. `SHOW_CART`, `THANKS`, `START_NEW_ORDER`, `CLEAR_CART`, `ORDER_STATUS` → ordinary routing без manual mutation;
6. `REMOVE_ITEM` текущего manual item → skip и очистка refs;
7. случайные `ну потом`, `не знаю`, `как-нибудь` → `UNKNOWN`/`AMBIGUOUS`, safe retry, draft и context без изменений;
8. voice `пармезан три килограмма` → тот же interrupt path;
9. callback `skip/back` сохраняет отдельный explicit UI path;
10. `_advance()`/resume behavior зафиксирован отдельным тестом, но не изменён.

На этом этапе не менялись parser, prompts, matching, предыдущие modal states, quantity semantics, `_advance()`, `_find_cart_item()`, persistence, transport или MAX support. `suspended_interaction` не добавлялся.

### 10. Результаты реализации

- Добавлен `CompatibilityContext.MANUAL_DETAILS`.
- `context_for()` проверяет active manual prompt до `CANDIDATE_SELECTION`, `NOT_FOUND`, `DUPLICATE_PENDING`, `UNIT_MISMATCH` и quantity contexts.
- Valid manual rename остаётся `CONTINUE`.
- Concrete `ADD_ITEMS` (`пармезан 3 кг`, `добавь укроп 2 кг`) получает `INTERRUPT` и создаёт отдельный `CartItem`.
- Manual item и его candidates/status/comment/quantity сохраняются при interrupt.
- Candidate fallback не запускается поверх manual prompt.
- TEXT и VOICE используют один policy path.
- `SHOW_CART`, `THANKS`, `REMOVE_ITEM`, callbacks `skip/back` используют существующие маршруты.
- `_advance()` и `_find_cart_item()` не менялись.

Проверки:

- manual и связанные modal/conversation regression tests — passed;
- `tests/conversation` — baseline: 3 известных failure, новых падений от этого diff не обнаружено;
- `tests/input/test_voice_input_contract.py` — 9 известных dirty failures, не связанных с этим diff;
- `ruff check` изменённых модулей и теста — passed;
- `git diff --check` — passed.

`NEXT FUNCTIONAL STEP`: **AWAIT_PRODUCT_ADD_DETAILS**.
