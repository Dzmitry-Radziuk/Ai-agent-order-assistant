# DATA INTEGRITY BLOCK E — CATALOG CANDIDATE EVIDENCE / MATCHER BOUNDARY

Статус: **PLAN READY / IMPLEMENTATION PENDING**
Проверено на HEAD `57d8184cfb59431404c6ab8858562d7f698d8258`, branch
`decompose_bot`. Код приложения, тесты и prompts в рамках анализа не менялись.

## 1. Scope

Block E отвечает только за границу между пользовательским товарным запросом и
каталогом:

`product query → candidate admission → ranking → compatibility → auto-select / clarify`.

Block D владеет provenance и применением каталожной строки к `CartItem`. Поэтому
Block E не должен изменять `source_query`, `source_line`, `comment`, количество,
единицу или supplier provenance. Не меняются parser/OpenAI prompts, PHOTO,
state machine, SessionRepository и decomposition.

Главный контракт: слабое fuzzy evidence может оставить строку в shortlist для
уточнения, но не может само по себе выбрать товар без пользователя.

**Текущий Block E MUST FIX = 0.** В свежем full suite нет падения, чей первый
неверный переход находится в deterministic catalog matching. Ниже зафиксированы
coverage gaps и границы будущей реализации; это не основание менять green
behavior без отдельного regression case.

## 2. Current matching call graph

Фактический runtime-путь:

```text
UpdateOrchestrator.process/update
  → ConversationEngine.handle
    → ConversationEngine._match_item
      → remove_phrase_overlap(item.source_query, item.comment)
      → CatalogResolver.search
        → rank_candidates
          → match_score
          → has_catalog_search_evidence
      → candidate list on CartItem
      → CatalogResolver.decide
        → is_broad_category_query
        → can_auto_select
        → numeric compatibility
        → qualifier conflict / unscoped variant gates
        → unverified_product_terms
      → _sanitize_catalog_facts_before_resolution
      → second CatalogResolver.decide
      → _apply_catalog or AMBIGUOUS/NOT_FOUND
  → UpdateOrchestrator._resolve_ai_pending (после engine, если остаётся AMBIGUOUS)
    → safe-equivalence shortcut
    → OpenAI shortlist decision
    → deterministic final gates
    → _apply_catalog or AMBIGUOUS/NOT_FOUND
```

`CatalogResolver.search()` не изменяет `ConversationState`; он возвращает
`CatalogSearchResult`. `CatalogResolver.decide()` координирует решение, но также
не мутирует state. Единственное применение выбранной строки принадлежит engine.

## 3. Search vs rank vs auto-select

| Уровень | Текущий владелец | Решение | Допустимая ошибка |
|---|---|---|---|
| Candidate admission | `rank_candidates()` + `has_catalog_search_evidence()` | Можно ли показать строку как возможную | permissive, если есть содержательная базовая связь |
| Ranking | `match_score()` | В каком порядке показать shortlist | fuzzy/ASR могут повышать порядок |
| Auto-selection | `CatalogResolver.decide()` + `can_auto_select()` + hard gates | Можно ли выбрать без вопроса | только conservative; conflict/unknown/numeric mismatch запрещают |

Один threshold для этих уровней использовать нельзя. Количество найденных строк
не является semantic evidence: один найденный похожий товар всё равно может быть
не тем продуктом.

## 4. Helper inventory

