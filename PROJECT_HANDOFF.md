# PROJECT HANDOFF

## CURRENT IMPLEMENTATION STATUS

`SUBMISSION_FAILED` реализован. Следующая функциональная задача — `review/modal contexts`.

> Актуальный статус: этап `NOT_FOUND` завершён. Следующий функциональный этап — `DUPLICATE_PENDING`.

## CURRENT ROADMAP OVERRIDE

## PRIORITY ROADMAP OVERRIDE (LATEST)

`SUBMISSION_FAILED` завершён. Следующая функциональная задача — `review/modal contexts`.
В рамках текущего этапа recovery lock остаётся последним изменённым функциональным слоем;
новый функционал до отдельного разрешения не начинать.

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

## REVIEW / MODAL CONTEXTS - ANALYSIS COMPLETE / IMPLEMENTATION PENDING

Анализ выполнен на HEAD `2f9cafff9e5c1e551a72e0f1168af457b2e75205`, ветка
`decompose_bot`. Application code, parser, prompts, callbacks и существующие
CompatibilityContext не изменялись. Следующий функциональный этап остаётся
`review/modal contexts`; `pending_new_order_confirmation` не начинался.

### 1. Inventory: semantic context versus UI metadata

| Context / fields | Owner and lifecycle | Classification | Stale/interruption risk |
|---|---|---|---|
| `stage=REVIEW`, `review_mode="cart"`, `final_review_page`, `cart_page` | `ConversationEngine`, `FinalReviewHandler`, `replies.py`; обычный draft/final-review | обычный editable UI, не отдельный review modal | низкий; новый ADD/EDIT/REMOVE должен менять актуальный cart |
| `stage=REVIEW`, `review_mode="sheet_link"` | `UpdateOrchestrator._handle_review_command`, `OrderReviewService`; token/fingerprint до cancel/submit | отдельный semantic review context | высокий: текст/voice проходят специальный router и могут быть подменены visible action |
| `review_token`, `review_snapshot_hash`, `review_venue_code`, `review_submission_in_progress` | orchestrator при входе/refresh/cancel/submit; worker/service при submission | semantic snapshot metadata для sheet review | token/hash должны жить до fresh submit или cancel; UNKNOWN сейчас сохраняет их намеренно, что может продлить старый режим |
| `AWAIT_SUBMIT_CONFIRM` | уже покрыт `CompatibilityContext.SUBMIT_CONFIRM` | завершённый modal context | не переоткрывать в этом этапе |
| `current_issue_item_id`, `current_issue_kind` | engine/final review и issue handlers | semantic pointer к item; не generic review | stale возможен после удаления/пересчёта, существующие handlers очищают pointer |
| `edit_multiple_index`, `CartItem.suggested_quantity` | engine quantity/multiple handlers | semantic quantity context, уже покрыт quantity/modal flows | stale возможен после interrupt; не создавать новый review context |
| `order_status_view_active`, `order_status_*` | `OrderStatusHandler`, `submission.py` worker | read-only/navigation metadata | не должно определять смысл новой команды; engine снимает view для распознанной обычной команды |
| `ui_revision`, `ui_message_id`, `ui_message_text`, `visible_actions` | orchestrator/replies/Telegram adapter | UI metadata | stale callback защищается revision; visible actions опасны только если используются как semantic fallback |

Actual `review_mode` values: `"cart"` (default ordinary draft) и
`"sheet_link"` (deep-link/sheet review). Значение `"sheet"` в коде не найдено;
его нельзя вводить по аналогии.

### 2. Review intents and owners

| Intent | Source | TEXT/VOICE | CALLBACK | Mutation/effect | Owner |
|---|---|---|---|---|---|
| `REVIEW_ORDER`, `REVIEW_REFRESH` | parser/deep-link or review router | sheet review refresh | `v2:review:*` | Google Sheets read, new token/fingerprint, preview | orchestrator + `OrderReviewService.snapshot` |
| `REVIEW_SUBMIT` | review router/parser | only sheet review confirmation path | token + revision checked | fresh Sheets read, optional enqueue/external submission | orchestrator + `OrderReviewService.submit` |
| `REVIEW_CANCEL` | review router/parser | sheet review cancel/back | token + revision checked | clear sheet metadata, return to cart | orchestrator |
| `SHOW_CART`, `SHOW_FINAL_REVIEW` | global parser/callback parser | ordinary cart routing | explicit UI callback | local UI/page state | engine/`FinalReviewHandler` |
| `CHECK_MIN_SUM`, supplier intents | global parser and explicit callbacks | normal engine routing | callback target item | catalog read or item/supplier chooser | engine/supplier handlers |
| `ACCEPT_SUGGESTED_QUANTITY`, `KEEP_CURRENT_QUANTITY`, `KEEP_MULTIPLE`, `FIX_MULTIPLE`, `EDIT_MULTIPLE`, `ENTER_OTHER_QUANTITY` | parser/callback parser | quantity modal routing | explicit callback or parsed command | mutate only targeted item quantity/issue fields | engine quantity flow |

Supplier and multiple warnings are not one shared review state: their targets
are item-level fields and callback payloads. They must not be folded into a
generic `REVIEW_MODAL` policy.

### 3. Sheet-review flow and side-effect boundary

`/start review_<venue>` or `v2:review:<venue>` -> `REVIEW_ORDER` ->
`OrderReviewService.snapshot()` reads the live sheet -> preview card with
`v2:review_submit:<token>` and `v2:review_cancel:<token>` -> fresh callback
revision/token validation -> refresh on fingerprint mismatch or submit.

Before `REVIEW_SUBMIT`, this is not the ordinary cart and it does not create a
frozen `PendingSubmission`; the snapshot fingerprint is the freshness guard.
`REVIEW_SUBMIT` may enqueue the submission and perform the external side effect.
Cancel and regular recognized commands return to `review_mode="cart"` and clear
sheet metadata. Ordinary cart editing remains possible before actual submission.

### 4. Current input order and first unsafe transition

The real text path is:

`UpdateOrchestrator._parse_text_in_context()` -> global `openai.parse_text()` ->
`_parse_review_voice_command()` (despite its name this is called for TEXT too) ->
contextual comment/visible-action fallbacks -> orchestrator review dispatch or
`ConversationEngine.handle()`.

Voice transcribes first and then uses the same text path. Photo recognition
returns a parsed command directly and does not pass through the sheet-review
text router. Callbacks use explicit `parse_callback()` and token/revision guards.

The first confirmed unsafe transition is in
`_parse_review_voice_command()` for `stage=REVIEW` and
`review_mode="sheet_link"`:

1. global parsing runs;
2. direct review mappings can replace global intents with `REVIEW_*`;
3. a concrete `ADD_ITEMS` with a quantity is protected, but an `ADD_ITEMS`
   without quantity is not;
4. if no direct mapping applies, visible-action AI may map the free text to
   submit/cancel;
5. the resulting `REVIEW_*` command bypasses `ConversationEngine` and therefore
   bypasses `StateCompatibilityPolicy`.

Thus a new product without quantity, an uncertain phrase, or an independent
edit can be interpreted as a sheet-review button action. This is a routing
boundary problem, not a parser/comment/catalog problem. Existing tests confirm
the intentional but unsafe arbitrary-phrase-to-visible-submit behavior.

### 5. Current engine priority

`ConversationEngine.handle()` currently evaluates global command, voice
normalization, `evaluate_modal_routing`, stale callback revision, submission
failure recovery, submit/add-more/comment/manual/product-add/candidate and other
modal handlers, contextual rewrites, generic intents, review/final-review
handlers, and navigation. This ordering is correct for the already completed
contexts. Sheet review is the exception: orchestrator dispatches `REVIEW_*`
before engine, so it has no policy decision today.

`StateCompatibilityPolicy.context_for()` has contexts for quantity, comment,
manual/product-add details, candidate, not-found, duplicate, unit mismatch,
add-more, submit-confirm and submission-failed. It intentionally has no generic
plain-`REVIEW` context.

### 6. Priority and interrupt map

| Active context | Continue | Interrupt | Ambiguous/reject |
|---|---|---|---|
| sheet review (`sheet_link`) | explicit review refresh/submit/cancel semantics with valid token context | concrete ADD/EDIT/REMOVE, SHOW_CART, ORDER_STATUS, HELP/THANKS, START_NEW_ORDER, CLEAR_CART | uncertain free text: no submission and no cart mutation |
| ordinary cart review (`cart`) | normal final-review pagination/submit-confirm rules already implemented | normal editable cart intents | existing `SUBMIT_CONFIRM` behavior |
| supplier warning | targeted supplier callback/intent only | independent product/navigation/help intents | unknown target: no supplier mutation |
| multiple/suggested quantity | targeted quantity intent only | independent product/navigation/help intents | no guessed quantity |
| order-status view | status pagination/detail callbacks | any recognized cart/product command | unknown leaves read-only view unchanged |

Callbacks remain an explicit UI path. A stale callback must fail token/revision
validation before any review, supplier, multiple, pagination or order-status
mutation.

### 7. Draft editing and passive commands

For ordinary cart/final review, before `_prepare_submission()` the current cart
must remain editable: change quantity, remove item, add item, edit comment,
leave final review, then rebuild review from the current cart. `SUBMISSION_FAILED`
recovery is separate and must not become a lock for ordinary `REVIEW`.

`HELP`, `THANKS`, `GREETING`, and `SMALL_TALK` must never confirm, submit, choose
supplier, or choose a multiple quantity merely because a review UI is visible.

### 8. Minimal future implementation boundary

Do not add `if review_mode` logic to every handler and do not create a generic
review catch-all. The next implementation should introduce one narrow structured
context for the `sheet_link` route only (name to be decided after tests), or
reuse an equivalent policy boundary if it can be expressed without duplicating
intent sets. It must receive `ParsedCommand` plus structured state, never raw
language.

The policy decision belongs after global parsing and before
`_parse_review_voice_command()`/review dispatch. Independent commands must reach
normal routing; only a confirmed sheet-review action may continue the sheet
flow; uncertain input must preserve token/snapshot and avoid side effects.
Callbacks stay outside this text/voice policy. No parser, prompt, comment,
matching, catalog, or submission changes are part of this analysis.

### 9. Future regression matrix (implementation phase only)

For each real semantic context, cover TEXT, VOICE, PHOTO, fresh callback and
stale callback against: continue-current, ADD, REMOVE, EDIT, BACK, CANCEL,
SHOW_CART, ORDER_STATUS, HELP, THANKS, START_NEW_ORDER and CLEAR_CART. Include
sheet review with a product both with and without quantity, arbitrary text,
photo with/without recognizable products, token mismatch, revision mismatch,
fingerprint refresh, supplier warning targets, suggested/multiple quantity, and
editing every cart item before submission. No new tests were added in this
analysis-only phase.

### 10. Decomposition, MAX and agent-harness notes

No decomposition was started. Current responsibilities remain split between
orchestrator (input/review dispatch), engine (cart/state routing), final review,
navigation, replies, parser and `OrderReviewService`; a later move must be
behavior-preserving with temporary re-exports and focused regression checks.

MAX is not integrated. Review semantics currently depend on Telegram callback
data, message editing and `ui_revision`; a future adapter boundary should be
`Telegram/MAX adapter -> normalized incoming event -> conversation core ->
normalized reply`.

The agent harness remains `ANALYZE -> PLAN -> IMPLEMENT -> VERIFY -> STOP` with
structured parser output, policy authorization, deterministic side effects and
idempotency/checkpoints. This note does not authorize an autonomous production
loop.

### 11. Checks and status

- Application code was not changed.
- No tests were added or modified; the known historical full-suite failures were
  not touched.
- `git ls-files .env` is empty; GitLab was not used.
- `git diff --check` and the markdown-link check are required for this docs-only
  commit.
- `NEXT FUNCTIONAL STEP` remains `review/modal contexts`.

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

## 31. AWAIT_PRODUCT_ADD_DETAILS — analysis complete / implementation pending

Этап **AWAIT_PRODUCT_ADD_DETAILS** проанализирован. В этом этапе код приложения не менялся.
Следующая функциональная задача остаётся `AWAIT_PRODUCT_ADD_DETAILS`.

### Реальный flow

1. Для `NOT_FOUND` или `AMBIGUOUS` позиции кнопка `v2:addreq:<index>` разбирается в
   `Intent.PRODUCT_ADD` (`services/parser.py`).
