# DATA INTEGRITY BLOCK G — INPUT CONTRACT / PHOTO PROMPT / RECOVERY UX

Status: **PLAN READY / IMPLEMENTATION PENDING**

This document records an analysis only. No application code, tests, or prompts
were changed for Block G.

## 1. Scope

Block G has two independent boundaries:

1. **G1 PHOTO input contract** — vision prompt, structured schema, photo
   normalization, and the engine's photo handoff.
2. **G2 VOICE recovery UX** — voice parse result, engine routing, and the
   user-facing recovery card.

The current HEAD is `0be22d06163d6dec5d934cf12dd8a7f1de19eb6a` on
`decompose_bot`. Fresh baseline: **1254 collected / 1236 passed / 18 failed /
0 skipped / 0 xfailed / 0 errors**.

## 2. Current 18 failure classification

| Nodeid | First failure / owner | Classification |
|---|---|---|
| `tests/ai/test_photo_prompt_contract.py::test_photo_prompt_excludes_packaging_and_stock_from_order_quantity` | exact `"рукописные исправления"`; `_PHOTO_SYSTEM` already has correction semantics | C/D — stale literal; contract migration |
| `tests/ai/test_photo_prompt_contract.py::test_photo_prompt_forbids_moving_quantity_between_neighboring_rows` | exact `"не переноси значение из соседней строки"`; row-local wording already exists | C/D — stale literal; contract migration |
| `tests/conversation/test_add_more_prompt.py::test_product_sent_from_add_more_prompt_opens_duplicate_in_collecting_stage` | duplicate reply wording in duplicate flow | A — existing duplicate/UX baseline, outside G |
| `tests/conversation/test_comment_handling.py::test_late_global_comment_applies_to_existing_and_new_items_without_overlap` | late global-comment application | A — existing comment-flow baseline, outside G |
| `tests/conversation/test_engine.py::test_manual_action_without_an_open_item_uses_source_recovery_card` | recovery button emoji differs from old expectation | C/D — existing UX migration, outside G |
| `tests/docs/test_user_scenarios.py::test_catalog_references_only_existing_pytest_tests` | stale scenario reference | C — stale generated-doc contract |
| `tests/docs/test_user_scenarios.py::test_every_critical_scenario_has_an_automated_check` | same stale scenario reference | C — stale generated-doc contract |
| `tests/docs/test_user_scenarios.py::test_generated_scenario_views_are_current` | same stale scenario reference | C — stale generated-doc contract |
| `tests/docs/test_user_scenarios.py::test_markdown_contains_mermaid_and_all_scenario_ids` | same stale scenario reference | C — stale generated-doc contract |
| `tests/docs/test_user_scenarios.py::test_catalog_explains_permissions_confirmation_and_recovery` | same stale scenario reference | C — stale generated-doc contract |
| `tests/docs/test_user_scenarios.py::test_every_flow_is_linked_to_tested_scenarios` | same stale scenario reference | C — stale generated-doc contract |
| `tests/docs/test_user_scenarios.py::test_html_catalog_is_autonomous_and_filterable` | same stale scenario reference | C — stale generated-doc contract |
| `tests/input/test_voice_input_contract.py::test_empty_voice_add_items_uses_the_source_recovery_card` | old voice title, not routing or buttons | C/D — stale UX literal; contract migration |
| `tests/input/test_voice_input_contract.py::test_unknown_empty_voice_uses_the_same_source_recovery_card` | old voice title, not routing or buttons | C/D — stale UX literal; contract migration |
| `tests/input/test_voice_input_contract.py::test_voice_beef_cannot_gain_an_unspoken_qualifier_or_auto_select` | qualifier retained in voice product query | A — existing voice provenance baseline, outside G |
| `tests/input/test_voice_quantity_recovery.py::test_packaging_and_order_sentence_collapses_ai_shadow_items` | shadow item remains after voice recovery | A — existing Block B/C baseline, outside G |
| `tests/input/test_voice_quantity_recovery.py::test_partial_voice_model_result_restores_the_omitted_conjoined_item` | omitted conjoined item not restored | A — existing Block B/C baseline, outside G |
| `tests/quantity/test_duplicate_quantity.py::test_repeated_product_with_quantity_requires_explicit_merge` | duplicate reply contract | A — existing duplicate-quantity baseline, outside G |

Accounting: **Block G functional MUST FIX = 0**; **contract migrations = 4**;
**stale Block G tests = 4**. The four migrations are the two PHOTO literal
assertions and the two voice recovery-card title assertions.

## 3. G1 PHOTO runtime path

```text
Telegram photo
  -> InputRecognitionService._recognize_photo
  -> OpenAIService.parse_photo
  -> _PHOTO_SYSTEM + ParsedInputSchema
  -> OpenAIService._normalise_photo_command
  -> UpdateOrchestrator / ConversationEngine
```

`parse_photo()` passes the dedicated vision prompt and `ParsedInputSchema`.
Normalization then owns positive-quantity filtering, department-column
aggregation, printed-order right-cell handling, and handwritten replacement
handling. The engine receives only the normalized command.

## 4. PHOTO literal-vs-semantic contract audit

