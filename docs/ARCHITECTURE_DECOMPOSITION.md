# Architecture decomposition plan

## Block 0 scope

This document is the design contract for the decomposition. Block 0 changes
documentation only. No Python module, test, prompt, database schema, Docker
file, CI file, or serialized state contract is changed by this block.

The repository is currently on `decompose_bot` at accepted baseline
`f9cbc3195c0eae843de3208e488c3f46baa5a5ec`. The current automated baseline is
`1362 collected / 1362 passed`. The untracked forensic log is local diagnostic
data and is not part of the project architecture or any commit.

The design must preserve the existing semantic pipeline:

```text
Telegram text / voice / photo / callback
    -> input normalization and recognition
    -> global semantic parsing
    -> source-evidence reconciliation
    -> catalog retrieval
    -> evidence and ranking
    -> deterministic safety decision
    -> state compatibility and handler
    -> draft mutation
    -> reply
    -> checkpointed external side effects (submission only)
```

AI can propose structure or a candidate decision. It does not directly mutate
the draft, state, database, or external sheet.

## Current runtime boundary

| Entry point | Current owner | Contract |
|---|---|---|
| `api/app.py:telegram_webhook` | FastAPI transport | Validate Telegram secret, normalize update, enqueue once. |
| `workers/tasks.py:process_telegram_update` | Celery delivery | Retry and delegate one update to the orchestrator. |
| `services/orchestrator.py:UpdateOrchestrator.process` | Application pipeline | Claim update, lease chat, authorize venue, parse, call engine, checkpoint state/reply, enqueue effects. |
| `services/engine.py:ConversationEngine.handle` | Conversation state machine | Apply a `ParsedCommand` to a `ConversationState` and return `EngineResult`. |
| `services/submission.py:SubmissionService` | Submission use case | Re-read access and draft, write/checkpoint/recalculate/dispatch with lease fencing. |
| `integrations/telegram.py:TelegramClient` | Telegram adapter | Download files and deliver replies/callback acknowledgements. |
| `integrations/google_sheets.py:GoogleSheetsGateway` | Sheets adapter | Read catalog/statuses and prepare/apply order, catalog, and recalculation operations. |
| `repositories/*` | Persistence adapters | Updates, sessions, submissions, events, and venue bindings. |

## Current package ownership

## Size audit of current modules

Line counts below are from the accepted baseline checkout. Size is a discovery
signal only; responsibility boundaries, callers, and side effects determine the
decomposition.

| Current module | Lines | Main classes/symbols | Boundary finding |
|---|---:|---|---|
| `services/engine.py` | 3353 | `ConversationEngine` plus ~70 methods | State routing, draft mutation, comments, catalog, review, and submission preparation are coupled. |
| `integrations/openai_prompts.py` | 2378 | prompt constants only | One external contract file; move as a unit, do not split by line count. |
| `services/orchestrator.py` | 2152 | `UpdateOrchestrator`, `ClaimedUpdate` | Transaction pipeline, input routing, catalog AI pending, persistence, and task scheduling are coupled. |
| `services/submission.py` | 2047 | `SubmissionService` plus checkpoint stages | Order, product-add, status, catalog mutation, recalculation, dispatch, and notification are separate use cases sharing lease/checkpoint rules. |
| `integrations/openai_parsing.py` | 1800 | five schemas and reconciliation functions | Schemas, source reconciliation, comments, quantities, and shadow collapse need named sub-owners. |
| `services/parser.py` | 1687 | global intent/callback functions plus product facade | Command parsing and product parsing are distinct; product implementation is the selected first move. |
| `services/replies.py` | 1017 | reply builders and supplier warning groups | Conversation screens and status/issue presentation are mixed but share exact UX contracts. |
| `integrations/openai_client.py` | 927 | `OpenAIService` | HTTP transport, deterministic bypasses, parsing, candidate matching, and visible actions are mixed. |
| `integrations/google_sheets.py` | 884 | `GoogleSheetsGateway` and result contracts | Catalog, order, status, registration, recalculation, and mutation verification share an adapter. |
| `conversation_handlers/state_compatibility.py` | 848 | `StateCompatibilityPolicy` and decision enums | One coherent policy, but its location is under the old services tree. |
| `services/matching.py` | 830 | canonical/evidence/scoring/safety functions | Three decisions currently share one module; no behavior rewrite is implied. |
| `services/venue_registration.py` | 808 | directory, access registry, registration service | Venue data and chat registration are related but separable application responsibilities. |
| `services/product_parser.py` | 673 | deterministic product functions | Already coherent and side-effect-free; lowest-risk extraction boundary. |