| Helper | Файл | Назначение | Search | Rank | Auto-select | Safe equivalence | Текущие callers / риск |
|---|---|---|---:|---:|---:|---:|---|
| `tokens` | `matching.py` | нормализация слов, stop words/units | indirectly | да | indirectly | да | почти все token checks; stop-word риск |
| `_product_identity_tokens` | `matching.py` | слова идентичности без чисел/units | да | нет | indirectly | нет | `has_catalog_search_evidence`; общие слова могут быть слабыми |
| `_product_identity_tokens_in_order` | `matching.py` | ordered identity tokens | да | нет | нет | нет | admission leading-token rule |
| `_qualifier_tokens` / `_qualifier_root` | `matching.py` | признаки и bounded root groups | нет | нет | да | нет | qualifier conflict gates; root list не должен стать classifier |
| `_token_matches` | `matching.py` | exact, substring, first-letter + SequenceMatcher | да | да | indirectly | нет | fuzzy admission/ranking; короткие слова требуют отдельного контроля |
| `has_catalog_search_evidence` | `matching.py` | admission gate для shortlist | да | нет | нет | нет | вызывается из `rank_candidates`; сейчас допускает leading base + unknown tail для clarification |
| `has_complete_query_evidence` | `matching.py` | все значимые query words подтверждены candidate | да, strict supplier | нет | indirectly | нет | `CatalogResolver._complete_candidates`, engine sanitizer; не должен стать вторым auto policy |
| `query_evidence_tokens` | `matching.py` | какие query tokens подтверждены | indirectly | нет | indirectly | нет | resolver split/engine evidence/AI breadth; общий evidence signal |
| `unverified_product_terms` | `matching.py` | source terms без подтверждения candidate | нет | нет | да | нет | resolver и AI final gate; не parser/comment classifier |
| `has_conflicting_catalog_qualifiers` | `matching.py` | hard veto противоположных variant roots | нет | нет | да | нет | resolver + AI final; unknown qualifiers не выводятся из отсутствия |
| `has_product_variant_qualifier` | `matching.py` | bounded product-variant risk | нет | нет | indirectly | нет | direct tests/related safety; не расширять словарём |
| `has_unscoped_product_variant_qualifier` | `matching.py` | риск ошибочного AI comment | нет | нет | да | нет | resolver + AI final; provenance boundary |
| `has_compatible_numeric_characteristics` | `matching.py` | размеры/ranges/packaging compatibility | нет | нет | да | indirectly | resolver, engine, AI; Block C/D ownership остаётся выше |
| `match_score` | `matching.py` | aggregate order score | нет | да | indirectly | нет | `rank_candidates`; score не является safety proof |
| `rank_candidates` | `matching.py` | построение shortlist | да | да | нет | нет | единственная текущая admission/ranking entry point |
| `can_auto_select` | `matching.py` | top score + top/runner-up gap | нет | нет | да | нет | resolver; требует внешних semantic hard gates |
| `is_broad_category_query` | `matching.py` | общий category/family query | нет | нет | да | нет | resolver + AI pending; один candidate не доказывает exact product |
| `is_safe_catalog_name_equivalent` | `matching.py` | строгая exact-equivalence после AI pending | нет | нет | да | да | orchestrator shortcut; частично пересекается с complete evidence |
| `supplier_matches_hint` | `matching.py` | supplier scope filter | да | indirectly | indirectly | нет | resolver search/canonical hint; это scope, не product identity |

## 5. Current heuristic map

`matching.py` сочетает пять разных сигналов:

1. normalized token overlap и ordered leading-token admission;
2. substring/root matching и `SequenceMatcher` fuzzy threshold;
3. compact voice recovery для слитного `сыропроза` → `Сироп Роза`;
4. bounded qualifier roots для hard conflicts;
5. numeric characteristics для range/packaging compatibility.

Сейчас они взаимодействуют так: `rank_candidates()` сначала получает score,
затем отбрасывает строки без `has_catalog_search_evidence()`. В resolver
shortlist проходит через strict decision gates. Поэтому fuzzy допускается на
этапе показа, но не должно автоматически пересекать final safety boundary.

`tokens()` исключает `_STOP_WORDS`, units и служебные слова. Это полезно для
identity overlap, но domain stop word может быть частью реального имени товара.
Риск зафиксирован как coverage task; `_STOP_WORDS` в Block E plan не изменяется.

`_token_matches()` сейчас поддерживает exact, substring для достаточно длинного
фрагмента, first-letter constraint и SequenceMatcher (порог 0.8 для трёхбуквенных
слов, 0.72 для более длинных). Это объясняет легитимные voice typo/inflection
сценарии, но не является доказательством одинакового продукта.