| Expected literal | Exact in prompt | Equivalent safety semantics | Decision |
|---|---:|---|---|
| `остатки` | yes | printed reference/stock values are not order quantity | keep |
| `фактического заказа` | yes | actual order field is authoritative | keep |
| `рукописные исправления` | no | `рукописное исправление`, replacement of crossed-out value, `handwritten_correction` | migrate test |
| `Зал`, `Бар`, `Кухня` | yes | department columns and row ownership | keep |
| `зачёркнутое` | yes | crossed-out old value is replaced, never summed | keep |
| `крайней правой ячейке` | yes | printed form order cell is rightmost cell | keep |
| `hall, bar, kitchen` | yes | schema and normalization expose all three fields | keep |
| `не переноси значение из соседней строки` | no | `не переноси между соседними строками`, including row above/below | migrate test |
| `пусты, полностью пропусти именно эту строку` | no | empty current row/right cell is skipped completely | migrate test |
| `строка ниже` | yes | quantity belongs to the lower row only | keep |

There is no demonstrated G1 safety gap. The prompt already protects row-local
ownership, stock/packaging separation, department isolation, and handwritten
replacement. Adding duplicate prose only to satisfy the old literals would be
prompt bloat.

## 5. PHOTO safety invariants

- A quantity belongs only to the product on the same row/cell.
- Printed packaging, stock, prices, and references never become order quantity.
- `client_order_sheet` keeps `hall`, `bar`, and `kitchen` independently and
  drops rows with no positive department quantity.
- `printed_order_form` uses only a readable number in the rightmost order cell;
  the middle/reference column is not an order.
- A handwritten correction replaces a crossed-out value and is never summed with
  the old value.

## 6. PHOTO test strategy and coverage

| Layer | Existing coverage |
|---|---|
| Prompt-only | `tests/ai/test_photo_prompt_contract.py` (four stale literal assertions) |
| Schema | `tests/ai/test_ai_media.py::test_openai_input_schema_has_fixed_department_quantity_fields` and structured media tests |
| Normalization | `test_client_order_sheet_*`, `test_supplier_form_keeps_only_filled_printed_or_handwritten_order_cells`, `test_photo_drops_every_item_without_actual_order_quantity`, `test_handwritten_replacement_wins_over_crossed_out_client_sheet_quantity` |
| Engine photo | `tests/conversation/test_engine.py::test_photo_without_order_quantities_does_not_create_draft_items`, quantity and handwritten/correction cases |
| Pipeline / E2E-ish | `tests/conversation/test_orchestrator_pipeline.py::test_photo_pipeline_reports_download_recognition_and_catalog_stages` and timeout recovery |

Behavioral coverage is stronger than the failing literal tests. The remaining
gap is contract-test design: prompt tests should assert stable semantic clauses
or a small set of safety markers, not one exact sentence.

## 7. G2 voice recovery runtime path

```text
VOICE
  -> transcription / shared text parsing
  -> ParsedCommand(ADD_ITEMS, items=[]) or UNKNOWN
  -> ConversationEngine.handle
  -> unrecognized_voice_reply
  -> BotReply
```

For both empty voice commands, `ConversationEngine` deliberately selects
`unrecognized_voice_reply()`. On an empty draft it returns:

- text beginning `⚠️ <b>К сожалению, мне не удалось распознать голосовое сообщение</b>`;
- `Обновить статусы` / `v2:orders`;
- `Добавить товары` / `v2:add`.

No cart item, quantity, or modal context is changed. TEXT `UNKNOWN` remains on
`unknown_intent_reply()` and is not converted to voice recovery UX.

The two failing tests assert the former title `Не удалось распознать голосовое
сообщение`. The first assertion fails on the title; the buttons match the current
empty-draft recovery card. This is a deliberate UX wording migration, not an
engine routing bug. `replies.py` owns the text; `engine.py` owns the already
correct voice-vs-text branch.

## 8. Voice modal safety

Existing modal preemption tests for quantity, comment scope, ambiguous candidate,
not-found, duplicate, unit mismatch, submission failure, submit confirmation,
new-order confirmation, and voice route safety pass. Empty/unknown voice must
not reset an active modal state; that policy remains outside this docs-only plan.

## 9. Functional fixes vs contract migrations

No production fix is justified by the four Block G failures:

- `_PHOTO_SYSTEM` does not need modification;
- `ParsedInputSchema` and photo normalization already enforce the safety boundary;
- `ConversationEngine` routes both empty voice forms safely;
- `unrecognized_voice_reply()` already has the intended text and buttons.

Future implementation, after separate approval, should migrate the four stale
test contracts to semantic markers/current UX. It must not weaken the prompt or
change voice routing merely to make the old assertions green.

## 10. Expected implementation split

If a future evidence-based gap appears, keep owners separate:

- G1: prompt/schema/normalization boundary, primarily
  `src/restaurant_bot/integrations/openai_prompts.py` or
  `src/restaurant_bot/integrations/openai_client.py` only if safety is missing;
- G2: `src/restaurant_bot/services/replies.py` for intentional UX wording, or
  `engine.py` only if a first-bad routing transition is demonstrated;
- tests: `tests/ai/test_photo_prompt_contract.py` and
  `tests/input/test_voice_input_contract.py` after contract approval.

## 11. Out of scope

Do not change Block A–F provenance, shadow, quantity, catalog, matcher, modal
routing, visible actions, prompts, tests, or decomposition in this analysis
stage. Do not start Block H or MAX.

## 12. Success criteria for a future migration

1. PHOTO tests assert semantic safety markers and retain behavioral coverage.
2. Voice recovery tests assert current error wording and current button IDs.
3. Empty `ADD_ITEMS` and empty `UNKNOWN` voice remain safe and state-preserving.
4. TEXT `UNKNOWN` remains on the ordinary text recovery card.
5. No prompt duplication or unrelated Block A–F behavior changes.