### Domain and persistence

| Current path / symbols | Responsibility today | Callers and tests | Action |
|---|---|---|---|
| `domain/models.py:Intent, ParsedCommand, ExtractedItem, CatalogProduct, Candidate, CartItem, ConversationState, EngineResult` | Pydantic/enumeration contracts and serialized state shape. | All application layers; `tests/conversation`, `tests/input`, `tests/repositories`. | **KEEP**. It is the stable downward contract. |
| `db.py`, `db_models.py` | SQLAlchemy session and persistence schema. | API, repositories, workers; repository and concurrency tests. | **KEEP**. No schema work in decomposition. |
| `repositories/updates.py` | Inbox claim, update sequencing, retry/redrive. | `tests/workers/test_concurrency_block.py`, orchestrator tests. | **KEEP**. |
| `repositories/sessions.py` | Versioned `ConversationState` persistence. | `tests/conversation`, `tests/review`, concurrency tests. | **KEEP**. |
| `repositories/submissions.py` | Submission checkpoints and idempotency records. | `tests/submission/*`, concurrency tests. | **KEEP**. |
| `repositories/order_events.py`, `venue_bindings.py` | Audit events and venue access bindings. | `tests/repositories`, `tests/venue`, concurrency tests. | **KEEP**. |

### Input and semantic parsing

| Current path / symbols | Current responsibility | Current callers | Tests protecting it | Final action |
|---|---|---|---|---|
| `services/input_normalizer.py:normalize_telegram_update` | Convert Telegram payloads to `TelegramEvent`. | `api/app.py`, `UpdateOrchestrator`. | `tests/input/test_input_normalizer.py`, `test_input_routing.py`. | **MOVE** to `input/telegram.py`; keep a facade during migration. |
| `services/input_recognition.py:InputRecognitionService` | Voice download/transcription, photo recognition, visible-action recognition, processing cards. | `UpdateOrchestrator`, direct input tests. | `tests/input/test_voice_*`, `test_photo_worker_flow.py`, `tests/conversation/test_global_voice_navigation.py`. | **MOVE** to `input/recognition.py`; keep external adapters injected. |
| `services/parser.py:infer_intent, parse_callback, dialogue_response_for` | Global command/intent parsing and command-target extraction. | Engine, orchestrator, OpenAI service, pending quantity handler, tests. | `tests/input/test_parser.py`, routing and modal tests. | **MOVE** command parsing to `parsing/commands.py`; keep `services/parser.py` as a temporary facade. |
| `services/product_parser.py:parse_product_lines, parse_quantity_unit, has_explicit_global_comment_scope, _extract_global_comment` | Deterministic product lines, order quantity, packaging, and explicit global comment scope. | `services/parser.py`, OpenAI reconciliation, input recognition, engine, pending quantity. | `tests/input/test_input_edge_cases.py`, `test_voice_quantity_recovery.py`, parser/comment/quantity tests. | **MOVE** to `parsing/products.py`; Block 1. |
| `services/text.py:clean_text, normalize_text, normalize_unit, numeric_range_spans, parse_number_words` | Lexical normalization and measurement primitives. | Nearly every service and several adapters. | Input, catalog, quantity, and submission tests. | **MOVE** to `parsing/text.py` only after product/command callers are migrated; do not create `utils.py`. |
| `integrations/openai_parsing.py:ParsedInputSchema, ProductMatchDecision, CommentBindingSchema, recover_omitted_explicit_items, restore_explicit_order_terms, comment/collapse helpers` | Structured AI schemas plus deterministic reconciliation of source evidence, comments, quantities, ranges, and shadow items. | `OpenAIService`; one direct integrity test and many end-to-end tests. | `tests/input/test_ai_result_integrity.py`, `test_voice_quantity_recovery.py`, comment and AI tests. | **SPLIT/MOVE** to `parsing/ai/schemas.py` and `parsing/ai/reconciliation.py`; preserve one public facade first. |
| `integrations/openai_prompts.py` | Text, photo, voice, candidate, visible-action, and comment-scope prompt contracts. | `OpenAIService`. | `tests/ai/test_photo_prompt_contract.py`, voice/input contracts. | **MOVE** to `integrations/openai/prompts.py`; preserve exact prompt text and schema. |
| `integrations/openai_client.py:OpenAIService` | OpenAI transport, transcription, structured parse calls, deterministic short-input bypasses, candidate/visible-action/comment-scope calls. | Orchestrator, input recognition, workers, tests. | `tests/ai/*`, voice/photo/input contracts. | **SPLIT/MOVE**: transport to `integrations/openai/client.py`; use-case facade to `application/ai/service.py`; compatibility facade required. |

