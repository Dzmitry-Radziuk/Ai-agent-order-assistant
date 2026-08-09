# DATA INTEGRITY BLOCK C — ORDER QUANTITY / PACKAGING / RANGE PROVENANCE

Статус: **PLAN READY / IMPLEMENTATION PENDING**.

Текущий baseline: **1245 collected, 1214 passed, 31 failed**.

Block C пока не меняет application code, tests или prompts. План ограничен
границей TEXT/VOICE structured parsing. PHOTO, catalog/matching, state machine,
submission, logging и decomposition остаются отдельными областями.

## 1. Current pipeline

Фактический путь для текста и уже расшифрованного голоса:

```text
TEXT / VOICE transcript
  ↓
InputRecognitionService / input normalizer
  ↓
OpenAIService._parse_text_once()
  ├─ deterministic infer_intent()/parse_product_lines() fast path
  └─ OpenAI ParsedInputSchema
       ↓
recover_omitted_explicit_items()
  ├─ deterministic = parse_product_lines(source_text)
  ├─ Block A source/comment reconciliation
  ├─ restore_explicit_order_terms()
  ├─ Block B shadow-item reconciliation
  └─ ParsedCommand.model_validate()
       ↓
ConversationEngine._build_item()
       ↓
_match_item() / CatalogResolver
       ↓
_apply_catalog() и catalog-specific quantity guards
       ↓
CartItem
```

Для Block C первый подозрительный переход находится внутри
`recover_omitted_explicit_items()`: deterministic parser уже возвращает роль
диапазона/фасовки, однако `_restore_reference_ranges_in_queries()` и
`_restore_unordered_measurement_pair()` определены, но не имеют callers. В
результате AI scalar `quantity` может попасть в `ParsedCommand` даже когда
source доказывает только packaging или range.

Отдельно после каталога есть другая граница:
`ConversationEngine._catalog_packaging_measurement()`,
`_sanitize_catalog_facts_before_resolution()` и
`_reconcile_quantity_with_catalog_name()`. Они используют живое название
каталога и потому относятся к Block D, а не к Block C.

## 2. Semantic roles

| Role | Пример | Безопасный смысл |
|---|---|---|
| Explicit order quantity | `курица 5 кг`, `пять штук` | Записывается в `quantity/unit`, если source relationship однозначен. |
| Product fact | `сыр 45%`, `масло 82.5%` | Остаётся в `product_query`; не становится order quantity. |
| Packaging/reference | `упаковка 500 г`, `бутылка 1 л` | Сохраняется в packaging/query fields; не становится количеством заказа без отдельного order evidence. |
| Range/constraint | `от 3 до 5 кг`, `500–700 г` | Сохраняется как range/packaging fact; scalar quantity очищается при отсутствии отдельного заказа. |
| User packaging preference | `желательно в бутылках по 1 л` | Полная формулировка остаётся в `comment`, если это текущий supplier-preference contract. |
| Ambiguous measurement | `5,5 кг, 16,5 кг` без order marker | Не угадывать; сохранить source meaning и запросить уточнение на следующем слое. |

Главный инвариант: `quantity` появляется только из доказанного order-quantity
evidence. Валидность поля AI schema сама по себе таким доказательством не
является.

## 3. Existing semantic model

Текущей модели достаточно для первого implementation pass:

- `quantity`, `unit` — scalar order quantity;
- `quantity_source` — отдельная provenance-метка, уже используемая PHOTO и
  engine guards;
- `packaging_text`, `packaging_role`, `packaging_confidence` — reference/
  preference/range representation;
- `product_query` — идентичность товара и source-supported product facts;
- `comment`, `user_comment_to_supplier`, `comment_source` — supplier request;
- `source_line` — исходная строка/транскрипт для evidence;
- `order_entry_text`, `order_entry_type` — PHOTO order-cell contract.

Новые поля и occurrence model не нужны до тех пор, пока focused tests не
докажут невозможность выразить роль существующими полями. Внутри reconciliation
допускается локальная evidence-пара `(source span, role, confidence)`, но она не
сохраняется в `ExtractedItem`, `ParsedCommand`, `CartItem` или state.

## 4. Quantity and packaging helper inventory

