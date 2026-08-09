# Full Regression / Behavior Audit

Статус: analysis-only аудит завершён, исправления ожидают отдельного этапа.

## 1. Scope and baseline

- Checkout: `decompose_bot`
- HEAD: `68f7e99c04ef73f2c1d56edc6910cd77e9a6c1fa`
- Origin: `https://github.com/Dzmitry-Radziuk/test_bot.git`
- Рабочее дерево перед аудитом и после тестов (до создания audit docs) было чистым;
  текущие изменения ограничены двумя audit-документами.
- `.env` не читался; `git ls-files .env` возвращает пустой результат.
- Реальные Telegram, Google Sheets, supplier POST и заявки не выполнялись.
  Внешние вызовы в тестах заменены mock/fake-объектами.

Команда полного прогона:

```text
.\.venv\Scripts\python.exe -m pytest -q --tb=no
```

Результат: **1237 collected, 1185 passed, 52 failed, 0 skipped, 0 xfailed,
0 errors, 15.31 s**.

Тестовый baseline не равен доказательству production-поведения. Поэтому ниже
отдельно отмечены покрытые маршруты, пробелы наблюдаемости и риски внешних
эффектов.

## 2. Failure classification

Текущий HEAD не добавил подтверждённых новых падений. В handoff до текущего
этапа уже был зафиксирован тот же набор из 45 dirty-checkout failures. Ещё семь
падений вызваны устаревшей ссылкой сценария `SUB-08`, появившейся после переименования
теста в `e96de7c` (`test_review_voice_maps_arbitrary_confirmation_through_visible_action_ai`
→ `test_sheet_review_does_not_map_arbitrary_text_to_visible_action`).

### 2.1 Confirmed new regressions

**0.** В пределах текущего 13-block refactor подтверждённых новых regression
не найдено. Все focused policy suites для 13 блоков проходят; failures ниже
относятся к ранее изменённым AI/parser/voice/catalog/UX областям или к stale
scenario catalog.

### 2.2 OLD BASELINE FAILURE — 45 tests

Для каждого перечисленного теста ожидаемый контракт — значение из assertion;
наблюдаемое значение — текущее значение из pytest; первый неверный слой общий
для строки группы. Источник baseline — предыдущие записи `PROJECT_HANDOFF.md`
(45 dirty-checkout failures до текущего блока).

