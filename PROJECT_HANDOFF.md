# PROJECT HANDOFF

## PHOTO-GEOMETRY-14 - current corrective pass

### Root cause

Prompt-only changes were insufficient: the vision response was structurally valid but
could omit blank rows and bind a quantity to the neighboring spreadsheet row. The
runtime also sent two representations of the same dense screenshot and used a vertical
crop for taller images. The latest live runs showed nondeterministic results for the same
445px screenshot: one run returned ten rows but marked the order area incomplete, and the
next returned zero rows. A separate live run assigned the first quantity to the preceding
`Тестовый товар` row instead of `Васаби`. The catalog then correctly resolved the wrong
observation; it was not the source of the error. The duplicate per-request row-filtering
instruction is removed, and the runtime now uses one canonical reading view so the
first and last visible rows remain in the same coordinate system.

The latest correction keeps one coordinate system but narrows a clearly recognized
Google Sheets viewport to the product/order/comment area, excluding summary columns that
consume image width. Ambiguous layouts keep the full frame, so tables without Hall/Bar/Kitchen
columns and ordinary photos are not cropped by assumption.

### Image preparation

For dense wide images (`width >= 900`, `height >= 200`, aspect ratio `>= 1.25`) the
runtime sends one `table_focus` view. When a Google Sheets-like header is reliably visible,
the view starts at that table header and ends after the warm-colored order/comment area,
before the green summary columns. This is a visual-layout reduction only: it does not read
text, infer quantities or change row coordinates. If the header pattern is absent or
ambiguous, the complete original frame is retained, which covers ordinary photos, printed
forms and tables without Hall/Bar/Kitchen columns. The selected view is enlarged 2x normally,
3x when its source height is below 700px, and 5x when a very short wide screenshot leaves
less than 320px for the table rows. This keeps small-font screenshots readable without OCR,
perspective correction or a second AI pass.

### Contracts and boundaries

- `responses.parse()` remains exactly one call per photo.
- `PhotoDocumentObservation`, same-row authorization, department quantities,
  incomplete-photo handling and trusted venue identity remain unchanged.
- For a table with order quantities, the observation contract now requires a contiguous
  row range from the first visible product row through the last row with an order cell;
  blank rows inside that range must be returned with their own `visual_row_index`.
- Dense-photo input no longer adds a conflicting row-filtering instruction and explicitly
  identifies the single canonical reading view as the geometry source. It requires row
  bands to be established from top to bottom before any quantity is attached, so a first
  filled cell cannot be assigned to a preceding test row.
- The photo contract explicitly covers paper photos, direct table screenshots and screenshots
  containing a chat or browser around the document. It requires the largest readable document
  region to be used and the surrounding interface/preview to be ignored.
- `crossed_out_quantity_text` is never summed with `corrected_quantity_text`; the handwritten
  replacement is the only active quantity. `printed_reference_text`, packaging, stock, price
  and product-name measurements cannot authorize an order quantity. The same observation
  contract supports `order_table` and `free_list` documents without department columns.
- Header aliases such as `order_quantity`, `actual_order_quantity`, `ordered_quantity` and
  `quantity_decimal` are recognized as order-column evidence, so a short screenshot is not
  downgraded to `product_card` only because the model used a technical column name.
- A client sheet row with generic order evidence but no Hall/Bar/Kitchen quantity now
  returns `incomplete_photo_read` with reason
  `client_sheet_quantity_without_department`; the backend never infers Kitchen.
- The model and catalog behavior are unchanged.
- Added dependency: `Pillow` only; OpenCV and external OCR were not added.
- `photo_image_views_prepared` logs dimensions, table-focus size, view count, upscale
  factor and the dense-table flag; image bytes are not logged.

### Verification

- Targeted photo/view/OpenAI request and ingestion guard tests: passed, including guarded
  Sheets-header cropping and full-frame fallback for non-Sheets images.
- Full pytest with a fresh basetemp: all tests passed.
- Ruff, format check, mypy for changed modules, `python scripts/build_agent_context.py`
  and `git diff --check`: passed.
- The new regression checks pass for very short `1280x210` screenshots, technical order-column
  aliases, screenshot prompt handling, handwritten corrections, free lists and packaging
  provenance. Full pytest passes `1611` tests.
- Docker Compose rebuild completed after the adaptive short-screenshot changes. API and
  worker are healthy, beat is running, and Postgres/Redis are healthy.
- Manual real-image acceptance: `NOT VERIFIED` here — five clean-screenshot runs and
  three monitor-photo runs still need to be performed in Telegram. Unit tests prove only
  deterministic view preparation and one-call request composition, not vision accuracy.
- The first post-rebuild Telegram check (`update_id=247308064`) arrived as `1280x680`
  and used the previous original-only path, returning the right quantities but
  misidentifying the first product. The next check (`update_id=247308072`) arrived as a
  short `1280x350` crop and also used the previous original-only path. The current
  preparation sends a full-height table-focus view for this screenshot class.
- The latest old-build checks (`update_id=247308101` and `247308102`) used
  `view_count=2` on the same 1280x445 screenshot. The first returned ten rows but
  `order_area_complete=false`; the second returned zero rows. These runs are not
  acceptance evidence for the new one-view preparation because they occurred before the
  current local rebuild.
- The first small-font check on the one-view build (`update_id=247308113`) used
  `1280x508` and therefore only 2x under the old 400px threshold. Vision saw 24 rows
  but returned no rows. The threshold is now 700px so this screenshot class receives
  3x before the next acceptance run.
- The first live check with horizontal Sheets focusing (`update_id=247308128`) used
  `1280x722` and produced one `2493x1482` view, but Vision still returned zero rows.
  This confirmed that the remaining loss was caused by the empty lower part of the
  screenshot, not only by summary-column width. The reading view now also removes a
  confirmed empty tail after the last dark order/comment content; the latest rebuild
  containing that change still needs a new Telegram acceptance run.
