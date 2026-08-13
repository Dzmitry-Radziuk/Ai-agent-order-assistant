# DATA INTEGRITY BLOCK D — CATALOG / TITLE / ATTRIBUTE RECONCILIATION

Status: **PLAN READY / IMPLEMENTATION PENDING**
Current HEAD: `0d7d9537c7640762e1bdc56d5d8c3d75ba4f8f4d`
Branch: `decompose_bot`
Baseline: `1252 collected / 1226 passed / 26 failed / 0 skipped / 0 xfailed / 0 errors`

This document is an implementation plan only. No application code, tests, prompts,
state-machine rules, matching thresholds, submission code, or decomposition is changed
by Block D planning.

## 1. Scope and invariants

Block D defines the boundary between **user-owned meaning** and **catalog-owned facts**.

The catalog describes a selected product. It must not rewrite what the user said.
After catalog resolution, catalog data may populate catalog fields, confirm a candidate,
or reject an incompatible candidate, but it must not silently become:

- `source_query` / `source_line`;
- user `comment`, `user_comment_to_supplier`, or `global_comment`;
- order `quantity` or explicit `unit`;
- user `supplier_hint`;
- source packaging provenance.

The existing intentional duplication remains valid: a user fact may be present in both
`source_query` and `comment`. Catalog overlap is not a reason to globally deduplicate
those fields.

Block D owns only post-source/catalog reconciliation. Candidate ranking, fuzzy thresholds,
morphology, token evidence, and AI matcher policy remain Block E / existing contracts.

## 2. Current catalog call graph

The verified runtime path is:

```text
Telegram text/voice/photo command
  -> UpdateOrchestrator._parse / input recognition
  -> catalog loading (_needs_catalog)
  -> ConversationEngine.handle
       -> _build_item(ExtractedItem -> CartItem)
       -> _match_item
            -> remove_phrase_overlap(source_query, comment)
            -> CatalogResolver.search
                 -> rank_candidates / has_catalog_search_evidence
            -> _catalog_packaging_measurement
            -> optional CatalogResolver.split_explicit_supplier_comment
            -> CatalogResolver.decide
            -> _apply_catalog on auto-select
                 -> _reconcile_quantity_with_catalog_name
                 -> catalog fields / status
  -> UpdateOrchestrator._resolve_ai_pending for unresolved candidates
       -> optional safe-equivalence or AI selection
       -> ConversationEngine._apply_catalog
  -> review/submission reads CartItem.catalog_* and CartItem.comment
```

`CatalogResolver.search()` and `CatalogResolver.decide()` are read-only with respect to
conversation state. The mutation boundary is in `ConversationEngine`, especially
`_match_item()`, `_apply_catalog()`, and the reconciliation helpers.

## 3. First bad transitions

| Good state | Current transition | Bad state / evidence |
|---|---|---|
| `CartItem` contains source quantity, source line, and a catalog candidate | `_match_item()` searches and calls `decide()` without calling `_sanitize_catalog_facts_before_resolution()` | Catalog title measurements can remain as order quantity while the item is clarified (`test_catalog_title_facts_are_not_quantity_or_comment`). |
| User comment may contain a catalog-confirmed product fact | `_apply_catalog()` copies `user_comment` unchanged; the catalog-fact sanitizer is not wired into the runtime path | `comment="белое"`, `comment="один килограмм десять килограмм"`, and `comment="Травяной откорм"` survive as supplier comments. |
| `source_query` is the user/source representation | `_apply_catalog()` calls `_reconcile_quantity_with_catalog_name()` | The helper assigns `item.source_query = product_name`, replacing user wording with the catalog title. This violates provenance even when the match is successful. |
| A catalog title contains package/size/reference numbers | `_reconcile_quantity_with_catalog_name()` parses numbers outside the matched title and assigns `item.quantity/unit` | A title number can become an order quantity (`test_exact_catalog_title_number_requires_explicit_order_quantity`). |
| A user comment is separate from catalog metadata | `_refresh_cart_order_values()` calls `_remove_exact_comment_fragments(item.comment, product.comment)` | Reopening/review can remove a user comment solely because identical text exists in catalog metadata; provenance is not checked. |

