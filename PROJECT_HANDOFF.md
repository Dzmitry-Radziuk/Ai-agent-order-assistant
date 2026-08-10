# Project handoff

## 1. Project purpose

This repository contains a production Python Telegram bot for restaurant
procurement. A user sends text, voice, photo, or a callback. The bot parses the
request, resolves products against the catalog of the selected venue, keeps an
editable draft, shows clarification when identity or requirements are unsafe,
and submits a confirmed request to the venue's Google Sheet.

The bot is not a free-form autonomous agent. AI assists recognition and
matching; deterministic application code owns state mutation, safety gates,
idempotency, persistence, and external effects.

## 2. Current checkout

- Repository: `Dzmitry-Radziuk/test_bot`.
- Branch: `decompose_bot`.
- Accepted baseline SHA: `f9cbc3195c0eae843de3208e488c3f46baa5a5ec`.
- The branch is intended to track GitHub `origin/decompose_bot` only.
- Current automated baseline: `1362 collected / 1362 passed`.
- `manual_smoke_forensic_logs.txt` is an intentional local untracked diagnostic
  file. It is not source, test input, or a commit candidate.

## 3. Runtime and data flow

```text
Telegram update
  -> api/app.py:telegram_webhook
  -> repositories/updates.py (enqueue_once)
  -> workers/tasks.py (Celery delivery/retry)
  -> services/orchestrator.py:UpdateOrchestrator.process
  -> venue authorization and chat lease
  -> input normalization / voice or photo recognition
  -> global ParsedCommand
  -> source-evidence reconciliation
  -> ConversationEngine and StateCompatibilityPolicy
  -> CatalogResolver and deterministic safety gates
  -> ConversationState/cart mutation
  -> repositories/sessions.py checkpoint
  -> Telegram reply
```

Submission is a separate boundary:

```text
editable draft
  -> final review and fresh snapshot
  -> SubmissionService checkpoint
  -> venue Google Sheet write/read-back
  -> recalculation checkpoint
  -> optional central dispatch checkpoint
  -> completion notification checkpoint
```

The update inbox, per-chat lease, versioned session, submission checkpoints, and
reply delivery are separate guarantees. A successful in-memory transition is
not itself proof that an external write or Telegram notification succeeded.

## 4. Current architecture and ownership

### Domain and persistence

- `domain/models.py` owns `Intent`, `ParsedCommand`, `ExtractedItem`,
  `CatalogProduct`, `Candidate`, `CartItem`, `ConversationState`, reply
  contracts, submission state, and serialized enums. This is the downward data
  contract and must remain stable during decomposition.
- `db.py` and `db_models.py` own SQLAlchemy sessions and database schema.
- `repositories/updates.py` owns Telegram inbox claiming, sequencing,
  recovery, and redrive.
- `repositories/sessions.py` owns versioned serialized conversation state.
- `repositories/submissions.py` owns submission checkpoints and idempotency.
- `repositories/order_events.py` owns bounded lifecycle audit events.
- `repositories/venue_bindings.py` owns user/chat-to-venue bindings.

### Input and parsing

- `services/input_normalizer.py` converts Telegram payloads to `TelegramEvent`.
- `services/input_recognition.py` downloads/recognizes voice and photo input,
  chooses transcription, and matches visible actions.
- `services/parser.py` owns global intent/callback parsing and currently acts as
  a compatibility facade for product parsing.
- `services/product_parser.py` owns deterministic product lines, order
  quantity, packaging, and explicit global comment scope.
- `services/text.py` owns lexical normalization, units, numbers, and ranges.
- `integrations/openai_parsing.py` owns structured AI schemas plus deterministic
  source-evidence reconciliation, comment provenance, quantity recovery, and
  shadow-item collapse.
- `integrations/openai_prompts.py` owns exact prompt contracts.
- `integrations/openai_client.py` currently combines OpenAI transport with the
  application-facing AI use cases and deterministic short-input paths.

### Catalog and conversation

- `services/matching.py` owns canonical tokens, evidence, numeric
  characteristics, scoring, ranking, and auto-selection gates. The final design
  will split these decisions without changing their behavior.
- `services/catalog_resolver.py` owns venue-scoped candidate retrieval and
  pure `CatalogDecision` results; it does not mutate `ConversationState`.
- `services/conversation_handlers/state_compatibility.py` is the single
  modal compatibility policy. It decides CONTINUE, INTERRUPT, AMBIGUOUS,
  REJECT, or NOT_APPLICABLE from an already parsed command.
- `services/conversation_handlers/*` contains focused handlers for quantity,
  candidate selection, comment scope, final review, navigation, and modal
  routing.
- `services/engine.py` is still the application state machine. It coordinates
  handlers, catalog application, draft/cart mutation, comments, quantities,
  duplicate handling, review, and submission preparation.
- `services/replies.py` owns conversation cards, keyboards, issue replies, and
  user-facing text.

### Orders, venues, and side effects

- `services/order_review.py` owns fresh review snapshots and review tokens.
- `services/product_add_flow.py` owns pure pending product-add helpers.
- `services/submission.py` owns submission, product-add, status, catalog
  mutation, recalculation, dispatch, completion notification, checkpoint, and
  lease-fencing flows.
- `services/submission_presenter.py` owns submission and status reply rendering.
- `services/venue_registration.py` owns the central venue directory, access
  registry, and chat registration flow.
- `integrations/telegram.py` owns Telegram HTTP/file transport.
- `integrations/google_sheets.py` owns catalog, order, status, registration,
  recalculation, and mutation-verification adapter calls.
- `integrations/cache.py` owns catalog cache and Redis chat/submission locks.
- `workers/tasks.py` owns Celery delivery and scheduled task boundaries only.