- The first two post-final-rebuild compact screenshot checks succeeded: `update_id=247308132`
  (`1280x446`, one `2544x924` view) and `update_id=247308139` (`1280x475`, one `2553x930`
  view) both returned and admitted all four expected rows: Vasabi, Dijon mustard, domestic
  horseradish and 5kg horseradish. Repeated final-build checks `update_id=247308150` and
  `update_id=247308151` used the same full-screen `1280x719` input and the same single
  `2475x1473` 3x reading view, but Vision returned `visible_product_row_count=24` with
  `returned_row_count=0` both times. The backend then correctly failed closed as
  `unknown`/`no_items_recognized`; the catalog was never given a product to resolve.
  This confirms the remaining gap is dense full-screen Vision recall, not image-preparation
  randomness, catalog matching or row-admission logic.
- The next paired check separated two different inputs: `update_id=247308149` was a
  screenshot of the Telegram conversation itself, with the spreadsheet only as a small
  preview (`1280x210`). Vision returned ten rows but classified the resulting structure as
  `product_card`, so the backend correctly admitted zero items. The direct spreadsheet
  screenshot `update_id=247308156` (`1280x529`) returned and admitted all four expected
  rows with quantities `3, 5, 4, 2`. This is not a recognition regression: the bot must
  receive the original table image, not a Telegram chat screenshot containing a thumbnail.
- Older live runs before this corrective pass prepared multiple views and had inconsistent
  department-field ownership; the backend correctly dropped those rows rather than
  inventing a department. The required repeated acceptance runs on the rebuilt one-view
  image path remain pending. The new reading-view detector also needs a real Telegram
  check: a Sheets screenshot with summary columns should log a smaller table-focus width,
  while a photo without recognized headers should retain the full-frame dimensions.

### Latest text-search evidence

- `update_id=247308190` entered the OpenAI text path because the mixed product line did not
  satisfy the deterministic-list contract. The model returned the first product with quantity
  `5 кг`, but the catalog produced two equally strong candidates (`100.0` vs `100.0`), so the
  safety gate correctly blocked automatic selection and requested clarification.
- For the second product the model returned `10 шт`, but its `source_line`/`source_span` ended
  at the product text and omitted the trailing `10 штук`. Quantity reconciliation therefore
  found no authorized order fact and rejected the value, while correctly preserving `(12/1)` as
  packaging. This was an AI-to-source-evidence contract defect, not random catalog search.
- The parser boundary now derives a local `source_span` from the ordered product anchors when
  several AI items contain shortened source lines. It extends a provided local line only through
  a proven order-quantity suffix; arbitrary trailing comments are not absorbed. Packaging-range
  recovery uses the same local span and never falls back to the full multi-item message for a
  specific item. The strict reconciliation and the catalog ambiguity gate remain fail-closed.
- Regression coverage includes the exact mixed line from `update_id=247308190`: the second item
  keeps `10 штук`, `(12/1)` remains `catalog_attribute`, and the first item receives no leaked
  packaging. Full verification after the fix: `1612` tests passed, mypy passed for `166` files,
  and Ruff check/format passed. The equal-score catalog tie remains intentionally blocked and
  must be resolved by clarification or stronger source/catalog evidence.

## PHOTO-PROMPT-RESET-13 - current corrective pass

### Что изменено

Активный photo prompt и observation contract переведены на единый русский контракт
`PhotoDocumentObservation`. Vision теперь сначала ищет заполненные ячейки «Зал» / «Бар» /
«Кухня», привязывает их горизонтально к товару в той же строке и возвращает evidence,
а не готовую доменную заявку. Пустые строки каталога можно не перечислять; потенциально
заполненная, но непривязанная ячейка должна отмечаться через
`order_area_complete=false` и `uncertain_order_row_count`.

Убраны из активного prompt противоречивые требования одновременно перечислять все пустые
строки и пропускать строки без количества, а также устаревшее требование выводить
`intent=add_items` и `items`. Сохранены same-row ownership, раздельные Hall/Bar/Kitchen,
печатная фасовка как reference, рукописные исправления и row-local comments.

Добавлена узкая backend-защита для валидной структуры client sheet без возвращённых строк:
если есть заголовки подразделений, виден регион товаров, `rows=[]` и
`order_area_complete=false`, результат становится `incomplete_photo_read`, а не
`unsupported_photo`.

### Границы

- `OPENAI_VISION_MODEL` не менялся; используется прежняя модель `gpt-5-mini`.
- Vision-вызов остаётся один на фотографию.
- Text, voice, generic catalog matching, submission и Docker/DevOps-контракт не менялись.
- Реальные Telegram-фото, внешняя отправка, webhook, tunnel и production deploy не
  выполнялись.

### Проверка текущего прохода

- Full pytest: `1611 passed`.
- Targeted prompt и photo-ingestion tests: `28 passed`.
- Ruff check, changed-file Ruff format check, mypy, compileall и `git diff --check`:
  проходят.
- Добавлен регресс пустого observation при нечитаемой order-area.
- Локальная Compose-пересборка выполняется после commit и push.

## PHOTO-COMPLETENESS-CORRECTIVE-11 - current corrective pass

### Root cause

The previous gate in `input/photo_ingestion.py` rejected the entire observation when `scan_complete` was false or when `visible_product_row_count != len(rows)`. That conflated imperfect transcription of blank/reference rows with loss of order information and produced false `incomplete_photo_read` replies for dense venue-sheet screenshots.

### New integrity rule

`photo_order_area_integrity()` is now the single deterministic completeness decision. A count mismatch is diagnostic only and is accepted with `blank_row_count_mismatch_irrelevant` when order-area evidence is complete. The legacy `scan_complete` field remains compatible but is not fatal by itself. Fatal `incomplete_photo_read` is reserved for `order_area_complete=false`, a positive `uncertain_order_row_count`, or an actually truncated/incomplete structured OpenAI response. This preserves same-row ownership, department-column ownership, and fail-closed handling of potentially missed filled rows.