The primary first bad transition for the four catalog failures is the missing
pre-resolution ownership boundary in `_match_item()`. The primary transition for the
successful-match provenance bug is `_reconcile_quantity_with_catalog_name()` replacing
`source_query` inside `_apply_catalog()`.

## 4. Helper inventory

| Helper | File | Phase | Reads | Mutates | Catalog/source dependency | Current wiring | Block D decision |
|---|---|---|---|---|---|---|---|
| `ConversationEngine._match_item` | `services/engine.py` | orchestration around match | `CartItem`, catalog, comments | candidates, status, packaging, supplier lock; indirectly all apply fields | both | runtime | keep as coordinator; add one explicit ownership boundary in planned implementation |
| `CatalogResolver.search` | `services/catalog_resolver.py` | pre/post candidate generation | query, catalog, supplier scope | none | catalog + query | runtime | do not change for Block D |
| `CatalogResolver.decide` | `services/catalog_resolver.py` | candidate decision | full query, candidates, comment, packaging | none | catalog candidate + source fields | runtime | do not change unless a failing case proves a decision-layer defect; otherwise Block E |
| `_catalog_packaging_measurement` | `services/engine.py` | after candidate generation | source line, scalar quantity, candidates | caller sets packaging fields and clears scalar quantity | source evidence + catalog numeric evidence | runtime | preserve Block C behavior; no title-only quantity inference |
| `_sanitize_catalog_facts_before_resolution` | `services/engine.py` | intended pre-decision | candidate name, source line/query, quantity/comment | quantity/unit/comment | catalog + source | **defined but no runtime caller** | reuse only after narrowing ownership rules; do not blindly activate |
| `_apply_catalog` | `services/engine.py` | post-selection | selected candidate/product | all `catalog_*`, supplier, quantity/unit/status; indirectly source query | catalog + source | runtime and orchestrator AI path | owner of final catalog application; must preserve user provenance |
| `_reconcile_quantity_with_catalog_name` | `services/engine.py` | post-selection | source line, catalog title | `source_query`, quantity, unit | catalog title + source line | runtime through `_apply_catalog` | replace with provenance-safe reconciliation; never assign source query to catalog title |
| `_remove_catalog_fact_comments` | `services/engine.py` | intended sanitation / migration | comment, source query, catalog title | returned comment only | catalog + source | only through dead sanitizer and direct reopen helper | keep as a narrow, provenance-aware helper; no global “same words => delete” rule |
| `_normalize_existing_catalog_comments` | `services/engine.py` | existing-draft migration | state cart, catalog name | user comment/source | catalog metadata | no production caller; direct focused test only | plan explicit call site or migration boundary before implementation |
| `_comment_left_after_catalog_match` | `services/engine.py` | residue extraction | query, catalog title | none | catalog + source | no callers found | dead/unwired; do not activate without a proven contract |
| `_remove_exact_comment_fragments` | `services/engine.py` | review refresh | user comment, catalog comment | returned comment | catalog + user | runtime from `_refresh_cart_order_values` | inspect/replace with provenance-safe behavior in implementation |
| `_validate_supplier_hint` | `services/engine.py` | pre-search | extracted hint, catalog | returned `ExtractedItem` | catalog supplier values | runtime | keep supplier normalization separate from catalog-title facts |
| `UpdateOrchestrator._resolve_ai_pending` | `services/orchestrator.py` | post-engine unresolved resolution | candidates, source/comment, catalog | status and calls `_apply_catalog` | catalog + AI matcher | runtime | coordinator only; no new sanitation rules here |
| `is_safe_catalog_name_equivalent` | `services/matching.py` | AI pending safe-equivalence | source query, catalog title | none | matching | runtime | keep unchanged; ranking/matching is Block E |

Dead/unwired status was verified with repository-wide `rg`: the only production calls
to `_remove_catalog_fact_comments` are absent; `_normalize_existing_catalog_comments`
is referenced by its focused test only; `_comment_left_after_catalog_match` has no caller.

