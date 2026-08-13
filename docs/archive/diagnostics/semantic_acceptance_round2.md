# Semantic acceptance round 2 — diagnostic checkpoint

Status: review checkpoint only. This document does not claim that the current
implementation is finished or accepted. No behavior is changed by this report.

## Checkpoint identity

- Historical working branch: `decompose_bot`.
- Base commit before branching: `3a9ac8693f4ec266a100a880fe4b343bdc701ad7`.
- Diagnostic branch: `diagnostics/semantic-acceptance-round2`.
- The branch preserves the existing uncommitted semantic/state changes.
- The local `manual_smoke_forensic_logs.txt` file is intentionally not part of
  this checkpoint; it contains raw forensic material and remains local.
- No `.env`, credentials, tokens, cookies, authorization headers, or database
  connection strings are included here.

Manual acceptance context: Telegram smoke round on 2026-08-10. The report uses
only bounded structured events available in the local forensic log and the
runtime log excerpt; absent fields are explicitly marked as unproven.

## Pipeline used for the review

The relevant path is:

`TEXT/VOICE → transcription → AI structured parse → deterministic
reconciliation → catalog retrieval/ranking → AI shortlist decision →
deterministic safety gate → ConversationEngine state transition → draft/reply`.

The current catalog search is venue-scoped and performs a deterministic scan of
the in-memory catalog. Only a bounded shortlist (maximum five candidates) is
sent to the AI matcher. No full catalog is sent to an LLM.

## Manual PASS guards

These remain regression guards and are not a release sign-off:

- Duck: `350–380 г` remains a catalog/package characteristic, `3 кг` remains
  the order quantity, and `желательно нежирные` remains a user comment.
- Specific chickpea: the ENDAKSI/package/450 g evidence selects the matching
  row over a broad chickpea category.
- Almond: `орех миндаль лепестки` remains distinct from other almond forms.
- Specific sour cream: the 15%/brand/package request is identified, while the
  order quantity is requested separately.
- Sea urchin: the unresolved `с длинными иголками` qualifier prevents unsafe
  automatic selection.
- Quantity ownership remains separate from catalog dimensions and packaging.
- In the available runtime excerpt, mustard pending quantity plus voice
  `Хрен столовый домашний, две штуки` followed the expected interruption path:
  horseradish was added with `2 шт`, and mustard remained `missing_qty` as the
  current issue. A separate failure of this scenario is not proven by the
  supplied forensic file.

## FAIL or suspicious cases

### Cucumber — confirmed failure

Observed voice transcript:

`Огурцы 40 на 45 Майер, 10 литров, 700 грамм, 500 грамм, Германия, 5 штук.`

The AI parse contained `product_query=Огурцы 40 на 45 Майер`, `quantity=5
шт`, and a packaging-like comment. The next normalized command changed the
quantity to `10 л` before catalog decision. Five candidate IDs were retrieved,
but the available event did not contain candidate titles, scores, or a
winner/runner-up margin. The AI decision returned `not_found` with a
contradiction about the requested size/brand, and the item remained ambiguous.

First known incorrect boundary: **voice reconciliation/quantity recovery** at
`text_command_normalized` (5 шт became 10 л). Candidate disappearance and the
exact deterministic gate reason are **not proven by the available logs**.

### Trout — not proven by available logs

The supplied forensic file contains no trout interaction. The reported
`not_found` response and the point at which the correct trout candidate may
have disappeared cannot be assigned to ASR, parser, reconciliation,
canonicalization, retrieval, or evidence filtering from current evidence.

Required follow-up evidence: one bounded trace containing the ASR transcript,
normalized query, candidate count/titles, evidence, and gate reason.

### Ribs — source survives; comment ownership is suspicious

Observed voice transcript:

`Ребрышки барбекю крупные куски охлажденная вакуумная упаковка останки на пять килограмм.`

AI parsing retained the full product query and `5 кг`; the comment was empty in
the raw structured parse while the binding identified the descriptive phrase.
The normalized command then contained the catalog-like phrase as a comment.
The item was matched with one candidate, but the saved comment was
`крупные куски охлажденная вакуумная упаковка`.

First known incorrect boundary: **comment provenance/reconciliation**, between
the raw AI parse and `text_command_normalized`. The source product query was not
lost before matching, so the available evidence does not assign this symptom
to catalog ranking.

### Broccoli — current available trace is clarification, not a silent no-op

The available voice trace was transcribed as:

`Просто брокколи, вес, крупные кочаны нужны только.`

The parser produced `product_query=брокколи крупные кочаны`; the catalog returned
one generic broccoli candidate; AI returned `not_found` because the large-head
qualifier was not confirmed. The engine kept the item `ambiguous` and emitted a
clarification outcome. The trace does not reproduce a silent drop.

First incorrect boundary for the reported no-op: **not proven by available
logs**. A separate trace is required if the Telegram UI showed no reply.

### Pending mustard → new product — failure not proven in supplied logs

The local forensic file contains no mustard/horseradish interaction. The latest
readable runtime excerpt instead shows:

`voice → Хрен столовый домашний, две штуки → add_items → horseradish matched;
mustard remains missing_qty/current issue`.

Therefore the reported regression cannot be assigned to transcription,
`StateCompatibilityPolicy`, handler routing, or persistence from the supplied
evidence. A failing trace must include the voice transcript, parsed command,
modal context, compatibility decision, and resulting cart state.

## Open questions for external review

1. Should the cucumber packaging phrase remain a comment, a packaging role, or
   structured catalog evidence when the same numbers are present in the title?
2. Why did the cucumber AI matcher reject the candidate after normalization, and
   what were the candidate titles/scores and gate reasons?
3. Is the ribs descriptive phrase a source-backed local requirement or only a
   catalog fact recovered by postprocessing?
4. What exact boundary produced the reported broccoli no-op?
5. Does the reported mustard failure come from a different runtime revision or
   a voice transcript not present in the current log set?

## Current automated validation

Fresh run on this checkpoint working tree: **1359 collected / 1359 passed / 0
failed**, with no skipped, xfailed, or error tests. The run completed in about
22.8 seconds using a project-local pytest basetemp. No tests or behavior were
changed to improve the numbers.

Additional fresh checks:

- `ruff check src tests`: passed.
- `mypy src`: passed, 52 source files.
- Markdown links: passed, 27 files.
- `git diff --check`: passed.
- Targeted `ruff format --check` reports six touched files needing formatting;
  no formatting was applied: `src/restaurant_bot/services/conversation_handlers/comment_scope.py`,
  `src/restaurant_bot/services/conversation_handlers/state_compatibility.py`,
  `src/restaurant_bot/services/engine.py`, `src/restaurant_bot/services/matching.py`,
  `tests/conversation/test_comment_handling.py`, and
  `tests/conversation/test_quantity_state_preemption.py`.
- The repository-wide format check reports 15 files: the six touched files
  above plus nine historical files outside this patch. No global formatting is
  performed.
- Alembic read-only check in the naturally available healthy dev database:
  `0008 (head)`.

## Scope boundary

This checkpoint records evidence only. It does not change prompts, parsing,
matching thresholds, state routing, tests, deployment configuration, or
catalog data. Further fixes require a separate review decision.