2. `ConversationEngine.handle()` (`services/engine.py:429-446`) проверяет текущий item,
   создаёт/сохраняет `CartItem.product_add_request_id`, заполняет
   `pending_product_add_item_index` и `pending_product_add_request_id`, переводит state в
   `SessionStage.AWAIT_PRODUCT_ADD_DETAILS` и показывает `product_add_prompt()`.
3. Prompt просит одним сообщением передать название, бренд, фасовку/объём и другие детали;
   существующие примеры — «Мисо-паста Genzo, 1 кг» и «Краб камчатский М/Л, 6 кг».
   Поэтому название + quantity/unit в этом состоянии может быть корректным описанием
   текущего товара, а не новой строкой заявки.
4. После ответа `engine.py:632-684` создаёт запись в `product_add_requests` с
   `source_item_id`, `original_query`, полным `description`, `description_event_key` и
   метаданными Telegram; исходный item получает `SKIPPED`, очищается
   `current_issue_item_id`, state становится `REVIEW`, а результат ставит
   `enqueue_product_add=True`.
5. Orchestrator ставит `submit_product_add` в Celery (`orchestrator.py:1753-1756`).
   `SubmissionService.submit_product_add()` (`submission.py:510-568`) повторно блокирует
   сессию, находит request по `pending_product_add_request_id`, пишет только description в
   лист добавления товара и сохраняет статус `submitted`, `write_failed` или
   `write_uncertain`. При применении результата очищаются
   `pending_product_add_request_id` и `product_add_write_in_progress`; завершённый request
   остаётся в `product_add_requests`, а итоговое подтверждение и свежий draft строятся
   отдельно.

### Состояние и идентификаторы

- `ConversationState.pending_product_add_item_index` — индекс исходного unresolved item.
- `ConversationState.pending_product_add_request_id` — request, ожидающий записи.
- `ConversationState.product_add_write_in_progress` — флаг отложенной записи.
- `ConversationState.product_add_requests` — отдельная история запросов снабженцу.
- `CartItem.product_add_request_id` — стабильная связь item с request.
- `current_issue_item_id/current_issue_kind` — текущая issue-ссылка; при успешном описании
  `current_issue_item_id` очищается, а `current_issue_kind` явно не сбрасывается этим блоком.

`new_product_add_request_id()` формирует устойчивый request ID из времени, очищенного item ID
и случайного суффикса. `clear_product_add_pending()` сбрасывает только три временных поля
(`pending_product_add_item_index`, `pending_product_add_request_id`,
`product_add_write_in_progress`) и не удаляет `product_add_requests`.

### Первый unsafe transition

Первое неверное решение находится не в parser, а в `ConversationEngine.handle()` на
`engine.py:632`. Блок

```python
if state.stage == SessionStage.AWAIT_PRODUCT_ADD_DETAILS:
    description = (command.text or event.text).strip()
```

срабатывает после общих passive/navigation/final handlers, но до обычной ветки
`Intent.ADD_ITEMS` (`engine.py:688+`). Если строка непустая, она без дополнительного
решения считается описанием и сразу создаёт product-add request. Поэтому `ADD_ITEMS` и
`UNKNOWN` с текстом не доходят до обычного добавления товара и могут изменить исходный
unresolved item на `SKIPPED`.

Уже распознанные независимые intents чаще всего не доходят до этого блока:

| ParsedCommand intent | Текущий путь до generic product-add block | Изменение product-add request |
|---|---|---|
| `GREETING`, `HELP`, `THANKS`, `SMALL_TALK` | `PassiveIntentHandler` | не создаётся; state/pending обычно сохраняются |
| `SHOW_CART`, `START_NEW_ORDER`, `CLEAR_CART` | обычная navigation ветка | `SHOW_CART` сохраняет pending; `START_NEW_ORDER` открывает подтверждение; `CLEAR_CART` очищает draft и transient product-add refs |
| `ORDER_STATUS` | `OrderStatusHandler` | не создаётся; ставится status-task, pending сохраняется |
| `PRODUCT_ADD_LIST` | список запросов снабженцу | не создаётся; pending сохраняется |
| `BACK` | `_clear_transient_dialog_state()` | pending очищается, draft показывается |
| `CANCEL` | `_contextual_negative_command()` переписывает в `PRODUCT_ADD_SKIP` | исходный item пропускается, pending очищается |
| `REMOVE_ITEM`, `EDIT_QUANTITY`, `MANUAL_CURRENT` | соответствующие handlers | generic block не достигается; результат зависит от target/current item |
| `SUBMIT_REQUEST`, `SHOW_FINAL_REVIEW` | `FinalReviewHandler` | при unresolved item показывается issue, request не создаётся |
| `UNKNOWN` с непустым текстом | generic product-add block | текст записывается как description, item становится `SKIPPED`, enqueue включается |
| `ADD_ITEMS` с непустым `command.text` | generic product-add block | текст записывается как description вместо создания нового `CartItem` |

Это фактический порядок ветвей текущего engine, а не новое правило поведения.

### ADD_ITEMS: description против независимого товара

Существующий `ParsedCommand` содержит `intent`, `text`, `items`, quantity/unit внутри
`ExtractedItem`, но не содержит отдельного признака «это ответ на product-add prompt» или
«это новая строка текущей заявки».

- Для валидного ответа «Мисо-паста Genzo, 1 кг» текст может быть `UNKNOWN` с непустым
  `command.text` либо `ADD_ITEMS` с одним item; текущий generic block сохраняет весь исходный
  текст как description.
- Для голосового ответа существующий regression fixture передаёт `ADD_ITEMS`, полный
  transcript в `command.text` и один `ExtractedItem`; фактический путь тот же, что для TEXT.
- Для явного независимого «добавь пармезан 3 кг» глобальный parser должен вернуть
  `ADD_ITEMS` с item и текстом, но текущий engine не имеет поля, отличающего его от
  допустимого product-add description «пармезан 3 кг». При текущем порядке оба попадают в
  generic block.

Следовательно, `StateCompatibilityPolicy` не может надёжно решить эту пару только по
текущему `ParsedCommand` без разбора natural language: структуры команд совпадают. Это
зафиксированный semantic gap, а не основание добавлять regex/blacklist в policy.

### Два engine-блока и их reachability

Блок A (`engine.py:632-684`) принимает `(command.text or event.text)` и является обычным
TEXT/VOICE путём. Он перехватывает любой непустой ответ до нормального `ADD_ITEMS`.

Блок B (`engine.py:706-756`) находится внутри `if command.intent == Intent.ADD_ITEMS`. Он
использует `command.text.strip()` или объединяет `item.source_line/item.product_query`.
При непустом `command.text` для TEXT/VOICE блок A возвращает раньше, поэтому B для этого
случая недостижим. B достижим, когда A не получил текст (прежде всего структурированный
PHOTO/ParsedCommand без caption): тогда он восстанавливает description из распознанных
items. Поля request, мутация source item, pending refs, stage и enqueue в A и B одинаковы;
различается только источник строки description. Это дублирование одной бизнес-операции,
которое следует устранять только в implementation diff после фиксации семантики.

### TEXT / VOICE / PHOTO и callbacks

- TEXT: `UpdateOrchestrator._parse_text_in_context()` сначала вызывает global
  `openai.parse_text()`, затем передаёт результат в `ConversationEngine.handle()`.
- VOICE: `InputRecognitionService._recognize_voice()` сначала транскрибирует, затем
  вызывает тот же callback `_parse_text_in_context()`; отдельного product-add voice bypass
  нет. После этого engine получает тот же тип `ParsedCommand`, что и для TEXT.
- PHOTO: `_recognize_photo()` вызывает `openai.parse_photo()`. При пустом `command.text` и
  отсутствии caption возможен блок B с `source_line/product_query` extracted items; фото
  не имеет отдельного product-add semantics.
- Callbacks остаются явным UI-путём: `v2:addreq:<index>`, `v2:addreqskip:<index>`,
  `v2:addreqretry:<request_id>`, `v2:back`, `v2:cancel`. Их нельзя смешивать с natural-language
  policy. `PRODUCT_ADD_RETRY` повторно использует request ID, а `PRODUCT_ADD_SKIP` помечает
  исходный item `SKIPPED` и очищает pending.

### Idempotency и cleanup

До product-add обработки engine проверяет `description_event_key == event.update_id`
(`engine.py:365-369`). Повтор Telegram update возвращает draft без повторного request и
enqueue; этот механизм нельзя менять при routing refactor.

`BACK`, `CANCEL`/`PRODUCT_ADD_SKIP`, `CLEAR_CART` и `_clear_transient_dialog_state()` очищают
временные product-add refs. `START_NEW_ORDER` сохраняет старые completed requests и при
активном draft сначала открывает подтверждение. Worker после submitted/failed/uncertain
очищает pending request ID и write flag, сохраняя request status. Отдельного
`suspended_interaction` по результатам аудита не требуется: исходный item и request refs уже
персистируются в `ConversationState`; необходимость отдельного resume-контекста пока не
доказана.

### Нужен ли новый CompatibilityContext

Да, для implementation нужен `CompatibilityContext.PRODUCT_ADD_DETAILS`: stage является
семантическим modal prompt, но `context_for()` сейчас видит только статусный
`CANDIDATE_SELECTION` или `NOT_FOUND` и не может выразить приоритет product-add prompt.
Новый context должен проверяться после `COMMENT_SCOPE`/`MANUAL_DETAILS` и до
status-based candidate/not-found contexts, только при валидных pending item/request refs.

Предлагаемая ответственность policy:

| ParsedCommand semantics | PRODUCT_ADD_DETAILS decision |
|---|---|
| Валидное описание текущего товара, включая название + quantity/unit | `CONTINUE` |
| Явно распознанный независимый `SHOW_CART`, `REMOVE_ITEM`, `THANKS`, `START_NEW_ORDER`, `CLEAR_CART`, `ORDER_STATUS`, `BACK` и т.п. | `INTERRUPT` |
| Команда, для которой global parser не дал достаточной семантики и product-add UI требует уточнения | `AMBIGUOUS` без мутации |
| Явный skip/cancel текущего product-add запроса или невалидный callback | существующий safe `REJECT`/skip path |

Критическое ограничение: `ADD_ITEMS + quantity` нельзя автоматически считать `INTERRUPT`,
потому что тот же набор полей является валидным подробным описанием из текущего prompt.
Для реализации сначала нужен семантический признак от global parser (например, роль
сообщения «ответ на текущий prompt» против «новая позиция»); policy должна только принять
решение по уже готовому ParsedCommand и не искать слова «добавь», «потом» или другие признаки
в raw text.

### Поведение случайных фраз

Текущий UI действительно ожидает свободное непустое описание. Поэтому `UNKNOWN` с текстом
«ну потом», «не знаю», «как-нибудь» или «ладно» сегодня технически становится description;
это наблюдаемое поведение generic block, а не доказательство того, что такие фразы являются
валидными товарными данными. `THANKS`, если global parser распознал его как `THANKS`,
обрабатывается passive handler и не создаёт request. Перед implementation нужно выбрать
источник истины для weak/unknown input: принимать любую непустую фразу как описание либо
возвращать безопасное уточнение. Новые blacklist/regex для этого этапа не предлагаются.

### Предлагаемая граница implementation (не выполнена)

1. Сначала зафиксировать global-parser contract, который различает ответ на product-add
   prompt и независимый `ADD_ITEMS`; не менять это различие внутри policy по словам.
2. Добавить `PRODUCT_ADD_DETAILS` в общую `StateCompatibilityPolicy`/`ModalRoutingDecision`.
3. Подключить один policy gate непосредственно перед product-add обработкой в engine.
4. Оставить существующие request fields, source-item mutation, Celery enqueue, worker
   idempotency, callbacks, `_advance()` и `_find_cart_item()` без изменений.
5. После подтверждения семантики механически объединить A/B в одну операцию создания
   request, сохранив для PHOTO fallback только источник description.

### Regression plan (только план, тесты не добавлялись)

- valid free-text description без quantity;
- description с quantity/unit (оба UI-примера);
- `ADD_ITEMS` как явно независимый новый товар — новый `CartItem`, старый product-add
  context не попадает в него;
- `SHOW_CART`, `THANKS`, `GREETING`, `HELP`, `SMALL_TALK`, `ORDER_STATUS`, `REMOVE_ITEM`,
  `EDIT_QUANTITY`, `MANUAL_CURRENT`, `START_NEW_ORDER`, `CLEAR_CART`, `BACK`, `CANCEL`;