## 5. Field ownership matrix

| Field | Owner before catalog | Catalog may set | Catalog may clear | Catalog may replace | Rule |
|---|---|---|---|---|---|
| `source_query` | parser/source pipeline | no | no | no | preserve user wording; catalog title lives in `catalog_name` |
| `source_line` | raw user/source evidence | no | no | no | immutable provenance |
| `quantity` | parser/source evidence | no | only a proven catalog-packaging reinterpretation with no explicit order evidence | no | never derive order quantity from title numbers |
| `unit` | parser/source evidence | only when user unit is empty, as catalog default | only with the same packaging reconciliation | no explicit user unit replacement | confirmation remains explicit in UI/state |
| `quantity_source` | parser/source evidence | no | no | no | provenance marker is not inferred from catalog |
| `comment` / `user_comment_to_supplier` | parser/comment policy | no | only a proven catalog-derived residue, never a user request | no | identical text in catalog is not enough to delete user intent |
| `comment_source` | parser/comment policy | no | may become `none` only when the comment was proven non-user residue | no | preserve `semantic` / `explicit_marker` user provenance |
| `global_comment` | parser/state flow | no | no | no | catalog never changes order-wide user intent |
| `supplier_hint` | user/parser | canonicalize only against an explicit matching supplier | no | no inference from country/brand/title | supplier lock remains separate |
| `packaging_text/role/confidence` | source parser / Block C | may confirm an existing source measurement using candidate evidence | no | no source-role overwrite | preserve Block C source quantity/packaging/range contract |
| `catalog_product_id` | empty before selection | yes, after selected candidate | yes on explicit re-search/reset | yes only through selected candidate | catalog identity field |
| `catalog_name` | empty before selection | yes | yes on re-search/reset | yes only through selected candidate | full catalog title, never source query |
| `catalog_comment` / `catalog_comment_source` | empty before selection | yes from catalog row | yes when catalog row has none | yes on refreshed catalog | informational catalog metadata, not supplier comment |
| `catalog_unit`, price, multiples, stock/department values | empty before selection | yes from selected catalog row | refresh may update | yes on selected/refreshed row | catalog-owned operational data |
| `candidates` | empty before search | yes from resolver | yes on reset | yes from a new search | candidates are not user meaning |

## 6. Catalog/user reconciliation rules

1. Build the user-owned `CartItem` first. Keep full `source_query`, `source_line`,
   quantity/unit provenance, and comments unchanged.
2. Create temporary search data only for candidate generation. It must never be written
   back to source fields.
3. Search and ranking use existing resolver/matching contracts. Block D does not change
   token evidence, fuzzy scoring, morphology, or `can_auto_select()`.
4. Before candidate decision, reconcile only values that have explicit source evidence:
   an order quantity is authoritative when the source marks it as an order; a catalog
   measurement can be packaging/reference data only when the existing source/candidate
   checks prove that role.
5. A comment can be removed only when its origin is proven to be a catalog/title residue
   (for example, all its words are an unmarked remainder of a catalog-confirmed title and
   the source does not express it as a supplier instruction). A user instruction remains,
   even if the catalog contains the same words.
6. On selection, populate `catalog_*` fields and preserve all user-owned fields. A
   rejected candidate must not mutate source data.
7. On refresh/reopen, normalize only legacy catalog residue with the same provenance
   rule. Do not treat equal text in `CatalogProduct.comment` as proof that the user
   comment is catalog-owned.
8. Reconciliation must be idempotent: applying the same selected candidate twice must
   not erase another source fact, append a duplicate comment, change quantity twice, or
   rewrite `source_query`.

### Explicit examples

