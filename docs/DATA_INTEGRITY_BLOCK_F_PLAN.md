# DATA INTEGRITY BLOCK F — VISIBLE ACTION / SCREEN ACTION ROUTING

Статус: **PLAN READY / IMPLEMENTATION PENDING**
Проверено на HEAD `3907a48b8ec94e29bc47e51f55340ca5fe987c04`, branch
`decompose_bot`. Block E закрыт без functional fix. В рамках анализа application
code, tests и prompts не менялись.

## 1. Scope and invariants

Block F отвечает только за преобразование TEXT/VOICE в действие текущего экрана:

`transcript/text → visible-action resolution → ParsedCommand → state machine`.

Видимая кнопка — контекст UI, а не глобальный словарь команд. Действие можно
выбрать только если его `action_id` присутствует в актуальном
`ConversationState.visible_actions` и выбранное действие однозначно подтверждено.

Реальный товарный ввод имеет приоритет над общей похожестью с кнопкой. При этом
parser не является абсолютным источником истины: action-like псевдотовар вроде
`Править поставщику.` не должен мутировать draft, если semantic screen resolver
недоступен.

Callbacks остаются отдельным явным путём. Block F касается только natural-language
активации видимых действий текстом или голосом и не меняет callback contract.

**Block F MUST FIX предварительно = 3** — ровно три текущих visible-action
failures, перечисленные ниже. Их expectations не переписываются до реализации.

## 2. Current routing call graph

Фактический путь из `UpdateOrchestrator`:

```text
TEXT
  → UpdateOrchestrator._parse
    → _parse_text_in_context
      → OpenAIService.parse_text
      → sheet-review / pending-comment contextual guards
      → InputRecognitionService.match_visible_action (только UNKNOWN или ADD_ITEMS без items)
      → _needs_visible_action_ai
      → OpenAIService.choose_visible_action
      → infer_intent("", selected_action_id)

VOICE
  → UpdateOrchestrator._parse
    → InputRecognitionService.recognize_media
      → transcribe(primary)
      → requires_high_accuracy_transcription
      → optional transcribe(high_accuracy=True)
      → select_transcription_result
      → _parse_text_in_context(transcript, state)
      → тот же visible-action path, что и TEXT

CALLBACK
  → UpdateOrchestrator._parse
    → infer_intent("", callback_data)
    → parser.parse_callback
    → callback_revision / callback_target
    → существующий stale-revision guard
```

UI persistence после ответа:

```text
BotReply.rows
  → _attach_ui_revision(reply, state.ui_revision)
  → _store_visible_actions(state, reply)
  → ConversationState.visible_actions / ui_message_text / ui_revision
```

Текстовая или голосовая симуляция кнопки должна использовать только этот
последний сохранённый список; каталог для выбора кнопки не загружается.

## 3. Helper and owner inventory

| Helper / owner | File | Input | Output | Deterministic | Mutates state | Current risk |
|---|---|---|---|---:|---:|---|
| `_parse_text_in_context` | `services/orchestrator.py` | text, state | `ParsedCommand` | partly | no | generic parse выполняется до exact screen matching |
| `_match_visible_action` | `services/orchestrator.py` | text, state | action id или `""` | yes | no | thin wrapper; реальный owner ниже |
| `InputRecognitionService.match_visible_action` | `services/input_recognition.py` | text, `visible_actions` | current action id | yes | no | filler removal + exact/subset score; bounded, not full semantic NLP |
| `_needs_visible_action_ai` | `services/orchestrator.py` | text, parsed, state | bool | yes | no | concrete `ADD_ITEMS` with pseudo-item блокирует semantic fallback |
| `_requires_high_accuracy_transcription` | `services/input_recognition.py` | transcript, state | bool | yes | no | только решает retry, не выбирает action |
| `_select_transcription_result` | `services/input_recognition.py` | primary/retry text | transcript | yes | no | должен сохранять primary при неудачном retry |
| `recognize_media` / `_recognize_voice` | `services/input_recognition.py` | Telegram voice | parsed command | orchestration | no | единый callback `parse_text` сохраняет TEXT/VOICE parity |
| `OpenAIService.choose_visible_action` | `integrations/openai_client.py` | text, screen text, allowed actions | allowed `action_id` или `""` | no, constrained | no | allowlist и confidence >= 0.9 уже проверяются здесь |
| `infer_intent("", callback_data)` | `services/parser.py` | callback data | `ParsedCommand` | yes | no | единственный callback→intent owner; revision извлекается parser'ом |
| `_attach_ui_revision` | `services/orchestrator.py` | reply, revision | callback suffixes | yes | reply only | добавляет `:rN` к кнопкам |
| `_store_visible_actions` | `services/orchestrator.py` | state, reply | state fields | yes | state | source of truth для следующего text/voice input |