## 6. First bad transitions

| Сценарий | Первый слой | Текущее решение | Безопасный контракт |
|---|---|---|---|
| `свиная шея без костей` vs `Вишня без косточки` | `has_catalog_search_evidence` получает уже очищенный core query через engine | cherry не проходит identity admission | сохранить source/comment и не давать constraint создать базовый candidate |
| `сироп` vs `Сироп Роза` | `rank_candidates` | candidate показывается | только clarification, не auto-select |
| `свинина` vs `Сало свиное` | admission может оставить related suggestion | `CatalogResolver.decide` обязан clarify/not-found | не считать единственный candidate доказательством |
| `уши копчёные` vs `уши свежие` | `has_conflicting_catalog_qualifiers` | hard veto | conflict нельзя компенсировать score |
| unknown `CHATEL Франция` | `unverified_product_terms` на decision boundary | shortlist допустим, auto-select запрещён | не переносить слово в comment |
| wrong/right numeric range | `has_compatible_numeric_characteristics` | mismatch блокирует final selection | диапазон остаётся product/catalog compatibility fact |
| merged ASR `сыропроза` | compact branch admission | shortlist допустим | AI/final gates не получают права выбрать unrelated word |

Engine-level `has_complete_query_evidence()` нужен для provenance sanitizer и
strict supplier candidates. Он не должен развиться в независимый matcher policy.

## 7. Current 21 failure classification

Свежий запуск на текущем HEAD: **1254 collected / 1233 passed / 21 failed / 0
skipped / 0 xfailed / 0 errors**, duration 15.85s. Exact failures:

| Nodeid | Classification | Почему не Block E |
|---|---|---|
| `tests/ai/test_photo_prompt_contract.py::test_photo_prompt_excludes_packaging_and_stock_from_order_quantity` | BLOCK G | PHOTO prompt contract |
| `tests/ai/test_photo_prompt_contract.py::test_photo_prompt_forbids_moving_quantity_between_neighboring_rows` | BLOCK G | PHOTO prompt contract |
| `tests/conversation/test_add_more_prompt.py::test_product_sent_from_add_more_prompt_opens_duplicate_in_collecting_stage` | OTHER | duplicate UX wording/routing |
| `tests/conversation/test_comment_handling.py::test_late_global_comment_applies_to_existing_and_new_items_without_overlap` | OTHER | comment scope/provenance |
| `tests/conversation/test_engine.py::test_manual_action_without_an_open_item_uses_source_recovery_card` | OTHER | recovery-card UX |
| `tests/docs/test_user_scenarios.py::test_catalog_references_only_existing_pytest_tests` | STALE DOCS | stale SUB-08 reference |
| `tests/docs/test_user_scenarios.py::test_every_critical_scenario_has_an_automated_check` | STALE DOCS | same catalog validation error |
| `tests/docs/test_user_scenarios.py::test_generated_scenario_views_are_current` | STALE DOCS | same catalog validation error |
| `tests/docs/test_user_scenarios.py::test_markdown_contains_mermaid_and_all_scenario_ids` | STALE DOCS | same catalog validation error |
| `tests/docs/test_user_scenarios.py::test_catalog_explains_permissions_confirmation_and_recovery` | STALE DOCS | same catalog validation error |
| `tests/docs/test_user_scenarios.py::test_every_flow_is_linked_to_tested_scenarios` | STALE DOCS | same catalog validation error |
| `tests/docs/test_user_scenarios.py::test_html_catalog_is_autonomous_and_filterable` | STALE DOCS | same catalog validation error |
| `tests/input/test_voice_input_contract.py::test_empty_voice_add_items_uses_the_source_recovery_card` | BLOCK G | voice recovery-card contract |
| `tests/input/test_voice_input_contract.py::test_unknown_empty_voice_uses_the_same_source_recovery_card` | BLOCK G | voice recovery-card contract |
| `tests/input/test_voice_input_contract.py::test_voice_beef_cannot_gain_an_unspoken_qualifier_or_auto_select` | OTHER | failure is in source/AI reconciliation before matcher; no catalog decision is reached |
| `tests/input/test_voice_processing_card.py::test_visible_action_timeout_returns_unknown_instead_of_product` | BLOCK F | visible-action precedence |
| `tests/input/test_voice_processing_card.py::test_free_form_visible_button_phrase_uses_exact_screen_action` | BLOCK F | visible-action contract |
| `tests/input/test_voice_processing_card.py::test_semantic_voice_action_can_only_choose_a_visible_button` | BLOCK F | visible-action contract |
| `tests/input/test_voice_quantity_recovery.py::test_packaging_and_order_sentence_collapses_ai_shadow_items` | OTHER | Block C/B provenance/shadow item |
| `tests/input/test_voice_quantity_recovery.py::test_partial_voice_model_result_restores_the_omitted_conjoined_item` | OTHER | source occurrence recovery, not matching |
| `tests/quantity/test_duplicate_quantity.py::test_repeated_product_with_quantity_requires_explicit_merge` | OTHER | duplicate pending UX/state |