- weak/random input с выбранным после обсуждения контрактом;
- одинаковые сценарии TEXT и VOICE после transcription;
- PHOTO без caption и с extracted `source_line/product_query` fallback;
- PRODUCT_ADD/RETRY/SKIP callbacks и stale callback revision;
- до/после valid description: source item, status, issue refs, pending refs, request fields,
  `product_add_write_in_progress`, stage и enqueue;
- повтор одного `update_id` не создаёт второй request и не ставит вторую задачу;
- cleanup после BACK/CANCEL/REMOVE/START_NEW_ORDER/CLEAR_CART и worker success/failure;
- сохранение существующих product-add, modal routing и baseline tests.

### Ограничения этапа

В рамках анализа не изменялись parser, prompts, OpenAI schemas, matching, submission,
persistence, `_advance()`, `_find_cart_item()`, другие modal states, UX или callback contract.
Сделан только этот handoff-документ.

## AWAIT_PRODUCT_ADD_DETAILS — DONE

Реализован отдельный этап защиты ожидания подробного описания товара.

- `ParsedCommand.explicit_add_items` вычисляется только в parser-нормализации;
  для обычного описания товара и структурированного количества значение `False`,
  для явного `добавь ...` — `True`.
- TEXT и транскрибированный VOICE используют одинаковый parser-контракт.
  PHOTO принудительно не получает семантику явной текстовой команды.
- `CompatibilityContext.PRODUCT_ADD_DETAILS` добавлен в общий policy и имеет приоритет
  над status-контекстами при валидных pending product-add refs.
- `ADD_ITEMS` с `explicit_add_items=True` прерывает product-add flow и проходит обычный
  routing; старый item, request refs и pending context не переносятся в новую позицию.
- Описание с `ADD_ITEMS=False` или непустым `UNKNOWN` продолжает текущий product-add flow.
  Пустой ответ остаётся без мутации и возвращает текущую подсказку.
- Два прежних engine-блока создания product-add request объединены через
  `_submit_product_add_description`; request fields, idempotency и callback contracts сохранены.
- Добавлены regression tests для parser flag, policy priority, explicit interruption,
  pending-context isolation и navigation interruption.

## NEXT FUNCTIONAL STEP

`AWAIT_ADD_MORE_CONFIRM` — следующий функциональный этап StateCompatibilityPolicy.

## AWAIT_PRODUCT_ADD_DETAILS — VERIFICATION

- `tests/product_add`: passed.
- `tests/conversation/test_not_found_preemption.py`: passed.
- `tests/conversation/test_voice_route_safety.py`: passed.
- Existing unrelated baseline failures remain separately tracked; no parser/comment
  or prompt changes were made for them.

## AWAIT_PRODUCT_ADD_DETAILS — FOLLOW-UP ROUTING SAFETY

- Contextual rewrites are now disabled when `PRODUCT_ADD_DETAILS` policy returns
  `INTERRUPT`, so the pending product-add prompt cannot rewrite an independent command.
- Candidate textual fallback is `NOT_APPLICABLE` while `AWAIT_PRODUCT_ADD_DETAILS` is
  active, including when the source item is also `AMBIGUOUS` with candidates.
- The legacy `PRODUCT_ADD` phrase fallback is skipped for the same interrupted route;
  explicit `ADD_ITEMS` continues through ordinary cart routing.
- Regression coverage added for text descriptions, candidate overlap, and explicit add
  interruption. `AWAIT_ADD_MORE_CONFIRM` remains the next functional step.

## AWAIT_ADD_MORE_CONFIRM — ANALYSIS BASELINE (IMPLEMENTATION COMPLETED BELOW)

### Граница текущего состояния

`AWAIT_ADD_MORE_CONFIRM` устанавливается в `_advance()` только после того, как
обработан добавленный batch и `_first_unresolved(state)` не нашёл нерешённых позиций.
Перед установкой prompt очищаются `current_issue_item_id` и `current_issue_kind`, а
`pending_added_items_count` переносится в локальный `prompt_count` и сразу сбрасывается
в `0`. Поэтому штатное состояние prompt не относится к конкретному `CartItem`.

Точка входа одна: `_advance(state, added_count=...)` →
`added_items_question_reply(state, prompt_count)`. Текущий UI-контракт:

- текст: `Товар добавлен в черновик заказа` / `Товары добавлены в черновик заказа`;
- вопрос: `Добавить ещё товары?`;
- `Да, добавить товары` → `v2:add` → `Intent.ADD_MORE`;
- `Нет, к черновику` → `v2:back` → `Intent.BACK`.

`parse_callback()` оставляет callback-путь явным. Ревизия `:rN` проверяется в engine до
мутации; устаревшая кнопка только повторно показывает текущую карточку. Callback не
смешивается с semantic policy.

### Порядок текущего engine

Сейчас порядок обработки такой:

1. `evaluate_modal_routing()` вызывается, но для `AWAIT_ADD_MORE_CONFIRM` решения нет:
   `CompatibilityContext` не содержит этого состояния.
2. Выполняются общие contextual rewrites.
3. Если пришёл конкретный `ADD_ITEMS`, stage синхронно меняется на `COLLECTING`.
4. `BACK/CANCEL/SHOW_CART` в `AWAIT_ADD_MORE_CONFIRM` сбрасывают
   `pending_added_items_count`, переводят state в `REVIEW` и возвращают `cart_reply()`.
5. Далее идут passive/navigation handlers, обычный `ADD_ITEMS`, quantity/edit/remove,
   final-review и submit handlers.

Таким образом, modal-состояние определяется отдельными ветками engine, а не единой
точкой `ParsedCommand → StateCompatibilityPolicy → routing`.

### Проверка входных фраз

Ниже зафиксирован результат текущего deterministic `infer_intent()` и фактическая
ветка engine для state с одним matched-товаром и `stage=AWAIT_ADD_MORE_CONFIRM`.

| Вход | Global ParsedCommand | Contextual rewrite | Текущая ветка и эффект |
|---|---|---|---|
| `да` | `CONFIRM` | TEXT: нет; VOICE: `ADD_MORE` | TEXT вызывает `_confirm_current()` и повторно доходит до add-more prompt; VOICE переводит в `COLLECTING`. |
| `да, добавим ещё` | `ADD_ITEMS`, один item с разговорной фразой | TEXT: нет; VOICE: `ADD_MORE` по explicit yes | TEXT сбрасывает stage и пытается добавить псевдотовар; VOICE корректно продолжает сбор. |
| `давай добавим ещё` | `ADD_MORE` | не требуется | `COLLECTING`, prompt сбрасывается. |
| `давай ещё` | `ADD_ITEMS`, один item без количества | не требуется для TEXT | TEXT добавляет разговорный фрагмент как товар; это тот же риск, что и выше. |
| `нет` | `CANCEL` | VOICE дополнительно → `BACK` | `REVIEW`, `pending_added_items_count=0`, cart не меняется. |
| `не надо` | отрицательная команда/`CANCEL` в зависимости от parser output | VOICE → `BACK` при распознанном отказе | Ожидаемая ветка `REVIEW`; нужен regression на обе modality. |
| `хватит` | `ADD_ITEMS`, один item | VOICE → `BACK` по prefix `хват` | TEXT может добавить псевдотовар; VOICE возвращает черновик. |
| `покажи черновик` | `SHOW_CART` | не требуется | `REVIEW`, count сбрасывается, cart не меняется. |
| `назад` | `BACK` | не требуется | `REVIEW`, count сбрасывается, cart не меняется. |
| `пармезан 3 кг` | конкретный `ADD_ITEMS` | не требуется | Сначала `COLLECTING`, затем обычный add; старый batch не переносится, duplicate/qty/unit flows работают в collecting context. |
| `добавь пармезан 3 кг` | `ADD_ITEMS`, `explicit_add_items=True` | не требуется | Та же обычная ветка нового товара, без наследования metadata prompt. |
| `удали сыр` | `REMOVE_ITEM` | не требуется | `_remove_item()` помечает найденную строку `SKIPPED`; именованный remove возвращает cart reply, но сам branch не нормализует stage. |
| `измени количество сыра на 5 кг` | `EDIT_QUANTITY` | quantity helper может уточнить target | `_edit_quantity()` изменяет найденную позицию и вызывает `_advance()`; при отсутствии unresolved обычно получается `REVIEW`. |
| `покажи статус` | `ORDER_STATUS` | не требуется | OrderStatusHandler запускает чтение истории; cart не меняется, stage add-more сам по себе не сбрасывается. |
| `новая заявка` | `START_NEW_ORDER` | не требуется | При активном cart ставится `pending_new_order_confirmation`; при пустом/отправленном cart создаётся fresh state. |
| `очисти черновик` | `CLEAR_CART` | не требуется | `_start_new_order()` создаёт fresh state, count и cart очищаются. |
| `спасибо` | `THANKS` | не требуется | Passive handler отвечает без мутации cart; stage остаётся прежним. |
| `помощь` | `HELP` | не требуется | Passive handler отвечает без мутации cart; stage остаётся прежним. |
| `отправляй` / `отправить заявку` | `SUBMIT_REQUEST` | VOICE также нормализуется в submit | FinalReviewHandler переводит в `AWAIT_SUBMIT_CONFIRM`, новый товар не создаётся. |
| `да, отправляй` | `SUBMIT_REQUEST` в текущем deterministic parser | VOICE prefix также → submit | `AWAIT_SUBMIT_CONFIRM`; требуется отдельный TEXT/VOICE regression, чтобы global parser не вернул `ADD_ITEMS`. |
| `ну` / `не знаю` / `может быть` / `ладно` | текущий deterministic parser возвращает `ADD_ITEMS` с фрагментом без quantity | специального safe fallback нет | Фрагмент может стать новой позицией и открыть quantity/other issue; это небезопасно для modal prompt. |

Для перечисленных строк с обычными навигационными intent общая проблема не в мутации
cart, а в том, что stage иногда остаётся `AWAIT_ADD_MORE_CONFIRM` после passive,
order-status или именованного remove. Следующий вход затем снова видит старую modal
ветку, хотя пользователь уже выполнил независимое действие.

### TEXT / VOICE / PHOTO

Голосовые команды проходят тот же global parse, но дополнительно попадают в
`_contextual_voice_command()`. Только там сейчас сосредоточены правила `да`, `хватит`,
`отправляй` и некоторых разговорных вариантов. Поэтому TEXT и VOICE неэквивалентны:
voice исправляет часть ошибочных `ADD_ITEMS`, а text — нет.

PHOTO не блокируется этим stage: если после prompt приходит photo с items, ранняя ветка
`AWAIT_ADD_MORE_CONFIRM + ADD_ITEMS` сначала переводит state в `COLLECTING`, после чего
фото проходит обычный add-items flow. Фото без items получает обычный
`photo_without_quantities_reply`; архитектуру фото менять не требуется.

### Lifecycle `pending_added_items_count`

Поле используется только в engine:

- `_advance(added_count=N)` записывает число позиций нового batch;
- если unresolved ещё есть, число временно живёт до их обработки;
- когда unresolved нет, `_advance()` читает число, создаёт `AWAIT_ADD_MORE_CONFIRM` и
  немедленно сбрасывает его в `0`;
- `_skip_current()` уменьшает число на один при пропуске unresolved item;
- `_clear_transient_dialog_state()` и ветка `BACK/CANCEL/SHOW_CART` сбрасывают его в `0`.

В штатном add-more prompt count уже равен нулю. Потенциально stale значение возможно,
если state был сохранён между unresolved/independent command или если именованный
`REMOVE_ITEM`, passive intent либо `ORDER_STATUS` оставили stage без нормализации. При
следующем `_advance()` такое значение способно снова показать вопрос «Добавить ещё
товары?». Поле удалять нельзя; lifecycle должен быть покрыт regression tests.

### Нужен ли `CompatibilityContext.ADD_MORE_CONFIRM`

Да, нужен как единая точка совместимости для modal navigation stage, но только при
валидном инварианте: `stage=AWAIT_ADD_MORE_CONFIRM`, `current_issue_item_id` пуст,
нет pending comment/product-add/manual refs и нет unresolved item. Если этот инвариант
нарушен, приоритет должны иметь уже существующие item-specific contexts (comment,
product-add, manual, candidate, not-found, duplicate, unit, quantity), а не add-more.

Предлагаемые решения policy:

| ParsedCommand | `ADD_MORE_CONFIRM` action | Контракт |
|---|---|---|
| `ADD_MORE`, явное «да» | `CONTINUE` | Перевести в `COLLECTING`, показать prompt для новых товаров. |
| concrete `ADD_ITEMS` | `CONTINUE` с normal reprocess | Сначала сбросить modal stage, затем выполнить обычный add-items; не переносить batch metadata. |
| `BACK`, `CANCEL`, `SHOW_CART`, явное «нет» | `CONTINUE` по navigation contract | Перейти в `REVIEW`, count=0, cart не менять. |
| `SUBMIT_REQUEST`, `SHOW_FINAL_REVIEW`, `REMOVE_ITEM`, `EDIT_QUANTITY`, `ORDER_STATUS`, `START_NEW_ORDER`, `CLEAR_CART`, `HELP`, `THANKS` и другие strong intents | `INTERRUPT` | Обычный routing; add-more context не подменяет команду. |
| `UNKNOWN` без надёжного смысла | `AMBIGUOUS` | Повторить вопрос/не менять cart и count; не создавать item. |
| stale/невалидный callback | `REJECT` | Текущий callback revision contract, без мутации. |

`ADD_ITEMS` требует отдельного решения контракта: policy не должна разбирать raw natural
language. Текущий parser иногда отдаёт разговорное «да, давай добавим ещё» как item, поэтому
одной policy над существующим `ParsedCommand` недостаточно без подтверждённого semantic
признака «ответ на prompt» либо корректного global parse. Это открытый вопрос реализации,
а не повод добавлять regex в engine.

### Граница следующей реализации

Изменения должны ограничиться `StateCompatibilityPolicy`, `ModalRoutingDecision`, одной
точкой preemption в `ConversationEngine` и regression tests. Не менять parser/prompts,
matching, product-add и предыдущие modal contexts без отдельного доказательства. Не
добавлять `suspended_interaction`, не менять `_advance()` и `_find_cart_item()`.

До кода зафиксировать black-box matrix:

- TEXT/VOICE: `да`, `давай добавим ещё`, `да, давай добавим ещё`, `нет`, `не надо`,
  `хватит`, direct/explicit `ADD_ITEMS`, `SHOW_CART`, `REMOVE_ITEM`, `EDIT_QUANTITY`,
  `SUBMIT_REQUEST`, `ORDER_STATUS`, `THANKS`, `HELP`, random/unknown;
- PHOTO: список товара сразу после prompt и пустое фото;
- CALLBACK: `v2:add`, `v2:back`, их revision suffix и stale revision;
- state invariants: count cleanup, отсутствие metadata leakage, duplicate после direct
  product в `COLLECTING`, отсутствие мутации cart при passive/navigation;
- проверить, что `AWAIT_ADD_MORE_CONFIRM` не возникает при оставшемся unresolved item.

### Итог анализа

- Первый подтверждённый unsafe transition: TEXT «да, давай добавим ещё» (также «хватит»
  и random-фразы) → global `ADD_ITEMS` с псевдотоваром → обычный add flow. Voice-only
  contextual rewrite скрывает этот дефект, но не устраняет разницу modality.
- `CompatibilityContext.ADD_MORE_CONFIRM` нужен; implementation пока не начиналась.
- `NEXT FUNCTIONAL STEP` остаётся `AWAIT_ADD_MORE_CONFIRM`.
- В application code, parser, prompts, matching и callbacks в рамках этого анализа ничего
  не изменялось.

## AWAIT_ADD_MORE_CONFIRM — IMPLEMENTATION DONE (2026-08-09)

Реализована единая маршрутизация для активного вопроса «Добавить ещё товары?».

- `DialogueResponse` (`NONE`, `AFFIRM`, `DECLINE`, `UNCERTAIN`) определяется при parser/AI-нормализации;
  policy не анализирует raw natural language.
- `CompatibilityContext.ADD_MORE_CONFIRM` добавлен в общий `StateCompatibilityPolicy` и
  используется через `ModalRoutingDecision`.
- Уверенное `AFFIRM` переводит сбор в `COLLECTING`; `DECLINE`, `BACK`, `CANCEL` и `SHOW_CART`
  переводят в `REVIEW`; `UNCERTAIN` повторяет существующий prompt без изменения корзины.
- Конкретный `ADD_ITEMS` прерывает modal prompt и проходит обычный add-items routing;
  старый batch count и контекст не переносятся в новый товар.
- Независимые intents прерывают prompt и маршрутизируются обычным путём.
- Stale callbacks проверяются до state mutation; `v2:add` и `v2:back` используют общий policy path.
- Text и voice используют одну семантическую классификацию; photo-контракт и callback path
  сохранены.

Добавлены regression tests для semantic responses, text/voice parity, нового товара,
неуверенного ответа, благодарности и callback revision. Изменения не затрагивают prompts,
matching, comment logic, `_advance()` или предыдущие modal contexts.

## NEXT FUNCTIONAL STEP

`AWAIT_SUBMIT_CONFIRM` — следующий функциональный этап StateCompatibilityPolicy.

## AWAIT_SUBMIT_CONFIRM — ANALYSIS COMPLETE / IMPLEMENTATION PENDING (2026-08-09)

Этап проанализирован без изменения application code, parser, prompts, matching,
quantity, product-add, submission implementation, Telegram-контрактов и предыдущих
modal contexts. `NEXT FUNCTIONAL STEP` остаётся `AWAIT_SUBMIT_CONFIRM`.

### Entry contract и валидный контекст

`FinalReviewHandler` переводит state в `AWAIT_SUBMIT_CONFIRM`, когда для
`SUBMIT_REQUEST` или `SHOW_FINAL_REVIEW` нет `first_unresolved(state)` и есть хотя бы
один `MATCHED` item. Перед этим он очищает `current_issue_item_id` и
`current_issue_kind`. При unresolved он оставляет пользователя на issue card, а при
пустом наборе `MATCHED` возвращает `empty_draft_reply()`.

`SUBMIT_AS_IS` приходит, в частности, из callback `v2:submit`; его обработчик сначала
повторяет unresolved guard, затем проверяет multiple/suggested quantity warning.
`CHECK_MIN_SUM` в обычном engine-пути перехватывается отдельной веткой и возвращает
`supplier_warning_details_reply()`; standalone `FinalReviewHandler` также знает этот
intent для повторного показа final review.

Истинный submit-confirm контекст безопасен только при следующем инварианте:

```text
stage == AWAIT_SUBMIT_CONFIRM
нет unresolved item
есть хотя бы один MATCHED item
нет более приоритетного current issue / pending modal context
```

При нарушении инварианта submit confirmation не должен иметь приоритет над
item-specific context.

### Текущий UI-контракт

`final_review_reply()` формирует заголовок «📦 Финальная проверка», показывает до 20
активных позиций на странице и сохраняет `final_review_page`.

- pagination: `v2:finalpage:{page}`;
- обычная отправка: `Отправить в таблицу заказа` → `v2:submit`;
- возврат: `К черновику` → `v2:back`;
- один multiple warning: `Выбрать количество` → `v2:mulone`,
  `Оставить {current} {unit}` → `v2:keepwarn`;
- несколько multiple warnings: `Выбрать количество` → `v2:mulone`;
- supplier/minimum warning: `Показать товары поставщика` или
  `Проверить минимальные суммы` → `v2:minsum`.

Оркестратор добавляет к callback текущую ревизию `:rN` перед отправкой. Поэтому
реальный callback final review имеет тот же action с суффиксом revision.

### Фактический порядок engine и первый unsafe transition

Для TEXT/VOICE фактический порядок в `ConversationEngine.handle()` такой:

```text
DialogueResponse normalization
→ для VOICE при CONFIRM/ADD_MORE _contextual_voice_command()
→ evaluate_modal_routing()
→ stale callback guard
→ contextual modal branches / navigation handlers
→ generic CONFIRM → _confirm_current()
→ FinalReviewHandler.handle()
→ _prepare_submission()
```

Первый submit-specific unsafe transition — строка с generic branch
`if command.intent == Intent.CONFIRM: return self._confirm_current(state)` до вызова
`FinalReviewHandler`. Поэтому текстовый `CONFIRM` не является подтверждением отправки
на `AWAIT_SUBMIT_CONFIRM`: он обслуживается общим механизмом подтверждения текущего
item и вызывает `_advance()`.

Для VOICE существует ещё более раннее modality-specific расхождение:
`_contextual_voice_command()` до policy переписывает affirmative/prefix
`подтверждаю`/`отправляй` в `SUBMIT_AS_IS`, а отрицание — в `BACK`.

### TEXT и VOICE: подтверждённые black-box результаты

Deterministic parser возвращает:

| Фраза | ParsedCommand | DialogueResponse |
|---|---|---|
| `да` | `CONFIRM` | `AFFIRM` |
| `подтверждаю` | `CONFIRM` | `AFFIRM` |
| `всё верно` | `CONFIRM` | `AFFIRM` |
| `да, отправляй` | `SUBMIT_REQUEST` | `NONE` |
| `отправляй` | `SUBMIT_REQUEST` | `NONE` |
| `нет` | `CANCEL` | `DECLINE` |
| `передумал` | `CANCEL` | `DECLINE` |
| `назад` | `BACK` | `NONE` |

Текстовый engine на matched item в `AWAIT_SUBMIT_CONFIRM` даёт:

- `да`, `подтверждаю`, `всё верно` → generic `_confirm_current()`, `stage=REVIEW`,
  `enqueue_submission=False`;
- `да, отправляй`, `отправляй` → `FinalReviewHandler` повторно рисует final review,
  `stage=AWAIT_SUBMIT_CONFIRM`, `enqueue_submission=False`;
- `нет`, `передумал`, `назад` → `stage=COLLECTING`, cart сохраняется, enqueue нет.

Голосовой engine с тем же parser output доходит до другого результата:

- `да`, `подтверждаю`, `всё верно`, `да, отправляй`, `отправляй` после contextual
  rewrite → `SUBMIT_AS_IS` → `_prepare_submission()`, `stage=SUBMITTING`,
  `enqueue_submission=True`;
- `нет`, `передумал`, `назад` → voice rewrite в `BACK`, затем `COLLECTING`, cart
  сохраняется.

Таким образом, текущие TEXT и VOICE неэквивалентны на confirmation screen.

### DialogueResponse

Существующий контракт достаточен и не требует нового enum:

- `AFFIRM` — возможный affirmative answer текущего modal;
- `DECLINE` — отказ/возврат;
- `UNCERTAIN` — безопасное повторение вопроса или уточнение;
- `NONE` — маршрутизация по обычному `intent`.

Сейчас policy не имеет `CompatibilityContext.SUBMIT_CONFIRM`, поэтому эти значения
не образуют единой точки принятия решения для final review. Дополнительный
`submit_confirmation_response` не вводить.

### Независимые команды на confirmation screen

После global parsing они проходят обычные ветки engine, но stage не всегда
нормализуется одинаково:

- concrete `ADD_ITEMS` → обычное добавление; новый item не получает metadata старого
  confirmation context, submission не запускается; после `_advance()` строится новый
  review/issue flow;
- `REMOVE_ITEM` → найденный item помечается `SKIPPED` и возвращается cart reply; при
  именованном удалении текущий `AWAIT_SUBMIT_CONFIRM` может сохраниться;
- `EDIT_QUANTITY` → редактируется выбранный item и вызывается `_advance()`, обычно
  возвращая `REVIEW`, если unresolved больше нет;
- `SHOW_CART` → cart reply без мутации; специальная просьба показать товары без
  черновика даёт supplier warning details;
- `ORDER_STATUS`, `THANKS`, `HELP` → navigation/passive handler, cart не меняется;
  stage может сохраниться;
- `START_NEW_ORDER` при активном cart открывает `pending_new_order_confirmation`, а
  `CLEAR_CART` сразу создаёт fresh collecting state.

Ни один из этих intents не должен подменяться submit-confirm fallback. Отдельно
нужны regression tests на сохранение/нормализацию stage после independent command.

### Неуверенные и случайные фразы

`dialogue_response_for()` распознаёт `ну`, `не знаю`, `может быть`, `ладно` как
`UNCERTAIN`, но deterministic fallback одновременно возвращает `ADD_ITEMS` с
псевдотоваром без quantity. На `AWAIT_SUBMIT_CONFIRM` текущий engine добавляет такой
item (обычно со статусом `NOT_FOUND`) вместо безопасного повторения final review.
Это подтверждённая зона regression coverage для будущей policy; новый regex в engine
сейчас не добавлялся.

