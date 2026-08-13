# Block 5O/5V — аудит `InputRecognitionService`

## Актуальный owner после Block 5W-A

Fallback между моделями OpenAI не является input-policy: его чистое правило находится
в `integrations/openai_transcription_policy.py`, а `InputRecognitionService` только
координирует media/provider/state-aware flow. Это исправление владельца не меняет
prompt, transcription contract или voice recovery semantics.

## Block 5P — выполненный перенос pure transcript policy

Выбранный в Block 5O seam завершён без изменения алгоритма. Канонический модуль
`input/voice_transcript_policy.py` владеет только `has_supported_voice_letters` и
`select_transcription_result`; зависимости ограничены stdlib `re` и
`text_normalization.normalize_text`. `InputRecognitionService` остаётся смешанным
координатором media/provider/state-aware flow, а `requires_high_accuracy_transcription`,
`voice_transcription_prompt`, visible actions, progress и model fallback не переносились.
Старые class-level и orchestrator test-only wrappers удалены после caller-аудита,
тесты импортируют канонический модуль напрямую. Corpus comparison: `MISMATCHES=0`.
Новый migration seam после Block 5P пока не назначен.

## Block 5V-B — дополнительное controlled extraction

После caller-аудита в `input/voice_policy.py` находятся чистые политики
`match_visible_action`, `voice_transcription_prompt`,
`requires_high_accuracy_transcription` и `has_distinct_models`. Их старые
class-level вызовы сохранены как тонкие compatibility adapters внутри
`InputRecognitionService`; media download/cleanup, OpenAI voice/photo calls,
retry, progress и state orchestration намеренно остались в service. Prompt text,
visible-action contextual semantics и voice fallback contract не менялись.
Сравнение focused voice suite после переноса: `274 passed`.

## Статус и границы

Аудит выполнен на ветке `decompose_bot` при starting SHA
`9c19f784ef88a0721c8be99bb8cece80ae4ebfe4`. Удалённый
`origin/decompose_bot` указывает на тот же SHA. Семантический baseline:
`f9cbc3195c0eae843de3208e488c3f46baa5a5ec`.

В Block 5O не изменялись `src/**/*.py`, `tests/**/*.py`, prompt-контракты,
Docker, CI/CD, `.agents/runtime` и `input/telegram.py`. Старый путь
`services/input_normalizer.py` не возвращается: канонический raw Telegram
adapter остаётся в `input/telegram.py`.

Цель аудита — определить реальные границы ответственности, callers, внешние
эффекты и ровно один следующий code seam. Перенос всего класса в
`input/recognition.py` без разделения ответственности не подтверждён.

## Фактический состав символов

В модуле находятся класс, 12 методов и два module-level символа:

- `InputRecognitionService`;
- `InputRecognitionService.__init__`;
- `recognize_media`;
- `_recognize_voice`;
- `_recognize_photo`;
- `update_processing`;
- `match_visible_action`;
- `has_supported_voice_letters`;
- `voice_transcription_prompt`;
- `select_transcription_result`;
- `requires_high_accuracy_transcription`;
- `has_distinct_transcription_fallback`;
- `has_distinct_models`;
- `_OPENAI_TRANSIENT_ERRORS`;
- `logger`.

Других классов, функций, re-export-ов или скрытого module-level API в файле нет.

## Caller-аудит