### Catalog resolution

| Current path / symbols | Current responsibility | Current callers | Tests | Final action |
|---|---|---|---|---|
| `services/matching.py:canonical_search_query, query_evidence_tokens, has_catalog_search_evidence, match_score, rank_candidates` | Canonicalization, morphology/fuzzy evidence, numeric characteristics, candidate ranking. | Catalog resolver, engine, orchestrator, OpenAI service. | `tests/catalog/test_matching.py`, `test_product_matching.py`, AI pending tests. | **SPLIT/MOVE** to `catalog/evidence.py` (canonical/evidence), `catalog/scoring.py` (score/rank), and `catalog/safety.py` (selection gates). |
| `services/matching.py:can_auto_select, is_broad_category_query, has_conflicting_catalog_qualifiers, has_product_variant_qualifier, is_safe_catalog_name_equivalent` | Precision gates and unresolved qualifier safety. | Engine, resolver, orchestrator. | Candidate selection, AI pending, product matching tests. | **MOVE** to `catalog/safety.py`; one owner must decide auto-select. |
| `services/catalog_resolver.py:CatalogResolver, CatalogDecision, CatalogSearchResult, CatalogQuerySplit` | Venue-scoped candidate retrieval, bounded shortlist, deterministic decision, no state mutation. | Engine. | `tests/catalog/test_catalog_resolver.py`, candidate/matching tests. | **MOVE** to `catalog/resolver.py`; preserve the pure resolver contract. |
| `engine.py:_match_item, _apply_catalog, _sanitize_catalog_facts_before_resolution` | Currently combines catalog retrieval, AI pending resolution, and draft application. | `ConversationEngine.handle`. | Engine, AI pending, candidate selection, comment tests. | **MERGE** resolver calls into `catalog/resolver.py`; **MOVE** draft application to conversation draft owner. |

### Conversation and state