| User/source | Catalog row | Expected boundary result |
|---|---|---|
| `Вино ... сухое белое`, comment `белое` | title confirms `сухое белое` | keep `source_query`; catalog residue may be removed from supplier comment only if provenance is proven |
| `Привезти охлаждённым` | any row containing a similar qualifier | keep the full user comment |
| `Фундук ... один килограмм десять килограмм`, parser quantity `10 кг` | `Фундук ... 1кг` | do not infer an order quantity from title; remove only proven quantity residue |
| `Джем клубничный ...`, no explicit order request | `Джем клубничный 1кг д/п` | `1 кг` remains catalog/package data; item stays `MISSING_QTY` |
| explicit `Закажи ... 65 г` | title `... 65 г` | keep `quantity=65`, `unit=г`; do not classify as packaging |
| user named supplier explicitly | catalog title includes country/brand | preserve only the validated explicit supplier hint; do not infer one from title |

## 7. MUST FIX / likely / out of scope

### Block D MUST FIX (5)

These five failures all reach the catalog boundary and currently fail because catalog
facts/residue are applied without a proven ownership check:

- `tests/catalog/test_product_matching.py::test_catalog_product_facts_are_not_saved_as_supplier_comments`
- `tests/catalog/test_product_matching.py::test_catalog_title_facts_are_not_quantity_or_comment`
- `tests/catalog/test_product_matching.py::test_quantity_only_comment_residue_is_removed`
- `tests/catalog/test_product_matching.py::test_exact_catalog_title_number_requires_explicit_order_quantity`
- `tests/supplier/test_supplier_lock.py::test_short_catalog_supplier_matches_selected_full_supplier_name`

The fifth test is named supplier-related, but its actual failure is
`item.comment == "Травяной откорм"` instead of empty after a successful catalog match;
the owner is the same catalog/title comment boundary, not supplier candidate filtering.

### Green neighboring catalog contracts to preserve

- `test_catalog_packaging_attribute_must_match_candidate`
- `test_spoken_catalog_packaging_is_not_used_as_order_quantity`
- `test_explicit_order_quantity_is_not_reclassified_as_packaging`
- `test_size_range_requires_an_equivalent_catalog_row`
- `test_size_range_matches_the_same_catalog_variant_without_becoming_comment`
- `test_conflicting_product_qualifier_blocks_automatic_catalog_selection`
- `test_full_name_term_missing_from_catalog_is_not_saved_as_comment`
- `test_product_variant_is_not_replaced_by_the_only_similar_catalog_row`
- `test_asr_product_residue_is_not_saved_as_supplier_comment`

### Block E boundary

Do not change `matching.py` candidate ranking, `rank_candidates()`,
`has_catalog_search_evidence()`, fuzzy scores, morphology, `can_auto_select()`, or
`is_safe_catalog_name_equivalent()` in Block D. If a failure is proven to originate in
candidate generation or ranking rather than apply/sanitization, move it to Block E.
No evidence currently requires a `CatalogResolver` algorithm change; its search/decision
contracts are read-only and already covered by green resolver/matching tests.

### Other current failures (not Block D)

From the current 26-nodeid baseline:

- PHOTO prompt: 2 (PHOTO/out of scope);
- visible-action voice: 3 (visible actions/Block F);
- generated scenario/docs contract: 7 (stale docs references);
- add-more prompt, late global comment, manual recovery card, empty voice recovery,
  voice qualifier, shadow count, omitted conjoined item, duplicate quantity: 9
  (existing out-of-scope follow-ups).

No Block D change should be justified by these failures.

## 8. Negative regression matrix for implementation

The implementation phase must add focused tests (without changing prompts or matching)
for at least these natural source/catalog pairs:

| # | User/source | Catalog row | Expected result |
|---|---|---|---|
| 1 | product title with package number, no order quantity | same title with `1 кг` | no order quantity from catalog |
| 2 | explicit order quantity plus same catalog number | same title/number | explicit quantity preserved |
| 3 | no user percentage | catalog title has `%` | percentage stays catalog data |
| 4 | no user country | title has country | no supplier hint from country |
| 5 | user explicitly names country/supplier | title confirms it | explicit source hint preserved |
| 6 | no user comment | catalog has `CatalogProduct.comment` | `item.comment` remains empty; `catalog_comment` is separate |
| 7 | user supplier comment plus catalog comment | both present | user comment remains; catalog comment remains separate |
| 8 | catalog package range | source has no order quantity | range is packaging/catalog attribute, not quantity |
| 9 | user package range matching catalog | explicit order quantity separate | both roles remain distinct |
| 10 | conflicting user qualifier | incompatible catalog qualifier | no auto-selection; source/comment preserved |
| 11 | failed candidate match | rejected row | no catalog mutation of source data |
| 12 | successful exact match | compatible row | catalog fields set; source/query/comment provenance preserved |

