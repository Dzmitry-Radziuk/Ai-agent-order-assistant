# BLOCK A — SOURCE EVIDENCE / PROVENANCE RECONCILIATION
## Статус и границы

Это implementation plan only для HEAD 8392aeadfa8e650b827d6213bb6d955bbfe758b7
ветки decompose_bot. В этом этапе application code, tests и prompts не меняются.

Block A исправляет только границу между structured AI result и нормализованным
ExtractedItem/ParsedCommand. Он не меняет catalog, matching, state machine,
visible actions, photo parser, submission, idempotency, MAX, logging или
decomposition.

## 1. Точный owner и call graph

### TEXT, semantic AI branch

    UpdateOrchestrator._parse
      → UpdateOrchestrator._parse_text_in_context
      → OpenAIService.parse_text
      → OpenAIService._parse_text_once
      → deterministic infer_intent(text)
      → deterministic fast paths, если они безопасны
      → OpenAI responses.parse(_TEXT_SYSTEM, ParsedInputSchema)
      → recover_omitted_explicit_items(parsed.model_dump(), text)
      → ParsedCommand.model_validate(payload)
      → StateCompatibilityPolicy / ConversationEngine

Точная runtime-точка Block A — вызов recover_omitted_explicit_items в
src/restaurant_bot/integrations/openai_client.py. Владелец reconciliation —
src/restaurant_bot/integrations/openai_parsing.py, где определена функция.

### Deterministic fast path

    raw text
      → parser.infer_intent / parse_product_lines
      → ParsedCommand

Ветки navigation, support message, simple product list, short product,
packaged product и другие безопасные deterministic branches не вызывают Block A.
Они уже получают source-derived ExtractedItem. Их контракты остаются green и не
переписываются AI recovery.

### VOICE

    audio
      → InputRecognitionService._recognize_voice
      → transcription (primary/high-accuracy retry)
      → _parse_text_in_context(transcript, state)
      → OpenAIService.parse_text(transcript)
      → тот же recover_omitted_explicit_items

Новая voice-specific reconciliation не нужна: при одинаковом transcript TEXT и
VOICE должны проходить одинаковую границу.

### PHOTO

    photo
      → InputRecognitionService._recognize_photo
      → OpenAIService.parse_photo(_PHOTO_SYSTEM)
      → _normalise_photo_command
      → engine/catalog

PHOTO не входит в Block A. Его quantity/row/printed-packaging contract остаётся
отдельным блоком и не исправляется text reconciliation.

## 2. Что сейчас вызывается и что нет

| Helper | Файл | Вход/выход | Мутации | Runtime сейчас | Решение Block A |
|---|---|---|---|---|---|
| recover_omitted_explicit_items | openai_parsing.py | payload + source → payload | items, comments, global_comment | вызывается из openai_client | единая entry point |
| _clear_unknown_item_placeholders | openai_parsing.py | items → None | department/supplier_hint/source_department только для unknown values | вызывается | оставить первым |
| _apply_semantic_comment_bindings | openai_parsing.py | payload/items/bindings/source → items | comment, user_comment, comment_source, shadow bindings | вызывается | оставить, затем усилить source gate |
| restore_explicit_order_terms | openai_parsing.py | items + source → items | source_line, quantity, unit | только tests/import | включить для явно произнесённых quantity/unit |
| _discard_unverified_item_comments | openai_parsing.py | items/bindings/source/global → None | comment, user_comment, comment_source, product_query | не вызывается | включить после source restoration, исправив semantic gate |
| _restore_dropped_unclassified_terms | openai_parsing.py | items + deterministic reference + global → None | product_query, source_line | не вызывается | включить ограниченно для source-supported product terms |
| remove_unsupported_query_qualifiers | openai_parsing.py | items + source → items | product_query | только definition/tests отсутствуют runtime | не включать без отдельного qualifier contract |
| _collapse_redundant_ai_items | openai_parsing.py | items → new list | quantity/unit/comment merge, item count | не вызывается | Block B, shadow items |
| _remove_contained_query_fragments | openai_parsing.py | items → new list | quantity merge, item count | не вызывается | Block B |
| _remove_connector_fragment_items | openai_parsing.py | items → new list | item count | не вызывается | Block B |
| collapse_comment_shadow_items | openai_parsing.py | items + global → new list | quantity merge, item count | не вызывается | Block B |
| _apply_trailing_root_processing_comment | openai_parsing.py | items + source → new list | comments, comment_source, item count | не вызывается | Block B unless proof shows inseparable binding |
| _restore_reference_ranges_in_queries | openai_parsing.py | items + source + deterministic → None | packaging fields, product_query, comment | не вызывается | Block C |
| _restore_unordered_measurement_pair | openai_parsing.py | items + deterministic → None | product_query, quantity/unit, comment, packaging | не вызывается | Block C |