### Multiple / suggested quantity warning

`FinalReviewHandler.SUBMIT_AS_IS` блокирует подготовку submission, если у `MATCHED`
позиции `suggested_quantity != quantity`, повторно оставляет stage
`AWAIT_SUBMIT_CONFIRM` и возвращает `final_review_reply()` с warning buttons.
`KEEP_MULTIPLE`/`KEEP_CURRENT_QUANTITY` очищает suggestion, `ACCEPT_SUGGESTED_QUANTITY`
принимает recommendation, `ENTER_OTHER_QUANTITY` переводит в ручной quantity flow.
До снятия warning реальная отправка не начинается. Повторный submit при неизменённом
warning безопасно остаётся на этом экране; quantity semantics не менять.

### BACK / CANCEL / DECLINE

- callback `v2:back` и callback input возвращают `cart_reply()` без очистки stage;
- текстовый `BACK` очищает transient dialog refs и ставит `COLLECTING`;
- generic `CANCEL` очищает transient refs, ставит `COLLECTING`, cart сохраняется и
  возвращается reply с заголовком «Отправка отменена»;
- parser `DECLINE` представлен `CANCEL`, а voice contextual path дополнительно
  нормализует отрицание в `BACK`.

Это также требует единого policy решения: возврат к draft и cancel отправки — не
одно и то же действие, но оба не должны запускать submission.

### Реальная граница side effects

До `SUBMIT_AS_IS` и успешного прохождения guards confirmation screen не создаёт
`PendingSubmission`, не ставит `SUBMITTING` и не enqueue-ит Celery task.

`_prepare_submission()`:

1. блокирует повтор после `dispatch_uncertain`;
2. повторно использует существующие `pending_submission.order_no/rows`, если snapshot
   уже создан;
3. иначе собирает только `MATCHED` items, создаёт order number и immutable rows,
   включая quantity, department и comment;
4. сохраняет `PendingSubmission`, ставит `stage=SUBMITTING`/`status=submitting` и
   возвращает `enqueue_submission=True`.

Только после checkpoint engine-result оркестратор ставит Celery task. Google Sheets и
центральная отправка находятся в `SubmissionService.submit()`; `submission.py` в этом
этапе не менялся.

Защиты от двойной отправки уже существуют: deduplication update в
`UpdateRepository`, checkpoint `state_applied/reply_sent/tasks_enqueued`, chat lock,
повторное использование order number/rows и блокировка повторной отправки после
`dispatch_started` без completion. Новую idempotency-защиту не добавлять в рамках
анализа.

### PHOTO, callbacks и pagination

PHOTO проходит через `InputRecognitionService._recognize_photo()` и
`openai.parse_photo()`, а затем попадает в обычный engine `ADD_ITEMS` путь. Наличие
`AWAIT_SUBMIT_CONFIRM` само по себе photo не блокирует. Фото с items добавляет строки
обычным matching/quantity flow; фото без items возвращает
`photo_without_quantities_reply()` без мутации cart. `DialogueResponse` для PHOTO не
подменяет photo parser.

Fresh callbacks final review: `v2:submit`, `v2:back`, `v2:finalpage:{page}`,
`v2:minsum`, `v2:mulone`, `v2:keepwarn` (с добавленной ревизией при отправке).
Stale `callback_revision != state.ui_revision` проверяется в engine до modal transition,
cleanup или другой state mutation; он возвращает текущую issue/cart reply и не может
enqueue submission. Pagination остаётся UI-route внутри review и не является
подтверждением отправки.

Отдельный `review_mode=sheet` deep-link flow имеет собственные token/fingerprint и
revision guards. Он не является тем же обычным cart `AWAIT_SUBMIT_CONFIRM` и не должен
смешиваться с новой policy.

### Unresolved priority

Текущий `first_unresolved()` приоритет: `DUPLICATE_PENDING`, `UNIT_MISMATCH`,
`MISSING_QTY`, `AMBIGUOUS`, `NOT_FOUND`, `NEW`, `AI_PENDING`. `FinalReviewHandler`
проверяет его перед submit/show-review. `MANUAL_DETAILS`, `PRODUCT_ADD_DETAILS` и
`COMMENT_SCOPE` представлены stage/pending refs и перехватываются раньше через
существующие modal policies; в `StateCompatibilityPolicy` submit context пока нет.

Будущий `SUBMIT_CONFIRM` должен уступать любому активному item-specific modal
context/ref и `first_unresolved()`. Рекомендуемый порядок: comment/product-add/manual,
candidate/not-found/duplicate/unit/quantity, затем submit-confirm только для
валидного matched-only state.

### Предлагаемый policy boundary (без реализации)

Добавить в существующую `StateCompatibilityPolicy` один контекст
`CompatibilityContext.SUBMIT_CONFIRM` и один `ModalRoutingDecision` field. Policy
получает только `ParsedCommand`, `DialogueResponse` и state context; raw natural
language в ней не разбирается.

| ParsedCommand / contextual result | SUBMIT_CONFIRM action | Ожидаемый контракт |
|---|---|---|
| `SUBMIT_AS_IS`, `DialogueResponse.AFFIRM` после однозначного submit intent | CONTINUE | FinalReviewHandler → guards → `_prepare_submission` |
| `BACK`, `CANCEL`, `DialogueResponse.DECLINE` | CONTINUE | обычный возврат/cancel, без enqueue |
| final-review pagination, `SHOW_FINAL_REVIEW`, `SHOW_CART`, `CHECK_MIN_SUM` | CONTINUE | соответствующий UI/details route, без submit |
| concrete `ADD_ITEMS`, `REMOVE_ITEM`, `EDIT_QUANTITY`, `ORDER_STATUS`, `HELP`, `THANKS`, `START_NEW_ORDER`, `CLEAR_CART` | INTERRUPT | обычный routing, submit context не подменяет команду |
| `DialogueResponse.UNCERTAIN`, UNKNOWN без надёжного intent | AMBIGUOUS | повторить final review/уточнить, cart и state не менять |
| stale/invalid callback | REJECT | существующий revision guard, без mutation/enqueue |

`SUBMIT_REQUEST` нужно отдельно определить как повторный запрос final review (без
enqueue) либо как explicit confirmation только после отдельного UX-решения; текущий
код фактически выбирает первый вариант. `CONFIRM` нельзя оставлять generic branch
перед FinalReviewHandler.

### Будущий regression plan

Зафиксировать для TEXT и VOICE один набор: `да`, `подтверждаю`, `всё верно`,
`отправляй`, `да, отправляй`, `нет`, `не отправляй`, `передумал`, `назад`, concrete
`ADD_ITEMS`, `REMOVE_ITEM`, `EDIT_QUANTITY`, `SHOW_CART`, `ORDER_STATUS`, `THANKS`,
`HELP`, `START_NEW_ORDER`, `CLEAR_CART`, UNKNOWN и UNCERTAIN. Отдельно проверить PHOTO
с items/без items, fresh/stale submit/back/pagination callbacks, supplier warning,
20+ item pagination, unresolved priority, multiple warning, repeated submit и отсутствие
mutation/enqueue на AMBIGUOUS.

### Итог анализа

- application behavior в рамках этого этапа не менялся;
- подтверждён основной unsafe order: generic `CONFIRM` до `FinalReviewHandler`;
- подтверждено modality расхождение TEXT/VOICE на affirmative confirmation;
- `CompatibilityContext.SUBMIT_CONFIRM` нужен как единая точка policy, но реализация
  отложена до отдельного functional diff;
- `SUBMISSION_FAILED` остаётся отдельным следующим этапом и здесь не анализируется
  глубже retry semantics;
- `suspended_interaction` не требуется для этого анализа.

## AWAIT_SUBMIT_CONFIRM — IMPLEMENTATION

Этап реализован минимальным functional diff без изменения `submission.py`,
OpenAI parsing/prompts, каталожного matching или внешней границы enqueue.

Добавлен `CompatibilityContext.SUBMIT_CONFIRM` в существующую
`StateCompatibilityPolicy`. Контекст активен только для обычного cart review с
одними `MATCHED` позициями и без другого pending/modal context. `ModalRoutingDecision`
теперь отдаёт это решение engine до state-specific routing.

Порядок TEXT/VOICE теперь такой:

`global ParsedCommand → submit policy → обычный routing или FinalReviewHandler`.

- `AFFIRM`, `CONFIRM`, `SUBMIT_REQUEST`, `SUBMIT_AS_IS` проходят существующий
  `SUBMIT_AS_IS` guard и только затем создают `PendingSubmission`/enqueue;
- `DECLINE`, `CANCEL`, `BACK` возвращают пользователя к сохранённому review без
  отправки;
- `UNCERTAIN` и `UNKNOWN` повторяют final review без изменения cart;
- concrete `ADD_ITEMS`, `ADD_MORE` и остальные независимые intents прерывают modal
  context и не получают его metadata;
- `SHOW_CART` сохраняет специальный voice route для supplier warning details;
- photo с товарами проходит тот же независимый `ADD_ITEMS` путь;
- fresh/stale submit, back и pagination callbacks сохраняют существующий revision
  guard, stale callback не меняет state и не enqueue-ит задачу.

Удалён старый raw-phrase submit fallback из `_contextual_voice_command`; voice
нормализация остаётся перед policy, поэтому текст и голос используют одну точку
совместимости. Дополнительный `suspended_interaction` не вводился.

Добавлен `tests/conversation/test_submit_confirm_routing.py` (33 regression cases):
affirmative/negative/uncertain TEXT и VOICE, independent intents, ADD_MORE с
affirmative dialogue, unresolved priority, multiple warning, photo и fresh/stale
callbacks.

Проверки текущего checkout:

- новые submit-confirm tests: **33 passed**;
- целевые conversation/voice/submission/telegram suites: функционально новые тесты
  проходят; остаются ранее существующие падения контрактов UI/AI/postprocessing,
  не затронутые этим diff;
- `ruff` по изменённым source/test files: passed;
- `mypy` по изменённым source/test files: passed;
- `git diff --check`: passed.

Полный checkout baseline сейчас содержит известные pre-existing failures вне этой
задачи (в том числе старые AI/comment/voice recovery и UI-text expectations); они
не исправлялись заодно.

## NEXT FUNCTIONAL STEP

`SUBMISSION_FAILED` — отдельный analysis/implementation этап. Не смешивать его с
текущим `AWAIT_SUBMIT_CONFIRM` и не менять retry/uncertain submission semantics до
нового согласования.

## SUBMISSION_FAILED — ANALYSIS COMPLETE / IMPLEMENTATION PENDING

Анализ выполнен на `e304ce85410ace02d8bb47c5c982684dcf2b5b11`, ветка
`decompose_bot`. Application code не менялся.

### Две разные причины входа в SUBMISSION_FAILED

#### Обычный повторяемый failure: `SubmissionService._fail()`

Внешний worker вызывает `_fail()` только при `report_failure=True` после исчерпания
своих Celery retry. До этого временная ошибка сохраняется через
`_remember_transient_error()` и task автоматически повторяется.

Фактический контракт `_fail()`:

- `state.stage = SUBMISSION_FAILED`;
- `state.status` не изменяется. После `_prepare_submission()` это обычно остаётся
  `submitting`, а не отдельный failure status;
- `state.pending_submission` сохраняется;
- `pending_submission.last_error` заполняется;
- `pending_submission.failed_stage` не заполняется и обычно остаётся пустым;
- `SubmissionRecord.last_error` заполняется;
- в `OrderEvent` добавляется `submission_failed` со статусом `error`;
- `SubmissionRecord` не имеет отдельной колонки `failed_stage`; это поле есть только
  у `PendingSubmission`, сохранённого в session state.

Это обычный retryable-контекст только по отсутствию подтверждённого dispatch
uncertain. Сам текст ошибки не является источником решения.

#### Неопределённый dispatch: `SubmissionService._mark_dispatch_uncertain()`

Этот путь запускается либо при обнаружении в записи
`dispatch_started=True and dispatch_completed=False`, либо после исключения из
`send_order_submission()`.

Фактический контракт:

- `state.stage = SUBMISSION_FAILED`;
- `state.status = dispatch_uncertain`;
- `pending_submission.failed_stage = dispatch_uncertain`;
- `pending_submission.last_error` заполняется;
- `SubmissionRecord.dispatch_started` остаётся true,
  `dispatch_completed` остаётся false;
- `SubmissionRecord.last_error` заполняется;
- в `OrderEvent` добавляется `submission_dispatch_uncertain` со статусом
  `uncertain`;
