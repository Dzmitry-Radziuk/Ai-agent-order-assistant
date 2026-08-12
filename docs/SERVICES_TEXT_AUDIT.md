# Block 5L — аудит `services/text.py`

## Block 5R — завершённый перенос presentation formatting

`escape` и `format_number` теперь принадлежат
`presentation/telegram/formatting.py`; старых callers из `services.text` нет.
В `services/text.py` сохранены units, numbers, overlap и parsing primitives.
Measurement symbols в этот перенос не входили.

## Статус и границы

Аудит выполнен на ветке `decompose_bot` в исходной ревизии
`7e8a84c134f72749439d28b7dd187739f70182d8`. Этот SHA является исходным audit baseline Block 5L;
он не описывает текущий remote после последующих commits. Семантический baseline,
указанный для сравнения, — `f9cbc3195c0eae843de3208e488c3f46baa5a5ec`.

Block 5L выполнен только как исследование. Файлы `src/**/*.py` и `tests/**/*.py`
не изменялись. Block 5K не пересматривается: порт `BackgroundTaskDispatcher`,
`CeleryBackgroundTaskDispatcher`, `_enqueue_side_effects`, checkpoint
`tasks_enqueued` и Celery-контракты остаются без изменений.

Проверены исходный файл целиком, AST-импорты, прямые вызовы, тестовые импорты,
re-export и динамические пути. Поиск `importlib`, `__import__`, строковых путей
и `getattr` для символов `services.text` дополнительных callers не обнаружил.
`services/parser.py` и `integrations/openai_client.py` не re-export-ят символы
из `services.text` через `__all__`.

## 1. Фактический состав модуля

В исходном снимке Block 5L `src/restaurant_bot/services/text.py` содержал 326 строк,
три словаря и двенадцать функций. После Block 5M в нём осталось 320 строк и десять
функций, а две чистые функции нормализации находятся в отдельном owner-модуле.
Все функции не имеют внешних эффектов и не мутируют переданные значения.

| Символ | Строки | Фактическая ответственность |
|---|---:|---|
| `UNIT_ALIASES` | 8–84 | Алиасы единиц измерения, фасовок и тары к коротким каноническим значениям. |
| `DEPARTMENT_ALIASES` | 86–101 | Алиасы отделов/зон заявки к заголовкам листа. |
| `NUMBER_WORDS` | 103–151 | Словесные числительные и их числовые значения. |
| `clean_text` | `text_normalization.py:7–10` | Приведение произвольного значения к строке, схлопывание пробелов и trim. |
| `normalize_text` | `text_normalization.py:12–16` | Канонизация текста для сравнений: lower, `ё/е`, кавычки и разрешённые символы. |
| `remove_global_comment_overlap` | 167–190 | Удаление общего комментария из локального комментария с удалением остатка области действия. |
| `remove_phrase_overlap` | 193–211 | Временное удаление подтверждённой фразы из поисковой копии. Исходные поля не меняет. |
| `normalize_unit` | 214–218 | Поиск канонической единицы по `UNIT_ALIASES`. |
| `normalize_department` | 220–223 | Поиск канонического отдела по `DEPARTMENT_ALIASES`. |
| `numeric_range_spans` | 226–251 | Поиск цифровых и словесных диапазонов характеристик товара. |
| `to_float` | 253–274 | Безопасный разбор положительного числа, включая запись из Google Sheets. |
| `escape` | 276–278 | HTML-экранирование текста для Telegram. |
| `format_number` | 281–287 | Отображение числа в пользовательском сообщении с десятичной запятой. |
| `parse_number_words` | 290–311 | Разбор одного числового фрагмента, записанного цифрами или словами. |
| `convert_quantity` | 313–326 | Пересчёт совместимых `г/кг` и `мл/л`. |

Таким образом, один файл одновременно владеет лексической нормализацией,
комментариями, поиском, единицами, числами, отделами и Telegram-представлением.
Это подтверждённое смешение ответственностей, а не проблема размера файла.

## 2. Таблица символов и callers

В таблице production callers указаны точные модули с прямым импортом символа.
Тестовые callers перечислены отдельно; остальные тесты используют символы косвенно
через production pipeline.