| Current path / symbols | Current responsibility | Callers/tests | Final action |
|---|---|---|---|
| `services/conversation_handlers/state_compatibility.py:StateCompatibilityPolicy, CompatibilityContext, CompatibilityDecision` | Single policy for modal compatibility and intent preemption. | `modal_routing.py`, engine, orchestrator. | Modal routing and preemption tests. | **MOVE** to `conversation/routing/state_compatibility.py`; preserve one policy owner. |
| `services/conversation_handlers/modal_routing.py:evaluate_modal_routing` | Evaluates all active modal contexts into one decision object. | Engine and orchestrator. | Modal routing tests. | **MOVE** to `conversation/routing/modal_routing.py`; no duplicated strong-intent list. |
| `conversation_handlers/pending_quantity.py:PendingQuantityHandler` | Quantity continuation only after global parsing/policy. | Engine, compatibility policy. | `test_quantity_state_preemption.py`, quantity tests. | **MOVE** to `conversation/handlers/pending_quantity.py`. |
| `conversation_handlers/candidate_selection.py:CandidateSelectionHandler` | Safe contextual candidate selection. | Engine, input recognition. | Candidate selection/preemption tests. | **MOVE** to `conversation/handlers/candidate_selection.py`. |
| `conversation_handlers/comment_scope.py:CommentScopeHandler` | Apply a confirmed comment scope without changing global parse. | Engine, orchestrator fallback. | Comment scope/precedence tests. | **MOVE** to `conversation/handlers/comment_scope.py`. |
| `conversation_handlers/final_review.py:FinalReviewHandler` | Review/submit guards and page transitions. | Engine. | Review/submit confirmation tests. | **MOVE** to `conversation/handlers/final_review.py`. |
| `conversation_handlers/navigation.py:PassiveIntentHandler, OrderStatusHandler` | Passive replies and order-status navigation. | Engine. | Navigation/status tests. | **MOVE** to `conversation/handlers/navigation.py`. |
| `conversation_handlers/state.py:first_unresolved, item_index` | Pure state queries. | Handlers and engine. | Modal/engine tests. | **MOVE** to `conversation/state/queries.py`; no behavior change. |
| `services/engine.py:ConversationEngine.handle, _advance, _build_item, _apply_catalog, _remove_item, _edit_quantity, _edit_existing_comment, _prepare_submission` | Current state machine, draft mutation, catalog application, comments, quantity, duplicate flow, review, and submission preparation. | Orchestrator and 39 tests. | All conversation, catalog, quantity, comment, review, and submission tests. | **SPLIT/MOVE** into `conversation/engine.py` (orchestration), `conversation/draft.py` (cart mutation), `conversation/comments.py`, and existing handlers. Keep a thin engine facade while migrating. |
| `services/replies.py` | All user-facing text/card/keyboard builders for conversation and issue states. | Engine, handlers, order review, submission. | UI/reply and conversation tests. | **MOVE** to `conversation/replies.py`; split only by coherent screen family after callers are known. |

### Orders, venues, and external effects

| Current path / symbols | Current responsibility | Current callers/tests | Final action |
|---|---|---|---|
| `services/order_review.py:OrderReviewService, ReviewSnapshot` | Fresh Sheets-backed review link, snapshot token, and review submission handoff. | Orchestrator, review tests. | **MOVE** to `orders/review.py`; keep token and stale-preview contracts. |
| `services/product_add_flow.py` | Pure pending product-add request helpers. | Engine and product-add tests. | **MOVE** to `orders/product_add.py`; tiny named module remains justified. |
| `services/submission.py:SubmissionService` | Order/product-add/status submission, catalog mutation, recalculation, dispatch, checkpoints, lease fencing, completion notification. | Workers, engine, five submission test groups. | **SPLIT/MOVE** to `submission/service.py`, `submission/checkpoints.py`, `submission/catalog.py`, and `submission/status.py`; facade required until callers migrate. |
| `services/submission_presenter.py` | Submission and order-status replies. | Submission service and tests. | **MOVE** to `submission/presenter.py`; keep exact HTML/callback contracts. |
| `services/venue_registration.py:VenueDirectory, VenueAccessRegistry, VenueRegistrationService` | Central venue directory/access and chat registration flow. | Orchestrator, CLI, review/submission, venue tests. | **SPLIT/MOVE** to `venues/directory.py`, `venues/access.py`, `venues/registration.py`; keep service facade during migration. |
| `integrations/telegram.py:TelegramClient` | Telegram HTTP transport and file operations. | API-adjacent application services, input recognition, submission. | **MOVE** to `integrations/telegram/client.py`; transport-only owner. |
| `integrations/google_sheets.py:GoogleSheetsGateway` | Catalog, order sheet, status, registration, recalculation, and mutation verification adapter. | Venue, order review, submission, cache, tests. | **SPLIT/MOVE** to `integrations/google_sheets/catalog.py`, `orders.py`, `venues.py`, and `recalculation.py` behind a compatibility gateway. |
| `integrations/cache.py:CatalogCache, ChatLease, chat_lock, google_submission_lock_key` | Redis catalog cache and concurrency leases/locks. | Orchestrator, submission, workers, tests. | **SPLIT/MOVE** to `infrastructure/cache/catalog.py` and `infrastructure/concurrency/chat_lease.py`; do not change lease semantics. |
| `services/orchestrator.py:UpdateOrchestrator, ClaimedUpdate` | Transactional application pipeline and effect scheduling. | `workers/tasks.py`, orchestrator tests. | **MOVE** to `application/update_pipeline.py`; keep workers as delivery boundary and remove upward worker imports later. |