- `_send_dispatch_uncertain_once()` отправляет предупреждение один раз и отмечает
  `dispatch_uncertain_notified`.

Это не обычный failure и не должно попадать в generic retry policy.

### Failure phase matrix

| Точка ошибки | Stage/status | Pending/record checkpoints | Повтор | Риск |
|---|---|---|---|---|
| До `increment_catalog_quantities` | `SUBMISSION_FAILED`, status от предыдущего state | `catalog_updated=false`, остальные false; pending сохранён | повторяет snapshot | безопасно, если side effect ещё не начался |
| Во время catalog update до checkpoint | тот же обычный `_fail` | `catalog_updated=false` даже если внешний вызов частично применился | повторяет increment | **не доказано безопасно**: timeout после применения может дать повторное изменение |
| После catalog checkpoint, во время cache invalidation | ordinary failure | `catalog_updated=true` | increment пропускается | side effect каталога не повторяется |
| Во время recalculation | ordinary failure | `catalog_updated=true`, `recalc_done=false` | повторяет recalculation | повторный recalc, но не POST |
| После `recalc_done`, до `dispatch_started` | ordinary failure | catalog/recalc true, dispatch false | пропускает завершённые этапы | безопасно для внешней заявки |
| После `_mark_dispatch_started()` до ответа POST | `SUBMISSION_FAILED`, `dispatch_uncertain` | started true, completed false | запрещён | результат POST неизвестен, duplicate order возможен |
| После ответа POST, но при ошибке `_mark_dispatch_completed()` | обычный `_fail` на уровне engine/task | started true, completed может остаться false | worker повторно увидит started/incomplete и переведёт в uncertain до POST | повторный POST блокируется, но UI некоторое время обычный |
| После `dispatch_completed`, при ошибке finalize | ordinary `_fail` | dispatch completed true | пропускает POST и повторяет finalize | внешняя заявка не дублируется |
| После finalize, при Telegram completion notification | stage не становится failed | record finalized true, completion_notified false; pending уже очищен | `_load_unnotified_completion()` повторяет только уведомление | order не дублируется |

`history_written` существует в модели `SubmissionRecord`, но текущий
`SubmissionService.submit()` его не устанавливает и отдельной history-фазы в этом
pipeline нет.

### Необратимая граница dispatch

Точный порядок:

`_mark_dispatch_started()` → commit `dispatch_started=true` → HTTP POST →
`_mark_dispatch_completed()`.

Перед POST worker повторно читает запись. Если started уже true, а completed false,
он вызывает `_mark_dispatch_uncertain()` и не вызывает `prepare_order_submission()` или
`send_order_submission()` повторно. Поэтому safety не зависит только от кнопки:

- `pending_submission.failed_stage == dispatch_uncertain` блокирует
  `_prepare_submission()` ещё в engine;
- `SubmissionService.submit()` повторно блокирует POST по `SubmissionRecord`;
- fresh `v2:submit` при dispatch uncertain не enqueue-ит задачу;
- stale callback отбрасывается до state mutation;
- worker task после dispatch uncertain не получает исключение от самого POST и не
  запускает автоматический повтор.

Остаётся UI-особенность: worker отправляет uncertain-card отдельным сообщением и не
обновляет `ui_revision`/`visible_actions` в session. Старая кнопка submit может
остаться технически доступной, но её fresh callback снова блокируется pending marker,
а stale callback — revision guard.

### UI и callback contracts

Обычный failure:

- текст: `⚠️ <b>Отправка не завершена</b>`, номер заявки и фраза о пропущенных
  завершённых этапах;
- кнопки: `Повторить отправку` → `v2:submit`, `К черновику` → `v2:back`;
- `_callback_with_revision()` добавляет `:rN`.

Dispatch uncertain:

- текст: `⚠️ <b>Нужно проверить отправку</b>`, order code и явный запрет повторной
  отправки;
- `rows=[]`, retry button отсутствует;
- order code показывается пользователю;
- state остаётся `SUBMISSION_FAILED`/`dispatch_uncertain`;
- pending snapshot сохраняется.

### Retry button и snapshot reuse

Fresh `v2:submit:rN` проходит `parse_callback()` как `SUBMIT_AS_IS`, затем:

`ConversationEngine` → `FinalReviewHandler.SUBMIT_AS_IS` → `_prepare_submission()` →
если `pending_submission.order_no` и `rows` существуют, используется тот же order
number и тот же snapshot → `SUBMITTING` → `enqueue_submission=True` →
`SubmissionService.submit()`.

Новый order number и rows при retry не создаются. Checkpoint reuse обеспечивается в
`SubmissionService.submit()`:

- `catalog_updated=true` пропускает повторный catalog increment;
- `recalc_done=true` пропускает recalculation;
- `dispatch_completed=true` пропускает POST и использует сохранённый external order;
- `order_no` и `rows` берутся из `PendingSubmission`/`SubmissionRecord.payload`.

### Первый unsafe transition: failure → back/edit → retry frozen snapshot

Это наиболее опасный подтверждённый переход текущего кода.

1. При обычном failure `pending_submission` сохраняется с исходными rows.
2. Callback `v2:back` возвращает `cart_reply()`, но оставляет stage
   `SUBMISSION_FAILED` и pending snapshot.
3. Текстовая команда «назад» ставит `COLLECTING`, очищает только transient dialog
   fields и также сохраняет pending snapshot.
4. Пользователь меняет количество или удаляет/добавляет товар.
5. Fresh `v2:submit` снова вызывает `_prepare_submission()` и enqueue-ит старые rows.

Black-box reproduction на текущем checkout: в cart quantity была изменена с `2` на
`9`, но после retry `pending_submission.rows` сохранил `Кол-во=2`, order number
остался прежним, `stage=SUBMITTING`, `enqueue_submission=True`.

Следствие: retry после редактирования отправляет не текущий cart, а замороженный
snapshot. Если `catalog_updated=false`, retry дополнительно может повторить catalog
side effect по старым rows. Это не duplicate POST при dispatch uncertain, но это
нарушение ожидаемой связи «retry → актуальный черновик» и потенциальный duplicate
catalog increment после неясного ответа Google Sheets.

### BACK, независимые команды и новая заявка

Для обычного failure и dispatch uncertain текущий engine фактически ведёт себя так:

- `SHOW_CART`, `HELP`, `THANKS`, `ORDER_STATUS` не очищают pending snapshot;
- `ADD_ITEMS`, `REMOVE_ITEM`, `EDIT_QUANTITY` меняют текущий cart, но не меняют
  `pending_submission`;
- `ADD_MORE` переводит stage в `COLLECTING`, pending сохраняется;
- callback `BACK` оставляет `SUBMISSION_FAILED`, текстовый `BACK` переводит в
  `COLLECTING`;
- `START_NEW_ORDER` и `CLEAR_CART` создают `_fresh_order_state()`, очищают cart и
  pending submission из session state;
- `SubmissionRecord` и `OrderEvent` при этом не удаляются, поэтому audit trail
  сохраняется;
- для dispatch uncertain после new/clear state теряет удобный pending marker, но
  database record с `dispatch_started=true/completed=false` остаётся защитой от POST.

Отзыв доступа проверяется в `SubmissionService.submit()` до основного `try`. При
отказе доступа отправляется access-disabled reply, но stage/status не переводятся в
`SUBMISSION_FAILED`; pending остаётся. Это отдельный эксплуатационный gap, не обход
access protection.

### TEXT / VOICE / `/submit` фактическая маршрутизация

Deterministic `infer_intent()` сейчас возвращает:

| Фраза | Intent | Текущий результат в обычном `SUBMISSION_FAILED` |
|---|---|---|
| «повтори» | `ADD_ITEMS` с псевдопозицией | пытается добавить/сопоставить новый item; retry не выполняется |
| «повтори отправку» | `SUBMIT_AS_IS` | retry snapshot, enqueue |
| «отправь ещё раз» | `ADD_ITEMS` с псевдопозицией | не retry, возможна лишняя unresolved позиция |
| «отправляй» | `SUBMIT_REQUEST` | показывает final review, enqueue нет |
| «да» | `CONFIRM` + `AFFIRM` | generic confirm/current-item route, enqueue нет |
| «попробуй снова» | `ADD_ITEMS` с псевдопозицией | не retry |
| `/submit` | `SUBMIT_REQUEST` | показывает final review, enqueue нет |

Для VOICE после transcription используется тот же parser/engine contract; на stage
`SUBMISSION_FAILED` submit-specific voice fallback не добавляет отдельного retry
правила. Поэтому misclassification коротких retry-фраз переносится и на voice.

При `dispatch_uncertain` ни одна из этих фраз не выполняет повторный POST:
`SUBMIT_AS_IS` блокируется `_prepare_submission()`, `SUBMIT_REQUEST` только
показывает review, а псевдотоварные `ADD_ITEMS` могут менять cart, но не запускают
внешнюю отправку.

### Completion notification и Celery

`submit_order` использует `autoretry_for=(Exception,)`, backoff/jitter и до восьми
повторов. `report_failure=True` включается только на финальной попытке; поэтому
обычная failure-card появляется после worker retries, а не после первой временной
ошибки.

После `_finalize()` локальная переменная `finalized=True` выставляется до отправки
Telegram completion card. Ошибка Telegram не вызывает `_fail()`: сохраняется
transient error, а следующий worker run находит
`finalized=True, completion_notified=False` через `_load_unnotified_completion()` и
повторяет только уведомление. Внешняя заявка не создаётся повторно.

### Нужен ли `CompatibilityContext.SUBMISSION_FAILED`

Нужен один отдельный контекст `CompatibilityContext.SUBMISSION_FAILED`, но не два
параллельных policy-механизма. Внутри него достаточно structured mode:

- известный обычный failure: `stage=SUBMISSION_FAILED` и
  `pending_submission.failed_stage != dispatch_uncertain`;
- uncertain: `state.status=dispatch_uncertain` или
  `pending_submission.failed_stage=dispatch_uncertain`;
- окончательная защита от crash-gap остаётся в `SubmissionRecord` перед POST.

Policy не должна анализировать `last_error`/exception text. При отсутствии marker в
session, но наличии `dispatch_started=true/completed=false` только worker может
доказательно восстановить режим; это архитектурный gap между DB checkpoint и
session state, который нельзя закрывать regex-правилом.

Предлагаемая матрица будущей policy:

| Mode | Команда | Решение |
|---|---|---|
| ordinary failure | `SUBMIT_AS_IS`/retry callback | `CONTINUE` к frozen-snapshot retry |
| ordinary failure | `BACK`, `SHOW_CART` | `CONTINUE` navigation без enqueue |
| ordinary failure | status/help/thanks | `INTERRUPT` в обычный routing |
| ordinary failure | add/edit/remove | `INTERRUPT`, но отдельно решить invalidation snapshot |
| ordinary failure | unknown/uncertain | `AMBIGUOUS`, повторить failure card |
| dispatch uncertain | любой submit/retry-like intent | `REJECT`, никогда не enqueue |
| dispatch uncertain | back/status/help/new order | безопасная navigation без очистки audit |
| dispatch uncertain | add/edit/remove | `INTERRUPT`, marker/audit не терять |

### Implementation boundary

Минимальный будущий diff может начинаться с `StateCompatibilityPolicy`,
`ModalRoutingDecision`, `ConversationEngine` и focused tests. Но policy-only fix
недостаточен для естественных текстовых retry-фраз: текущий deterministic parser
возвращает «повтори», «отправь ещё раз» и «попробуй снова» как `ADD_ITEMS` с
псевдотоварами. Такую семантику нельзя безопасно восстановить в policy без raw
natural-language parsing. Потребуется либо исправление глобального structured
parser/intent contract, либо безопасный contextual visible-action fallback до
candidate/item routing.

Отдельно потребуется решить snapshot invariant: после `BACK`/редактирования либо
замораживать cart от retry, либо явно инвалидировать `PendingSubmission` и строить
новый snapshot/order number. Нельзя автоматически пересобирать snapshot после
`catalog_updated=true`, иначе повторится catalog side effect.

### Future regression plan

Добавить только на implementation этапе:

- обычный failure: fresh retry callback, text/voice retry phrases, back, show cart,
  status, thanks/help, add/edit/remove, new order, clear cart;
- snapshot: retry без изменений, retry после quantity/edit/remove/add, сохранение
  order number и rows, invalidation policy;