Every case should be exercised through both direct `_match_item`/`_apply_catalog`
boundaries where appropriate and the normal text/voice orchestration path when the
existing fixture supports it. Repeat application of the same candidate to verify
`D(D(item, catalog), catalog) == D(item, catalog)` for user-owned fields.

## 9. Block E boundary and non-goals

Out of scope: prompt changes, OpenAI schemas, deterministic product parsing, state
compatibility/modal routing, PHOTO, visible actions, submission side effects, logging,
new model fields, new catalog policy modules, and decomposition.

`CatalogResolver` changes are not planned. `matching.py` changes are not planned.
If implementation evidence contradicts this, stop and reclassify the case before coding.

## 10. Green neighbors and safety gates

Before and after implementation, retain:

- Block A source-evidence focused tests;
- Block B shadow-item focused tests (7/7);
- Block C MUST FIX (5/5), seven new quantity/packaging tests, and both local-comment
  correction tests;
- current full-suite accounting `1252 / 1226 / 26` as the comparison baseline.

The two PHOTO failures, three visible-action failures, and seven stale scenario failures
must remain separately classified and must not be counted as Block D regressions.

## 11. Expected implementation footprint

Planned application owner:

- `src/restaurant_bot/services/engine.py`: one explicit pre-decision catalog/source
  reconciliation boundary; provenance-safe post-selection apply; idempotent handling of
  quantity and comments; no source-query rewrite.

Planned focused tests:

- `tests/catalog/test_product_matching.py`;
- `tests/supplier/test_supplier_lock.py` for the comment-boundary regression;
- a dedicated focused test file only if the existing fixtures cannot express the
  ownership matrix without duplication.

No `matching.py` or `catalog_resolver.py` modification is expected. No new fields are
required: `CartItem` already separates `source_query`, user `comment`, `catalog_name`,
`catalog_comment`, `catalog_unit`, packaging provenance, and candidates.

Implementation order:

1. Add/activate the narrow ownership-aware pre-decision boundary, after observing the
   current source fields and before `CatalogResolver.decide()`.
2. Make post-selection reconciliation preserve `source_query/source_line`, explicit
   quantity/unit, and user comments; keep catalog data in `catalog_*` fields.
3. Define the safe legacy-draft normalization call site, if required, without applying it
   to new user comments indiscriminately.
4. Add the 12-case matrix and idempotency tests.
5. Run focused catalog/supplier tests, Block A/B/C regressions, full pytest, Ruff,
   mypy, markdown links, and `git diff --check`; classify any delta before further work.

Risk is concentrated in comment provenance and quantity fallback. Rollback is a single
application commit; no schema, prompt, external-service, or migration change is planned.

## 12. Success criteria

Block D implementation is complete only when:

1. The five MUST FIX tests are green without changing the matching/ranking contract.
2. `source_query` and `source_line` remain user/source values after successful and failed
   catalog matching.
3. Catalog title measurements never become order quantity without explicit source order
   evidence.
4. Explicit user quantity/unit and user comments survive catalog matching.
5. `catalog_name`, `catalog_comment`, and catalog unit remain separate catalog-owned
   fields and are rendered/submitted through their existing contracts.
6. Applying the same catalog result twice is idempotent for all user-owned fields.
7. Block A/B/C focused suites remain green and no new full-suite regressions are introduced.
8. Block E, PHOTO, visible actions, submission, and decomposition remain untouched.

## Checks for this planning stage

The planning change must run only documentation checks:

```text
python scripts/check_markdown_links.py
git diff --check
```

No application/test execution or functional correction is part of this Block D plan.