## Responsibility conflicts to remove

These are design findings, not changes made in Block 0:

1. `engine.py` currently knows both pure catalog decisions and mutable cart
   application. `CatalogResolver` should return a decision; conversation draft
   code should apply it.
2. `matching.py` currently contains evidence, score, and auto-select policy.
   Retrieval, ranking, and precision safety must be separate call sites with
   one shared canonical representation.
3. `parser.py` is a command facade that also re-exports product parsing. The
   product parser is already a separate owner and should be moved behind a
   named `parsing.products` module.
4. `openai_client.py` combines HTTP transport with deterministic routing and
   application-specific recovery. Transport and AI use cases need separate
   owners; prompt/schema contracts must remain unchanged.
5. `openai_parsing.py` combines schemas, comment provenance, quantity recovery,
   and shadow-item collapse. These are related reconciliation responsibilities,
   but the schema definitions should not depend on the reconciliation engine.
6. `google_sheets.py` is one adapter for several external contracts. Its
   eventual split must preserve one authenticated gateway and must not change
   sheet writes in this architecture phase.
7. `orchestrator.py` imports Celery tasks lazily to enqueue effects. The final
   application layer should receive an effect scheduler port so application
   code does not import the worker layer.
8. `cache.py` couples catalog cache data with chat/submission locks. Both are
   infrastructure concerns but have different failure and ownership contracts.

## DELETE candidates after migration

Deletion is conditional on a repository-wide import scan and green regression
gates. No file is deleted in Block 0.

- `services/product_parser.py` implementation: delete after the
  `services/parser.py` compatibility re-export and any direct callers migrate
  to `parsing/products.py`.
- Product-parser aliases in `services/parser.py`: delete only after all callers
  use the named parsing owner; the command parser itself remains until its own
  move.
- `services/matching.py` old implementation: delete only after evidence,
  scoring, and safety callers are migrated to the three catalog owners.
- `integrations/openai_client.py` old transport/use-case implementation:
  delete only after both the transport adapter and application AI facade have
  migrated and no public compatibility caller remains.
- `integrations/google_sheets.py` and `integrations/cache.py`: not currently
  proven obsolete. They are facade candidates, not deletion candidates, until
  adapter callers and external contracts are split and verified.

## Proposed final package tree

The tree is intentionally moderate in depth. A package exists only where it
contains several coherent responsibilities.