| Символ | Production callers | Test callers | Меняет состояние/внешний эффект | Канальная специфика | Текущий owner / целевой owner | Действие |
|---|---|---|---|---|---|---|
| `UNIT_ALIASES` | `catalog/evidence.py`, `catalog/safety.py`, `conversation/comments.py`, `integrations/openai_client.py`, `orders/catalog_resolution.py`, `parsing/ai/{item_reconciliation,quantity_reconciliation,shadow_items}.py`, `parsing/commands/{dialogue,navigation,router}.py`, `parsing/{products,quantities}.py`, `services/conversation_handlers/pending_quantity.py`, `services/{engine,replies}.py` | Нет прямых | Нет | Нет | `services/text.py` → `domain/measurements.py` или отдельный owner измерений | `MOVE_LATER` |
| `DEPARTMENT_ALIASES` | Только через `normalize_department` в `integrations/google_sheets.py` и `services/engine.py` | Нет прямых | Нет | Нет | `services/text.py` → `domain/departments.py` | `MOVE_LATER` |
| `NUMBER_WORDS` | `catalog/{evidence,safety}.py`, `conversation/comments.py`, `parsing/ai/{item_reconciliation,shadow_items}.py`, `parsing/commands/{dialogue,item_commands}.py`, `parsing/{products,quantities}.py`, `services/engine.py` | Нет прямых | Нет | Нет | `services/text.py` → owner числовых parsing primitives | `MOVE_LATER` |
| `clean_text` | `integrations/{google_sheets,openai_client}.py`, `parsing/ai/{comment_reconciliation,item_reconciliation,quantity_reconciliation,reconciliation,shadow_items}.py`, `parsing/commands/{comment_commands,item_commands,router}.py`, `parsing/{comment_scope,packaging,products}.py`, `input/telegram.py`, `services/{orchestrator,venue_registration}.py` | Косвенно через все соответствующие сценарии | Нет | Нет | `services/text.py` → `text_normalization.py` | `DONE` вместе с `normalize_text` |
| `normalize_text` | `catalog/{evidence,resolver,safety,scoring}.py`, `conversation/{comments,draft,selection}.py`, `conversation/routing/item_resolution.py`, `integrations/{google_sheets,openai_client}.py`, `orders/catalog_resolution.py`, `parsing/ai/{comment_reconciliation,item_reconciliation,quantity_reconciliation,reconciliation,shadow_items}.py`, `parsing/commands/{item_commands,normalization,router}.py`, `parsing/{comment_scope,packaging,products,quantities}.py`, `services/{conversation_handlers/navigation,pending_quantity,engine,input_recognition,orchestrator,venue_registration}.py` | Косвенно через все соответствующие сценарии | Нет | Нет | `services/text.py` → `text_normalization.py` | `DONE` вместе с `clean_text` |
| `remove_global_comment_overlap` | `parsing/ai/comment_reconciliation.py`; `conversation/comments.py` импортирует его под alias и предоставляет одноимённый wrapper; `services/engine.py` использует уже conversation-owner | Нет прямых | Нет | Нет | Алгоритм — `services/text.py`; wrapper — `conversation/comments.py` | `AUDIT_REQUIRED`, не объединять в один блок |
| `remove_phrase_overlap` | `orders/catalog_resolution.py`, `services/orchestrator.py` | `tests/catalog/{test_catalog_resolver,test_matching}.py` | Нет | Нет | `services/text.py` → `catalog/search_query.py` или `catalog/evidence.py` после отдельного search-аудита | `MOVE_LATER` |
| `normalize_unit` | `catalog/{evidence,safety}.py`, `conversation/{comments,draft}.py`, `integrations/openai_client.py`, `orders/catalog_resolution.py`, `parsing/ai/{quantity_reconciliation,shadow_items}.py`, `parsing/commands/dialogue.py`, `parsing/{packaging,products,quantities}.py`, `services/{conversation_handlers/pending_quantity,engine,input_recognition,replies}.py` | Нет прямых | Нет | Нет | `services/text.py` → owner измерительных/фасовочных единиц | `MOVE_LATER` |
| `normalize_department` | `integrations/google_sheets.py`, `services/engine.py` | Нет прямых | Нет | Нет | `services/text.py` → `domain/departments.py` | `MOVE_LATER` |
| `numeric_range_spans` | `integrations/openai_client.py`, `orders/catalog_resolution.py`, `parsing/ai/quantity_reconciliation.py`, `parsing/{packaging,products,quantities}.py` | Косвенно через quantity/packaging tests | Нет | Нет | `services/text.py` → quantity/evidence owner после сравнения regex | `MOVE_LATER` |
| `to_float` | `integrations/google_sheets.py`, `integrations/openai_client.py`, `parsing/ai/{comment_reconciliation,item_reconciliation,quantity_reconciliation,shadow_items}.py` | `tests/input/test_input_edge_cases.py` | Нет; читает входные значения | Нет | `services/text.py` → numeric value primitive, но Sheets-контракт требует отдельной проверки | `AUDIT_REQUIRED` |
| `escape` | `services/{engine,order_review,replies,submission,venue_registration}.py`, `presentation/telegram/submission.py` | Нет прямых | Нет | Да, Telegram HTML | `presentation/telegram/formatting.py` | `DONE` |
| `format_number` | `services/replies.py` | Нет прямых | Нет | Косвенно пользовательский UI | `presentation/telegram/formatting.py` | `DONE` |
| `parse_number_words` | `catalog/{evidence,safety}.py`, `parsing/ai/quantity_reconciliation.py`, `parsing/{products,quantities}.py`, `services/conversation_handlers/pending_quantity.py` | Косвенно через quantity tests | Нет | Нет | `services/text.py` → quantity parsing owner | `MOVE_LATER` |
| `convert_quantity` | `services/engine.py`, `services/replies.py` | Косвенно через unit-flow tests | Мутирует `CartItem` только callers, сам pure | Нет | `services/text.py` → order measurement owner | `MOVE_LATER` |