The prompt now asks vision to prioritize Hall/Bar/Kitchen cells and their product-row alignment. It may omit blank rows; it must mark the order area incomplete when a potentially filled quantity cannot be trusted or bound to a product row. The backend still performs final row admission and does not let vision create order items directly.

### Preserved contracts

- The trusted venue identity priority remains row number plus lexical sanity, canonical exact identity, conservative OCR-tolerant unique identity, then the existing catalog fallback. The horseradish OCR case remains covered.
- Hall/Bar/Kitchen quantities remain separate through `PhotoObservation -> ExtractedItem -> CartItem -> submission mapping`; single-department and combined-department regressions are covered.
- Generic catalog scoring, retrieval, safety, thresholds, text parsing, voice parsing, comments, handwriting/corrections, and supplier behavior were not changed.
- Vision calls remain one per photo before and after this pass; no retry or OCR call was added. `OPENAI_VISION_MODEL` was not changed.

### Verification

- Full pytest: `1608 passed`.
- Targeted photo integrity, prompt, worker-flow, and submission mapping tests: passed.
- `mypy src`: `Success: no issues found in 165 source files`.
- Ruff check, changed-file Ruff format check, compileall, and `git diff --check`: passed.
- Scenario catalog: 41 scenarios; Markdown links: 38 files.
- New regressions cover dense count mismatch with filled rows, omitted blank rows, imperfect non-order transcription, uncertain filled row, cropped order area, empty complete table, four-row mismatch, and Hall/Bar/Kitchen department round-trip.

### Files changed

`src/restaurant_bot/parsing/ai/schemas.py`, `src/restaurant_bot/input/photo_ingestion.py`, `src/restaurant_bot/integrations/openai_prompts.py`, `src/restaurant_bot/integrations/openai_client.py`, `tests/ai/test_photo_ingestion.py`, `tests/ai/test_photo_prompt_contract.py`, and this handoff.

No real Telegram photo, external order submission, webhook, tunnel, production deployment, or secret was used. The requested final action is a local Docker Compose rebuild after the commit is pushed.

## PHOTO-RUNTIME-CORRECTIVE-10 - current corrective pass

### Confirmed root cause and ownership

The photo vision contract previously encouraged the model to omit rows with empty order quantities. That made an incomplete or truncated table look like a valid no-quantity photo, and left no deterministic signal for a distinct user response. The runtime now keeps observation separate from admission: vision returns every reliably visible product row, while `input/photo_ingestion.py` authorizes only same-row positive department quantities and preserves existing comment, correction, packaging, and quantity-provenance rules.

`PhotoDocumentObservation` now carries `visible_product_row_count`, `scan_complete`, and `scan_warning`. A row-count mismatch or an explicit incomplete scan fails closed as `photo_outcome=incomplete_photo_read`; an unsupported/product-card or genuinely empty order remains a safe no-quantity outcome. The Telegram reply asks the user to resend an image that can be read clearly instead of claiming that no quantities were found.

For an already authorized `client_order_sheet`, catalog identity resolution has a PHOTO-only trusted priority: reliable visible `sheet_row_number`, unique canonical identity, then a unique OCR-tolerant identity with measurement noise ignored only when strong non-measurement anchors are sufficient. Ambiguous or unsafe matches return to the existing catalog pipeline; text and voice do not receive this privilege. Current venue catalog scope, fuzzy scoring, retrieval, thresholds, supplier matching, and submission behavior remain unchanged. Hall/Bar/Kitchen department quantities still round-trip into submission rows independently.

### Verification

- Full pytest: `1603 passed`.
- Targeted PHOTO, worker, media, engine, and submission tests: passed.
- `mypy src`: `Success: no issues found in 165 source files`.
- `ruff check src tests`, changed-file `ruff format --check`, `compileall`, and `git diff --check`: passed.
- Scenario catalog check: 41 scenarios; Markdown links check: 38 files.
- Added regressions cover incomplete row counts, explicit incomplete vision output, distinct user reply, OCR-tolerant horseradish identity, live four-row admission, duplicate size variants, row-number lexical sanity, and department round-trip.

### Operational boundary

No real Telegram photo, external order submission, webhook, tunnel, production deployment, or secret was used. The requested final action is a local Docker Compose rebuild after the commit is pushed. A real-image Telegram smoke test remains an external manual check.

## PHOTO-PROD-HARDENING-09 — текущий corrective pass

### Корневая причина

До этого этапа один vision-вызов заполнял generic `ParsedInputSchema` доменными `ExtractedItem`, а `_normalise_photo_command` уже после ответа пытался определить тип документа, разрешить quantity и отделить фасовку от заказа. Поэтому наблюдение модели смешивалось с авторизацией домена: quantity мог быть перенесён из соседней строки, proposal `document_type` мог применяться без структурного подтверждения, а reference/packaging числа могли попасть в заказ.

### Новый ownership

- `OpenAIService.parse_photo` получает один `PhotoDocumentObservation` и не создаёт готовые позиции из ответа vision.
- `input/photo_ingestion.py` владеет классификацией документа, row alignment, quantity authorization, correction policy и row-local comments.
- `CatalogResolutionService` владеет только unique exact venue-table identity fast path и затем использует прежний fuzzy/catalog pipeline.
- `ConversationEngine` и `build_cart_item` остаются владельцами мутации корзины и доменного draft.

Для `client_order_sheet` строка допускается только при положительном `hall`/`bar`/`kitchen` в той же строке; department values сохраняются в `DepartmentQuantities`. Для обычной таблицы и free-list используется только same-row explicit order evidence, а отдел берётся из `default_department`.

Correction policy: `corrected_quantity_text` заменяет crossed-out value; crossed-out без замены и два active values без correction relationship отклоняются. Reference, price, stock и packaging не являются order quantity.