```text
src/restaurant_bot/
    api/
        app.py                         # HTTP health and Telegram webhook
    domain/
        models.py                      # serialized commands, state, draft, replies
    input/
        telegram.py                    # Telegram payload -> TelegramEvent
        recognition.py                 # voice/photo recognition and visible actions
    parsing/
        text.py                        # lexical normalization, units, numeric ranges
        commands.py                    # global intent and callback parsing
        products.py                    # product lines, quantity, packaging, scope
        ai/
            schemas.py                 # structured AI response contracts
            reconciliation.py          # source evidence and AI reconciliation
    catalog/
        evidence.py                    # canonical tokens and evidence extraction
        scoring.py                     # scores and bounded candidate ranking
        safety.py                      # qualifier, category, and auto-select gates
        resolver.py                    # venue-scoped retrieval and decision
    conversation/
        engine.py                      # state-machine orchestration only
        draft.py                       # cart/item mutation primitives
        comments.py                    # comment provenance and edits
        state/
            queries.py                 # pure state queries and priorities
        routing/
            state_compatibility.py    # the one modal compatibility policy
            modal_routing.py           # aggregate modal decisions
            visible_actions.py        # contextual UI fallback
        handlers/
            pending_quantity.py
            candidate_selection.py
            comment_scope.py
            final_review.py
            navigation.py
            manual_details.py
            product_add_details.py
            add_more_confirm.py
            submit_confirm.py
            submission_failed.py
    orders/
        review.py                      # fresh review snapshot and token
        product_add.py                 # pending product-add request
    submission/
        service.py                     # use-case orchestration
        checkpoints.py                 # durable uncertain/at-most-once states
        catalog.py                     # catalog mutation stage
        status.py                      # history/status read path
        presenter.py                   # submission/status replies
    venues/
        directory.py                   # central venue directory
        access.py                      # access registry and bindings
        registration.py                # chat registration flow
    application/
        update_pipeline.py             # claim, lease, auth, parse, engine, save
        ai_service.py                  # AI use-case facade over OpenAI adapter
    integrations/
        telegram/client.py             # Telegram HTTP adapter
        openai/client.py               # OpenAI HTTP adapter
        openai/prompts.py              # prompt contract text
        google_sheets/catalog.py       # catalog adapter
        google_sheets/orders.py        # order/review/status adapter
        google_sheets/venues.py        # registration adapter
        google_sheets/recalculation.py # recalculation adapter
    repositories/
        updates.py, sessions.py, submissions.py, order_events.py, venue_bindings.py
    infrastructure/
        cache/catalog.py               # catalog cache
        concurrency/chat_lease.py     # chat/submission lock and lease fencing
        db.py, logging.py, observability.py
    workers/
        celery_app.py, tasks.py        # delivery and scheduled tasks only
```

`config.py`, `db_models.py`, and `cli.py` remain top-level operational modules.
They are not moved merely to make the tree symmetrical.

## Dependency directions

The final dependency rule is deliberately simpler than a full Clean
Architecture implementation:

```text
domain
  <- parsing, catalog, conversation
  <- input, orders, venues, submission
  <- application
  <- api, workers, integrations, repositories, infrastructure
```

Allowed directions:

- `domain` imports only validation libraries and domain-local contracts.
- `parsing` imports `domain`; it does not import conversation, repositories,
  workers, or external adapters.
- `catalog` imports `domain` and parsing canonical primitives; it does not
  mutate state or call OpenAI/Sheets.
- `conversation` imports `domain`, parsing, catalog, and its reply builders;
  it does not import Celery, database sessions, or Google Sheets.
- `orders`, `venues`, and `submission` are application use cases. They may
  depend on domain and explicit adapter protocols, repositories, and reply
  presenters, but not on API or worker modules.
- `application` coordinates input, parsing, catalog, conversation, venues,
  repositories, and injected integrations. It owns scheduling ports, not
  Celery task imports.
- `integrations` depend on config and domain transport types only. OpenAI,
  Telegram, Sheets, and Redis adapters must not import the orchestrator.
- `repositories` depend on persistence models and domain serialization; they
  do not import services or integrations.
- `api` and `workers` are outer delivery adapters and may import application
  entry points. No lower layer imports them.

During migration, compatibility facades can temporarily violate the final
shape, but every facade must have a named deletion step and an import scan.

## Compatibility strategy

Every move follows four phases:

1. **Create owner:** add the target module with a mechanical transfer and no
   behavior change.
2. **Migrate consumers:** update internal imports while retaining the old path
   as a re-export facade where external/test imports exist.
3. **Prove facade usage:** search source, tests, CLI, workers, and packaging for
   old imports; run focused and full tests.
4. **Delete facade:** remove the old implementation only after no justified
   caller remains, then run the same gates again.

No facade may contain a second algorithm. It may only re-export the target
owner. No compatibility layer may change a serialized `ConversationState`,
Telegram callback, prompt, HTML, or sheet contract.

## Test ownership after migration

Tests remain behavior-oriented; they do not move solely because a private
function moved.