| Test | Expected / observed | First bad path | Classification / severity |
|---|---|---|---|
| `tests/ai/test_ai_media.py::test_ai_invented_supplier_comment_is_not_preserved` | comment `""` / `"охлаждённым"` | AI result normalization → comment provenance recovery | OLD BASELINE FAILURE / HIGH |
| `test_ai_global_comment_scope_filler_is_not_saved_as_local_comment` | second comment empty / `"всё это дело на завтра"` | AI global/local comment postprocessing | OLD BASELINE FAILURE / HIGH |
| `test_spoken_word_quantity_survives_ai_normalization` (4 parametrized cases) | unit `шт` / empty unit | AI structured result → quantity normalization | OLD BASELINE FAILURE / HIGH |
| `test_conversational_product_leadin_is_removed_by_semantic_parser` | `product_query="свежая кукуруза"` / `"кукуруза"`, comment `"свежая"` | semantic parser comment/product split | OLD BASELINE FAILURE / HIGH |
| `test_original_packaged_product_line_is_preserved_for_catalog_check` | source line equals input / source line empty | AI item normalization | OLD BASELINE FAILURE / HIGH |
| `test_numeric_range_with_supplier_qualifier_uses_semantic_ai` | qualifier remains in product query / moved to comment | AI product/comment split for range | OLD BASELINE FAILURE / HIGH |
| `test_text_ai_unknown_placeholders_do_not_lock_supplier_search` | normalized unit `шт`, cleared placeholders / raw `штук`, placeholders survive | AI result cleanup | OLD BASELINE FAILURE / MEDIUM |
| `tests/ai/test_photo_prompt_contract.py::test_photo_prompt_excludes_packaging_and_stock_from_order_quantity` | prompt contains required prohibition / current prompt contract assertion fails | photo prompt contract snapshot | OLD BASELINE FAILURE / MEDIUM |
| `test_photo_prompt_forbids_moving_quantity_between_neighboring_rows` | prompt contains row-isolation rule / current prompt contract assertion fails | photo prompt contract snapshot | OLD BASELINE FAILURE / MEDIUM |
| `tests/catalog/test_product_matching.py::test_catalog_product_facts_are_not_saved_as_supplier_comments` | catalog fact not in supplier comment / `comment="белое"` | catalog application → comment cleanup | OLD BASELINE FAILURE / HIGH |
| `test_catalog_title_facts_are_not_quantity_or_comment` | no order quantity/comment from title / quantity `10` | catalog title normalization | OLD BASELINE FAILURE / HIGH |
| `test_quantity_only_comment_residue_is_removed` | comment empty / quantity phrase remains in comment | catalog comment residue cleanup | OLD BASELINE FAILURE / HIGH |
| `test_exact_catalog_title_number_requires_explicit_order_quantity` | quantity `None` / quantity `1` | catalog title number interpretation | OLD BASELINE FAILURE / HIGH |
| `tests/conversation/test_add_more_prompt.py::test_product_sent_from_add_more_prompt_opens_duplicate_in_collecting_stage` | text contains `Товар уже в черновике` / `Товар уже есть в черновике` | reply wording | OLD BASELINE FAILURE / LOW (stale UX expectation) |
| `tests/conversation/test_comment_handling.py::test_late_global_comment_applies_to_existing_and_new_items_without_overlap` | `catalog_comment="тест"` / empty | late global comment fixture/application | OLD BASELINE FAILURE / MEDIUM |
| `tests/conversation/test_engine.py::test_manual_action_without_an_open_item_uses_source_recovery_card` | emoji-prefixed button / current emoji-free button | recovery-card presenter | OLD BASELINE FAILURE / LOW (stale UX expectation) |
| `tests/input/test_voice_input_contract.py::test_empty_voice_add_items_uses_the_source_recovery_card` | old heading `Не удалось распознать голосовое сообщение` / current `К сожалению, мне не удалось...` | voice recovery presenter | OLD BASELINE FAILURE / LOW (stale UX expectation) |
| `test_unknown_empty_voice_uses_the_same_source_recovery_card` | same old heading / current wording | voice recovery presenter | OLD BASELINE FAILURE / LOW (stale UX expectation) |
| `test_voice_comment_shadow_is_not_created_as_a_separate_product` | one item / two items, shadow `холодным` | voice AI recovery/postprocessing | OLD BASELINE FAILURE / HIGH |
| `test_real_audio_cross_item_shadow_is_removed` | no cross-item shadow / shadow remains | voice recovery/postprocessing | OLD BASELINE FAILURE / HIGH |
| `test_global_comment_is_not_duplicated_as_a_product` | global text not product / extra product created | voice comment recovery | OLD BASELINE FAILURE / HIGH |
| `test_local_comments_are_recovered_when_ai_leaves_them_only_in_source_lines` | local comments restored / comments absent or misplaced | voice source-line recovery | OLD BASELINE FAILURE / HIGH |
| `test_packaging_connector_is_not_created_as_a_product` | connector not an item / connector item remains | voice packaging recovery | OLD BASELINE FAILURE / HIGH |
| `test_root_cut_requirement_is_applied_to_the_products_instead_of_becoming_one` | requirement bound to products / extra item | voice comment binding | OLD BASELINE FAILURE / HIGH |
| `test_voice_beef_cannot_gain_an_unspoken_qualifier_or_auto_select` | no invented qualifier/selection / qualifier or auto-selection appears | voice AI/catalog handoff | OLD BASELINE FAILURE / HIGH |
| `tests/input/test_voice_processing_card.py::test_visible_action_timeout_returns_unknown_instead_of_product` | UNKNOWN / ADD_ITEMS | visible-action timeout fallback | OLD BASELINE FAILURE / HIGH |
| `test_free_form_visible_button_phrase_uses_exact_screen_action` | exact visible action / assertion mismatch | visible-action matching | OLD BASELINE FAILURE / MEDIUM |
| `test_semantic_voice_action_can_only_choose_a_visible_button` | only visible action / assertion mismatch | visible-action matching | OLD BASELINE FAILURE / HIGH |
| `tests/input/test_voice_quantity_recovery.py::test_packaging_and_order_sentence_collapses_ai_shadow_items` | one product item / shadow item remains | voice quantity recovery | OLD BASELINE FAILURE / HIGH |
| `test_packaging_connector_and_order_collapses_ai_shadow_items` | one product item / shadow item remains | voice quantity recovery | OLD BASELINE FAILURE / HIGH |
| `test_voice_ambiguous_weight_pair_clears_model_quantity` | quantity cleared / model quantity retained | voice range recovery | OLD BASELINE FAILURE / HIGH |
| `test_voice_from_to_range_clears_model_quantity` | quantity cleared / model quantity retained | voice range recovery | OLD BASELINE FAILURE / HIGH |
| `test_voice_unbound_product_fact_is_restored_to_query` | fact in product query / fact lost or moved | voice product-fact recovery | OLD BASELINE FAILURE / HIGH |
| `test_voice_range_is_restored_to_product_query_not_comment` | range in product query / range in comment | voice packaging recovery | OLD BASELINE FAILURE / HIGH |
| `test_catalog_packaging_role_is_preserved_when_order_quantity_is_separate` | catalog packaging role preserved / role mismatch | voice packaging recovery | OLD BASELINE FAILURE / HIGH |
| `test_packaging_preference_stays_in_full_comment` | full preference comment / comment split mismatch | voice packaging preference recovery | OLD BASELINE FAILURE / HIGH |
| `test_ambiguous_packaging_role_is_not_moved_between_fields` | ambiguous role retained / moved between fields | voice packaging recovery | OLD BASELINE FAILURE / HIGH |
| `test_ai_duplicate_items_from_one_voice_line_are_collapsed` | duplicate shadows collapsed / duplicates remain | voice duplicate recovery | OLD BASELINE FAILURE / HIGH |
| `test_ai_packaging_alternative_drops_connector_fragment` | connector fragment dropped / fragment item remains | voice packaging recovery | OLD BASELINE FAILURE / MEDIUM |
| `test_partial_voice_model_result_restores_the_omitted_conjoined_item` | omitted item restored / item absent | voice omission recovery | OLD BASELINE FAILURE / HIGH |
| `tests/quantity/test_duplicate_quantity.py::test_repeated_product_with_quantity_requires_explicit_merge` | legacy duplicate wording / current `Товар уже есть...` | duplicate presenter | OLD BASELINE FAILURE / LOW |
| `tests/supplier/test_supplier_lock.py::test_short_catalog_supplier_matches_selected_full_supplier_name` | empty comment / catalog fact `Травяной откорм` | catalog supplier lock normalization | OLD BASELINE FAILURE / MEDIUM |