Row comments принимаются только из row-local `explicit_marker`/`user_note`. Document comment становится global только при `document_comment_scope=order` и явной фразе общего охвата; detached note не broadcast-ится.

### Exact venue fast path

Для уже authorized `client_order_sheet` строки сохраняется typed `catalog_identity_provenance=venue_table_exact_candidate`. В текущем venue catalog строится безопасное canonical identity сравнение: case-fold, `ё/е`, кавычки, пробелы вокруг безопасной пунктуации и canonical unit spelling. Значимые слова, brand, country, packaging и числа не удаляются и не исправляются эвристически.

Ровно одна canonical full-identity строка выбирается напрямую через существующий `apply_catalog`; duplicate exact identity и не доказанное совпадение возвращаются в существующий catalog search/resolution без изменения его ranking или thresholds. Blank rows отбрасываются до catalog search. Cross-venue search не добавлялся: используется только catalog текущего `state.spreadsheet_id`.

### AI и production safety

До и после остаётся один vision request на фото, без OCR, второго vision запроса и внешнего OCR-сервиса. Structured response truncation/empty output закрывается fail-closed. Добавлены компактные события `photo_document_classified`, `photo_row_admission_decision` и `photo_exact_catalog_identity`; image bytes и base64 не логируются.

Изменения не затрагивают text/voice parsing, quantity architecture, supplier matching, catalog scoring/retrieval/safety, thresholds, duplicate logic, submission, persistence, callbacks, Docker или secrets.

### Проверки на текущем этапе

Добавлены schema, classification, row ownership, handwritten/correction, comments, product-card, exact identity и mocked one-call end-to-end regressions в `tests/ai/test_photo_ingestion.py`; существующие photo tests сохранены. Точечный тест новых правил: 9 passed; полный suite: 1594 passed. `ruff check`, `mypy src` (165 файлов), `compileall`, сценарный каталог и markdown links проходят. Реальные фотографии Telegram не запускались; требуется manual runtime checklist из задания PHOTO-PROD-HARDENING-09.

## COMMENT-OWNERSHIP-06 — исторический corrective pass

Подтверждённые изменения ограничены семантическим admission и маршрутизацией.
Детерминированный product parser сохраняет один товарный anchor для длинных
каталожных строк, разделяет только подтверждённые списки и не принимает
атрибутную фасовку без товарного anchor. Decimal comma остаётся частью числа.
Для многословной бессмысленной строки без каталожного anchor resolver оставляет
позицию в `AMBIGUOUS`, не выполняя подстановку товара; односоставные неизвестные
товары сохраняют прежний `NOT_FOUND`-сценарий.

Явные глобальные комментарии получают область заявки, а управляющий префикс не
попадает в payload. История и общий статус заявок классифицируются до разбора
товара; product-specific history и текущая корзина сохраняют свои маршруты.
Удаление позиции возвращает общий заголовок черновика и вторичное уведомление.
Изменения не добавляют AI-вызовов, не меняют persistence, callbacks, Docker,
Alembic или transport.

Проверено: комментарий разрешается после intake по стабильным `CartItem.id`, а
неоднозначное ownership сохраняется отдельно от товарных позиций. Полный suite,
mypy, Ruff, compileall, сценарный каталог и `git diff --check` проходят.
Исторический этап COMMENT-OWNERSHIP-06 находится в коммите `4c65f4c`; `.env` не отслеживается и не читался.

Актуально для ветки `decompose_bot` после исправления границы канонического
поискового запроса, source evidence и product-line boundary 2026-08-17. Git SHA
текущей версии документа
нужно получать командой `git rev-parse HEAD`.

## Предыдущая задача

COMMENT-OWNERSHIP-06 завершена в текущем checkout.

Корневая причина: `ConversationEngine` обрабатывал `command.comment_clarification`
до ветки `ADD_ITEMS`, поэтому AI-неуверенность превращалась в пользовательский
вопрос, а товары оставались в `pending_comment_items`. Теперь intake сначала
создаёт и разрешает позиции, затем `resolve_comment_ownership` принимает одно
решение по стабильным `CartItem.id`: `item`, `group`, `order`, `ambiguous` или
`rejected`.

Одна новая позиция получает локальный комментарий автоматически даже при старых
товарах в корзине. Явная группа и глобальная область применяются без вопроса;
отдельная фраза с несколькими реальными кандидатами переводится в clarification.
Если товар ещё требует catalog/quantity/product issue, этот issue показывается
первым, а комментарий сохраняется и не вызывает повторного добавления товаров.
Ответ на scope работает по сохранённым стабильным ID и не переигрывает `ADD_ITEMS`.
ORDER_QUANTITY и catalog facts не допускаются как supplier comment.

Поиск каталога, ranking/fuzzy matching, product boundary и quantity provenance в
этом проходе не изменялись. Обновлены только ссылки сценария на переименованный
регрессионный тест и производные `docs/USER_SCENARIOS.md`/HTML.

Проверено на live-сценарии: `Горчица ... 5 штук. Привезти завтра до 8 вечера.`
создаёт одну позицию с количеством `5 шт` и комментарием доставки без
`AWAIT_COMMENT_SCOPE`; сценарий с двумя товарами и detached-комментарием сначала
создаёт обе позиции, затем задаёт вопрос области.

PRODUCTION-HARDENING-04 завершён в текущем незакоммиченном checkout; ниже
сохранены исторические записи предыдущих этапов.

PRODUCT-BOUNDARY-02 завершён: сохранён канонический
`product_query` главным запросом retrieval, использовать `source_line` только как
вторичное доказательство и не заменять чистое название товара разговорной
оболочкой или пунктуацией. После этого перейти только к подготовке
контролируемого Telegram pilot. Новую общую декомпозицию и production refactor не
начинать.

Подробный verdict и scores:
[`docs/INDEPENDENT_ENGINEERING_AUDIT.md`](docs/INDEPENDENT_ENGINEERING_AUDIT.md).