`OpenAIService.choose_visible_action()` уже строит allowlist из переданных
`action_id`, отправляет только эти действия и отбрасывает ответ с неизвестным
ID или confidence ниже 0.9. Поэтому предварительный риск находится в ordering и
trigger policy orchestrator, а не в генерации arbitrary callback.

## 4. Exact, deterministic free-form and semantic matching

Нужно сохранить три уровня с разными confidence и precedence:

1. **Exact/normalized UI action.** Например, `К черновику.` сопоставляется с
   label `К черновику`; punctuation и filler-префиксы нормализуются.
2. **Deterministic free-form action.** Например, `Я хочу выбрать количество`
   сопоставляется с `Выбрать количество`. Текущий matcher удаляет ограниченный
   набор conversational fillers и допускает subset token score от 0.72.
3. **Semantic AI fallback.** Например, `Нет, оставим всё как было` выбирает
   `Оставить 10 кг`, только если это одна из текущих кнопок и AI вернул её ID с
   достаточной уверенностью.

Нельзя превращать это в exact-string-only mapping или bag-of-words autoclick.
Глобальное правило `оставить → KEEP_CURRENT_QUANTITY` запрещено: без видимой
кнопки та же фраза не должна синтетически создавать callback.

## 5. Three first bad transitions

### 5.1 Semantic resolver timeout

Тест: `test_visible_action_timeout_returns_unknown_instead_of_product`.

Фактический переход:

```text
"Править поставщику."
  → parse_text возвращает ADD_ITEMS(items=[product_query="Править поставщику."])
  → _needs_visible_action_ai == False из-за parsed.items
  → choose_visible_action не вызывается
  → возвращается ADD_ITEMS
```

Ожидание: action-like ambiguity + unavailable semantic resolver дают
`UNKNOWN`, `items=[]`, без draft mutation. Если semantic resolver действительно
вызывается и timeout происходит внутри него, тот же safety result сохраняется.

### 5.2 Deterministic free-form action

Тест: `test_free_form_visible_button_phrase_uses_exact_screen_action`.

Фактический переход:

```text
"Я хочу выбрать количество"
  → OpenAIService.parse_text вызывается первым
  → тестовый MagicMock не возвращает ParsedCommand
  → exact/deterministic matcher не получает право вернуть v2:mulone:r4
```

Ожидание: bounded deterministic visible-action match до generic AI parsing,
`infer_intent("", "v2:mulone:r4")`, `Intent.FIX_MULTIPLE`,
`callback_revision=4`, `parse_text` не вызывается.

### 5.3 Semantic visible action

Тест: `test_semantic_voice_action_can_only_choose_a_visible_button`.

Фактический переход:

```text
"Нет, оставим всё как было"
  → parse_text возвращает ADD_ITEMS с action-like псевдотоваром
  → _needs_visible_action_ai == False из-за parsed.items
  → возвращается ADD_ITEMS вместо v2:keep_current:r8
```

Ожидание: narrow action-like trigger разрешает semantic fallback; selected ID
проходит allowlist текущих `visible_actions`, затем превращается в
`Intent.KEEP_CURRENT_QUANTITY` с revision 8.

`InputRecognitionService.match_visible_action()` сам по себе не является первым
неверным узлом этих трёх тестов: он уже покрывает green shortened-label case.

## 6. Timeout and failure semantics

| Состояние входа | Result |
|---|---|
| exact action found | synthetic callback command из текущего action ID |
| deterministic free-form action found | synthetic callback command |
| concrete product + quantity | сохранить `ADD_ITEMS`, не вызывать button AI |
| action-like/suspicious parsed result + AI selected visible ID | synthetic callback command |
| action-like/suspicious parsed result + AI timeout | `UNKNOWN`, `items=[]`, no mutation |
| AI returns non-visible action ID | reject; для action-like input — `UNKNOWN`, для сильного независимого parsed intent — сохранить его |
| no action evidence | обычный parsed command |
| high-accuracy transcription timeout | сохранить primary transcript и пропустить его через единый parser |

Нельзя смешивать timeout транскрипции и timeout semantic action resolver: первый
сохраняет текст, второй при action-like ambiguity запрещает fallback в товар.

## 7. Product and navigation protection

Уже green contract `test_real_product_with_quantity_is_not_replaced_by_visible_add_action`
защищает реальный товар: `ParsedCommand.ADD_ITEMS` с concrete item, quantity и
comment не проходит в visible-action fallback.

Будущая narrow policy должна сохранить эту защиту и аналогично не подменять
сильные `SHOW_CART`, `BACK`, `CANCEL`, `SEARCH_ALL_SUPPLIERS`, `ADD_MORE` и другие
явные navigation intents. Она может рассматривать только `UNKNOWN` или
подозрительный `ADD_ITEMS` без достаточного product evidence.

Evidence не строится большим blacklist фраз. Нужна комбинация уже имеющихся
структурированных полей `ParsedCommand` (concrete items, quantity/unit,
explicit-add markers, confidence) и текущего UI context. Любое новое правило
должно иметь отрицательный тест на реальный товар.