Ни один символ не вызывает внешнюю систему, не пишет в БД/Sheets и не меняет
`ConversationState` сам по себе. Внешние эффекты появляются только у callers
(`GoogleSheetsGateway`, Telegram replies, engine state transitions).

## 3. Caller matrix

| Caller module | Символы из `services.text` | Слой | Зависимость после миграции | Блокер |
|---|---|---|---|---|
| `catalog/evidence.py` | `NUMBER_WORDS`, `UNIT_ALIASES`, `normalize_text`, `normalize_unit`, `parse_number_words` | catalog/core | text normalization, measurements, numeric evidence | Нельзя смешать поиск и нормализацию |
| `catalog/resolver.py` | `normalize_text` | catalog/core | `text_normalization.py` | Нет |
| `catalog/safety.py` | `NUMBER_WORDS`, `UNIT_ALIASES`, `normalize_text`, `normalize_unit`, `parse_number_words` | catalog/safety | отдельные numeric/measurement owners | Safety не должен импортировать services |
| `catalog/scoring.py` | `normalize_text` | catalog/core | `text_normalization.py` | Нет |
| `conversation/comments.py` | `NUMBER_WORDS`, `UNIT_ALIASES`, `normalize_text`, `normalize_unit`, overlap wrapper | conversation/core | text, measurements, comment policy | Wrapper должен остаться channel-neutral adapter |
| `conversation/draft.py` | `normalize_unit` | conversation/core | measurements | Нет |
| `conversation/routing/item_resolution.py` | `normalize_text` | routing/core | `text_normalization.py` | Нет |
| `conversation/selection.py` | `normalize_text` | conversation/core | `text_normalization.py` | Нет |
| `orders/catalog_resolution.py` | `UNIT_ALIASES`, `normalize_text`, `normalize_unit`, `numeric_range_spans`, `remove_phrase_overlap` | orders/core | measurements, numeric evidence, search query owner | Нужен отдельный comparison corpus |
| `parsing/ai/comment_reconciliation.py` | `clean_text`, `normalize_text`, `remove_global_comment_overlap`, `to_float` | parsing/AI | text normalization, comment policy, numeric primitive | Provenance regression corpus |
| `parsing/ai/item_reconciliation.py` | `NUMBER_WORDS`, `UNIT_ALIASES`, `clean_text`, `normalize_text`, `to_float` | parsing/AI | text, measurements, numeric primitive | Shadow/recovery semantics |
| `parsing/ai/quantity_reconciliation.py` | `UNIT_ALIASES`, `clean_text`, `normalize_text`, `normalize_unit`, `numeric_range_spans`, `parse_number_words`, `to_float` | parsing/AI | quantity/evidence owners | Voice quantity and packaging corpus |
| `parsing/ai/reconciliation.py` | `clean_text`, `normalize_text` | parsing/AI | `text_normalization.py` | Нет |
| `parsing/ai/shadow_items.py` | `NUMBER_WORDS`, `UNIT_ALIASES`, `clean_text`, `normalize_text`, `normalize_unit`, `to_float` | parsing/AI | text, measurements, numeric primitive | Shadow collapse corpus |
| `parsing/commands/comment_commands.py` | `clean_text` | parsing/commands | `text_normalization.py` | Нет |
| `parsing/commands/dialogue.py` | `NUMBER_WORDS`, `UNIT_ALIASES`, `normalize_unit` | parsing/commands | command quantity owner | Не смешивать с global command routing |
| `parsing/commands/item_commands.py` | `NUMBER_WORDS`, `clean_text`, `normalize_text` | parsing/commands | text, command patterns | Нет |
| `parsing/commands/navigation.py` | `UNIT_ALIASES` | parsing/commands | measurements | Navigation должен оставаться command-specific |
| `parsing/commands/normalization.py` | `normalize_text` | parsing/commands | `text_normalization.py` | `normalize_command_text` — отдельная функция |
| `parsing/commands/router.py` | `UNIT_ALIASES`, `clean_text`, `normalize_text` | parsing/commands | text, measurements | Intent order не менять |
| `parsing/comment_policy.py` | `clean_text`, `normalize_text` | parsing/core | `text_normalization.py` | Comment policy остаётся owner policy |
| `parsing/comment_scope.py` | `clean_text`, `normalize_text` | parsing/core | `text_normalization.py` | Scope parser не смешивать с global normalization |
| `parsing/packaging.py` | `clean_text`, `normalize_text`, `normalize_unit`, `numeric_range_spans` | parsing/core | text, measurements, numeric evidence | Packaging/order distinction |
| `parsing/products.py` | `NUMBER_WORDS`, `UNIT_ALIASES`, `clean_text`, `normalize_text`, `normalize_unit`, `numeric_range_spans`, `parse_number_words` | parsing/core | product parser owners | Поведение Block 2A |
| `parsing/quantities.py` | `NUMBER_WORDS`, `UNIT_ALIASES`, `normalize_text`, `normalize_unit`, `numeric_range_spans`, `parse_number_words` | parsing/core | quantity owner | Quantity contract |
| `services/conversation_handlers/navigation.py` | `normalize_text` | transitional handler | `text_normalization.py` | Legacy handler remains |
| `services/conversation_handlers/pending_quantity.py` | `UNIT_ALIASES`, `normalize_text`, `normalize_unit`, `parse_number_words` | transitional handler | measurements, quantity owner | MISSING_QTY behavior |
| `services/engine.py` | `NUMBER_WORDS`, `UNIT_ALIASES`, `convert_quantity`, `escape`, `normalize_department`, `normalize_text`, `normalize_unit` | transitional state machine | domain measurements, presentation, text | Не начинать engine decomposition |
| `input/telegram.py` | `clean_text` | input adapter | `text_normalization.py` | Raw Telegram boundary остаётся отдельно от recognition |
| `services/input_recognition.py` | `normalize_text`, `normalize_unit` | input/voice adapter | text, measurements | Voice contract |
| `services/orchestrator.py` | `clean_text`, `normalize_text`, `remove_phrase_overlap` | application orchestration | text, search query owner | Block 5K не менять |
| `services/order_review.py` | `escape` | presentation/use case | presentation owner | Submission UX |
| `services/replies.py` | `UNIT_ALIASES`, `convert_quantity`, `escape`, `format_number`, `normalize_unit` | Telegram presentation | measurements, presentation | HTML/UX contract |
| `services/submission.py` | `escape` | submission presentation | presentation owner | Submission checkpoint UX |
| `presentation/telegram/submission.py` | `escape` | presentation | presentation owner | HTML contract |
| `services/venue_registration.py` | `clean_text`, `escape`, `normalize_text` | venue service | text, presentation | Access/registration contract |
| `tests/catalog/test_catalog_resolver.py` | `remove_phrase_overlap` | direct regression | target search owner | Preserve exact output |
| `tests/catalog/test_matching.py` | `remove_phrase_overlap` | direct regression | target search owner | Preserve exact output |
| `tests/input/test_input_edge_cases.py` | `to_float` | direct regression | numeric primitive | Preserve Sheets number formats |