| Helper | File | Input → output | Что меняет | Callers / status |
|---|---|---|---|---|
| `parse_quantity_unit` | `services/product_parser.py` (re-export `services/parser.py`) | short text → `(float, unit)` | ничего | parser edit/quantity hints, `_quantity_outside_packaged_query`; единый unit owner |
| `parse_number_words` | `services/text.py` | number-word tokens → value/end | ничего | product parser, parser, parsing recovery |
| `normalize_unit` | `services/text.py` | unit alias → canonical unit | ничего | общий normalizer, не создавать второй |
| `numeric_range_spans` | `services/text.py` | source → spans | ничего | product parser, parsing and engine guards |
| `_spoken_measurement_pair` | `services/product_parser.py` | one source line → packaging text/role/confidence | только deterministic result | `parse_product_lines`; source role owner |
| `_single_product_packaging_item` | `services/product_parser.py` | quantity marks + line → `ExtractedItem` | packaging/order fields in new result | `parse_product_lines`; keep packaging boundary separate |
| `parse_product_lines` | `services/product_parser.py` | source → deterministic `ExtractedItem[]` | создаёт source reference, quantity, packaging, query | OpenAI recovery, engine and pending quantity |
| `_quantity_outside_packaged_query` | `integrations/openai_parsing.py` | query + source → quantity/unit | ничего | only `restore_explicit_order_terms()` |
| `_quantities_with_units` | `integrations/openai_parsing.py` | source → explicit measurement list | ничего | only `restore_explicit_order_terms()` |
| `_terminal_order_quantity` | `integrations/openai_parsing.py` | line → terminal quantity/unit | ничего | `restore_explicit_order_terms()` |
| `_trailing_quantity_with_unit` | `integrations/openai_parsing.py` | line → final quantity/unit outside range | ничего | `restore_explicit_order_terms()` |
| `restore_explicit_order_terms` | `integrations/openai_parsing.py` | AI items + source → items | mutates quantity/unit/source_line | called from `recover_omitted_explicit_items`; must remain source-first |
| `_packaging_role_from_context` | `integrations/openai_parsing.py` | item/source/range → role/confidence | no direct mutation | currently only called by inactive range helper |
| `_restore_reference_ranges_in_queries` | `integrations/openai_parsing.py` | items + source + deterministic → items | packaging fields, query, bounded range comment | currently no caller; primary Block C candidate |
| `_restore_unordered_measurement_pair` | `integrations/openai_parsing.py` | singleton AI/deterministic pair → item | clears unsafe quantity/unit, restores packaging fields | currently no caller; primary Block C candidate |
| `_build_item` | `services/engine.py` | `ExtractedItem` → `CartItem` | can clear unsafe scalar quantity when source has no order quantity | post-ParsedCommand last guard; review only, no catalog dependency |
| `_catalog_packaging_measurement` | `services/engine.py` | CartItem + catalog candidates → packaging/quantity | clears quantity using catalog evidence | **Block D**, do not change in C |
| `_sanitize_catalog_facts_before_resolution` | `services/engine.py` | CartItem + candidate → sanitized item | clears quantity/comment from catalog facts | **Block D**, do not change in C |
| `_reconcile_quantity_with_catalog_name` | `services/engine.py` | CartItem + catalog title → item | may mutate source query/quantity/unit | **Block D**, catalog title is not user evidence |
| `_normalise_photo_command` | `integrations/openai_client.py` | photo ParsedCommand → filtered command | quantity/source from table/order cell | **PHOTO-specific**, shared policy must not replace it |

## 5. Field ownership matrix

| Field | Source of truth | May Block C mutate? | Rule |
|---|---|---:|---|
| `quantity` | raw/transcribed source + deterministic source relationship | Yes | Set only from explicit order evidence; clear AI-only/range/package scalar. |
| `unit` | same evidence as `quantity` + `normalize_unit` | Yes | Normalize together with quantity; never invent a unit. |
| `product_query` | AI query plus source-supported product facts/ranges | Bounded yes | Restore proven range/fact; never remove user-named product information. |
| `source_line` | raw/transcribed source | No, except existing source recovery contract | Never replace with catalog title or normalized search query. |
| `comment` | Block A provenance and explicit preference | Bounded yes | Preserve full packaging preference; remove only proven range/product residue. |
| `user_comment_to_supplier` | mirror of validated comment | Bounded yes | Keep in sync with `comment`; no generic overwrite from AI. |
| `packaging_text/role/confidence` | deterministic source structure + validated AI role | Yes | `catalog_attribute`, `user_preference`, or `ambiguous` only with evidence. |
| `quantity_source` | PHOTO/order-entry or explicit source contract | Read / conservative set only | Do not overwrite PHOTO provenance with text heuristics. |
| `global_comment` | explicit global scope from source/Block A | No | Packaging text must not become a global comment. |

## 6. Proposed reconciliation order

Existing Block A and Block B remain unchanged. The future C pass should be one
bounded reconciliation step in the existing OpenAI parsing boundary:

```text
AI structured result
  ↓
Block A source/comment provenance
  ↓
Block B shadow item count cleanup
  ↓
deterministic source reference (one parse, no catalog)
  ↓
explicit order evidence reconciliation
  ↓
range detection and scalar-quantity safety
  ↓
packaging/reference role restoration
  ↓
AI/source conflict resolution
  ↓
bounded query/comment restoration
  ↓
final mirroring → ParsedCommand.model_validate()
```

Implementation should reuse the existing helpers instead of adding a second
quantity parser:

1. Keep `restore_explicit_order_terms()` as the source-supported quantity seed.
2. Apply `_restore_unordered_measurement_pair()` for a singleton deterministic
   pair that is explicitly `ambiguous`/`catalog_attribute`; clear scalar AI
   quantity and keep packaging text/role.
3. Apply `_restore_reference_ranges_in_queries()` only when the source has one
   unambiguous range. It appends a catalog/reference range to `product_query`,
   removes only that range residue from comments, and preserves a full
   `user_preference` comment.
4. Resolve conflicts: explicit source quantity wins over AI; when neither side
   proves ownership, clear scalar quantity rather than silently choosing AI.
5. Mirror validated comment fields once and validate `ParsedCommand`.

The exact placement of steps 2–4 relative to the existing Block B call must be
covered by the regression matrix. The key constraint is that Block C mutates
fields only after source evidence exists and never reopens Block B item-count
classification.

## 7. First bad transitions and target failures

### Block C MUST FIX (5 current failures)

1. `test_voice_ambiguous_weight_pair_clears_model_quantity` — deterministic
   pair is `ambiguous`; current unused helper leaves AI `5.5 кг`.
2. `test_voice_from_to_range_clears_model_quantity` — deterministic `от ... до`
   range is `catalog_attribute`; current item has no restored packaging role and
   retains a scalar.
3. `test_catalog_packaging_role_is_preserved_when_order_quantity_is_separate` —
   range/packaging is not restored into query/packaging fields before validation.
4. `test_packaging_preference_stays_in_full_comment` — `user_preference` role is
   absent unless the inactive range helper runs.
5. `test_ambiguous_packaging_role_is_not_moved_between_fields` — role and range
   are lost because the source range helper is not called.

### Conditional / dependency cases

- `test_packaging_and_order_sentence_collapses_ai_shadow_items`;
  `test_packaging_connector_and_order_collapses_ai_shadow_items`: current
  failure is still an item-count/shadow projection failure. Block C must not
  duplicate Block B; fix only after confirming one real item reaches C.
- `test_partial_voice_model_result_restores_the_omitted_conjoined_item`: not a
  quantity-role fix; keep as a separate safe source-occurrence decision.

### OUT OF SCOPE

- PHOTO prompt contract (2 failures): Block G/contract drift; runtime photo
  normalizer remains separate.
- Catalog product/title/comment failures (4) and supplier lock (1): Block D/E;
  catalog data cannot decide user/source quantity role.
- Visible-action failures (3): Block F.
- Seven `tests/docs/test_user_scenarios.py` failures: stale scenario catalog.
- Duplicate wording, comment-handling fixture and recovery-card wording: UX/
  existing contract drift, not Block C.
- Voice beef qualifier/selection: source/product qualifier and catalog boundary,
  not scalar quantity provenance.

## 8. Natural regression matrix

| # | Source | AI result | After A/B | Expected after C | Why |
|---:|---|---|---|---|---|
| 1 | `курица 5 кг` | qty `5 кг` | one real item | qty `5 кг`, unit `кг` | explicit order evidence |
| 2 | `пармезан пять штук` | qty `5`, unit empty | one item | `5 / шт` | spoken quantity + existing unit normalization |
| 3 | `сыр в упаковке 500 г` | qty `500 г` | one item | qty empty; packaging/reference `500 г` | package is not order by itself |
| 4 | `сыр 5 кг, упаковки по 500 г` | qty `5 кг`, packaging lost | one item | qty `5 кг`; packaging `500 г` | two numbers, distinct roles |
| 5 | `сыр 45% 3 кг` | qty `45%` or `3 кг` | one item | product query keeps `45%`; qty `3 кг` | percentage is product fact |
| 6 | `бутылка 1 л, 12 шт` | qty `1 л` | one item | query keeps `1 л`; qty `12 шт` | bottle volume is packaging |
| 7 | `говядина от 3 до 5 кг` | qty `3`/`5 кг` | one item | qty empty; range retained, role catalog/ambiguous | endpoint is not scalar order |
| 8 | `яблоки 70–80` | qty endpoint | one item | qty empty; range in query/packaging | product-size constraint preserved |
| 9 | `грудинка 5,5 кг, 16,5 кг` | qty `5,5 кг` | one item | qty empty; role ambiguous | no order ownership proof |
| 10 | `форель 5 кг. Нужна фасовка по 0,8–1,3 кг` | qty `5 кг`, full comment | one item | qty `5 кг`; full preference comment; role user_preference | explicit supplier preference preserved |
| 11 | `курица 5 кг и сыр упаковка 500 г 3 кг` | shared/shifted quantities | two source occurrences | chicken `5 кг`; cheese `3 кг`; package `500 г` | no cross-item contamination |
| 12 | source `сыр 5 кг`, AI qty `3 кг` | conflicting scalar | one item | source `5 кг`, or conservative empty if span ambiguous | AI is not source truth |