Важная граница: существующие shadow/collapse helpers не следует вызывать
«все подряд». Их item-count mutations требуют отдельного Block B с отдельной
матрицей false-positive защиты.

## 3. Current first bad transition

recover_omitted_explicit_items сейчас:

1. принимает AI payload;
2. удаляет comment_bindings из payload;
3. очищает только unknown placeholders;
4. применяет semantic bindings;
5. нормализует global comment;
6. делает deterministic parse только если items пуст;
7. для непустого items зеркалит comment/user_comment и возвращает payload.

Поэтому непустой AI result считается полным, даже если он потерял явный source
term или придумал comment. Это первый Block A defect. ParsedCommand,
ConversationEngine и catalog получают уже повреждённые данные.

## 4. Proposed single entry point and exact order

Существующий recover_omitted_explicit_items остаётся единственной entry point.
Новая параллельная функция или reconciliation в engine не нужна.

Предлагаемый порядок:

    raw ParsedInputSchema.model_dump()
      ↓
    STEP 0: copy source text and comment_bindings; clear unknown placeholders
      ↓
    STEP 1: build deterministic source reference only
            (parse_product_lines для evidence, не для безусловного append)
      ↓
    STEP 2: apply semantic comment bindings and explicit global scope
      ↓
    STEP 3: restore explicit order quantity/unit from source
            (spoken words and unambiguous terminal quantity)
      ↓
    STEP 4: validate every comment against source context and bindings
            remove AI-only comment; keep source-confirmed duplicate facts
      ↓
    STEP 5: restore source-supported unclassified product terms
            without catalog title and without connector invention
      ↓
    STEP 6: mirror comment/user_comment and write final payload
      ↓
    ParsedCommand.model_validate

### Why this order

- Placeholder cleanup must precede evidence decisions.
- Bindings determine intended scope before comments are validated.
- Quantity restoration must happen before comment validation because quantity
  spans are removed from product/comment evidence.
- Unsupported comments must be removed before dropped product terms are restored;
  otherwise a hallucinated comment incorrectly blocks query recovery.
- Shadow-item collapse and packaging/range policy are deliberately not mixed into
  Block A.

### Mutation contract

STEP 0 may mutate only raw payload placeholders and local binding list.
STEP 1 creates a local deterministic reference and never replaces AI items.
STEP 2 may mutate comment, user_comment_to_supplier, comment_source, global_comment
and remove a binding-shadow item only when its binding is source-confirmed.
STEP 3 may mutate source_line, quantity and unit only from explicit source spans.
STEP 4 may remove unsupported comments and set comment_source=none; it must not
remove a source-confirmed product fact from product_query.
STEP 5 may append a term proven by source evidence to product_query/source_line;
it must not append a comment, catalog title or connector.
No step may write a temporary remove_phrase_overlap/search query back to these
fields.

## 5. Comment provenance contract

Current CommentSource values are NONE, SEMANTIC, EXPLICIT_MARKER and CATALOG.
There is no separate AI_INFERRED or UNKNOWN value. ExtractedItem's
infer_semantic_comment_source marks any nonempty comment as SEMANTIC when source
payload omitted comment_source. Therefore provenance must be checked before
ParsedCommand.model_validate, not inferred from the enum after validation.

The planned change to _discard_unverified_item_comments is:

- a semantic binding is retained only when its text is present in source context
  and its binding scope/index is valid;
- explicit_marker is retained only when the marker/instruction is present in source;
- global_comment is retained only with explicit global scope in source;
- duplicate product facts remain valid in both product_query and comment when both
  are source-supported;
- AI-only “охлаждённая” for source “курица 5 кг” is removed;
- “охлаждённая” for source “курица охлаждённая 5 кг” is retained.

No new model field is required. Raw source text, comment_bindings, current
CommentSource and existing structural scopes are sufficient for this block.

## 6. Field ownership matrix

