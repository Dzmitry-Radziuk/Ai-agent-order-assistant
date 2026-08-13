# BLOCK B — SHADOW ITEM COLLAPSE

## Статус и границы

Это implementation plan only для HEAD `34c2e8853080cdee3bf5fa5c76326132393a0c3e`
ветки `decompose_bot`. На этом этапе application code, tests и prompts не
изменяются.

Текущий baseline: **1240 collected, 1201 passed, 39 failed**. Block A уже
завершён и остаётся неизменяемым основанием для этого блока.

Block B отвечает только за безопасное удаление ложных AI-проекций после
source-evidence reconciliation и до `ParsedCommand`/catalog routing. Он не
решает, какой товар существует в каталоге, и не определяет заказное количество
в противоречивых фасовках.

## 1. Owner и call graph

### Единственная runtime boundary

```text
TEXT
  → OpenAIService._parse_text_once()
  → responses.parse(_TEXT_SYSTEM, ParsedInputSchema)
  → recover_omitted_explicit_items(payload, source_text)
      → Block A reconciliation
      → Block B shadow-item reconciliation (будущая точка)
  → ParsedCommand.model_validate(payload)
  → StateCompatibilityPolicy / ConversationEngine

VOICE
  → transcription
  → OpenAIService._parse_text_once(transcript)
  → тот же recover_omitted_explicit_items()

PHOTO
  → parse_photo()
  → отдельная boundary; Block B не участвует
```

Владелец реализации: `src/restaurant_bot/integrations/openai_parsing.py`.
Единая entry point остаётся `recover_omitted_explicit_items()`. Параллельный
collapse pipeline в `engine.py`, `orchestrator.py` или `openai_client.py` не
создаётся.

## 2. Рабочее определение shadow item

**Shadow item** — это AI item, для которого после Block A доказано, что он не
представляет отдельную заказанную пользователем товарную позицию, а является
проекцией уже принадлежащего другой сущности текста:

- локального комментария;
- общего комментария заявки;
- союза или служебной связки;
- требования обработки товара;
- упаковочного фрагмента без самостоятельной позиции;
- дублирующей записи той же source occurrence;
- query-фрагмента, полностью содержащегося в другом item той же occurrence.

Shadow не определяется только по длине query, отсутствию каталожного совпадения,
одному токену или «похожести» названий. Однословный товар, короткий товар и
товар без каталожного совпадения остаются реальными кандидатами, пока source
evidence не доказывает обратное.

## 3. Инвентаризация существующих helpers

| Helper | Назначение | Вход → выход | Item-count mutation | Текущий caller | Решение Block B |
|---|---|---|---|---|---|
| `_apply_semantic_comment_bindings` | применяет подтверждённые bindings и удаляет их shadow projection | payload/items/bindings/source → items | да, возвращает новый список | `recover_omitted_explicit_items` | оставить первым Block A prerequisite; не дублировать его правила |
| `collapse_comment_shadow_items` | удаляет item, совпадающий с local/global comment | items/global → items | да; может перенести quantity | runtime caller отсутствует | использовать только после source-aware gate; текущий helper без source context недостаточно безопасен |
| `_apply_trailing_root_processing_comment` | превращает псевдотовар требования к срезу корня в комментарий реальных items | items/source → items | да; добавляет comments | runtime caller отсутствует | включать отдельным структурным шагом после проверки source scope |
| `_remove_connector_fragment_items` | удаляет query, равный `и/или/на/...`, если source общий | items → items | да | runtime caller отсутствует | не подключать как blacklist; заменить/обернуть source ownership проверкой |
| `_collapse_redundant_ai_items` | схлопывает одинаковые items одной source line и служебные обломки | items → items | да; merge quantity/comment/packaging | runtime caller отсутствует | кандидат для строгого duplicate pass, но текущая эвристика не доказывает occurrence |
| `_remove_contained_query_fragments` | удаляет query, содержащийся в полном query того же source | items → items | да; переносит quantity при отсутствии у owner | runtime caller отсутствует | последний pass и только при доказанной source occurrence |
| `_restore_dropped_unclassified_terms` | возвращает source-supported product tail в query | items/deterministic/global → None | нет item-count | включён Block A | не менять; это восстановление данных, а не collapse |
| `restore_explicit_order_terms` | восстанавливает явные quantity/unit | items/source → items | нет item-count | включён Block A | не менять; packaging/range решения остаются Block C |

Все пять candidate collapse helpers (`collapse_comment_shadow_items`,
`_apply_trailing_root_processing_comment`, `_remove_connector_fragment_items`,
`_collapse_redundant_ai_items`, `_remove_contained_query_fragments`) сейчас
определены, но не вызываются production boundary. Их нельзя просто вызвать
последовательно без единой policy ownership.

## 4. Точный порядок будущей обработки

После завершённого Block A и до `ParsedCommand.model_validate` предлагается
следующий порядок:

```text
1. source occurrence reference
   (локальная карта source spans/детерминированных строк; не сохраняется)
2. trailing processing projection
   (только явный root-processing pattern с несколькими real items)
3. local/global comment shadow projection
   (только подтверждённый binding или явный global scope)
4. connector-only projection
   (только структурный connector fragment, не blacklist)
5. exact duplicate projection
   (одна source occurrence, одинаковый product/query/quantity contract)
6. contained query fragment projection
   (тот же source occurrence, строгая containment и безопасное ownership)
7. final item list
```

### Почему такой порядок

1. **Source occurrence reference** строится первым, потому что все последующие
   удаления должны отличать один AI shadow от двух независимо заказанных строк.
2. **Trailing processing** сначала переводит известную структурную инструкцию в
   комментарий реальных товаров. Иначе последующие passes могут принять её за
   самостоятельный product fragment и потерять требование поставщику.
3. **Comment/global shadow** удаляет projection уже после Block A, когда comment
   scope нормализован. Перенос quantity разрешён только при доказанном owner.
4. **Connector pass** работает по структуре source и соседним owner items, а не
   по глобальному словарю русских слов.
5. **Exact duplicate pass** сначала объединяет одинаковые projections, чтобы
   сохранить non-empty comment/unit/packaging metadata у owner.
6. **Contained fragment pass** выполняется последним: после удаления shadows
   проще доказать, что fragment действительно является частью того же item, а
   не отдельной позицией.

Каждый pass должен быть чистым относительно внешнего состояния: менять только
локальный список payload items и возвращать его в reconciliation boundary.

## 5. Инварианты удаления и merge

При удалении shadow item:

- нельзя суммировать quantity;
- нельзя переносить quantity от shadow к соседнему item без однозначного owner;
- unit переносится только вместе с доказанным quantity той же occurrence;
- comment переносится только к item, к которому он уже привязан source/binding;
- `product_query` real item не заменяется каталогом или AI shadow query;
- `source_line` owner сохраняется; shadow line не становится новой source line;
- порядок real items сохраняется;
- `comment_source` и `user_comment_to_supplier` зеркалятся только после merge;
- промежуточная occurrence map не попадает в `ExtractedItem`, `ParsedCommand` или
  session state.

Обязательная формула идемпотентности:

```text
B(B(items, source), source) == B(items, source)
```

Повторный запуск не удаляет следующий real item, не удваивает comment и не
переписывает quantity.

## 6. Защита реальных товаров

Будущий gate должен требовать одновременно:

1. source line/span подтверждает принадлежность фрагмента исходной фразе;
2. fragment не имеет самостоятельной quantity/unit occurrence, отличной от
   owner;
3. owner найден однозначно по source position/semantic binding;
4. удаление не уменьшает число независимых deterministic product occurrences;
5. для duplicate есть одинаковая quantity/unit либо отсутствующее значение
   только у одного projection;
6. для contained query совпадают source occurrence и совместимы количества.

Запрещены как критерии удаления:

- failure каталожного поиска;
- отсутствие кандидатов;
- token count или длина query;
- ручной blacklist характеристик/русских connector words;
- совпадение только по одному слову;
- cart duplicate logic из `ConversationEngine`.

## 7. Target failures текущего baseline

### MUST FIX в Block B — 7 тестов

1. `tests/input/test_voice_input_contract.py::test_voice_comment_shadow_is_not_created_as_a_separate_product`
2. `tests/input/test_voice_input_contract.py::test_real_audio_cross_item_shadow_is_removed`
3. `tests/input/test_voice_input_contract.py::test_global_comment_is_not_duplicated_as_a_product`
4. `tests/input/test_voice_input_contract.py::test_packaging_connector_is_not_created_as_a_product`
5. `tests/input/test_voice_input_contract.py::test_root_cut_requirement_is_applied_to_the_products_instead_of_becoming_one`
6. `tests/input/test_voice_quantity_recovery.py::test_ai_duplicate_items_from_one_voice_line_are_collapsed`
7. `tests/input/test_voice_quantity_recovery.py::test_ai_packaging_alternative_drops_connector_fragment`

### LIKELY FIX / отдельное решение

- `test_partial_voice_model_result_restores_the_omitted_conjoined_item` — это
  не только удаление shadow, а восстановление отсутствующего real item. В Block B
  допускается лишь отдельная source-occurrence pairing, если deterministic
  parser доказывает ровно один omitted item. Запрещён permissive append каждого
  deterministic item поверх непустого AI результата. При отсутствии доказуемой
  пары случай остаётся отдельным recovery follow-up.
- `test_packaging_and_order_sentence_collapses_ai_shadow_items` и
  `test_packaging_connector_and_order_collapses_ai_shadow_items` — shadow count
  может быть Block B, но выбор 90 г/5 кг и reference packaging относится к
  Block C. В Block B можно убрать доказанный duplicate, не решая quantity role.