- checkpoints: ошибка до catalog update, во время catalog update, после catalog,
  после recalc, до dispatch, после dispatch completed, finalize и notification;
- dispatch uncertain: отсутствие retry button, text/voice «повтори», `/submit`,
  fresh/stale `v2:submit`, отсутствие второго POST;
- access revoked, Celery retry interaction, completion-notification recovery и
  audit trail после new/clear.

### Итог анализа

- ordinary `_fail()` и `_mark_dispatch_uncertain()` — два разных state contracts;
- hard safety после `dispatch_started` уже присутствует в worker и engine;
- первый подтверждённый unsafe transition — retry frozen snapshot после BACK/edit;
- один `SUBMISSION_FAILED` context со structured mode достаточен, два contexts не
  нужны;
- parser misclassification коротких retry-фраз — доказанный blocker для одного
  только policy diff;
- notification failure после finalize не является `SUBMISSION_FAILED`;
- `SUBMISSION_FAILED` implementation не начиналась.
## SUBMISSION_FAILED — IMPLEMENTED

Этап реализован в текущем checkout и ограничен recovery routing.

- `CompatibilityContext.SUBMISSION_FAILED` добавлен в единый `StateCompatibilityPolicy`.
- Recovery lock сохраняет frozen `PendingSubmission`, блокирует mutation intents и не допускает retry при `dispatch_uncertain`.
- Standalone retry-фразы передаются как `ParsedCommand.retry_requested`; text и voice используют один путь.
- Обычный `_fail()` сохраняет structured status `submission_failed`.
- Добавлены регрессии в `tests/conversation/test_submission_failed_routing.py`.

## NEXT FUNCTIONAL STEP

`review/modal contexts` — следующий функциональный этап roadmap. Он не реализуется в текущем diff.

## ARCHITECTURAL REFACTOR STATUS

Декомпозиция не продолжалась; изменения ограничены существующими domain/parser/policy/modal/engine/submission границами.

## REVIEW / MODAL CONTEXTS — IMPLEMENTED

Этап REVIEW/MODAL CONTEXTS завершён минимальным изменением маршрутизации
карточки заявки из таблицы.

- В `StateCompatibilityPolicy` добавлен узкий `CompatibilityContext.SHEET_REVIEW`,
  активный только для `stage=REVIEW` и `review_mode="sheet_link"`.
- Обычный `review_mode="cart"` не получает modal-lock и остаётся редактируемым.
- В `UpdateOrchestrator` global `ParsedCommand` проходит policy до contextual
  sheet-review нормализации и review dispatch.
- `ADD_ITEMS` с реальными позициями, включая позицию без quantity, и остальные
  независимые business intents проходят обычный routing и не превращаются в
  submit/cancel карточки.
- `SUBMIT_REQUEST`, `SUBMIT_AS_IS`, `CONFIRM`/AFFIRM нормализуются в
  `REVIEW_SUBMIT`, а `CANCEL`, `BACK`/DECLINE — в `REVIEW_CANCEL` с текущим
  `review_token`.
- Удалён sheet-review fallback через `choose_visible_action`; произвольная или
  uncertain-фраза получает безопасный ambiguous reply с актуальными
  submit/refresh/cancel controls и сохраняет token, fingerprint, venue и cart.
- Callback-путь, token/revision guards и fingerprint refresh сохранены.
- Фото остаётся отдельным recognition path и не может отправить sheet review.
- `_parse_review_voice_command` переименован в `_parse_sheet_review_command`;
  helper больше не угадывает UI-действие по raw language.

Проверки:

- focused и связанные routing/review тесты прошли;
- Ruff, mypy, `git diff --check` и проверка markdown-ссылок прошли;
- один известный исторический failure
  `test_manual_action_without_an_open_item_uses_source_recovery_card` не входит
  в текущий diff и оставлен без изменения.
- `build_agent_context.py` ранее не смог обновить runtime snapshot из-за
  `PermissionError`; это отдельная проблема harness и не относится к этапу.

## NEXT FUNCTIONAL STEP

`pending_new_order_confirmation` — следующий функциональный этап roadmap.
До его отдельного анализа и реализации semantics блока не менять.

## ARCHITECTURAL REFACTOR STATUS

Декомпозиция при реализации REVIEW/MODAL CONTEXTS не продолжалась; изменены
только существующие policy/orchestrator boundaries и focused regression tests.

## PENDING NEW ORDER CONFIRMATION — ANALYSIS COMPLETE / IMPLEMENTATION PENDING

Анализ выполнен без изменения application code. Checkout на момент анализа:
`decompose_bot`, HEAD `e96de7c53e5f2f89539b9caf49a0d7dbd263b75d`, `origin` — только
GitHub. Файл `.env` не читался.

### Фактический контракт и владельцы состояния

`ConversationState.pending_new_order_confirmation` объявлен в
`src/restaurant_bot/domain/models.py` и по умолчанию равен `False`. Единственное
место, где флаг устанавливается в обычном runtime, — ветка `START_NEW_ORDER` в
`ConversationEngine.handle()`:

```text
SUBMITTED или нет активных позиций -> _start_new_order()
иначе -> pending_new_order_confirmation=True и карточка подтверждения
```

Сброс в обычном runtime выполняется только в текущем confirmation-блоке при
отказе/навигации. При подтверждении вызывается `_start_new_order()`, который
создаёт новый `ConversationState`; поэтому флаг и все данные текущего черновика
исчезают вместе со старым state. Других прямых присваиваний флагу не найдено.

Активный черновик определяется как наличие хотя бы одного `CartItem`, чей статус
не `SKIPPED`. Пустая корзина и корзина только со `SKIPPED` не требуют подтверждения.
`product_add_requests` сами по себе активным черновиком не считаются.

### Текущий порядок обработки

Фактический путь `ConversationEngine.handle()` сейчас выглядит так:

```text
TEXT/VOICE ParsedCommand
  -> voice contextual normalization
  -> StateCompatibilityPolicy для уже поддержанных modal contexts
  -> stale callback revision guard
  -> SUBMISSION_FAILED recovery
  -> submit/add-more/comment/manual/not-found modal guards
  -> contextual rewrites, duplicate/unit/candidate/product-add handling
  -> pending_new_order_confirmation block
  -> pending quantity и обычный navigation/business routing
```

То есть confirmation-флаг проверяется после большинства contextual преобразований
и не является частью `StateCompatibilityPolicy`. Его блок содержит собственный
разбор raw text: `normalize_text`, `_is_explicit_yes`, `has_negation` и префиксы
`нет/остав/сохран/передум`. Это второй независимый источник решения о смысле
сообщения и главный архитектурный конфликт с единым policy-пайплайном.

### Первый подтверждённый небезопасный переход

Для активной корзины с `pending_new_order_confirmation=True` фактическая
репродукция дала следующие результаты:

| Вход | ParsedCommand | Текущее действие |
|---|---|---|
| `да` | `CONFIRM/AFFIRM` | очищает корзину и создаёт новый state |
| `нет` | `CANCEL/DECLINE` | сбрасывает флаг, корзина остаётся |
| `пармезан 3 кг` | `ADD_ITEMS` с реальным item | confirmation повторяется, товар не добавляется |
| `удали сыр` | `REMOVE_ITEM` | confirmation повторяется, удаление не выполняется |
| `измени курицу на 5 кг` | `EDIT_QUANTITY` | confirmation повторяется |
| `покажи черновик` | `SHOW_CART` | флаг сбрасывается, показывается корзина |
| `помощь`, `спасибо`, `статус заявок` | независимый/близкий intent | confirmation повторяется |
| `новая заявка` | `START_NEW_ORDER` | трактуется как подтверждение и очищает корзину |
| `очисти черновик` | `CLEAR_CART` | немедленно очищает корзину |
| `ну посмотрим` | deterministic `CHECK_MIN_SUM` | confirmation повторяется |

Самый опасный переход — смешанная фраза `да, добавь пармезан 3 кг`. Парсер
возвращает реальный `ADD_ITEMS`, но текущий engine сначала смотрит на raw token
`да` и вызывает `_start_new_order()`. В результате исходный черновик удаляется,
а новый товар не добавляется. Это не частный случай товара: он показывает, что
raw-language confirmation имеет приоритет над уже структурированным intent.

Также `нет, добавь сыр` сейчас теряет товар ещё на этапе parser и становится
`CANCEL/DECLINE`; policy не должна пытаться восстановить такую семантику по raw
строке. На этапе реализации нужно отдельно решить, достаточно ли изменить
structured parser contract для смешанных фраз.

### Callback и destructive boundary

Карточка подтверждения создаёт кнопки `v2:clear` и `v2:back`. Callback parser
преобразует их в `CLEAR_CART` и `BACK`; варианты с `:rN` несут revision. Свежий
callback проходит stale-revision guard до любого state mutation. Старый
revision отклоняется. Legacy callback без revision пока принимается — это
отдельный compatibility gap, не исправляемый в этом analysis-only этапе.

`_start_new_order()` является destructive boundary. Новый state сохраняет только
идентичность заведения/пользователя, историю номеров, metadata и глубокую копию
`product_add_requests`. Он уничтожает корзину, комментарии, quantities,
кандидатов, статусы, issue refs, pending submission, order trace, review data,
pending modal contexts и UI revision/actions.

Сохранение `product_add_requests` намеренно подтверждено существующими тестами.
Запросы не связаны с `order_trace_id`/`order_no`, поэтому после начала новой
заявки они остаются общей историей заведения и могут визуально смешиваться с
новым заказом. Это отдельный follow-up, не изменение текущего этапа.

Подтверждён дополнительный риск: если вручную выставить confirmation-флаг в
`SUBMITTING`, то `да` вызывает `_start_new_order()` и уничтожает
`pending_submission`. Обычный путь установки флага из `SUBMITTING` сейчас не
защищён. При будущей реализации confirmation policy состояние с активной
отправкой должно быть `REJECT`/recovery-lock, а не обычным подтверждением.

### Сосуществование с текущими modal contexts

| Текущее состояние | Что происходит при `START_NEW_ORDER` сейчас | Риск |
|---|---|---|
| `COLLECTING`/`REVIEW` с активной корзиной | включается confirmation overlay | overlay перехватывает независимые intents |
| `AWAIT_UNIT_QUANTITY` | сохраняются missing-quantity refs, затем включается overlay | после `нет`/`SHOW_CART` modal остаётся скрытым, UI показывает только корзину |
| `AWAIT_MANUAL_DETAILS`, `AWAIT_PRODUCT_ADD_DETAILS` | refs остаются, overlay ставится поверх | resume-контракт не определён |
| `AWAIT_ADD_MORE_CONFIRM` | текущая ветка меняет stage на `REVIEW`, затем ставит overlay | исходный add-more context не восстанавливается |
| `AWAIT_COMMENT_SCOPE` | pending comment context остаётся до destructive reset | при отказе возможен скрытый scope-контекст |
| candidate/`NOT_FOUND`/duplicate/unit | item refs/candidates сохраняются | confirmation перекрывает contextual choice |
| `AWAIT_SUBMIT_CONFIRM` | submit policy прерывается, stage может перейти в `REVIEW` | review/submit context не имеет единого resume |
| `SUBMISSION_FAILED` | recovery policy имеет более ранний приоритет и обычно блокирует START/CLEAR | требует явного запрета обхода recovery |
| `SUBMITTING` | прямой policy lock отсутствует | подтверждён риск потери frozen submission |
| `SUBMITTED` или пустая/только skipped корзина | новый state создаётся сразу | безопасно по текущему контракту |
| sheet review | orchestrator закрывает sheet metadata перед обычным routing | нужно сохранить token/revision invariants |

Существующий state способен сохранить underlying modal data, поэтому отдельный
`suspended_interaction` для этого этапа не нужен. Но при `NO`/`INTERRUPT` нужно
будущее правило возобновления: снять только overlay и передать команду обычному
modal handler либо показать соответствующий текущему stage вопрос. Простая
отправка `cart_reply` оставляет state modal и UI несогласованными.

### Независимые intents и будущая policy

Новая policy должна быть единственной точкой решения и получать уже готовый
`ParsedCommand`; raw natural language не должен повторно разбираться в
`orchestrator`/`engine`.

Предлагаемый будущий контекст — узкий
`CompatibilityContext.NEW_ORDER_CONFIRMATION`, активный только при
`pending_new_order_confirmation=True`. Точка подключения: после stale callback
guard и SUBMISSION_FAILED/SUBMITTING safety, до underlying modal handlers и до
старого confirmation block. Структуру `StateCompatibilityPolicy` и
`ModalRoutingDecision` нужно расширить, а не создавать параллельный набор if в
engine.