| Символ | Production callers | Test callers | Прямой/косвенный вызов | Состояние | Внешний эффект | Канал / provider | Текущая ответственность | Риск |
|---|---|---|---|---|---|---|---|---|
| `InputRecognitionService` | `UpdateOrchestrator.__init__`, ленивый `_recognizer` | Косвенно через `_parse` voice/photo; прямого импорта в tests нет | Прямой constructor в orchestrator, косвенный из workers factory | Не мутирует | Хранит ссылки на клиентов | Telegram и OpenAI | Координатор распознавания media | Средний |
| `__init__` | Два места создания в `services/orchestrator.py` | `object.__new__` обходит constructor | Прямой | Не мутирует | Нет | Telegram/OpenAI | Dependency wiring | Низкий |
| `recognize_media` | `UpdateOrchestrator._parse` для `VOICE` и `PHOTO` | `test_high_accuracy_retry_*`, фото pipeline через `_parse` | Прямой service call из orchestrator | Читает state | Telegram download, удаление временного файла, logging; далее OpenAI | Telegram + OpenAI | Media transport и dispatch к voice/photo | Средний |
| `_recognize_voice` | Только внутренний вызов из `recognize_media` | Косвенно через voice pipeline tests | Внутренний | Читает state, не мутирует | 1–2 вызова OpenAI, logging, parser callback | Voice/OpenAI | Provider call, retry и parse integration | Высокий |
| `_recognize_photo` | Только внутренний вызов из `recognize_media` | `test_photo_pipeline_*` косвенно | Внутренний | Читает event | OpenAI vision и два progress update в Telegram | Photo/OpenAI/Telegram | Photo provider flow с progress | Средний |
| `update_processing` | `_recognize_photo`; orchestrator `_update_processing` делегирует ему | Фото pipeline проверяет результат косвенно | Внутренний плюс thin wrapper | Не мутирует state | `TelegramClient.send_reply`, проглатывание исключения, warning log | Telegram presentation | Редактирование progress card | Средний |
| `match_visible_action` | `requires_high_accuracy_transcription`; orchestrator wrapper и text routing | `test_voice_phrase_without_button_verb_matches_visible_action` | Прямой static call и wrapper | Только читает `visible_actions` | Нет | UI/callback semantics | Контекстное сопоставление visible action | Средний |
| `has_supported_voice_letters` | Voice validation и transcript selection | `test_voice_transcript_rejects_unrelated_script` | Прямой импорт из `input/voice_transcript_policy.py` | Нет | Нет | Voice transcript | Проверка допустимого алфавита | Низкий |
| `voice_transcription_prompt` | Дважды из `_recognize_voice` | Prompt tests через orchestrator wrapper | Прямой static call и wrapper | Читает item/stage/actions | Нет | Voice/OpenAI prompt | Runtime prompt policy | Средний |
| `select_transcription_result` | `_recognize_voice` после retry | Два focused теста через canonical owner | Прямой импорт из `input/voice_transcript_policy.py` | Нет | Нет | Voice transcript | Детерминированный выбор primary/retry | Низкий |
| `requires_high_accuracy_transcription` | `_recognize_voice` | Четыре focused теста через wrapper | Прямой class call и wrapper | Читает state, candidates, actions | Вызывает `infer_intent` и scoring | Voice + conversation/catalog context | Решение о retry | Высокий |
| `has_distinct_transcription_fallback` | `_recognize_voice` | Проверяется через orchestrator wrapper | Прямой instance call | Читает OpenAI settings | Нет | OpenAI provider | Проверка доступности отдельной модели | Низкий |
| `has_distinct_models` | `has_distinct_transcription_fallback`; orchestrator wrapper | Тест same-model/mini fallback | Прямой static call и wrapper | Читает settings | Нет | OpenAI provider | Model capability heuristic | Низкий |
| `_OPENAI_TRANSIENT_ERRORS` | `_recognize_voice` | Ошибки timeout в voice tests косвенно | Прямой exception guard | Нет | Определяет retry/recovery | OpenAI transport | Классификация временных ошибок | Средний |
| `logger` | Все media/voice/photo/progress ветки | Через mock side effects | Внутренний | Нет | Structured logging | Инфраструктурный | Наблюдаемость | Низкий |

### Дополнительные caller-факты

- Единственный production import класса — `services/orchestrator.py`.
- `workers/tasks.py::dependencies()` создаёт `UpdateOrchestrator`; сам
  `InputRecognitionService` создаётся внутри его constructor, а не worker-ом.
- `api/app.py` класс напрямую не создаёт.
- Тесты не импортируют `InputRecognitionService` напрямую. Pure transcript policy
  тестируется прямым импортом из `input/voice_transcript_policy.py`; wrappers
  orchestrator для двух функций удалены. Остальные state-aware wrappers сохраняются
  для prompt, visible actions и high-accuracy policy.
- Voice/photo pipeline tests создают orchestrator через `object.__new__`,
  подменяют `telegram` и `openai`, а `_recognizer()` лениво создаёт service.
- `rg`, AST и поиск строковых путей не нашли subclassing, fixture с отдельным
  service, `importlib`, monkeypatch пути к классу, alias-import или dynamic path.
- `_recognize_voice` и `_recognize_photo` имеют только внутренние callers.

## Constructor и dependency graph

