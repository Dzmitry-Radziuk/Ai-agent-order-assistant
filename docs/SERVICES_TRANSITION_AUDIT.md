# Block 5H — аудит transitional services/

## Границы

Аудит выполнен на ветке decompose_bot, starting SHA
d1eb9981e91a75a70931b4a3fd5b5e1dc6a8eec6. Production Python в этом блоке не
изменялся. Пакет services/ рассматривается по ADR-015 как transitional layer,
а не как целевая архитектура. Callers проверены по src, tests, api, workers и
cli.py; динамические границы проверены по orchestrator/tasks и callback-фасадам.

## Состав пакета

Top-level: catalog_resolver.py, comment_policy.py, engine.py, input_normalizer.py,
input_recognition.py, matching.py, orchestrator.py, order_review.py, parser.py,
product_add_flow.py, replies.py, submission_presenter.py, submission.py, text.py,
venue_registration.py.

conversation_handlers/: candidate_selection.py, comment_scope.py, final_review.py,
modal_routing.py, navigation.py, pending_quantity.py, state.py,
state_compatibility.py, __init__.py.

## File-level classification

| MODULE | CURRENT OWNER / CALLERS | EFFECTS / COUPLING | STATUS | TARGET / ACTION | PRIORITY |
|---|---|---|---|---|---|
| catalog_resolver.py | Re-export catalog API; production callers нет, test caller test_catalog_resolver.py | catalog only; no effects | Compatibility facade | catalog.resolver / DELETE_CANDIDATE | P2 |
| matching.py | Re-export catalog API and nearest_valid_multiple; production callers нет, test-only callers | core only; no mutation | Compatibility facade | catalog/* and conversation quantity / DELETE_CANDIDATE | P2 |
| comment_policy.py | Three pure supplier-comment functions; callers catalog, conversation, parsing, AI, engine | Parsing/evidence, no effects | Real temporary core owner | parsing/comment_policy.py / MOVE | P1 |
| engine.py | ConversationEngine: handle, routing, modal actions, duplicate/comment/product-add/submission preparation, catalog callers | Mutates ConversationState/CartItem; builds replies | Mixed state-machine coordinator; catalog core moved in 5G | application/conversation plus conversation owners / SPLIT | P4 |
| input_normalizer.py | Telegram payload to TelegramEvent; api and orchestrator callers | Telegram/raw update coupling; no external effect | Input adapter | input/telegram.py / MOVE | P3 |
| input_recognition.py | File download, OpenAI voice/photo recognition, visible actions, progress | Telegram + provider + state-aware prompts and progress side effects | Mixed input adapter | input/recognition plus channel progress / SPLIT | P3 |
| orchestrator.py | Claim/lease, access, session, recognition, parsing, catalog, engine, checkpoints, delivery, review/analytics | DB/Redis/Sheets/OpenAI/Telegram/Celery effects | Application coordinator mixed with use cases | application/update_pipeline and use cases / SPLIT | P5 |
| order_review.py | Review snapshot, stale token, preview, submit handoff | Redis/DB/Sheets/Telegram | Review use case mixed with presentation | submission/review plus presentation / SPLIT | P4 |
| parser.py | infer_intent and parse_callback; imports parsing command owners | Pure parsing; callback is channel contract | Partial facade, real public owner | parsing/commands plus input/callback / SPLIT | P3 |
| product_add_flow.py | Request ID, prompt, clear pending; engine callers | Small state helper plus presentation text | Coherent temporary owner | orders/product_add plus presentation / SPLIT | P4 |
| replies.py | BotReply renderers, cards, keyboards, issue/candidate/status text | State reads, display aggregation, callback construction | Presentation with residual calculations | presentation/telegram replies / SPLIT | P4 |
| submission_presenter.py | Submission/status/recovery/history rendering | Pure presentation and callbacks | Presentation owner | presentation/telegram/submission / MOVE | P3 |
| submission.py | Submit, read-back, checkpoints, catalog/recalc, dispatch fencing, completion, product-add write | DB/Redis/Sheets/Telegram effects | Safety-sensitive mixed service | submission/service, catalog, dispatch / SPLIT | P5 |
| text.py | Cleanup, normalization, units/departments, ranges, number words, conversion, numeric parse, HTML/number formatting | Pure but high fan-in across lower layers and presentation | Mixed core/presentation primitive owner | parsing text, domain units, presentation formatting / SPLIT | P2 |
| venue_registration.py | Directory, invite, access registry, binding, context, replies | HTTP/Redis/DB/Sheets and access mutation | Mixed venue service | venues/directory, access, registration / SPLIT | P5 |
| handlers/candidate_selection.py | Adapter to conversation.selection; engine/test callers | State read, EngineResult/reply | Correct adapter | conversation routing / KEEP_TEMP | P3 |
| handlers/comment_scope.py | Pending scope validation/application; engine/test callers | Mutates comments/stage; renders replies | State/presentation adapter; core in conversation/comments | conversation routing / KEEP_TEMP | P3 |
| handlers/final_review.py | Final guards and submit preparation | Mutates stage/issue; renders reply | Review adapter | conversation/review / MOVE later | P4 |
| handlers/modal_routing.py | Re-export; no production, test-only | No effects | Dead facade | conversation.routing / DELETE_CANDIDATE | P2 |
| handlers/navigation.py | Passive replies and order-status paging | Mutates history view state; async request | Mixed navigation/history adapter | conversation navigation + history use case / SPLIT | P4 |
| handlers/pending_quantity.py | Quantity response parser and CartItem mutation | Modal state mutation; parser/text dependencies | Quantity state adapter | conversation quantity flow / MOVE later | P3 |
| handlers/state.py | Re-export state queries; no production, test-only | No effects | Dead facade | conversation.state.queries / DELETE_CANDIDATE | P2 |
| handlers/state_compatibility.py | Re-export policy/contracts; no production, test-only | No effects | Dead facade | conversation.routing / DELETE_CANDIDATE | P2 |
| handlers/__init__.py | Empty package marker | None | Compatibility package marker | Keep until imports gone / KEEP_TEMP | P4 |

## Dependency graph

Production imports from services:
workers/tasks → orchestrator; api/app → input_normalizer; orchestrator → engine,
input, order_review, parser, text, venue; engine → parser, product_add_flow,
replies, submission, submission_presenter, comment_policy, handlers;
order_review → replies, text, venue; submission → replies, submission_presenter,
text, venue; cli → venue_registration; openai_client → parser, text.

Lower/core → services dependencies:
catalog/resolver → comment_policy, text;
catalog/evidence, scoring, safety → text;
orders/catalog_resolution → text;
conversation/comments, draft, selection, routing/item_resolution → comment_policy/text;
parsing and parsing/ai → comment_policy/text;
integrations/google_sheets → text.

These are not all bugs. text.py and comment_policy.py are temporary primitive
owners. The direct lower/core dependency is the main reason for the next seam.
Presentation dependencies from old services use cases are acceptable until a
dedicated presentation/application block.

## text.py responsibility map

- Generic cleanup: clean_text, normalize_text; core callers across parsing/catalog/conversation.
- Search/comment cleanup: remove_phrase_overlap, remove_global_comment_overlap; catalog and conversation evidence.
- Units: UNIT_ALIASES, normalize_unit, convert_quantity; domain/parsing quantity primitives.
- Departments: DEPARTMENT_ALIASES, normalize_department; order and Sheets mapping.
- Numeric parsing: numeric_range_spans, to_float, parse_number_words, NUMBER_WORDS; parsing, AI, catalog and Sheets.
- Presentation: format_number and escape; replies, submission, review and venue.
Не переименовывать файл целиком в common/text.py: кластеры имеют разные owners и
требуют отдельного caller audit.

## comment_policy.py responsibility map

explicit_supplier_comment распознаёт явный маркер пожелания; supplier_comment_start
находит начало подтверждённой инструкции; comment_semantic_key deduplicates
инструкции. Это pure parsing/evidence policy, не state и не UI. Дубликата тех же
regex в conversation/comments, parsing/comment_scope и AI reconciliation не найдено.
Следующий owner — parsing/comment_policy.py.

## Symbol-level map

| CLUSTER | CURRENT | TARGET / ACTION | WHY |
|---|---|---|---|
| ConversationEngine.handle and contextual routing | services/engine.py | application/conversation + conversation/routing / SPLIT | One state-machine entry coordinates many modal flows |
| Engine quantity/unit and duplicate/comment/product-add actions | engine plus handlers | conversation quantity/draft/comments, orders/product_add / SPLIT | State mutation remains in coordinator |
| Engine catalog methods | engine wrappers | orders/catalog_resolution / KEEP_TEMP facade | Owner proven by 5G |
| Orchestrator process, claim, checkpoint | orchestrator.py | application/update_pipeline + repositories / SPLIT | Reliability and side-effect ordering |
| Orchestrator review/analytics | orchestrator.py | application use cases / SPLIT | Independent use cases |
| text core normalization | text.py | parsing/domain/catalog / SPLIT | High fan-in, preserve exact algorithms |
| text presentation formatting | text.py | presentation/formatting / MOVE later | HTML/number output |
| SubmissionService.submit | submission.py | submission/service.py / SPLIT | Checkpoint order |
| Submission catalog/recalc | submission.py | submission/catalog.py | Read-back and uncertainty |
| Submission dispatch/completion | submission.py | submission/dispatch.py | At-most-once delivery |
| Venue directory/access/registration | venue_registration.py | venues/directory, access, registration / SPLIT | Distinct security contracts |
| Input voice/photo | input_recognition.py | input/recognition + channel progress / SPLIT | Provider and Telegram effects mixed |
| Replies renderers | replies.py | presentation/telegram / SPLIT | UX/callback contract |

## Facades and delete candidates

catalog_resolver.py and matching.py have no production callers and contain no
algorithm; test imports are the only blockers and are not, by themselves, a
permanent architecture reason. modal_routing.py, state.py and
state_compatibility.py are similarly test-only re-exports. parser.py is not a
facade as a whole: infer_intent and parse_callback remain public owners until a
separate callback/text contract block.

## Roadmap

### NOW

Сохранить текущие services adapters/facades and all Block 5G owners. No production
move in this audit.

### NEXT — ровно один code seam

Механический перенос services/comment_policy.py в parsing/comment_policy.py:
только explicit_supplier_comment, supplier_comment_start, comment_semantic_key и
private regex/constants. Не входят services/text, conversation/comments,
parsing/comment_scope, AI reconciliation, engine или orchestrator. Starting SHA:
d1eb9981e91a75a70931b4a3fd5b5e1dc6a8eec6.

Почему: маленький pure owner, шесть lower/core production callers, нулевые внешние
эффекты, и он устраняет наиболее прямую зависимость catalog/conversation/parsing
от transitional services без state-machine изменений. Regression corpus: comment
provenance, product/packaging parsing, AI comment reconciliation, catalog safety,
полный 1362 passed, Ruff/mypy/Markdown/diff. Ожидаемый результат: ноль production
imports services.comment_policy.

### LATER

Targeted split text.py (сначала core normalization/unit tables, затем presentation);
input boundary; parser callback/text; conversation handlers; review/product-add;
reliability-sensitive orchestrator/submission/venue seams.

### FINAL CLEANUP

После доказанных переносов удалить obsolete facades catalog_resolver.py,
matching.py и modal/state facades. Не удалять services искусственно: application
coordinator, adapters и доказанные facades могут временно остаться.

## Что намеренно не делалось

Не менялись src/**/*.py, prompts, catalog thresholds, AI, callbacks, DB, Sheets,
Docker, workers, tests и UX. Block 5G не переоткрывался; новый ADR не добавлялся.