### 2.3 STALE TEST — 7 tests

Все семь `tests/docs/test_user_scenarios.py` assertions падают одинаково на
`SUB-08`: `scripts/generate_user_scenarios.py::validate_catalog()` не находит
старое имя теста. Исполняемый код и текущий тест используют новое имя после
`e96de7c`; это stale scenario catalog, не runtime regression.

Классификация для каждого:

- `test_catalog_references_only_existing_pytest_tests` — expected valid catalog / stale reference;
- `test_every_critical_scenario_has_an_automated_check` — expected valid catalog / same stale reference;
- `test_generated_scenario_views_are_current` — expected generated views / validation stops on stale reference;
- `test_markdown_contains_mermaid_and_all_scenario_ids` — expected generated catalog / validation stops early;
- `test_catalog_explains_permissions_confirmation_and_recovery` — expected valid catalog / same stale reference;
- `test_every_flow_is_linked_to_tested_scenarios` — expected valid links / same stale reference;
- `test_html_catalog_is_autonomous_and_filterable` — expected valid generated HTML / same stale reference.

Classification: **STALE TEST**, severity LOW, owner `docs/scenarios`; not caused
by HEAD `68f7e99`.

### 2.4 Counts

| Category | Count |
|---|---:|
| Confirmed REAL BUG introduced by current 13-block refactor | 0 |
| OLD BASELINE FAILURE | 45 |
| STALE TEST | 7 |
| ENVIRONMENT | 0 |
| EXPECTATION DRIFT (separate from stale catalog) | 0 |
| UNKNOWN / NEEDS INVESTIGATION among pytest failures | 0 |