`BLOCK E LIKELY = 0` среди текущих failures: ни один nodeid не показывает
первый неверный переход в `matching.py`/`CatalogResolver`. Matching hardening
нуждается в regression coverage, а не в бездоказательном исправлении baseline.

## 8. Existing green contracts

Уже защищены green-тестами:

- complete evidence отклоняет category-only match;
- safe equivalence поддерживает inflection и word order, но отклоняет related
  product;
- exact product auto-select, broad category и единственный похожий candidate —
  не auto-select;
- numeric range обязан совпасть с catalog row;
- conflicting qualifiers блокируют selection;
- неизвестный full-name term и короткий unknown term не исчезают в comment;
- catalog packaging attribute обязан совпасть;
- `свиная шея` не создаёт cross-category `Вишня без косточки`;
- однословные `пармезан`, `укроп`, `лук порей`, `картофель` сохраняют поиск;
- merged/vowelless ASR сохраняет candidate только как shortlist.

Основные файлы: `tests/catalog/test_matching.py`,
`tests/catalog/test_product_matching.py`, `tests/catalog/test_catalog_resolver.py`,
`tests/ai/test_ai_pending.py`.

## 9. Auto-select safety gates

Будущая реализация должна сохранить явный порядок gates:

1. нормализовать query;
2. допустить candidate по базовой identity evidence;
3. проверить qualifier compatibility и hard conflicts;
4. проверить numeric/range/packaging compatibility;
5. посчитать ranking score;
6. проверить top-vs-runner-up gap и broad category;
7. проверить unverified high-risk product terms;
8. только затем разрешить auto-select; иначе `AMBIGUOUS`/`NOT_FOUND`.

Score служит для порядка, а conflict, missing mandatory qualifier, numeric
mismatch и broad category — hard safety gates. Нельзя заменять их правилами
«один токен unsafe» или «два токена safe».

## 10. AI pending boundary

`UpdateOrchestrator._resolve_ai_pending()` получает только deterministic shortlist.
Он может выбрать только `product_id` из этого списка и только после финальных
deterministic checks: score, confidence, numeric compatibility, qualifier
conflicts, packaging role и unverified terms. AI не должен добавлять кандидата,
который был отвергнут admission/compatibility gate. Safe-equivalence shortcut
также должен оставаться строгим и не превращаться в альтернативный parser.

## 11. False-positive matrix

| Query | Catalog | Candidate display | Auto-select |
|---|---|---|---|
| `сливки` | `Сливки 33%` | допустимо как suggestion | нет без exact-product evidence |
| `свинина` | `Сало свиное` | только weak clarification, если admission оставляет | нет |
| `кукуруза` | `Крупа кукурузная` | suggestion может быть related | нет |
| `рис квадратный` | `Рис круглый` | не скрывать mismatch | нет, hard conflict |
| `уши копчёные` | `Уши свежие` | clarification/blocked | нет, hard conflict |

## 12. True-positive matrix