## Подтверждённые факты

- Ветка: `decompose_bot`.
- Remote: только GitHub `origin` → `Dzmitry-Radziuk/test_bot`.
- Полный suite после PRODUCT-BOUNDARY-01: `1533 passed` (без падений; запуск с
  локальным `--basetemp`, один предупреждающий `PytestCacheWarning` не связан с
  приложением).
- После corrective-pass сохранения search semantics: `1537 passed`; добавлены
  regression-тесты для простого retrieval, числового source evidence и чистого
  product query.
- PRODUCT-BOUNDARY-02: детерминированный parser больше не принимает числовые
  признаки длинного каталожного названия и его хвост фасовки за новую позицию.
  Упаковочные связки распознаются независимо от положения внутри строки,
  явное конечное количество сохраняется у одной позиции, а простые запятые
  разделяют только структурно независимые товарные части. Аббревиатуры с точками,
  каталожные размеры, фасовка, страна и повторные числовые признаки не становятся
  товарами или комментариями. Подтверждены текстовый/голосовой общий parser,
  списки `лук, картошка, морковь` и `молоко, хлеб, яйца`, а также два независимых
  заказа одного товара. Полный suite после этапа: `1542 passed`; изменены только
  product-line parsing и regression-тесты.
- Mypy: `159 source files, no issues`.
- Ruff check: pass; Ruff format: `316 files already formatted`.
- Compileall и `git diff --check`: pass.
- После refresh Markdown checker проверил 38 файлов.
- Scenario catalog вырос до 41 содержательного сценария; все mappings
  проверяются официальным generator.
- `.env` не tracked; значения не читались.
- Repository scan не нашёл строк, похожих на Telegram/OpenAI keys.
- Изменения текущего этапа ограничены venue-level маршрутизацией истории,
  channel-neutral ответом и связанными regression-тестами; схема БД, callbacks,
  отправка заявок и поставщики не менялись.
- Для прерванных modal-вопросов состояние хранит стек контекстов: новый
  независимый товар получает приоритет, вложенные прерывания возвращаются в порядке
  `C → B → A`, а товары одного сообщения сохраняют исходный порядок. Черновик и
  порядок позиций не переставляются.
- При возобновлении вопроса Telegram показывает реальное число активных
  нерешённых позиций с правильной русской формой слова и текущим выделенным
  названием; для проблем, не связанных с количеством, вопрос о количестве не
  добавляется.
- Граница товара и комментария уточняется по однозначному совпадению с активным
  черновиком. Неоднозначная граница приводит к безопасному уточнению; текст и
  голос используют один и тот же результат.
- Декоративные emoji удаляются только из подписей Telegram-кнопок; callback-данные,
  порядок строк и разрешённые стрелки не меняются. Мягкие предупреждения используют
  `🔸`, а `⛔` сохранён только для действительно блокирующего отказа в доступе.
- Telegram UX polish: названия товаров в карточках и списках экранируются и
  выделяются жирным, первые заголовки карточек — жирным подчёркиванием; порядок
  кнопок, callback-протокол, state-machine, база данных и семантика отправки не
  изменены.
- Для текста добавлен короткий промежуточный статус перед разбором; голос, фото и
  callback используют специализированные статусы. Callback сначала подтверждается
  и получает отключённую клавиатуру, после чего временная карточка заменяется
  результатом. Существующие revision/idempotency/lease-проверки сохранены.
- Добавлены channel-neutral contracts для вопросов о поставках: детерминированный
  parser распознаёт высокоуверенные русские формулировки, свободные варианты
  проходят существующий структурированный AI fallback через `ParsedInputSchema`,
  а text и voice используют один маршрут. Запрос истории не превращается в
  ADD_ITEMS и не меняет modal-контекст; местоименный товар принимается только при
  единственном безопасном товарном контексте.
- Источник истории строго ограничен листом Google Sheets с точным именем
  `История`. `GoogleHistoryRepository` выполняет один venue-scoped read за запрос;
  каталог, черновик, база данных и лист `История товары(API)` не используются.
- Историческая строка сохраняет evidence заявки, поставщика, стадии и даты для
  каждого товара из поля `Список товаров`. Для вопроса о том, приехал ли товар,
  остаются видимыми доставленные и завершённые строки; для будущей даты они
  исключаются. Стадия `Доставлено` классифицируется отдельно от `Завершена`,
  отмена остаётся доступной для текущего статуса, а неизвестные стадии не
  угадываются. Год в дате без года берётся из переданных часов приложения и его
  часового пояса.
- Запросы истории обслуживаются `HistoryQueryService` и отдельным Telegram
  presenter; существующая команда «Мои заявки» и её путь не заменены.
- LIVE-HISTORY-03A: в обычном интерфейсе «Мои заявки» технический номер заявки
  скрыт в списке, кнопке и заголовке деталей; вместо него показывается дата
  создания. История и статусы заявок используют общий человекочитаемый formatter,
  прямой ответ по истории начинается с товара, а прошедшая плановая дата явно
  помечается как прошедшая без вывода о фактической доставке. Внутренний поиск по
  номеру заявки, callback-протокол и исходные доказательства не изменены.
- LIVE-HISTORY-04: общие вопросы о поставках текущего заведения распознаются как
  `VENUE_DELIVERIES` с пустым `product_queries`, не попадают в каталог или not-found
  и не изменяют черновик. Формулировка с человеком или ролью не создаёт товар или
  поставщика; ответ показывает только доказанные строки листа «История» и отдельно
  сообщает, если источник не подтверждает, кто физически привезёт поставку.