Неисполненные black-box сценарии ниже являются coverage gaps, а не
подтверждёнными failures.

## 3. State-machine regression matrix

Focused suites проверены отдельно; callback остаётся отдельным UI-путём.

| Modal block | CONTINUE | INTERRUPT | AMBIGUOUS | REJECT | Evidence |
|---|---|---|---|---|---|
| MISSING_QTY / AWAIT_UNIT_QUANTITY | PASS | PASS | PASS | PASS | `test_quantity_state_preemption.py` 6/6 |
| AWAIT_COMMENT_SCOPE | PASS | PASS | PASS | n/a | `test_comment_scope_preemption.py` 19/19 |
| AMBIGUOUS / candidate | PASS | PASS | PASS | PASS | `test_ambiguous_candidate_preemption.py` 10/10 |
| NOT_FOUND | PASS | PASS | PASS | PASS | `test_not_found_preemption.py` 20/20 |
| DUPLICATE_PENDING | PASS | PASS | PASS | PASS | `test_duplicate_pending_preemption.py` 9/9 |
| UNIT_MISMATCH | PASS | PASS | PASS | PASS | `test_unit_mismatch_preemption.py` 17/17 |
| AWAIT_MANUAL_DETAILS | PASS | PASS | PASS | n/a | `test_not_found_preemption.py` + product routing |
| AWAIT_PRODUCT_ADD_DETAILS | PASS | PASS | PASS | PASS | `test_product_add_details_routing.py` 8/8 |
| AWAIT_ADD_MORE_CONFIRM | PASS | PASS | PASS | PASS | 10/11; one stale duplicate wording |
| AWAIT_SUBMIT_CONFIRM | PASS | PASS | PASS | PASS | `test_submit_confirm_routing.py` 33/33 |
| SUBMISSION_FAILED | PASS | PASS | PASS | PASS | `test_submission_failed_routing.py` 31/31 |
| REVIEW / SHEET_REVIEW | PASS | PASS | PASS | PASS | `test_sheet_review_routing.py` 39/39 |
| NEW_ORDER_CONFIRMATION | PASS | PASS | PASS | PASS | `test_new_order_confirmation_routing.py` 25/25 |

The common route is visible in `ConversationEngine.handle()`:

```text
global parse / media recognition
→ StateCompatibilityPolicy via evaluate_modal_routing()
→ stale callback guard
→ modal CONTINUE / INTERRUPT / AMBIGUOUS / REJECT
→ handler or ordinary routing
```

The policy is still a large mixed-responsibility module (848 lines) and imports
`PendingQuantityHandler`; no import cycle was observed, but this is architectural
debt for a future behavior-preserving decomposition, not a bug fixed here.

## 4. Channel matrix

### TEXT

Core text routes and all focused modal preemption suites pass. The three
conversation failures are wording/catalog-comment baseline mismatches. Verdict:
**functional modal routing PASS; full regression NOT READY because shared
AI/catalog baseline remains red**.

### VOICE

Deterministic voice route matrix: `147/147` pass; voice controls `19/19` and
voice routing contract `6/6` pass. However, 24 voice input/recovery/processing
tests fail in the old baseline, primarily product/comment shadows, packaging
ranges and visible-action fallback. Verdict: **NOT READY for full voice UX**.

Actual route is:

```text
Telegram voice → download → transcription (optional high-accuracy retry)
→ parse_text(transcript, state) → global policy → engine → reply
```

### PHOTO

Photo download/worker routing `2/2` and new-order photo interrupt tests pass.
Two photo prompt contract tests are in the old baseline. There is no complete
black-box matrix for every modal state with photo input; this is a coverage gap,
not evidence of an automatic YES/NO/candidate mutation.