```text
workers/tasks.dependencies()
  -> UpdateOrchestrator(...)
     -> InputRecognitionService(telegram, openai_service)

InputRecognitionService
  -> integrations.telegram.TelegramClient
  -> integrations.openai_client.OpenAIService
  -> domain.TelegramEvent / ConversationState / ParsedCommand / InputKind
  -> domain.ItemStatus / SessionStage / Intent
  -> services.parser.infer_intent
  -> services.parser.parse_quantity_unit
  -> services.text.normalize_unit
  -> conversation.selection.contains_score
  -> text_normalization.normalize_text
  -> structlog
  -> OpenAI transient exception classes
```

`parse_text` передаётся callback-ом из orchestrator и является границей между
распознаванием голоса и глобальным parsing. Service не владеет `ConversationState`
и не сохраняет его; state используется только для prompt, visible actions,
quantity/unit retry и candidate context.

Основные направления внешних вызовов:

```text
recognize_media -> TelegramClient.download_file
recognize_media -> _recognize_voice | _recognize_photo
_recognize_voice -> OpenAIService.transcribe (primary / high_accuracy)
_recognize_voice -> parse_text(transcript, state)
_recognize_photo -> TelegramClient.send_reply через update_processing
_recognize_photo -> OpenAIService.parse_photo
```

Циклов `input -> services.parser -> input`, `input -> conversation -> input`
и `application -> input -> application` в текущем AST-графе нет.

## Матрица побочных эффектов

| Символ | Telegram effect | OpenAI effect | Filesystem effect | State read | State mutation | Parser call | Presentation | Logging | Pure |
|---|---|---|---|---|---|---|---|---|---|
| `recognize_media` | `download_file` | Делегирует voice/photo | Удаляет временный файл в `finally` | Да, через voice | Нет | Косвенно | Нет | Да | Нет |
| `_recognize_voice` | Нет | Transcribe 1–2 раза | Нет | Да | Нет | `parse_text` | Нет | Да | Нет |
| `_recognize_photo` | Два `send_reply` | `parse_photo` | Нет | Event only | Нет | Нет | Progress card | Да | Нет |
| `update_processing` | `send_reply(edit_message_id)` | Нет | Нет | Нет | Нет | Нет | Да | Да | Нет |
| `match_visible_action` | Нет | Нет | Нет | `visible_actions` | Нет | Нет | Читает callback labels | Нет | Да относительно state |
| `has_supported_voice_letters` | Нет | Нет | Нет | Нет | Нет | Нет | Нет | Нет | Да |
| `voice_transcription_prompt` | Нет | Нет | Нет | item/stage/actions | Нет | Нет | Формирует prompt | Нет | Да относительно state |
| `select_transcription_result` | Нет | Нет | Нет | Нет | Нет | Нет | Нет | Нет | Да |
| `requires_high_accuracy_transcription` | Нет | Нет | Нет | item/stage/actions/candidates | Нет | `infer_intent`, `parse_quantity_unit` | Нет | Нет | Нет: decision policy |
| `has_distinct_transcription_fallback` | Нет | Нет | Нет | OpenAI settings | Нет | Нет | Нет | Нет | Да |
| `has_distinct_models` | Нет | Нет | Нет | Settings object | Нет | Нет | Нет | Нет | Да |

## Анализ кластеров

### A. Media transport

`recognize_media` действительно является Telegram-specific adapter: он принимает
`TelegramEvent`, вызывает `TelegramClient.download_file`, выбирает ветку по
`InputKind`, логирует длительность и удаляет созданный временный файл. Это не
provider-neutral core, потому что contract download и cleanup принадлежит
Telegram transport. При этом method одновременно dispatch-ит voice/photo
provider flow, поэтому весь класс нельзя переносить целиком.

### B. Voice provider flow

`_recognize_voice` объединяет primary transcription, transient-error recovery,
high-accuracy retry, выбор результата, alphabet guard и вызов общего parser-а.
Внутри есть как provider adapter, так и deterministic voice policy. Это главный
смешанный кластер и наиболее рискованное место для механического MOVE.

### C. Photo provider flow

`_recognize_photo` вызывает OpenAI vision и возвращает уже готовый
`ParsedCommand`. Его progress updates относятся к Telegram presentation, а
`parse_photo` — к provider adapter. Кластер отделим от voice, но не является
следующим выбранным seam: для него понадобится отдельный progress contract.