| Field | Owner | Allowed source | Block A may change? | Rule |
|---|---|---|---|---|
| product_query | parser/reconciliation | raw text, deterministic source reference | yes, restore missing source term only | never replace with catalog title |
| source_query | CartItem/engine | ExtractedItem.product_query | no direct field in Block A | search cleanup is temporary |
| source_line | parser/reconciliation | raw source/transcript | yes | preserve original line, do not invent |
| quantity | parser/reconciliation | explicit order quantity | yes | never from packaging/reference guess |
| unit | parser/reconciliation | explicit source unit/number word | yes | normalize only after source evidence |
| comment | parser/reconciliation | source text + valid binding | yes | remove unsupported AI-only comment |
| user_comment_to_supplier | parser/reconciliation | same as comment | yes | keep mirrored value |
| comment_source | parser/reconciliation | binding/source semantics | yes | NONE/SEMANTIC/EXPLICIT_MARKER only |
| global_comment | parser/reconciliation | explicit order-scope source text | yes | never make a product |
| supplier_hint | parser/catalog boundary | explicit supplier wording | no, except existing placeholder cleanup | catalog supplier is not source evidence |
| packaging_text/role/confidence | parser packaging contract | explicit range/preference | no | Block C owns ambiguous packaging |
| selection_query | command routing | user selection text | no | outside data reconciliation |

Catalog fields, catalog_comment and CartItem.source_query mutation are Block D.

## 7. Omitted explicit items and partial AI results

Current ADR-010 intentionally forbids appending a deterministic item when AI
returned a non-empty list. Block A must not revert to full deterministic reparse.

For a future bounded source-recovery implementation, an omitted item may be
appended only when all conditions hold:

1. deterministic source reference contains an independently parsed positive
   quantity/product line;
2. the product query is not represented by any AI query/source line;
3. the fragment is not a comment, global scope, connector or packaging-only
   remainder;
4. source order/span can be associated unambiguously with the missing item;
5. the operation is idempotent.

If these conditions cannot be proven with current parser outputs, the omitted-item
test remains LIKELY FIX/Block B rather than being solved by a permissive append.

## 8. Idempotency requirements

R(R(payload, source), source) must equal R(payload, source) for the supported
Block A fields.

- placeholder cleanup is idempotent;
- comment binding application must run once from a preserved binding list;
- quantity restoration assigns the same source-derived value on repeat;
- comment validation assigns a deterministic kept list and source;
- dropped-term restoration checks normalized query membership before appending.

The implementation must add direct idempotency assertions for one valid comment,
one unsupported comment and one spoken quantity. No helper may add a second item,
comment fragment or quantity on repeat.

## 9. Regression matrix

### MUST FIX in Block A

| Test | Expected result | Reason |
|---|---|---|
| tests/ai/test_ai_media.py::test_ai_invented_supplier_comment_is_not_preserved | unsupported comment removed | source-evidence gate |
| test_ai_global_comment_scope_filler_is_not_saved_as_local_comment | filler not local; explicit global retained | scope/provenance |
| test_spoken_word_quantity_survives_ai_normalization (4 cases) | quantity and unit шт restored | explicit source quantity |
| test_conversational_product_leadin_is_removed_by_semantic_parser | lead-in not comment; product meaning retained | source term recovery |
| test_original_packaged_product_line_is_preserved_for_catalog_check | source_line retained | source ownership |
| test_text_ai_unknown_placeholders_do_not_lock_supplier_search | unknown placeholders cleared/normalized | source-backed field cleanup |
| tests/input/test_voice_input_contract.py::test_local_comments_are_recovered_when_ai_leaves_them_only_in_source_lines | source-confirmed local comment restored | common TEXT/VOICE boundary |

### LIKELY FIX, but confirm scope during implementation

| Test | Why not promise |
|---|---|
| test_numeric_range_with_supplier_qualifier_uses_semantic_ai | range/packaging semantics overlap Block C |
| test_voice_unbound_product_fact_is_restored_to_query | source term recovery is Block A, but comment/product split needs fixture review |
| test_partial_voice_model_result_restores_the_omitted_conjoined_item | safe item append may require Block B span/shadow contract |
| test_voice_beef_cannot_gain_an_unspoken_qualifier_or_auto_select | qualifier is Block A; auto-select/catalog part is Block D/E |
| test_voice_recovery_supports_quantity_before_product | deterministic empty-result recovery already works; keep as neighbor |

### OUT OF SCOPE

- All connector/global/root/duplicate shadow-item tests: Block B.
- Range-only, ambiguous weight pair and packaging role tests: Block C.
- Catalog title quantity/comment/supplier facts: Block D.
- Candidate evidence and matching thresholds: Block E.
- Visible-action tests: Block F.
- Photo prompt contract tests and UX wording tests: Block G/contracts.
- Seven stale SUB-08 scenario references.
- State-machine, modal routing, submission and idempotency tests.