`application/`, `repositories/`, `api/` и `workers/` прямых импортов из
`services.text` не имеют. Это подтверждает правило outer adapters/application ports
из Block 5K.

## 4. Зависимости lower/core → `services.text`

```text
catalog/{evidence,resolver,safety,scoring} ─┐
conversation/{comments,draft,selection,routing} ─┤
orders/catalog_resolution ───────────────────────┤
parsing/{products,quantities,packaging,comment*,ai,commands} ─┤→ services/text.py
integrations/{google_sheets,openai_client} ──────┤
services/{engine,input*,orchestrator,replies,submission*,venue,...} ─┘
```

Это не цикл в текущем AST-графе: после Block 5K production cycles не найдены.
Но направление всё ещё нарушает роль `services/` как transitional layer: catalog,
conversation, orders и parsing зависят от исторического service-модуля.

После реализованного Group N фактическое направление стало:

```text
catalog / conversation / orders / parsing / integrations / services
    └──→ restaurant_bot.text.normalization
```

При этом `text_normalization.py` не импортирует `services`, `catalog`, `parsing`,
`domain` или внешние адаптеры. В отличие от отклонённого исторического target
`parsing/text.py`, такой owner не
приписывает общую нормализацию только parsing-слою.

## 5. Классификация кластеров