### D. Telegram progress presentation

`update_processing` редактирует progress card и проглатывает исключения. В
orchestrator есть похожий, но не идентичный `_send_processing_best_effort`:
первый метод редактирует существующее сообщение и пишет
`telegram_processing_update_failed`, второй отправляет новую карточку и пишет
`telegram_processing_card_failed`. Это близкие Telegram effects, но не доказанный
дубликат; объединять их в Block 5O нельзя.

### E. Visible action matching

`match_visible_action` читает `state.visible_actions`, нормализует label и
пользовательскую фразу, удаляет filler stems, затем применяет exact/subset
matching с порогом `0.72`. Возвращаемый `action_id` сохраняет UI/callback
семантику. Поэтому функция не является чистым catalog или parser primitive и не
должна переноситься в voice transcript policy.

### F. Voice text validation

`has_supported_voice_letters` — чистый predicate по латинскому/кириллическому
алфавиту. Единственная production implementation находится в
`input_recognition.py`. Regex в `parsing/ai/item_reconciliation.py` проверяет
mixed-script tokens и имеет другую цель; это не дубликат.

### G. Transcription prompt policy

`voice_transcription_prompt` зависит от текущего item, его status, stage,
catalog unit, candidates и visible actions. Это runtime voice prompt policy,
а не статический system prompt из `integrations/openai_prompts.py`. Простое
перемещение в `openai_prompts.py` нарушило бы ownership state-aware контекста.

### H. Transcript selection policy

`select_transcription_result` и `has_supported_voice_letters` — единственный
доказанный чистый кластер: они принимают строки, не знают Telegram/OpenAI client,
не меняют state и определяют, можно ли принять retry transcript. Текущие 24
focused теста `tests/input/test_voice_processing_card.py` покрывают placeholder,
неподдерживаемый script, короткий retry и потерю полного списка.

### I. High-accuracy retry policy

`requires_high_accuracy_transcription` не относится к тому же чистому кластеру.
Он зависит от visible actions, текущей позиции, каталожной unit, quantity parser,
`infer_intent` и `contains_score`. Это state-aware decision policy с заметным
behavioral risk; его следует аудировать отдельным блоком после чистого transcript
seam.

### J. Model fallback capability

`has_distinct_models` проверяет настройки OpenAI, одинаковые model names и
специальное правило `mini`; при неполной конфигурации сохраняет conservative
поведение. Это provider-specific capability helper, а не общий input primitive.

## Duplicate и existing-owner audit

| Тема | Текущая реализация | Другие реализации | Решение |
|---|---|---|---|
| Visible action matching | `InputRecognitionService.match_visible_action` | Только thin wrapper в orchestrator | KEEP до отдельного routing audit |
| Voice prompt | `voice_transcription_prompt` | Только thin wrapper; static prompts в `openai_prompts.py` не эквивалентны | KEEP DISTINCT |
| Supported alphabet | `has_supported_voice_letters` | Эквивалентных проверок нет; mixed-script regex имеет другую семантику | Включить в transcript seam |
| Transcript selection | `select_transcription_result` | Дубликатов нет | Включить в transcript seam |
| High-accuracy decision | `requires_high_accuracy_transcription` | Дубликатов нет | KEEP; отдельный будущий audit |
| Model fallback | `has_distinct_models` + instance wrapper | Дубликатов нет | KEEP provider-specific |
| Processing update | `update_processing` | `_send_processing_best_effort` похож, но создаёт новую card | KEEP DISTINCT до progress audit |
| Transient OpenAI errors | `_OPENAI_TRANSIENT_ERRORS` в input и orchestrator | Две одинаковые tuple-константы | Зафиксировать как cleanup candidate, не смешивать с выбранным seam |
| Text normalization | `text_normalization.normalize_text` | `services/text` больше не владеет `clean_text`/`normalize_text` | Использовать существующего owner |
| Unit normalization | `services.text.normalize_unit` | Другого owner не найдено | Не начинать units migration в 5O |

Ни один из найденных случаев не оправдывает создание `utils.py`, generic
`helpers.py` или нового класса только ради уменьшения файла.

## Канальная и provider-neutral граница