## 10. Natural regression examples

| # | Source | AI structured result | Current risk | Expected | Block A |
|---:|---|---|---|---|---|
| 1 | курица 5 кг | product=курица, comment=охлаждённая | invented comment survives | comment empty | YES |
| 2 | курица охлаждённая 5 кг | product=курица, comment=охлаждённая | source fact may be lost | comment retained; query may retain fact | YES |
| 3 | сыр 2 кг и ветчина 3 кг | items=[сыр] | partial result | do not blindly append; bounded source recovery decision | LIKELY |
| 4 | картофель пять штук | quantity=1, unit="" | spoken quantity lost | 5, шт | YES |
| 5 | свиная шея 5 кг без костей | query keeps fact, comment keeps fact | duplicate may be treated as error | fact in both fields | YES |
| 6 | всё привезти утром | global_comment=всё это дело утром | filler leaks to item | global_comment=утром only | YES |
| 7 | тесто 550 г на 20 шт, заказать 5 шт | two AI items | packaging connector shadow | one item | NO, Block B/C |
| 8 | форель 0,8–1,2 кг | quantity=0,8 кг | range becomes order quantity | quantity unresolved | NO, Block C |
| 9 | сироп роза, или говядина 5 кг | item query=или | connector becomes item | no connector item | NO, Block B |
| 10 | one source line duplicated twice by AI | two identical items | duplicate item persists | one item | NO, Block B |

## 11. Neighboring green contracts

The implementation must keep green:

- one-word deterministic products and product lists;
- fuzzy/morphological product matching inputs;
- explicit marker comments;
- explicit global comments;
- intentional product-fact duplication in query and comment;
- multiple real products with independent source lines;
- negative/navigation commands and empty AI result fallback;
- quantity without product in modal context;
- TEXT/VOICE parity for identical transcript;
- StateCompatibilityPolicy and all modal routing suites;
- catalog resolver/matching tests (without changing their thresholds).

Add at least one integration regression through OpenAIService._parse_text_once
with a mocked structured response, plus focused direct recovery tests. Do not use
giant conversation E2E as the only proof of a parsing micro-contract.

## 12. Expected implementation footprint

Likely files:

- src/restaurant_bot/integrations/openai_parsing.py — recovery entry point and
  source-evidence comment gate;
- src/restaurant_bot/integrations/openai_client.py — only if a small call-site
  adapter is required; preferred outcome is no logic change here;
- tests/input/test_ai_result_integrity.py — direct provenance/idempotency tests;
- tests/input/test_voice_input_contract.py — shared TEXT/VOICE boundary tests;
- tests/input/test_voice_quantity_recovery.py — spoken quantity neighbors;
- tests/ai/test_ai_media.py — mocked OpenAI integration contract.

Explicitly not changed: openai_prompts.py, parser/product_parser algorithms,
engine.py, catalog_resolver.py, matching.py, input_recognition.py,
state_compatibility.py, photo normalization/prompt, submission, logging and
decomposition.

No new parser regex blacklist and no new model fields are required by the plan.
No prompt change and no catalog change are allowed.

## 13. Observability for later implementation

Do not change logging in Block A. Later privacy-safe structured diagnostics should
capture only:

- AI item before reconciliation;
- source evidence hash/length or opt-in content;
- fields restored/removed;
- reason code (unsupported_comment, explicit_quantity, source_term, scope);
- item count before/after.

Never log secrets or raw user content when LOG_USER_CONTENT=false.

## 14. Acceptance criteria

Block A implementation is accepted only when:

1. all MUST FIX tests are green;
2. neighboring AI/voice recovery tests remain green;
3. source-confirmed comments and duplicated product facts are preserved;
4. AI-only comments are removed;
5. explicit spoken quantity/unit is restored without packaging guesses;
6. no new TEXT/VOICE divergence exists;
7. state-machine, matching and catalog targeted tests have no new failures;
8. prompts and catalog application are untouched;
9. full pytest has fewer failures, with every remaining failure classified;
10. idempotency and git diff checks pass.

## 15. Out-of-scope blocks

Block B: shadow/connector/duplicate item collapse.
Block C: packaging/range/measurement semantics.
Block D: catalog fact and application boundary.
Block E: candidate generation/evidence/matching.
Block F: visible-action/global intent precedence.
Block G: photo/UX contract updates.

После утверждения этого плана следующий шаг — отдельная реализация только Block A.