### A. Общая лексическая нормализация

`clean_text` и `normalize_text` образуют неделимую пару: `normalize_text` сначала
вызывает `clean_text`, а callers используют одинаковый контракт очистки перед
сравнением. Их ответственность не channel-specific и не относится только к
разбору команд. Каталог, разговорный routing, Google Sheets и venue service
используют её напрямую.

**Вывод:** единый следующий seam — перенести оба символа вместе в
`src/restaurant_bot/text_normalization.py`. Прямые callers уже переведены; временный
private import в `services/text.py` нужен только оставшимся алгоритмам этого файла.
Не переносить их в `parsing/text.py`: это создало бы ложную
зависимость catalog/integrations от parsing и смешало generic normalization с
command parsing.

### B. Comment/search операции

`remove_global_comment_overlap` — provenance-aware операция комментариев. Функция
из `conversation/comments.py` с тем же именем является тонким wrapper над
`services.text`; это не второй алгоритм, но два публичных пути одного понятия.
`remove_phrase_overlap` — временная search-копия, используемая только
orchestrator и orders/catalog_resolution. Эти функции нельзя переносить одной
группой: первая относится к comment provenance, вторая — к поисковому query.

**Вывод:** `remove_global_comment_overlap` требует отдельного решения владельца
comment policy; `remove_phrase_overlap` — отдельного catalog/search audit.

### C. Единицы и фасовка

`UNIT_ALIASES`, `normalize_unit`, `convert_quantity` связаны измерительным
контрактом, но словарь также содержит `уп`, `кор`, `пач`, `бан`, `бут`, `ведро`.
Это order/catalog packaging vocabulary, а не только parsing. Переносить их вместе
с generic text или presentation нельзя.

**Вывод:** отдельная будущая группа измерений/фасовки, вероятный owner
`domain/measurements.py` или другой явно названный модуль после проверки
serialisation и catalog-unit контрактов.

### D. Числа и диапазоны

`NUMBER_WORDS` и `parse_number_words` образуют лингвистическую пару. `numeric_range_spans`
используется как source-evidence primitive, а `to_float` — как tolerant scalar
parser для Sheets, AI payload и confidence. Их нельзя механически объединить.

В `catalog/safety.py`, `parsing/ai/quantity_reconciliation.py`, `parsing/packaging.py`
и `orders/catalog_resolution.py` есть собственные regex для разных ролей. Они
используют общие aliases, но не являются доказанными дублями: один разбирает
характеристики каталога, другой — quantity provenance, третий — packaging, четвёртый
восстанавливает voice order quantity.

**Вывод:** сначала отдельное сравнение regex/corpus; переносить только после
доказательства одинакового входного/выходного контракта.

### E. Отделы

`DEPARTMENT_ALIASES` и `normalize_department` имеют только два production callers:
Google Sheets catalog mapping и engine draft fallback. Это самостоятельный
department/domain mapping, не parsing. Риск мал, но эффект по dependency direction
меньше, чем у Group N.