Actual route is `InputRecognitionService._recognize_photo()` →
`OpenAIService.parse_photo()` → normalized `ParsedCommand` → engine policy.
Review intents from a photo are conservatively rewritten to `UNKNOWN` in the
orchestrator before review dispatch.

### CALLBACK

Fresh/stale callback contract, UI revision and submit/review guards pass in the
targeted suites. `ConversationEngine.handle()` rejects a mismatched revision
before modal transition or cleanup (`engine.py:192-205`); sheet review additionally
checks token and revision (`orchestrator.py:585-594`). Legacy callbacks without a
revision suffix are still accepted for compatibility; this is a known residual
risk, not a newly observed mutation.

## 5. Draft editing and submission invariants

### Arbitrary draft editing

The individual edit/remove/comment paths and final-review interrupt tests pass;
the current suite does not contain one complete three-item black-box journey
covering every edit phrase after BACK and reopening review. This remains a
coverage gap. `_find_cart_item()` returns `None` on equal top scores, so the
known duplicate-name tie is safe in the tested case; a broad data-driven tie
matrix is still needed. No confirmed data corruption was reproduced.

### Submission snapshot

`_prepare_submission()` creates `PendingSubmission` with `rows`, `order_no`,
`trace_id`, venue/spreadsheet identity and a frozen payload (`engine.py:3092-3175`).
Submission guard tests pass, including retry reuse and no retry after uncertain
dispatch. Before this boundary the cart remains editable; after `SUBMITTING`,
new-order and destructive actions are locked.

### SUBMISSION_FAILED and dispatch uncertainty

Retryable failure: same snapshot/order number is reused. `dispatch_uncertain`:
no repeat button/POST and no cart mutation. Missing pending snapshot has a safe
fallback. Focused and submission suites pass.

### External idempotency risk (HIGH-RISK FOLLOW-UP)

`SubmissionService.submit()` calls `sheets.increment_catalog_quantities()` and
only then checkpoints `catalog_updated` (`submission.py:131-144`). A timeout or
crash after the external increment but before `_checkpoint()` can leave the
checkpoint false; a retry can increment catalog quantities twice. This routing
refactor does not solve the issue. It is a **HIGH-RISK PRODUCTION FOLLOW-UP**
(potentially a production blocker for deployments where the increment is not
idempotent), not a change made in this audit.

The dispatch path itself is guarded by:

```text
dispatch_started checkpoint → HTTP POST → dispatch_completed checkpoint
```

and tests confirm a redelivery after `dispatch_started` does not issue a second
POST. No new repeat-POST path was found.

## 6. Realistic user journeys

These are test/fake journeys; no external side effect was executed.

| Journey | Current evidence | Verdict |
|---|---|---|
| A. text order → review → edit → submit | engine/review/submission tests; no single end-to-end three-item test | PARTIAL / coverage gap |
| B. voice missing quantity → voice quantity → submit | quantity and voice route tests pass; voice recovery baseline red | PARTIAL / NOT READY |
| C. photo → corrections → submit | photo route and submit interrupt tests; no complete vision journey | PARTIAL / coverage gap |
| D. candidate/not-found → independent product → resume | candidate/not-found preemption suites pass | PASS |
| E. new-order confirmation → NO → old draft | new-order routing suite pass | PASS |
| F. new-order confirmation → independent ADD | new-order routing suite pass | PASS |
| G. retryable submission failure → same snapshot retry | submission guards/submission suite pass | PASS |
| H. dispatch uncertain → safe lock | submission guards/submission suite pass | PASS |
| I. sheet review → independent command | sheet-review routing suite pass | PASS |
| J. order status → return to draft actions | status renderer/navigation tests pass; no complete voice/photo matrix | PARTIAL / coverage gap |

## 7. Comments, candidates and persisted query meaning

Focused comment-scope and candidate suites pass, including independent intent
preemption. The intended persisted invariant remains:

```text
product_query/source_query + comment are persisted meanings
temporary search normalization must not overwrite either field
```

The 45 baseline failures show that older AI/catalog/voice recovery contracts
still have comment/product-fact leakage. They are not caused by the current
new-order policy and must be fixed in a separate parser/postprocessing/catalog
task before declaring the full product/comment contract stable.