| Future owner | Existing protection to retain |
|---|---|
| `parsing/products.py` | `tests/input/test_parser.py`, `test_input_edge_cases.py`, `test_voice_quantity_recovery.py`, quantity and comment tests. |
| `parsing/commands.py` and conversation routing | `tests/conversation/test_*_preemption.py`, `test_*_routing.py`, `tests/input/test_input_routing.py`. |
| `parsing/ai/reconciliation.py` | `tests/input/test_ai_result_integrity.py`, voice recovery, comment handling, AI media/pending tests. |
| `catalog/evidence.py`, `scoring.py`, `safety.py` | `tests/catalog/test_matching.py`, `test_product_matching.py`, candidate selection, AI pending tests. |
| `conversation/handlers` and `draft.py` | All conversation, quantity, comment, candidate, duplicate, review, and state tests. |
| `submission/*` | `tests/submission/*`, product-add, lease-fencing, recalculation, completion notification tests. |
| `input/*` | Voice/photo/input normalizer/processing-card tests. |
| `venues/*` and Sheets adapters | Venue isolation/registration, review, submission mapping tests. |

At least one integration test must continue to cover source input through parse,
reconciliation, catalog decision, state mutation, and reply. Unit tests should
not become the only protection for the cross-layer contracts.

## Migration order

The order minimizes behavior risk and finishes existing partial boundaries:

1. **Block 1 — deterministic product parser extraction** (selected below).
2. Move command parsing from the facade into `parsing/commands.py`.
3. Split AI schemas/reconciliation while keeping the OpenAI client facade.
4. Split catalog evidence/scoring/safety from the resolver and engine.
5. Move modal policy and existing conversation handlers under
   `conversation/`; then extract draft/comment mutation from engine.
6. Move input normalization/recognition and introduce an application pipeline
   port for effect scheduling.
7. Split order review, product-add, submission checkpoints/status/presenter,
   and venue registration by external contract.
8. Split Telegram/OpenAI/Sheets/cache adapters and remove remaining facades.
9. Only after each contract is proven, reduce `engine.py` and
   `orchestrator.py` to orchestration-sized modules.

No step changes matching thresholds, prompts, UX, state serialization,
submission idempotency, or deployment configuration.

## Block 1 selection: product parser extraction

Block 1 is a documentation-only selection in Block 0. It is not implemented
here.

### Exact source and target

- Source implementation: `services/product_parser.py`, including
  `_is_standalone_quantity`, `_extract_global_comment`,
  `has_explicit_global_comment_scope`, `parse_product_lines`, and
  `parse_quantity_unit`.
- Target owner: `parsing/products.py` under a new `parsing` package.
- Temporary facade: `services/product_parser.py` re-exports the moved symbols
  for one migration interval, or `services/parser.py` continues to expose the
  existing public aliases while direct callers are verified.
- Consumer migration: `services/parser.py` changes its private imports to the
  target owner; OpenAI reconciliation and other callers continue through the
  parser facade until the import scan is complete.
- Final action: **MOVE** implementation; **DELETE** the old implementation
  and facade only after all callers, tests, CLI, workers, and packaging imports
  are migrated and the public compatibility decision is recorded.

### Why this is first

- The product parser is already a coherent, side-effect-free boundary.
- `services/parser.py` is the only production importer of its implementation;
  most tests use the parser facade.
- It separates quantity/packaging ownership without changing the global intent
  parser or state machine.
- It is reversible by restoring one facade import and has a narrow regression
  surface.

### Block 1 verification and rollback

Focused gates:

- `tests/input/test_parser.py`
- `tests/input/test_input_edge_cases.py`
- `tests/input/test_voice_quantity_recovery.py`
- `tests/conversation/test_comment_handling.py`
- `tests/quantity/test_quantity_contract.py`
- `tests/conversation/test_quantity_state_preemption.py`

Required full gates:

- `python -m pytest -q --tb=short`
- `ruff check src tests`
- `ruff format --check` on changed files
- `mypy src`
- `python scripts/check_markdown_links.py`
- `git diff --check`

Rollback boundary: revert the single mechanical extraction commit. No data
migration, Docker change, prompt change, state migration, or external effect is
allowed in Block 1.

## Block 0 completion criteria

Block 0 is complete when this document and `PROJECT_HANDOFF.md` describe one
current architecture, one next block, the actual callers/tests, explicit
dependency rules, and a reversible migration order. External review must approve
the design before Block 1 implementation begins.