### F. Представление

`escape` используется только service/presentation кодом и является Telegram HTML
контрактом. `format_number` используется только `services/replies.py`.
Дополнительно найден `services/order_review.py:format_quantity`: он форматирует
целые и дробные числа сходно, но не принимает `None` и оставляет десятичную точку,
тогда как `format_number` возвращает `-` и использует запятую. Это эквивалентный
кандидат для отдельного presentation audit, но не безопасный автоматический merge.

## 6. Семантические дубли

| Старый owner | Другой owner | Разница | Решение |
|---|---|---|---|
| `services/text.py:remove_global_comment_overlap` | `conversation/comments.py:remove_global_comment_overlap` | Второй только делегирует первому и сохраняет channel-neutral comment API. | `KEEP DISTINCT` до отдельного comment-owner решения; не создавать третий wrapper. |
| `services/text.py:format_number` | `services/order_review.py:format_quantity` | Разные контракты `None`, десятичный разделитель и presentation context. | `AUDIT_REQUIRED`, не объединять в Block 5L. |
| `services/text.py:normalize_text` | `catalog/evidence.py:_canonical_token` | `_canonical_token` расширяет нормализацию транслитерацией и является catalog evidence rule. | `KEEP DISTINCT`. |
| `services/text.py:normalize_text` | `parsing/commands/normalization.py:normalize_command_text` | Command normalizer добавляет punctuation cleanup, lead-in removal и `пожалуйста`. | `KEEP DISTINCT`. |
| `services.text` unit/range dictionaries | локальные regex в parsing/catalog/AI | Общие vocabulary aliases, но разные semantic roles и выходные структуры. | `AUDIT_REQUIRED`, не считать доказанным duplicate. |
| `clean_text` | локальные `.strip()`/`" ".join(...)` | Локальные операции не повторяют `None`/whitespace contract полностью. | `KEEP DISTINCT`, не заменять механически. |

Прямых вторых определений `UNIT_ALIASES`, `DEPARTMENT_ALIASES`, `NUMBER_WORDS`,
`normalize_unit`, `normalize_department`, `numeric_range_spans`, `to_float`,
`parse_number_words` и `convert_quantity` не найдено.

## 7. Миграционные группы

| Группа | Символы | Target owner | Почему вместе | Почему не с другими | Риск | Статус |
|---|---|---|---|---|---|---|
| N | `clean_text`, `normalize_text` | `text_normalization.py` | `normalize_text` напрямую зависит от `clean_text`; callers используют общий lexical contract. | Не включать единицы, числа, comments и presentation. | Средний: большой fan-in, но pure semantics. | `DONE` |
| U | `UNIT_ALIASES`, `normalize_unit`, `convert_quantity` | `domain/measurements.py` | Алиасы и пересчёт составляют measurement/packaging contract. | Не включать NUMBER_WORDS и UI formatting. | Высокий: catalog units, packaging и unit mismatch. | `MOVE_LATER` |
| R1 | `NUMBER_WORDS`, `parse_number_words` | quantity parsing owner | Словесное число и его разбор неделимы. | `numeric_range_spans` и `to_float` имеют другие contracts. | Средний/высокий: voice quantity. | `MOVE_LATER` |
| R2 | `numeric_range_spans` | source-evidence owner | Диапазон — отдельный span contract. | Не объединять с quantity scalar parser. | Высокий: packaging/range provenance. | `AUDIT_REQUIRED` |
| R3 | `to_float` | numeric scalar owner | Tolerant parse используется Sheets и structured AI. | Не объединять с linguistic parsing. | Высокий: Sheets formats и confidence. | `AUDIT_REQUIRED` |
| C | `remove_global_comment_overlap` | `conversation/comments` или `parsing/comment_policy`; `remove_phrase_overlap` отдельно | Оба удаляют overlap, но один comment provenance, другой search copy. | Объединение скроет разные инварианты. | Высокий: comment/product-query contract. | `AUDIT_REQUIRED` |
| D | `DEPARTMENT_ALIASES`, `normalize_department` | `domain/departments.py` | Один самостоятельный mapping. | Не включать generic text и catalog units. | Низкий/средний. | `MOVE_LATER` |
| P | `escape`, `format_number` и отдельный `format_quantity` audit | presentation owner | UI formatting имеет внешний HTML/text contract. | Не включать lower/core primitives. | Средний: UX snapshot. | `escape` и `format_number` DONE в Block 5R; `format_quantity` отдельно |