## 8. Audit and trace events

`UpdateOrchestrator` currently emits `conversation_state_loaded`,
`command_parsed`, `engine_transition_completed`, `reply_prepared`,
`telegram_reply_sent`, `background_tasks_checkpointed` and
`user_request_outcome`. Order events include `order_started`, `user_action`,
`order_cancelled` and `submission_requested`; submission service records stage
events and dispatch checkpoints.

### Logging gap matrix

| Data | Currently logged | Correlation | Missing / sensitive |
|---|---|---|---|
| raw text | command log `text` | update/trace in surrounding request | content is sensitive; sanitized by structlog config |
| voice transcript | OpenAI/recognizer completion event | request/trace where available | full transcript is not consistently present in one event |
| photo recognized content | item count and normalized command | request | recognized lines are not logged as a dedicated event |
| callback data | parsed target/revision and reply callbacks | update/UI revision | raw callback source and button label are separate |
| button label | not in reply log | UI revision only | label/action mapping missing |
| ParsedCommand | partial fields/items | update | no explicit policy decision field |
| state before/after | state loaded/transition completed | update | no exact state diff; cart is summarized |
| cart before/after | counts + summarized items | update/trace | no field-level diff |
| compatibility decision | not emitted as a dedicated event | implicit in engine result | handler/policy reason missing |
| handler/action | intent/outcome analytics | trace | concrete handler name missing |
| reply | prepared/sent + text length/buttons | update | button labels missing |
| visible actions | stored in state; callback IDs logged | UI revision | labels and semantic mapping missing |
| background task | task names/checkpoint | update | task execution trace is separate |
| Google write | submission stage logs/events | order/trace | per-row mutation correlation incomplete |
| submission stage | structured logs + order events | order_no/trace_id | failure checkpoint detail varies by stage |
| retry/error | failure logs/events | order/update | transport and business failure categories are not uniform |
| trace/order/update IDs | trace/order events, update DB/log context | partial | no single joined journey record |

Current logging cannot reconstruct a complete user journey from one
`trace_id/order_no/update_id` alone. It can reconstruct the broad stages, but
not the exact policy decision, handler, state diff, visible labels and every
external operation.

Security note: `logging.py` hashes content and identifiers by default and
redacts known token patterns. `comment_text` is not currently listed in
`_CONTENT_KEYS`, so this field requires a future sanitization review. No
secrets were read or emitted during this audit.

## 9. Product-add ownership

`ConversationState.product_add_requests` is a separate history list containing
`request_id`, `source_item_id`, `original_query`, description, update key,
user metadata, status and timestamps. `_fresh_order_state()` deep-copies this
list (`engine.py:1650-1666`), so `_start_new_order()` preserves it. Existing
tests confirm round-trip and retry identity. The remaining architectural
question is whether requests should be owned by order/trace rather than chat
history; no behavior is changed here.

## 10. Test-suite quality inventory

- 1237 collected tests; current slowest calls are Google Sheets mapping
  (~4.81 s), Telegram timeout retry (~1.28 s), docstring check (~0.42 s).
- Tests exercise many private methods (`ConversationEngine._match_item`,
  `_build_item`, `_prepare_submission`, orchestrator parsing helpers), so some
  coverage is coupled to implementation details.
- Modal policy is tested both directly and through engine; this is useful
  defense-in-depth but creates duplicate coverage that should be mapped before
  future consolidation.
- Scenario catalog is stale at `SUB-08`; no test was deleted in this audit.
- Largest production responsibilities remain mixed in `engine.py` (3246 lines),
  `orchestrator.py` (1843), `parser.py` (1687), `openai_prompts.py` (2378),
  `openai_parsing.py` (1455), `submission.py` (1010), `replies.py` (1017),
  `venue_registration.py` (808), `openai_client.py` (927) and
  `state_compatibility.py` (848). No decomposition was started.

## 11. Preliminary owner/dependency map

```text
domain/models.py
  → parser / input recognition / matching / state compatibility
  → handlers and engine
  → orchestrator (persistence/checkpoints/transport coordination)
  → repositories and workers
```

