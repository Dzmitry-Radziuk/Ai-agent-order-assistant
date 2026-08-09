# AI / CATALOG / VOICE DATA INTEGRITY — ANALYSIS
## 1. Scope

Это analysis-only аудит текущего checkout decompose_bot. Функциональный код,
тесты, prompts, matching, state-machine, MAX/submission idempotency, logging и
декомпозиция в рамках аудита не изменялись.

Проверка выполнялась против HEAD 3418fd8e5e9a906ff5679f3480737b86f746b578
до добавления этого документа.

## 2. Baseline

Полный запуск pytest -q --tb=short:

- 1237 collected;
- 1185 passed;
- 52 failed;
- skipped/xfailed/errors: 0.

Из 52 падений 45 относятся к ранее зафиксированному baseline, ещё 7 — к stale
SUB-08 references после переименования сценария. Это baseline состояния
checkout, а не доказательство регрессии от текущего docs-only изменения.

ruff check, mypy, git diff --check и проверка markdown-ссылок проходят.
.env не читается и не tracked.

## 3. Фактический pipeline

### TEXT

UpdateOrchestrator._parse → _parse_text_in_context →
OpenAIService.parse_text → deterministic fast path или
responses.parse(_TEXT_SYSTEM, ParsedInputSchema) →
recover_omitted_explicit_items → ParsedCommand →
StateCompatibilityPolicy/ConversationEngine.handle → _build_item →
_match_item → CatalogResolver.search/matching.rank_candidates →
_apply_catalog → CartItem.

### VOICE

InputRecognitionService._recognize_voice транскрибирует голос, при необходимости
повторяет распознавание с повышенной точностью, затем передаёт transcript в тот же
_parse_text_in_context. После этого действует TEXT pipeline. Поэтому ошибки
recovery и provenance имеют общий blast radius для текста и голоса.

### PHOTO

InputRecognitionService._recognize_photo → OpenAIService.parse_photo с
_PHOTO_SYSTEM → _normalise_photo_command → engine/catalog. Фото не проходит
recover_omitted_explicit_items, поэтому его контракт должен проверяться отдельно.

## 4. Lifecycle и provenance полей

| Поле | Источник | Где используется | Требование |
|---|---|---|---|
| product_query/source_query | пользовательский текст + AI/parser recovery | candidate search и matcher | не заменять названием каталога |
| comment | явное пользовательское пожелание, связанное с item | draft/provider output и matcher context | сохранять при наличии source evidence |
| global_comment | явный scope order | все относящиеся позиции | не превращать в item |
| quantity/unit | пользовательское количество | draft/catalog reconciliation | не брать из packaging/reference |
| packaging_* | фасовка/диапазон | candidate context | не смешивать с order quantity |
| catalog facts | живой каталог | display/matcher | не записывать как user comment |

Первый установленный разрыв контракта находится между структурированным AI
ответом и финальным ParsedCommand: recovery/reconciliation helpers существуют,
но не входят в основной вызов для уже непустого списка items.

## 5. Первый неверный переход

recover_omitted_explicit_items() в openai_parsing.py корректно запускается
после AI parsing, но при payload items не вызывает основную группу уже
существующих provenance/recovery helpers. Deterministic recovery срабатывает
только при пустом AI items. В результате частичный или ошибочный непустой AI
ответ считается финальным и затем попадает в catalog/application layer.

Также ExtractedItem.infer_semantic_comment_source() помечает любой непустой
comment как semantic. Без отдельной проверки source provenance выдуманный AI
comment может стать сохранённым CartItem.comment.

## 6. Кластеры корневых причин

| Кластер | Область | Наблюдение | Падения |
|---|---|---|---:|
| A | AI comment provenance | invented/local/global filler comments проходят без source evidence | 6 |
| B | AI recovery bypass | explicit terms, wrappers, ranges, quantities и qualifiers не восстанавливаются для непустого items | 9 |
| C | Shadow items | connectors, global comments, duplicated lines и root-processing fragments становятся товарами | 2 |
| D | Voice quantities/packaging | модельные quantity/unit/packaging role не сверяются с исходным transcript | 9 |
| E | Catalog facts leakage | title facts, supplier facts и quantity residues попадают в user comment/quantity | 7 |
| F | Candidate/application mutation | catalog application может перезаписать explicit source query | 1 |
| G | Range/packaging contract | диапазоны и order quantity не имеют единого provenance gate | 3 |
| H | Late global comment fixture | отсутствие кандидата для срп роза маскируется ожиданием late comment | 1 |
| I | Photo contract drift | тесты требуют старые exact prompt substrings | 2 |
| J | UX wording drift | несколько ожидаемых action/recovery текстов расходятся с текущим контрактом | 5 |

Кластеры A–H — data/semantic integrity (40 падений); I–J являются отдельными
contract/UX drift и не должны исправляться изменением data pipeline.

## 7. Детали кластеров

### A — comments

test_ai_invented_supplier_comment_is_not_preserved показывает comment
охлаждённым, которого нет в source. Также filler всё это дело на завтра
остаётся локальным у item, хотя global_comment уже равен на завтра.
Причина — отсутствует единый source-evidence gate, а не отсутствие словаря
характеристик.

### B–D — AI/voice recovery