## 5. Stable behavioral invariants

1. AI proposes; user source is evidence; catalog confirms or refines; deterministic
   code decides whether state or an external effect may change.
2. `product_query` and supplier `comment` are not mutually exclusive. A user
   requirement may intentionally exist in both fields.
3. Order quantity is distinct from catalog packaging, net weight, range, or
   dimensions. A source-supported order quantity wins over an AI scalar inferred
   from a product title or package.
4. Candidate retrieval optimizes recall, ranking orders plausible candidates,
   and auto-selection optimizes precision. These are separate decisions.
5. A material unresolved qualifier may block auto-selection without deleting a
   deterministic shortlist that proves the base product.
6. A weak or unrelated token never turns a different product into a candidate.
7. Text and voice converge after transcription on the same parsing, recovery,
   catalog, state, and reply pipeline.
8. Global parsing runs before contextual modal fallback. State is context for an
   understood command, not an authority that changes its meaning.
9. A strong independent command may interrupt a pending quantity, candidate,
   comment scope, or other modal context. Old pending context is preserved and
   cannot leak quantity or comments to a new item.
10. Comments require source-supported provenance. Catalog facts do not become
    supplier instructions; explicit user instructions remain visible.
11. Draft mutation is venue-scoped, versioned, and idempotent for repeated
    Telegram updates.
12. Submission uses a fresh frozen snapshot, per-venue locks, durable
    checkpoints, read-back/uncertain states, and at-most-once completion
    notification behavior.
13. Callback revision and current item identity are validated before mutation.
14. No request is silently replaced by an unrelated similar product. Unresolved
    products are clarified or recorded through the product-add flow.

## 6. Protected boundaries

The following are outside Block 0 and every mechanical decomposition block unless
the user explicitly approves a separate task:

- `docker/`, `Dockerfile`, `docker-compose.yml`, deployment scripts, and CI/CD;
- `.env`, credentials, API keys, Telegram tokens, service-account files, and
  production configuration;
- database schema and Alembic migrations;
- Telegram UX, HTML, emoji, button order, callback data, and serialized state;
- prompts, matching thresholds, catalog semantics, and submission/idempotency
  behavior;
- external Google Sheets, Telegram, OpenAI, webhook, or production actions.

GitHub is the only remote for this work. GitLab is not used.

## 7. Known unresolved manual acceptance issues

These are current acceptance scenarios to fix later. Block 0 does not implement
them and no product-specific rule may be introduced for them.

### A. Pending quantity navigation

After the bot asks for a quantity for `хлеб`, the user may say `новый товар`.
That phrase must not become bread quantity. It may open a safe request for the
new product name; a concrete new product command must interrupt the pending
quantity flow while preserving the unfinished bread item.

### B. Comment deletion safety

`удали все комментарии` must remove comments only. It must never become
`CLEAR_CART`, delete products, or delete the order.

### C. Comment edit/replace semantics

Given an existing comment `нужно на завтрак в 18:00`, a correction such as
`не на завтрак нужно, а на завтра` should replace the corrected part while
preserving unaffected information. This requires generic comment edit/replace
semantics, not a phrase-specific rule.

## 8. Current decomposition status

Block 0 is now the active design package. No application Python code, tests,
prompts, deployment file, migration, or serialized contract has been moved or
changed for this block.

The repository already contains valuable partial boundaries:
`conversation_handlers`, `product_parser`, `catalog_resolver`,
`comment_policy`, `input_recognition`, and OpenAI parsing/prompt modules. The
decomposition must finish those boundaries, not create parallel implementations.

The final design, responsibility map, dependency rules, compatibility strategy,
and selected first block are documented in
`docs/ARCHITECTURE_DECOMPOSITION.md`.

## 9. NEXT DECOMPOSITION BLOCK

**Block 1: mechanically move the deterministic product parser implementation to
`parsing/products.py`.**

Scope:

- move `_is_standalone_quantity`, `_extract_global_comment`,
  `has_explicit_global_comment_scope`, `parse_product_lines`, and
  `parse_quantity_unit` from `services/product_parser.py`;
- update only the parser facade imports;
- keep a re-export facade until all callers and tests are verified;
- preserve exact quantity, packaging, comment, and source-line behavior;
- do not change global intent routing, prompts, catalog matching, state, UX,
  persistence, or external effects.

Block 1 is selected for external review, not implemented in this handoff.

## 10. Validation requirements for each block

Before and after every mechanical move:

```text
python -m pytest -q --tb=short
ruff check src tests
ruff format --check <touched files>
mypy src
python scripts/check_markdown_links.py
git diff --check
```

Run focused tests for the moved owner and at least one end-to-end test from
input through state and reply. Check import cycles and search for remaining
facade callers. If the development database is naturally available, run
`alembic check`; do not create a migration for decomposition.

Do not call real submission, central dispatch, webhook registration, or
production services during validation.

## 11. Rules for future developers and Codex agents

- Read `AGENTS.md`, this file, the project map, development process, and
  decisions before changing code.
- Treat current code, tests, and serialized contracts as the source of truth;
  historical notes are not behavior.
- Explain the first incorrect boundary before adding a guard or fallback.
- Prefer one named owner over duplicate rules or generic `helpers.py`/`utils.py`.
- Keep all Python docstrings in Russian.
- Do not silently broaden a decomposition into behavior changes.
- Preserve unrelated user changes and the untracked forensic diagnostic.
- Never read or print `.env` or secrets.
- Do not use destructive Git commands, force push, or GitLab.
- Commit only the files explicitly in the current block and push only
  `origin/decompose_bot` after all required gates pass.
- Update this handoff only when the current status, contract, risk, or next
  decomposition block changes; keep one current status and one next step.