### OUT OF SCOPE

- PHOTO prompt/row quantity — 2 текущих failure;
- catalog facts, quantity/comment sanitization и catalog title semantics — 4;
- visible-action timeout/free-form matching — 3;
- ambiguous weight/range/packaging role/preference — Block C;
- qualifier/catalog auto-selection — Block D/matching boundary;
- docs scenario catalog drift — 7;
- stale UX/recovery-card wording, duplicate warning wording и supplier lock —
  отдельные follow-ups;
- ConversationEngine/state/modal routing, submission, MAX, logging и
  decomposition.

## 8. Natural examples

| Source | AI items после Block A | Ожидаемо после Block B | Почему |
|---|---|---|---|
| `курица 5 кг, привезти холодным` | `курица`; `холодным` | `курица` с comment | comment shadow, есть owner/binding |
| `курица 5 кг, сыр 2 кг, всё привезти завтра` | `курица`; `сыр`; `завтра` | `курица`; `сыр`; global comment | global scope подтверждён source |
| `сироп роза 3 шт и говядина 5 кг` | `сироп роза`; `и`; `говядина` | два real items | `и` не имеет самостоятельной occurrence |
| `укроп 2 кг, петрушка 3 кг, срез корня 5 см` | два товара + `срез корня` | два товара с общим processing comment | trailing root projection |
| `сыр пармезан 1 кг` | `сыр пармезан`; `пармезан` | один item | contained fragment той же source occurrence |
| `сироп роза 5 шт` | два одинаковых `сироп роза` | один item quantity 5 | exact duplicate, одинаковая occurrence |
| `сироп роза 5 шт и сироп роза 2 шт` | две строки | два items | разные quantity/source occurrences — не shadow |
| `пармезан` | один короткий item | один item | длина не доказывает shadow |
| `чай зелёный 1 кг, лук зелёный 2 кг` | два похожих item | два items | похожее слово не ownership |
| `сироп роза 10 шт и говядина 5 кг` при AI только `сироп роза` | один AI item | restore только при точном deterministic pairing; иначе без append | omitted item — отдельное доказательство |

## 9. Negative matrix

| Сценарий | Нельзя делать |
|---|---|
| однословный `укроп` | удалять item как короткий |
| товар без каталожного кандидата | удалять как shadow |
| два одинаковых товара в разных source spans | схлопывать по одному query |
| `сыр` и `сыр` с разными quantity | суммировать или выбирать первый |
| `чай зелёный` и `лук зелёный` | удалять по общему `зелёный` |
| comment совпадает со словами реального товара | удалять реальный item без scope |
| range `0,8–1,3 кг` | решать quantity/packaging в Block B |
| voice ASR опечатка | применять отдельный voice-only collapse |

## 10. TEXT / VOICE и PHOTO blast radius

TEXT и VOICE должны проходить один и тот же Block B после transcript. Новые
voice-specific правила запрещены: различия ASR должны оставаться входными
данными source evidence и покрываться теми же тестами.

PHOTO исключён полностью: `parse_photo`, `_PHOTO_SYSTEM` и photo tests не
меняются.

## 11. Ожидаемый footprint реализации

Основной application file: `src/restaurant_bot/integrations/openai_parsing.py`.
`openai_client.py`, `engine.py`, `orchestrator.py`, catalog и matching не должны
меняться без доказательства необходимости.

Focused tests для будущей реализации:

- `tests/input/test_voice_input_contract.py` — comment/global/connector/root;
- `tests/input/test_voice_quantity_recovery.py` — duplicate/alternative и
  отдельно conjoined recovery;
- `tests/input/test_ai_result_integrity.py` — source evidence и idempotency;
- `tests/ai/test_ai_media.py` — `_parse_text_once` integration и TEXT/VOICE
  parity.

## 12. Green neighbors и success criteria

До и после реализации должны оставаться зелёными:

- Block A MUST FIX suite;
- Block A idempotency tests;
- `_parse_text_once` integration test;
- one-word products и multiple-product parsing;
- comments и spoken quantity;
- candidate/matching boundary;
- state-machine focused tests.

Block B считается завершённым только если:

1. семь MUST FIX shadow tests проходят;
2. source evidence и comment persistence не регрессировали;
3. intentional duplicate и two-real-products negative matrix зелёные;
4. B(B(items, source), source) == B(items, source);
5. full suite не получил новых failures относительно 1240/1201/39;
6. partial conjoined item либо безопасно восстановлен доказуемым pairing, либо
   явно оставлен отдельным follow-up без permissive append;
7. PHOTO, catalog, matching, state machine и Block C boundary не изменены.

## 13. Out-of-scope blocks

- **Block C:** order quantity vs packaging/reference/range;
- **Block D:** catalog-owned facts и перезапись user-owned fields;
- **Block F:** visible actions;
- **Block G:** observability/other follow-ups;
- submission idempotency, MAX, logging и decomposition.