Для непустого AI items не вызываются restore_explicit_order_terms,
_discard_unverified_item_comments, _collapse_redundant_ai_items,
remove_unsupported_query_qualifiers, range/measurement restoration,
_restore_dropped_unclassified_terms, trailing-root processing и connector
cleanup. Эти функции определены, но их вызовы обнаружены только в прямых тестах
или отсутствуют в runtime. Поэтому partial results, voice quantity words,
packaging connectors и conjoined items не нормализуются системно.

### E–F — catalog boundary

ConversationEngine._apply_catalog() вызывает
_reconcile_quantity_with_catalog_name, который может записать product_name в
item.source_query. _sanitize_catalog_facts_before_resolution и
_normalize_existing_catalog_comments определены, но runtime callers не
обнаружены. Catalog title facts/comment residue поэтому могут перейти в draft.
Candidate generation использует временный remove_phrase_overlap query, но
последующая application boundary недостаточно защищает user-owned fields.

### G–H — ranges and fixtures

Packaging/range spans, ambiguous weight pairs и срп роза требуют раздельной
проверки provenance и fixture validity. Тест с срп роза ожидает catalog result,
которого текущий evidence gate не даёт; существующий voice typo contract
поддерживает compact српроза, а не автоматически любую двухтокенную форму.

### I–J — не смешивать с data fix

Два photo prompt tests проверяют старые фразы (рукописные исправления,
не переноси значение из соседней строки), тогда как текущий prompt содержит
эквивалентные, но иначе сформулированные правила. Пять UX tests требуют старые
тексты action/recovery/duplicate warnings. Эти расхождения должны решаться как
отдельные contract decisions, а не ослаблением AI/catalog guards.

## 8. Black-box examples

Ожидаемые инварианты:

    source: свиная шея 5 кг без костей без кожи без хрящиков
    product_query: same product facts
    comment: без костей без кожи без хрящиков
    quantity/unit: 5 / кг

remove_phrase_overlap(source_query, comment) даёт временный свиная шея; это не
должно записываться обратно в source/query/comment.

Нарушения, подтверждённые тестами: на/или как item, root-processing comment
как item, duplicate items, global comment как item, catalog title fact как
supplier comment, quantity из catalog title и invented AI comment.

## 9. Candidate and visible-action safety

matching.has_catalog_search_evidence() уже содержит fuzzy/morphology evidence
gate; его нельзя обходить по одному случайному shared token. rank_candidates
требует score/evidence. Полный product_query + comment должен оставаться
контекстом AI matcher, а очищенный search query — только временным
candidate input.

Visible-action failures принадлежат отдельному кластеру маршрутизации:
UpdateOrchestrator._parse_text_in_context и
InputRecognitionService.match_visible_action/choose_visible_action. Их нельзя
чинить изменениями comment/quantity provenance.

## 10. Blast radius

- TEXT и VOICE совместно затронуты recovery/provenance bypass.
- VOICE дополнительно зависит от ASR и чувствителен к quantity/connector shadow
  items.
- PHOTO использует отдельный parser/prompt и отдельный contract drift.
- Catalog application может затронуть все три входа после matching.

## 11. Validity и drift тестов

Каждое падение следует разделять на: (1) реальный source/provenance violation,
(2) stale expected contract, (3) fixture/evidence mismatch, (4) UX wording drift.
Нельзя исправлять тест простым ослаблением parser или matcher. Для каждого
будущего fix нужны positive и negative tests, а для voice — тот же сценарий после
transcription.

## 12. Предлагаемые блоки исправлений

1. Block A — P0 provenance/reconciliation. В одной точке после AI parse
   применять source-evidence validation и существующие recovery helpers; не
   удалять намеренное дублирование product facts в product_query и comment.
2. Block B — shadow-item collapse. После восстановления, но до ParsedCommand,
   атомарно удалить connectors/global fragments/duplicates по source line.
3. Block C — quantity/packaging. Разделить order quantity и packaging/reference
   provenance для TEXT/VOICE/PHOTO.
4. Block D — catalog boundary. Запретить _apply_catalog перезаписывать
   user-owned source/comment/quantity; catalog facts хранить только в catalog
   fields.
5. Block E — candidate safety. Сохранить temporary core search и full matcher
   context; отдельно подтвердить rare-token/fuzzy evidence regressions.
6. Block F — visible-action routing. Исправить global intent precedence отдельно.
7. Block G — contracts. После решения semantics обновить photo/UX tests и docs.

## 13. Рекомендуемый первый P0/P1 fix

Начать с Block A. Это первый общий неверный переход и он покрывает AI comments,
quantity words, ranges, dropped terms и shadow items до того, как данные попадут
в catalog или draft. Реализовывать source evidence через исходный text/transcript
и структурные bindings, а не через новый blacklist характеристик. До реализации
нужен отдельный минимальный план и regression matrix; этот документ код не меняет.

## 14. Observability

Для будущей диагностики нужны privacy-safe structured events с одним
trace/update correlation: input kind, parser output, recovery diff,
compatibility decision, candidate query, candidate ids, catalog application
diff, final draft fields, reply и external side effects. При
LOG_USER_CONTENT=false сохранять только hash/length; secrets, tokens и
credentials не логировать.

## 15. Decomposition/MAX и проверки

Аудит не меняет декомпозицию и не добавляет MAX-specific behaviour. Следующее
изменение должно быть поведенчески узким и не совмещать Block A с архитектурным
переносом больших модулей. После каждого блока обязательны focused tests, full
regression, Ruff, mypy и git diff --check.