- PROD-SEMANTICS-01: явные комментарии существующего товара распознаются как
  `EDIT_COMMENT` без обращения к каталогу; общая конструкция «товар и товар всё по
  N единиц» создаёт отдельные позиции с общей quantity и хвостовым пожеланием;
  ответы области комментария для текста и голоса проходят детерминированную
  проверку до AI; отмена уточнения сохраняет ожидающие товары и продолжает их
  добавление; `clear_all` удаляет пользовательские комментарии активных позиций,
  сохраняя каталожные данные; bare quantity разрешён только в modal
  `AWAIT_UNIT_QUANTITY`, а выбор кандидата остаётся отдельным контекстом.
- После PROD-SEMANTICS-01 полный suite: `1477 passed`; mypy, Ruff, форматирование,
  Markdown links, каталог сценариев, compileall и `git diff --check` прошли.
- PRODUCT-BOUNDARY-01: запятая больше не считается границей товара без отдельного
  доказательства; цепочки `число единица/число единица` и `число единица/кор` остаются
  признаками каталога. Полная исходная строка сохраняется в `source_line`, а каталог
  участвует в окончательной сверке количества и комментария. Неподтверждённое
  `quantity_source` не авторизует заказ автоматически; независимые photo/order-entry
  источники сохраняются. Добавлены шесть regression-тестов для pepper, mustard,
  повторяющихся чисел и смешанных комментариев.
- После коммита PRODUCT-BOUNDARY-01 выполнен `docker compose up -d --build`:
  `migrate` завершился с кодом 0, `api` и `worker` имеют состояние healthy,
  `beat` запущен, PostgreSQL и Redis healthy. Объёмы и Docker-конфигурация не
  изменялись.
- В corrective-pass `CatalogResolutionService` передаёт в `CatalogSearch` только
  канонический `source_query`; числовые признаки `source_line` применяются после
  поиска для проверки совместимых вариантов. Независимые photo/handwritten
  источники и детерминированное количество не превращаются в каталожные числа.
- Сверка AI не заменяет `product_query="лук"` на `лук.` или «Мне нужен лук»:
  семантическое сравнение игнорирует внешнюю пунктуацию и вежливую оболочку.

Финальные post-doc проверки зелёные. Documentation impact checker после commit
также прошёл; код истории сопровождается обновлённым каталогом сценариев.

## Что сейчас работает

- Telegram webhook authentication и durable inbox deduplication.
- Same-chat renewable lease, update sequencing и stale-owner fencing.
- Общий text/transcribed-voice semantic pipeline.
- Единая `StateCompatibilityPolicy` перед contextual modal fallback.
- Quantity, unit mismatch, ambiguous candidate, not-found, duplicate, comment,
  manual/product-add, review, submit, failure и new-order modal contexts.
- Source-evidence reconciliation для product identity, order quantity, packaging и
  comments.
- Удаление комментариев всей заявки одинаково распознаётся из текста и голосовой
  транскрипции, очищает только комментарии активных позиций и сохраняет товары и
  связи с каталогом.
- COMMENT-01: локальные и общие комментарии разделены через provenance
  `CartItem.order_comment_fragments`. Удаление общих комментариев удаляет только
  подтверждённые общие фрагменты и сохраняет локальные пожелания; старые состояния
  без provenance обрабатываются без разрушительных догадок. Явная цель товара не
  может незаметно превратиться в общий комментарий, а операции комментариев всегда
  возвращают обычный `Черновик заявки` с кнопками.
- ROUTING-01: слова о доставке сами по себе не определяют `history_query`.
  История требует семантики вопроса о существующей поставке; доказанное добавление
  товара и явное изменение комментария имеют приоритет. Даты и слова «привезти»/
  «доставить» в пожелании являются данными команды, а не самостоятельным intent.
  Это правило подтверждено для текста и транскрипции голоса; покрытие всех
  возможных разговорных формулировок русского языка не заявляется.
- Новые regression-тесты покрывают вложенные прерывания, динамическое количество
  уточнений, границу комментария для текста/голоса и очистку подписей кнопок.
- Conservative catalog shortlist и deterministic auto-select safety gates.
- Venue isolation и повторная проверка доступа до внешних effects.
- Submission lifecycle с read-back, recalc/dispatch uncertainty и at-most-once
  completion notification gate.
- Neutral conversation input/action contracts и architecture guards.

## Что не доказано или требует среды

- Реальный Telegram/ASR/vision/Google/Redis/PostgreSQL fault run в этой кампании не
  выполнялся.
- Hosted CI не проверялся.
- Production performance на 100 000 товаров не измерена; тест подтверждает только
  bounded `CatalogSearch` interface.
- DEPLOY-01 подтверждён локально 2026-08-14: `alembic/env.py` загружает модели
  через `restaurant_bot.persistence.alembic`, а не через отсутствующий
  `restaurant_bot.db_models`. Цепочка ревизий непрерывна до `0008`, `alembic check`
  сообщает об отсутствии новых операций. Существующая БД успешно прошла два
  последовательных `upgrade head`; обычный `docker compose up -d --build`
  завершил `migrate` с кодом 0, после чего `api` и `worker` стали healthy.
  Отдельная временная PostgreSQL без общего volume также прошла два `upgrade head`
  и содержит ровно пять текущих ORM-таблиц. Production и внешние записи не
  проверялись.
- Внешняя supplier dispatch должна оставаться выключенной для pilot.
- Token rotation из незакрытого `SECURITY.md` checklist не подтверждена репозиторием.
- Production alerts, backup/restore и rollback drill описаны, но не доказаны текущим
  запуском.

## Критические архитектурные правила

1. AI предлагает структуру; source text подтверждает факты; catalog уточняет identity;
   deterministic code разрешает действие.
2. State хранит контекст уже понятого сообщения и не определяет смысл следующего.
3. Text и voice проходят global interpretation до contextual fallback.
4. Новый сильный intent может прервать modal flow, не наследуя quantity/comment/
   candidate старого item.
5. Callback остаётся явным UI-путём с проверкой revision.
6. Доступ к venue повторно проверяется перед order/status/product-add effects.
7. Неизвестный результат внешнего POST не повторяется автоматически.
8. `services` содержит защищённые effect/runtime coordinators, а не общий core.
9. Production catalog пока list-backed; перед 100k нужен indexed scoped provider и
   benchmark.