## 8. UI revision and visible-action validation

`ConversationState` хранит `ui_revision`, `ui_message_text` и
`visible_actions: list[dict[str, str]]`. `_attach_ui_revision()` добавляет suffix
`:rN` к callback data; `_store_visible_actions()` сохраняет именно эти labels и
IDs. `parser.parse_callback()` извлекает revision, а engine/orchestrator уже
имеют stale-revision guard для явных callbacks.

Text/voice synthetic callback должен брать action ID только из актуального
`state.visible_actions`; старый revision нельзя синтезировать из пользовательской
фразы или AI output. AI output с неизвестным ID отбрасывается до `infer_intent`.

`ui_message_text` служит semantic context для OpenAI, но не должен стать
глобальной бизнес-логикой и не должен добавлять actions, которых нет в
`visible_actions`.

## 9. Text / voice parity

После транскрипции голос вызывает тот же `_parse_text_in_context(transcript,
state)`, что и TEXT. Voice-specific слой ограничен quality retry:

```text
VOICE → primary transcript → optional high-accuracy retry
       → select transcript → shared text/action routing
```

Одинаковый transcript и одинаковый `visible_actions` должны давать одинаковый
ParsedCommand. High-accuracy retry не выбирает кнопку; его timeout сохраняет
primary transcript. Callback clicks остаются неизменными.

## 10. Regression matrix

| Scenario | Expected |
|---|---|
| exact label `К черновику.` | current `v2:back` action |
| label + punctuation | same action, no retry |
| `Я хочу выбрать количество` + visible button | deterministic `FIX_MULTIPLE`, no `parse_text` |
| shortened `У всех поставщиков.` | current search-all action |
| semantic `Нет, оставим всё как было` | only one matching visible keep action |
| semantic resolver timeout | `UNKNOWN`, no product/item mutation |
| AI returns nonexistent action ID | reject, no synthetic callback |
| visible Add + `Сироп роза 1 шт` | keep concrete `ADD_ITEMS` |
| visible Add + unrelated product phrase | product parser wins only with real evidence; otherwise safe unknown |
| same action phrase when button absent | no global action; normal parser/UNKNOWN |
| stale revision-like action text | no old callback synthesis |
| two similar `Оставить 5 кг` / `Оставить 10 кг` + `оставить` | ambiguous/UNKNOWN, never first button |
| already parsed navigation `покажи черновик` | preserve navigation intent; no AI override |
| ordinary `ну ладно посмотрим` | no action, normal UNKNOWN/safe flow |
| primary transcript + high-accuracy timeout | primary transcript parsed normally |
| sheet-review arbitrary text | no visible-action AI, existing review safety preserved |

## 11. Out-of-scope boundaries

- Block A source evidence, Block B shadow items, Block C quantity/packaging and
  Block D catalog provenance remain unchanged.
- Block E matcher/candidate boundary remains closed (`MUST FIX = 0`). Catalog is
  never loaded to resolve a screen action.
- `engine.py`, `matching.py`, `catalog_resolver.py`, `openai_parsing.py`,
  `product_parser.py` и prompts не менять без доказанного first-bad-transition.
- Modal `StateCompatibilityPolicy` 13/13, callbacks, decomposition, PHOTO и
  unrelated 18 baseline failures остаются вне Block F.

## 12. Expected implementation footprint

Предполагаемый основной файл — `src/restaurant_bot/services/orchestrator.py`:
изменение порядка `_parse_text_in_context` и narrow trigger для semantic
fallback. `src/restaurant_bot/services/input_recognition.py` понадобится только
если deterministic matcher должен вернуть более явный результат/уверенность;
не создавать вторую policy в orchestrator.

`OpenAIService` менять только если inspection докажет нарушение allowlist/schema;
текущий client уже ограничивает ID и confidence. `engine.py`, `matching.py` и
catalog resolver не являются ожидаемыми owners.

Focused tests: `tests/input/test_voice_processing_card.py` и связанные
`tests/input/test_input_routing.py`; обязательно сохранить green neighbors,
voice retry и concrete-product protection.

## 13. Success criteria

До implementation approval необходимо:

1. exact/deterministic/semantic precedence зафиксирована одним routing owner;
2. action-like + semantic timeout даёт `UNKNOWN` без mutation;
3. concrete product/quantity и explicit navigation не подменяются кнопкой;
4. AI не может выбрать action вне текущего `visible_actions`;
5. revision сохраняется в synthetic callback;
6. TEXT/VOICE используют один routing path;
7. callbacks работают без изменений;
8. Block A–E contracts и baseline `1254 / 1233 / 21` сохранены;
9. focused regressions и полный suite проверены после отдельной реализации.

Следующее действие: отдельно утвердить этот план. До утверждения Block F
implementation не начинать.