| Query | Catalog | Expected |
|---|---|---|
| `сироп роз` | `Сироп Роза` | candidate; auto-select только при unique safe gap |
| `филе форели` | `Форель филе` | inflection/order candidate and safe equivalence |
| `сыропроза` | `Сироп Роза` | ASR candidate, not generic word match |
| exact catalog title | same title | auto-select if no competing row |
| exact range/variant | same range/variant | numeric compatibility may permit selection |

## 13. Natural regression matrix

| # | Query / catalog | Expected top | Auto-select / decision | Причина |
|---:|---|---|---|---|
| 1 | `пармезан` / `Пармезан` | exact row | yes if unique | exact one-word product |
| 2 | `сыр` / `Сыр Пармезан`, `Сыр Гауда` | both | no, ambiguous | broad category |
| 3 | `сироп роза` / exact + tarhun | exact rose | yes if gap safe | exact multiword |
| 4 | `филе форели` / `Форель филе` | same product | yes if unique | order/inflection |
| 5 | `сироп рза` / `Сироп Роза` | rose | no or AI pending | short typo lacks full certainty |
| 6 | `сыропроза` / `Сироп Роза` | rose | no direct silent choice | merged ASR |
| 7 | `рза` / `Роза` | rose suggestion | no automatic proof | short typo alone |
| 8 | `икс` / any nearby product | none/clarify | no | unrelated fuzzy word |
| 9 | `горчица CHATEL Франция` / no Франция | Chatel suggestion | no | unknown user term preserved |
| 10 | `уши копчёные` / fresh | fresh row maybe shown | no | contradictory qualifier |
| 11 | `сливки 33%` / `сливки 10%` | 10% may rank | no | percentage mismatch |
| 12 | `сливки` / `сливки 33%` | 33% suggestion | no unless exact contract | missing variant |
| 13 | `форель 0.9–1.3 кг` / `1.5–2 кг` | possible base row | no | range mismatch |
| 14 | same range / same range | exact variant | yes if unique | numeric match |
| 15 | one candidate weakly related | weak row | no | single-candidate trap |
| 16 | two near-equal candidates | top two | no | insufficient gap |
| 17 | exact + weak distractor | exact row | yes only with clear gap | top-vs-runner-up |
| 18 | catalog missing requested variant | base suggestion | no | catalog absence is not user comment |

## 14. Expected implementation footprint

Если отдельный functional fix будет доказан regression test, изменять сначала
`src/restaurant_bot/services/matching.py`; `catalog_resolver.py` — только если
координатор decision contract не позволяет выразить gate в existing helper.
`engine.py` допускается только для минимального call-site adjustment. Parser,
prompts, Block D sanitizers и AI contract не менять.

Не создавать `matcher/`, `candidate_policy/`, `evidence_engine/`, не добавлять
embeddings, vector DB, внешнюю fuzzy library или большой русский blacklist.

## 15. Out of scope: Blocks F/G and docs

- Block F: visible-action/global-intent precedence.
- Block G: PHOTO prompt, voice recovery cards и UX contract drift.
- Existing shadow-item/quantity/source-evidence failures: отдельные Block B/C
  follow-ups, не evidence/matcher bugs.
- Current stale scenario catalog: отдельная документационная задача.

PHOTO parsing вне Block E, но если PHOTO и TEXT дают одинаковый `product_query`,
matching semantics должны быть одинаковыми.

## 16. Success criteria

До отдельного implementation approval необходимо иметь:

1. exact call graph и helper inventory из этого документа;
2. `BLOCK E MUST FIX = 0`, пока нет доказанного matcher-first failure;
3. green contracts и false-positive/true-positive matrix;
4. regression tests для candidate admission, ranking и auto-select отдельно;
5. deterministic gates перед AI pending;
6. отсутствие mutation source/comment/quantity/unit/supplier provenance;
7. matching imports не зависят от engine/orchestrator;
8. focused matching suite + full baseline после любой будущей правки.

Следующее действие: отдельно утвердить этот план. До утверждения functional
implementation Block E не начинать.