10. Legacy `EngineResult`/`BotReply` bridge сохраняется до реального второго канала.

## PRIORITY ROADMAP — НЕ ПОТЕРЯТЬ

State-machine migration по принципу

```text
TEXT / VOICE
→ GLOBAL PARSING
→ STATE COMPATIBILITY POLICY
→ CONTINUE / INTERRUPT / AMBIGUOUS / REJECT
→ state handler или обычный routing
```

завершена для запланированных modal contexts и защищена regression suite. Следующий
приоритет — не новая policy и не декомпозиция, а эксплуатационное подтверждение
получившейся системы.

## NEXT FUNCTIONAL STEP

**Одна следующая задача: CONTROLLED HUMAN TELEGRAM PILOT PREPARATION.**

До pilot:

1. подтвердить отзыв старого Telegram token и отдельный test bot;
2. подготовить отдельные test venue, PostgreSQL, Redis и Google Sheets;
3. проверить `GOOGLE_ORDER_SUBMISSION_ENABLED=false`;
4. применить migrations и проверить health;
5. пройти live text/voice/photo/callback/Sheets smoke;
6. проверить redaction, `uncertain` events и операторскую остановку.

## ARCHITECTURAL REFACTOR STATUS

Общая декомпозиция остановлена. Следующие долги оплачиваются только по trigger:

- legacy reply bridge — при втором канале;
- indexed catalog backend — перед большим каталогом;
- engine/orchestrator/submission/prompt extraction — при изменении соответствующего
  flow и отдельном characterization proof;
- transactional outbox — перед broad production или при наблюдаемом duplicate reply/
  task delivery.

## Источники истины

- [`README.md`](README.md) — назначение, запуск, owners и runtime.
- [`docs/CURRENT_ARCHITECTURE.md`](docs/CURRENT_ARCHITECTURE.md) — текущие границы.
- [`docs/TESTING_READINESS.md`](docs/TESTING_READINESS.md) — automation/live readiness.
- [`docs/user-scenarios/scenarios.json`](docs/user-scenarios/scenarios.json) —
  канонический каталог сценариев.
- [`docs/INDEPENDENT_ENGINEERING_AUDIT.md`](docs/INDEPENDENT_ENGINEERING_AUDIT.md) —
  независимые scores, gaps и findings.
- `docs/archive/architecture/ARCHITECTURE_DECOMPOSITION.md`,
  `docs/archive/data-integrity/` и остальные block
  reports — исторические snapshots, не текущий roadmap.

## PROD-SEMANTICS-01 — CORRECTIVE BLOCK

Исправлены оставшиеся границы семантики комментариев и количества:

- формы удаления комментария с предлогами «о», «об» и «про» остаются `EDIT_COMMENT`;
- явный общий комментарий без товаров проходит как `EDIT_COMMENT` области `order`, а не как `ADD_ITEMS`;
- область комментария разрешается только среди позиций текущего состояния: поддержаны named, ordinal, all, order и cancel; неоднозначный выбор безопасно приводит к уточнению;
- голосовой транскрипт использует тот же `TelegramInputInterpreter.interpret_text`, что и текст;
- bare quantity в `AWAIT_UNIT_QUANTITY` поддерживает `MISSING_QTY`, `UNIT_MISMATCH` и `DUPLICATE_PENDING`, применяя ожидаемую единицу каталога; явная единица в `UNIT_MISMATCH` сохраняет путь high-accuracy разбора;
- candidate modal по-прежнему владеет числовым выбором вне quantity-modal;
- комментарии, область комментария, история и quantity-modal завершаются до catalog resolution.

Проверка после corrective-block: `1489 passed`, focused semantic/comment/voice набор — `89 passed`, mypy — `158 source files, no issues`, Ruff check и format — pass, Markdown links — `38 files`, scenario catalog — `41 сценарий`, compileall и `git diff --check` — pass.

Изменения ограничены parsing, input interpretation, semantic routing, comment scope и regression tests. Схема базы данных, Alembic, callback protocol и Docker-конфигурация не менялись.

## COMMENT-SCOPE-02 — EXISTING-ITEM COMMENT SCOPE

Исправлен жизненный цикл уточнения комментария для уже существующих позиций:

- активный scope определяется единым channel-neutral `has_pending_comment_scope`;
  обязательны стадия `AWAIT_COMMENT_SCOPE`, текст комментария и хотя бы одна
  действующая цель — активный ID существующей позиции или ожидающая новая позиция;
- пустой `pending_comment_items` больше не закрывает scope, если в состоянии
  сохранены действующие existing item IDs; пропущенные и устаревшие IDs не считаются
  целью;
- ответы `для всех товаров`, named и `только для последнего` применяются к
  существующим товарам без повторного `ADD_ITEMS` и без обращения к каталогу;
  `для всей заявки` сохраняет `order_comment_fragments`, а отмена не меняет корзину;
- смешанный existing + pending-new поток сохраняет прежнее добавление новых позиций:
  локальный групповой комментарий применяется до обычного catalog resolution,
  order provenance не создаётся для групповой области;
- прямые команды комментария разделяют GROUP и ORDER: групповой суффикс удаляется
  в parsing owner и не создаёт order provenance, заявочный суффикс сохраняет
  provenance всей заявки;
- text и voice используют один deterministic scope resolver, поэтому ответы области
  комментария не вызывают дополнительный AI scope call.

После COMMENT-SCOPE-02 полный suite: `1501 passed`; новые regression-тесты — `12`.

## MODAL-AUTHORITY-01 — GENERAL STATE-AWARE MODAL ROUTING

Исправлена граница между предложением глобального parser и авторизацией
действия активным modal-контекстом:

- quantity modal (`MISSING_QTY`, `UNIT_MISMATCH`, `DUPLICATE_PENDING`) сначала
  проверяет строгую форму ответа: короткое число, число с единицей, разговорную
  оболочку и контейнерную единицу. Произвольное числительное внутри фразы больше
  не меняет количество; неполное числительное остаётся безопасным уточнением.
- текст и транскрибированный голос используют один `PendingQuantityHandler`;
  «пять», «пусть будет пять», «три штуки» и «пять коробок» относятся к текущей
  позиции, а явные независимые команды могут прервать modal.
- `SELECT_CANDIDATE` разрешён только при текущем `AMBIGUOUS` item; вне candidate
  context голое число не создаёт выбор кандидата.
- `StateCompatibilityPolicy` принимает единственное решение о `CONTINUE`,
  `INTERRUPT` и `AMBIGUOUS` для quantity/duplicate/unit-mismatch контекстов.
- доказанный независимый ADD_ITEMS в `AWAIT_ADD_MORE_CONFIRM` не теряется из-за
  отсутствия совпавшей visible action; неуверенный no-op не показывает сообщение
  об успешном добавлении.
- provenance каталожной фасовки и количества заказа сохранён: regression для
  названия с `180 г` и отдельного заказа `500 г` проходит.

Проверки после этапа: полный pytest `1527 passed`, mypy `159 source files, no
issues`, Ruff check/format, Markdown links (`38 файлов`), каталог сценариев (`41`),
compileall и `git diff --check` — успешно. Изменены только маршрутизация modal,
quantity provenance/reconciliation и regression-тесты; DB/Alembic/Docker/History
source/callback protocol не менялись, новых AI-вызовов не добавлено.
Mypy, Ruff check/format, Markdown links, каталог сценариев, compileall и
`git diff --check` прошли. База данных, Alembic, Docker, History и callback protocol
в этом этапе не менялись.

## PRODUCT-QUANTITY-PROVENANCE-01 — КОЛИЧЕСТВО ЗАКАЗА И ЧИСЛА КАТАЛОГА

- Единый владелец `reconcile_order_quantity_evidence` отделяет числовые признаки
  identity каталога от подтверждённого количества заказа.
- Размеры, фасовка, диапазоны и дроби из имени товара не создают количество
  `CartItem`; остаётся только остаточное, подтверждённое исходной фразой количество.
- Text и voice сходятся после разбора. Выбор кандидата сначала проверяется в
  текущем списке кандидатов; явное независимое добавление прерывает modal fallback,
  а данные старой позиции не переносятся в новую.
- Подтверждены сценарии горчицы с `450 мл` и `1/6`, а также огурцов с размерами,
  фасовкой и остаточным `5 шт`; ранее доказанное количество и единица сохраняются.
- Регрессионные проверки provenance и candidate routing проходят. Полный suite
  собран на `1510` тестах: `1509 passed`, один известный baseline failure —
  `tests/quantity/test_unit_handling.py::test_packaging_in_name_and_order_weight_are_kept_separate`.
  База данных, Alembic, Docker, History и дополнительные AI-вызовы в этом этапе
  не менялись.

## SEMANTIC-OWNERSHIP-03 — ЕДИНЫЙ КОНТРАКТ ФАКТОВ

Добавлен внутренний semantic-контракт для границ товара, каталожных измерений,
фасовки, количества заказа и комментариев:

- `parsing/semantic/models.py` описывает неизменяемые факты с границами,
  нормализованным значением, уверенностью и происхождением;
- `parsing/semantic/measurements.py` канонизирует измерения и распознаёт
  компактные и разговорные связи фасовки без присвоения количества заказа;
- `parsing/semantic/boundaries.py` строит ссылки на товарные якоря и не считает
  хвост после фасовки самостоятельной позицией;
- `parsing/semantic/gate.py` выполняет последнюю проверку перед черновиком:
  схлопывает AI-фрагменты без источникового якоря, переносит количество только
  через существующую `reconcile_order_quantity_evidence` и удаляет каталожные
  факты из комментариев.

Детерминированный parser и существующий structured-AI контракт сохранены; новых
AI-вызовов, изменений callback, базы данных, Alembic, History и Docker нет.
Добавлены characterization/regression-тесты для длинного каталожного хвоста,
разговорной фасовки, десятичной запятой, границ списков, provenance фактов и
безопасного отбрасывания orphan-фрагментов. Полный suite после изменения:
`1550 passed`.

## SEMANTIC-OWNERSHIP-03B — ЗАКРЫТЫ ГРАНИЦЫ АВТОРИЗАЦИИ

Уточнён существующий semantic gate без добавления второго AI-вызова:

- несколько AI-проекций одного детерминированного якоря схлопываются в одну
  операцию; действительно независимые якоря сохраняются раздельно;
- неизвестное измерение получает нейтральный факт `measurement`, а количество
  заказа требует явного маркера или проверенного остаточного свидетельства;
- разговорные связи фасовки распознаются отдельно от соседнего веса товара и
  остаточного количества заказа;
- явный охват «всем товарам нужно ...» остаётся общим, а слова охвата и
  управляющее «нужно» удаляются из текста пожелания;
- комментарии проходят проверку источниковой семантики, числовые и каталожные
  фрагменты не становятся пожеланием поставщику;
- имя поставщика внутри названия товара не блокирует поиск, а явная связь
  «у поставщика ...» сохраняется как область поставщика.

Добавлены regression-тесты для четырёх форм фасовки макарон, повторных AI-
проекций стейка, общего комментария, коллизии имени поставщика, явной связи
поставщика, числового фрагмента комментария и устойчивой цели комментария.
Полный suite: `1558 passed`;
проверки semantic/AI, catalog/quantity/supplier и input/conversation зелёные.
Mypy, Ruff check/format, Markdown links, каталог сценариев, compileall и
`git diff --check` прошли. База данных, Alembic, Docker, History, callback
protocol и дополнительные AI-вызовы не менялись.