## 8. Почему нельзя переносить весь `text.py`

Перенос целиком в `parsing/text.py` формально сократил бы один import path, но
создал бы ложного владельца для четырёх разных подсистем:

1. catalog evidence/safety использует unit и numeric primitives, а не parsing API;
2. Google Sheets использует `to_float` и department mapping как integration contract;
3. Telegram presentation использует `escape` и `format_number`;
4. comment overlap и search overlap имеют разные владельцы и разные инварианты.

Такой перенос сохранил бы смешение ответственности и добавил бы dependency
`catalog/integrations → parsing`. Поэтому правильна частичная миграция по группам,
а не механический `MOVE ALL`.

## 9. Конечная судьба `services/text.py`

Фактическая судьба после Block 5M — **частичный transitional owner без facade для
Group N**:

1. `clean_text` и `normalize_text` имеют единственного owner в
   `src/restaurant_bot/text_normalization.py`;
2. внешние imports старого пути удалены, а public re-export из `services.text`
   не предоставляется;
3. `services/text.py` сохраняет только units, numbers, overlap, departments и
   presentation primitives;
4. последующие кластеры не переносятся в рамках Block 5M и требуют отдельного
   аудита callers и контрактов.

Такое частичное разделение сохраняет разные semantic contracts и не смешивает
нормализацию текста с measurement, numeric, search или presentation logic.

## 10. Ровно один следующий code seam

### Реализованный Group N

- **Исходный SHA Block 5L:** `7e8a84c134f72749439d28b7dd187739f70182d8`.
- **Символы:** `clean_text`, `normalize_text`.
- **Новый owner:** `src/restaurant_bot/text_normalization.py`.
- **Почему не `parsing/text.py`:** 10+ non-parsing групп (`catalog`, `conversation`,
  `orders`, `integrations`, `services`) используют эти функции напрямую; generic
  owner в parsing создаст неправильную семантическую зависимость.
- **Callers:** все строки caller matrix с этими двумя символами; production-группы
  перечислены в разделах 2–3. Тесты проверяются не только прямыми imports, но и
  полным pipeline corpus.
- **Facade strategy:** compatibility facade для Group N не создаётся; старые внешние
  imports удалены, а `services/text.py` использует private aliases нового owner только
  для своих оставшихся алгоритмов. Второй алгоритм не создаётся.
- **Out of scope:** `UNIT_ALIASES`, `NUMBER_WORDS`, overlap, ranges, `to_float`,
  departments, `escape`, `format_number`, весь `engine.py`, `orchestrator.py`,
  prompts, state machine, matching, persistence, Docker/CI и Block 5K.
- **Comparison corpus:** полный pytest baseline; catalog matching/resolver;
  parsing products/quantities/packaging и AI reconciliation; voice quantity/
  packaging/range tests; conversation comment/draft/routing; Sheets/catalog
  parsing; venue registration; input normalization; точечные cases `None`, пустая
  строка, повторные пробелы, `ё/е`, кавычки, punctuation, Latin/Cyrillic и `%.,/-`.
- **Риск:** средний из-за fan-in, но алгоритм pure и не имеет side effects.
  Главный риск — забытый import alias или изменение нормализации unicode.
- **Ожидаемое улучшение:** lower/core перестаёт зависеть от `services.text`; один
  semantic-neutral owner, отсутствие нового цикла и уменьшение mixed-owner facade
  на два наиболее общих символа.

Этот seam реализован в Block 5M; следующие кластеры остаются только предметом будущего
аудита и в текущем correction block не начинаются.

## 11. Проверка Block 5K и качество

- `services.orchestrator → workers.tasks` отсутствует.
- `workers.tasks → services.orchestrator` сохранено как внешнее направление.
- AST-граф production-пакета не содержит циклов.
- `src/` и `tests/` в Block 5L не изменялись.
- Baseline на текущей ревизии остаётся `1366 collected / 1366 passed` по последней
  подтверждённой проверке Block 5K; новый запуск ниже не требовался для audit-only
  Python diff.