| ParsedCommand/context | Решение |
|---|---|
| свежий `v2:clear` callback | `CONTINUE`/YES, destructive `_start_new_order()` |
| `CONFIRM` + `AFFIRM` без concrete items | `CONTINUE`/YES |
| `CANCEL`, `BACK`, `DECLINE` | `CONTINUE`/NO: снять overlay, сохранить draft и корректно возобновить context |
| concrete `ADD_ITEMS` | `INTERRUPT`: снять overlay, обычный add routing, старый draft не менять |
| `REMOVE_ITEM`, `EDIT_QUANTITY`, `EDIT_COMMENT` | `INTERRUPT`, обычный routing |
| `SHOW_CART`, `SHOW_FINAL_REVIEW`, status/help/thanks/navigation | `INTERRUPT` или безопасная navigation без destructive reset |
| повторный `START_NEW_ORDER` | `AMBIGUOUS`/повторить prompt; не считать сам себя подтверждением |
| typed `CLEAR_CART`/`/reset` | отдельное explicit destructive действие; не смешивать с YES callback semantics |
| unknown/uncertain | `AMBIGUOUS`, state и draft без изменений |
| `SUBMISSION_FAILED`/`SUBMITTING` | `REJECT`/recovery lock, pending submission не удалять |
| stale callback | `REJECT` до mutation |
| photo с реальными items | `INTERRUPT` в обычный product routing; OCR не может быть YES |

Критический приоритет — concrete items над affirmative/decline marker в смешанной
фразе. Если parser не сохраняет item, policy не должна угадывать его из raw
текста; это отдельная доказательная задача parser contract.

### Audit, trace и внешние эффекты

`UpdateOrchestrator._append_order_transition_events()` уже различает prompt,
подтверждённое очищение и отказ: prompt пишет только `user_action`, подтверждение
пишет `order_cancelled` один раз, отказ не отменяет trace. Будущее прерывание
confirmation независимым intent должно не создавать `order_cancelled`; событие
должно отражать только фактическое destructive очищение. Нужно отдельно проверить
случай, когда последний item становится `SKIPPED`: текущая проверка смотрит на
пустой список, а не на отсутствие активных позиций.

Обычные START/YES/NO проверены для текста и голоса: voice проходит тот же
engine confirmation block и демонстрирует те же перехваты. Фото recognition
также не может обойти блок: результат с реальным item повторно показывает
confirmation. Callback остаётся отдельным явным UI-путём и должен сохранить
revision guard.

### Безопасный порядок реализации

1. Добавить только `NEW_ORDER_CONFIRMATION` в существующую
   `StateCompatibilityPolicy`/`ModalRoutingDecision`.
2. Подключить один preemption point в `ConversationEngine` после stale/recovery
   guards и убрать raw-text confirmation precedence.
3. Явно закрыть `SUBMITTING`/`pending_submission` от destructive reset.
4. Реализовать корректное NO/INTERRUPT resume underlying modal context без
   `suspended_interaction`.
5. Только при доказанном parser gap отдельно исправлять смешанные structured
   clauses; prompts и общий parser без такого evidence не менять.

Нужны regression tests для text/voice/photo/callback: YES, NO, fresh/stale
callbacks, concrete ADD/REMOVE/EDIT/SHOW_CART/THANKS/help/status, unknown,
`SUBMITTING`, `SUBMISSION_FAILED`, every underlying modal context, preserved
`product_add_requests`, audit events, repeated delivery and resume after modal
interruption. До этой реализации новые тесты и application code не добавлялись.

## NEXT FUNCTIONAL STEP

`pending_new_order_confirmation` — реализовать описанную policy и единый
preemption/resume routing. После завершения этого этапа следующий roadmap-блок —
`FULL REGRESSION / BEHAVIOR AUDIT`.

## ARCHITECTURAL REFACTOR STATUS

Декомпозиция не продолжалась. Анализ использует существующие границы
`StateCompatibilityPolicy`, `ConversationEngine` и `UpdateOrchestrator`; переносов
между директориями и изменения application behavior в этом этапе нет.

## PENDING NEW ORDER CONFIRMATION — IMPLEMENTED

Этап 13/13 завершён в commit `refactor: protect new order confirmation routing`.
Изменён только последний modal block; decomposition, MAX integration и полный
behavior audit в этот commit не входят.

### Реализованный pipeline

```text
global ParsedCommand
  -> evaluate_modal_routing()
  -> stale callback guard
  -> SUBMISSION_FAILED / frozen SUBMITTING safety
  -> NEW_ORDER_CONFIRMATION decision
  -> underlying modal или обычный routing
```

Добавлен единый `CompatibilityContext.NEW_ORDER_CONFIRMATION` и поле
`ModalRoutingDecision.new_order_confirmation`. Policy не разбирает raw text,
не вызывает `normalize_text(command.text)`, не использует regex и не содержит
state-specific reparse.

Удалён старый engine-блок с `_is_explicit_yes`, `has_negation` и префиксами
`нет/остав/сохран/передум` для confirmation. Решение теперь принимает только
structured command и state.

### Приоритеты и инварианты

- concrete `ADD_ITEMS` с реальным item, `REMOVE_ITEM`, `EDIT_QUANTITY` и
  `EDIT_COMMENT` прерывают confirmation; draft не очищается;
- `да, добавь пармезан 3 кг` добавляет пармезан в текущую заявку, не удаляя курицу;
- `пармезан` без количества проходит обычный flow и может открыть quantity;
- чистое `CONFIRM/AFFIRM` (`да`, `ага`, `подтверждаю`) вызывает единственный
  destructive `_start_new_order()`;
- `CANCEL`, `BACK`, `DECLINE` снимают overlay и сохраняют draft;
- повторный `START_NEW_ORDER` остаётся `AMBIGUOUS` и повторяет карточку;
- explicit `CLEAR_CART` и свежий `v2:clear:rN` остаются destructive YES;
- `SHOW_CART`, `HELP`, `THANKS`, `ORDER_STATUS` и прочие независимые intents
  больше не проглатываются confirmation;
- unknown/uncertain сохраняет `pending_new_order_confirmation`, cart, stage и
  modal metadata без side effects;
- stale callback отклоняется до любого confirmation transition;
- `SUBMISSION_FAILED` сохраняет прежний recovery lock;
- `SUBMITTING`/`pending_submission` больше не могут быть уничтожены через YES,
  CLEAR или START_NEW_ORDER; возвращается безопасное сообщение о текущей отправке.

### Mixed parser contract

Фактический parser терял товар в `нет, добавь сыр` и превращал `да, добавь
пармезан 3 кг` в product query с вводным маркером. Добавлена небольшая общая
normalization-функция parser, не знающая о state confirmation:

- explicit positive action после вводного `да/нет` сохраняется как `ADD_ITEMS`;
- отрицание `не добавляй сыр` не становится положительным ADD;
- affirmative «да, начинай новую» нормализуется в `CONFIRM/AFFIRM`;
- standalone quantity получает structured `quantity_hint`, не меняя global intent.

Это позволило вернуть `5 кг` в существующий `PendingQuantityHandler` после снятия
overlay без raw-language policy.

### Underlying modal resume

После NO сохранённый underlying context отображается своим текущим presenter-ом:
quantity/candidate/not-found/duplicate/unit идут через `_advance()`, comment scope
повторяет clarification card, product-add и add-more используют существующие
prompts, submit-confirm показывает final review. `suspended_interaction`, state
stack и копирование предыдущего stage не добавлялись.

### Audit, trace и сохранённые данные

`_start_new_order()` и существующий audit contract не переписывались. Только
фактический destructive reset закрывает прежний trace и создаёт `order_cancelled`;
NO и independent interrupt trace не закрывают. `product_add_requests` сохраняются
как раньше; их ownership между заявками оставлен отдельным follow-up.

### Regression coverage

Добавлен `tests/conversation/test_new_order_confirmation_routing.py` с проверками:

- TEXT/VOICE YES, NO, AMBIGUOUS;
- mixed ADD с affirmative/decline marker и ADD без quantity;
- REMOVE, EDIT_QUANTITY, EDIT_COMMENT, SHOW_CART, HELP, THANKS;
- AWAIT_UNIT_QUANTITY resume;
- candidate selection resume;
- fresh/stale callbacks;
- PHOTO с товарами и без товаров;
- защита `SUBMITTING` frozen `PendingSubmission`;
- parser negative contract для `не добавляй`.

Focused routing suite прошёл. Соседний targeted набор прошёл после исключения одного
известного исторического теста duplicate prompt. Более широкий набор показывает
несвязанные с этим diff исторические падения voice/AI postprocessing recovery
(`tests/input/test_voice_input_contract.py`, `test_voice_quantity_recovery.py`,
`test_voice_processing_card.py`); они не исправлялись.

Проверки текущего diff:

- project `.venv` pytest focused/adjacent: passed;
- Ruff check: passed;
- mypy: passed (`52 source files`);
- markdown links: passed;
- `git diff --check`: passed;
- `.env` не читался и не tracked.

`build_agent_context.py` всё ещё не может обновить snapshot из-за
`PermissionError` на `.agents/runtime/CURRENT_CONTEXT.md`; это Agent Harness
follow-up, не часть state-machine diff.

## STATE MACHINE ROADMAP — 13/13 DONE

Все запланированные modal blocks реализованы: MISSING_QTY, COMMENT_SCOPE,
AMBIGUOUS/CANDIDATE, NOT_FOUND, DUPLICATE_PENDING, UNIT_MISMATCH,
AWAIT_MANUAL_DETAILS, AWAIT_PRODUCT_ADD_DETAILS, AWAIT_ADD_MORE_CONFIRM,
AWAIT_SUBMIT_CONFIRM, SUBMISSION_FAILED, REVIEW/MODAL CONTEXTS и
PENDING_NEW_ORDER_CONFIRMATION.

## REQUIRED FOLLOW-UPS

### FULL USER JOURNEY LOGGING

Отдельный production observability этап. Позже нужно связывать по `trace_id`:
input/transcript/photo/callback, ParsedCommand, state/cart before, compatibility
decision, handler, state/cart after, state diff, reply, visible actions, side
effects, errors/retries, `order_no` и `update_id`. Secrets (`.env`, API keys,
credentials, authorization tokens) не логировать. В текущем commit полное
пользовательское логирование не реализовывалось.

### PRODUCT_ADD_REQUESTS OWNERSHIP

Сохранить решение о том, являются ли `product_add_requests` глобальной историей
заведения или должны иметь ownership заявки/trace. В текущем этапе contract и
существующие тесты не менялись.

## NEXT FUNCTIONAL STEP

`Block A — AI source-evidence/provenance reconciliation` после завершённого
analysis-only аудита. Сначала подтвердить план и regression matrix, затем менять
только recovery/provenance boundary.

## FULL REGRESSION AUDIT — COMPLETE / FIXES PENDING

- 1237 тестов: 1185 passed, 52 failed, skipped/xfailed/errors: 0.
- Подтверждённых REAL BUG, внесённых 13-block refactor: 0.
- 45 падений — ранее зафиксированный old baseline; 7 — stale `SUB-08` scenario
  references после переименования теста.
- High-risk follow-up: повторный `increment_catalog_quantities()` при падении
  между внешней записью и checkpoint `catalog_updated`.
- Полная карта аудита: [docs/FULL_REGRESSION_AUDIT.md](docs/FULL_REGRESSION_AUDIT.md).

## DATA INTEGRITY ANALYSIS — COMPLETE / FIXES PENDING

- Добавлен [docs/DATA_INTEGRITY_ANALYSIS.md](docs/DATA_INTEGRITY_ANALYSIS.md).
- Зафиксированы 10 кластеров A–J: 8 data/semantic, photo contract drift и UX
  wording drift; 45 old baseline failures и 7 stale `SUB-08` failures сохранены
  отдельно.
- Первый рекомендуемый P0/P1 блок: source-evidence/provenance reconciliation
  вокруг `recover_omitted_explicit_items()`; код пока не менялся.
- Не изменялись prompts, parser, matching, state-machine, MAX/submission
  idempotency, logging и decomposition.

## ARCHITECTURAL REFACTOR STATUS

Декомпозиция не продолжалась. Изменения ограничены существующими границами
policy, parser normalization, engine routing и focused regression tests.