| Area | Current owner | Responsibility | Boundary note |
|---|---|---|---|
| input recognition | `input_normalizer.py`, `input_recognition.py` | Telegram normalization, voice/photo download and recognition | Telegram event types still cross into core |
| parsing | `parser.py`, `openai_parsing.py`, `openai_client.py` | deterministic/AI structured command and recovery | AI postprocessing is broad and currently red in baseline |
| compatibility/routing | `state_compatibility.py`, `modal_routing.py` | modal decision before handlers | policy still knows quantity handler; no cycle observed |
| handlers | `conversation_handlers/*`, portions of `engine.py` | modal-specific actions | engine still owns many implementations |
| catalog | `matching.py`, `catalog_resolver.py` | evidence, candidates, AI match handoff | catalog application still invoked by engine/orchestrator |
| review | `order_review.py`, orchestrator review branch | sheet snapshot/token/fingerprint | separate from cart review but coordinated in orchestrator |
| submission | `submission.py`, `submission_presenter.py`, workers | frozen snapshot, Sheets stages, dispatch/retry | external idempotency gap remains |
| venue | `venue_registration.py`, repositories | access and venue binding | large registration module |
| orchestration | `orchestrator.py` | update claim, parse, catalog, engine, checkpoint, reply/tasks | still includes business review logic |

No new circular import or dependency violation was observed. The future safe
decomposition boundary remains adapter → normalized event → conversation core →
normalized reply; MAX is not integrated and must not be started as part of this
audit.

## 12. Architecture and MAX readiness

The 13-block path does not introduce a generic `suspended_interaction`, raw NL
parsing in `StateCompatibilityPolicy`, or a second independent policy in the
engine. The main residual anti-pattern is responsibility concentration: engine
and orchestrator still mix routing, state mutation, presenters, persistence
checkpoints and Telegram reply metadata.

MAX readiness: **not ready**. `TelegramEvent`, `BotReply`, callback data,
message editing and `ui_revision` cross the conversation core directly. A future
adapter boundary is required before another transport is added.

## 13. Recommended fix order

1. **P0/P1 data integrity:** fix the confirmed old AI/catalog/voice
   product/comment/quantity leakage; preserve the product-query/comment
   provenance contract.
2. **P1 submission safety:** make catalog quantity increment/checkpoint
   idempotent across timeout/crash; retain dispatch-uncertain lock.
3. **P1 state hijack:** add black-box three-item draft-edit journey and expand
   photo-in-modal coverage before changing behavior.
4. **P2 stale tests/docs:** update the `SUB-08` scenario reference only after
   confirming the renamed test is the intended source of truth.
5. **P2 UX/cosmetic:** reconcile duplicate/recovery wording expectations.
6. **P3 architecture:** behavior-preserving decomposition after a stable
   regression baseline; then revisit MAX adapter boundaries.

## 14. Production readiness verdict

**NOT READY.**

The modal routing refactor is green in its focused suites and no new regression
was confirmed, but the full suite is 1185/1237 with 45 known baseline failures,
seven stale scenario tests, a high-risk external catalog checkpoint gap, and
incomplete cross-channel black-box coverage. This is not a production-ready
state until the P0/P1 items are separately fixed and the baseline is green or
explicitly accepted with product sign-off.

## 15. Validation performed

- Full pytest: 1185 passed / 52 failed; no skipped, xfailed or errors.
- Focused 13-block policy suites: pass except one known stale duplicate wording test.
- `ruff check src tests scripts alembic`: passed.
- `mypy src`: passed (`52 source files`).
- `scripts/check_markdown_links.py`: passed with the new audit document.
- `git diff --check`: passed with the current docs diff.
- `ruff format --check`: baseline fails on 18 pre-existing files; no formatter
  changes were made during audit.
- `scripts/generate_user_scenarios.py --check`: fails on stale `SUB-08` reference.
- `python scripts/build_agent_context.py`: succeeded in this run; earlier
  PermissionError is recorded as an agent-harness follow-up, not application behavior.

No application code, prompts, tests or production configuration were changed.