Additional cases must cover `масло 82.5%`, `пять штук`, `пятьсот грамм`, ASR
forms `от трех до пяти`, decimal comma/dot, and bare quantity after a dash.

## 9. TEXT / VOICE / PHOTO blast radius

- **TEXT:** uses the shared `_parse_text_once()` and is the primary C boundary.
- **VOICE:** after transcription it must use exactly the same pass. ASR errors
  before transcription are a separate input-recognition concern; no
  voice-specific quantity policy should be added.
- **PHOTO:** `_normalise_photo_command()` owns department columns, printed order
  cells, handwritten corrections and `quantity_source`. Block C may share only
  a future validation invariant (“order cell/source evidence required”), but
  must not change PHOTO prompt or normalizer in this plan. The two existing photo
  prompt failures remain contract drift, not C MUST FIX.

## 10. Green neighbors to preserve

Future implementation must rerun and preserve:

- Block A focused cases and idempotency;
- Block B 7/7 MUST FIX, shadow idempotency, one-word and two-product safety;
- comment/global scope and product-query/comment dual storage;
- candidate/matching boundary and 13/13 modal-routing suites;
- deterministic explicit quantity and TEXT/VOICE parity;
- PHOTO order-entry filtering without changing its prompt contract.

## 11. Expected implementation footprint

Likely production owner: `src/restaurant_bot/integrations/openai_parsing.py`,
inside `recover_omitted_explicit_items()` and the already existing range/measure
helpers. `src/restaurant_bot/services/product_parser.py` should change only if a
focused test proves its deterministic source role is wrong; do not create a new
quantity package or duplicate `normalize_unit`/`parse_quantity_unit`.

Likely tests:

- `tests/input/test_voice_quantity_recovery.py` for source role and conflicts;
- `tests/input/test_ai_result_integrity.py` for idempotency and field ownership;
- `tests/ai/test_ai_media.py` for `_parse_text_once()` integration and TEXT/VOICE
  parity;
- a PHOTO test only if shared validation is proven necessary, without changing
  PHOTO prompt semantics.

No new model field, prompt change, catalog change, matching change, state-machine
change or decomposition is proposed.

## 12. Out-of-scope boundaries

Block C must not change:

- `ConversationEngine._apply_catalog()`, `_catalog_packaging_measurement()`,
  `_sanitize_catalog_facts_before_resolution()` or
  `_reconcile_quantity_with_catalog_name()` (Block D);
- `CatalogResolver`/matching (Block E);
- visible actions (Block F);
- PHOTO prompt wording/contracts (Block G);
- submission, idempotency, logging, prompts and state compatibility;
- Block B shadow item count logic;
- decomposition or new `quantity_policy`/`packaging_domain` packages.

No large packaging dictionary, standalone `%`/`по`/dash regex classifier, catalog
evidence classifier or AI-only fallback is acceptable.

## 13. Success criteria for implementation

Implementation may be marked DONE only when:

1. Five Block C MUST FIX cases pass without changing their expectations.
2. Explicit order quantity survives for numeric and spoken forms.
3. Packaging/product facts/ranges never become scalar order quantity without
   source evidence.
4. Order + packaging and multiple-number cases preserve both roles.
5. AI/source conflicts are source-first or conservative, never silent AI wins.
6. Existing comments and Block B item-count safety remain green.
7. C(C(item, source), source) is idempotent for explicit, packaging and range
   cases.
8. TEXT and VOICE with the same transcript produce the same semantic result.
9. PHOTO, catalog, matching and state-machine boundaries remain unchanged.
10. Focused tests, full pytest delta, Ruff, mypy, markdown links and
    `git diff --check` pass.

## 14. Next action

This document is the implementation plan only. After explicit approval, perform
one focused code diff in the existing parsing boundary, run the regression
matrix and full delta, update this handoff, commit and push only to
`origin/decompose_bot`.