| Группа | Telegram-specific | Provider-specific | Можно повторно использовать для другого канала |
|---|---|---|---|
| Download/cleanup | Да | Нет | Только через новый media request contract |
| Voice transcription call | Через file/client contract | Да, OpenAI | Да после provider port |
| Transcript validation/selection | Нет | Нет | Да; это выбранный seam |
| State-aware retry decision | Нет по transport, но зависит от conversation/catalog | Нет | Да после явного state-neutral contract |
| Visible action matching | Да по `action_id`/UI labels | Нет | Только после channel-neutral action contract |
| Photo progress | Да | Нет | Нет без channel progress port |

Класс не должен стать конечным owner: он смешивает channel transport,
provider calls, state-aware policy и presentation effects. Целевое состояние —
тонкий application coordinator с отдельными именованными owners и удалением
facade только после caller-аудита.

## Ровно один следующий code seam

### Выбранный seam

**Voice transcript acceptance policy**:

- `has_supported_voice_letters`;
- `select_transcription_result`;
- target: `src/restaurant_bot/input/voice_transcript_policy.py`;
- текущий orchestrator: `_recognize_voice`;
- текущие regression callers: `tests/input/test_voice_processing_card.py` через
  wrappers `UpdateOrchestrator`.

### Почему именно этот seam

Это единственная доказанная чистая связная группа внутри класса. Она не требует
`TelegramClient`, `OpenAIService`, `ConversationState`, `ParsedCommand`,
`services.parser` или `services.text`; её contract — строки на входе и строка/
boolean на выходе. Перенос улучшит dependency direction, не смешает UI с
recognition policy и имеет существующий focused corpus.

### Выполненный mechanical diff Block 5P

В Block 5P изменено только:

1. создан `input/voice_transcript_policy.py` с двумя pure functions;
2. внутренние вызовы в `services/input_recognition.py` переведены на новый owner;
3. class-level compatibility wrappers и два orchestrator wrappers удалены после
   repository-wide caller-аудита;
4. существующие behavioral tests переведены на canonical owner без дублирования.

Не переносить вместе с ним `requires_high_accuracy_transcription`,
`voice_transcription_prompt`, `match_visible_action`, `update_processing`,
`_recognize_voice` или model fallback. Это были бы другие responsibility seams.

### Ожидаемый dependency graph после seam

```text
input/voice_transcript_policy.py
  -> text_normalization.py

services/input_recognition.py
  -> input/voice_transcript_policy.py
  -> services.parser / services.text / conversation.selection

services/orchestrator.py
  -> services/input_recognition.py
```

Cycle simulation для этой схемы даёт `0` циклов. `input/telegram.py` не
изменяется и не получает зависимость на recognition policy.

### Corpus и риск

- `tests/input/test_voice_processing_card.py`: 24 теста, текущий focused run —
  green.
- Обязательные проверки будущего MOVE: полный voice-focused suite, parser/AI
  voice recovery tests, text-vs-voice contract tests и полный pytest.
- Риск низкий, если сохранены pure contracts и class wrappers; риск становится
  средним/высоким при включении state-aware retry или visible actions.

## Почему нельзя переносить весь класс

Механический MOVE всего `services/input_recognition.py` в
`input/recognition.py` сохранит смешение семи разных responsibility: Telegram
download/cleanup, OpenAI voice, OpenAI photo, retry policy, state-aware prompt,
visible-action matching и progress presentation. Он также сохранит зависимости
на `services.parser`, `services.text`, conversation selection и
`ConversationState`, а значит не улучшит dependency direction. Правильная судьба
— постепенный SPLIT по доказанным seams, затем тонкий coordinator и удаление
старого service facade только после нулевого caller-аудита.

## Проверки Block 5O

- starting local/remote SHA: совпадают на `9c19f784ef88a0721c8be99bb8cece80ae4ebfe4`;
- `tests/input/test_voice_processing_card.py`: `24` collected, green;
- полный pytest после audit: `1366 collected / 1366 passed`;
- обязательные AST cycle/import проверки должны быть выполнены перед docs commit;
- инвариант Block 5K сохраняется: `services.orchestrator → workers.tasks = 0`;
- `manual_smoke_forensic_logs.txt` необязателен и не изменяется;
- `.env` не читается и не tracked;
- GitLab не используется.

Следующий migration seam — только описанный transcript policy. Другие группы и
полный MOVE в `input/recognition.py` в этом блоке не начинались.
