# Передача проекта

## 1. Назначение

Это production Python-бот для закупок ресторана в Telegram. Он принимает текст,
голос, фото и callback, ведёт редактируемый черновик, сопоставляет товары с
каталогом выбранного заведения и после подтверждения записывает заявку в Google
Sheets.

AI помогает понять свободную речь и найти кандидатов. Детерминированный код
владеет состоянием, проверками безопасности, идемпотентностью, хранением и
внешними эффектами.

## 2. Текущий checkout

- Репозиторий: `Dzmitry-Radziuk/test_bot`.
- Ветка: `decompose_bot`.
- Semantic baseline: `f9cbc3195c0eae843de3208e488c3f46baa5a5ec`.
- Текущий decomposition HEAD после Block 2B: `bad8d98cce2138691bd40bc6cb7221d210ec68ac`.
- Единственный рабочий remote: GitHub `origin/decompose_bot`.
- Автоматический baseline: `1362 collected / 1362 passed`.
- `manual_smoke_forensic_logs.txt` — намеренный локальный untracked-файл
  диагностики. Его нельзя добавлять в commit.

## 3. Основной pipeline

```text
Telegram update
  -> webhook и inbox
  -> Celery delivery
  -> UpdateOrchestrator
  -> авторизация и lease чата
  -> нормализация text/voice/photo
  -> глобальный ParsedCommand
  -> reconciliation исходного текста
  -> StateCompatibilityPolicy
  -> ConversationEngine и CatalogResolver
  -> изменение ConversationState
  -> checkpoint сессии и ответ Telegram
```

Подтверждение заявки — отдельная граница: свежий снимок, запись в лист, read-back,
пересчёт, optional dispatch и контрольная точка уведомления.

## 4. Владельцы текущих модулей

### Domain и persistence

- `domain/models.py` — Intent, ParsedCommand, ExtractedItem, CartItem,
  ConversationState, ответы, submission-контракты и сериализованные enum.
- `db.py`, `db_models.py` — SQLAlchemy и схема.
- `repositories/updates.py` — inbox и идемпотентная последовательность update.
- `repositories/sessions.py` — версионированное состояние диалога.
- `repositories/submissions.py` — checkpoints и idempotency отправки.
- `repositories/order_events.py`, `venue_bindings.py` — аудит и привязки
  пользователей/чатов к заведению.

### Ввод и разбор

- `services/input_normalizer.py` — Telegram payload -> TelegramEvent.
- `services/input_recognition.py` — voice/photo, транскрибация и visible actions.
- `services/parser.py` — глобальный intent/callback parser и совместимый фасад.
- `parsing/products.py` — orchestration разбора товарных строк и сборка
  итогового списка `ExtractedItem`.
- `parsing/quantities.py` — короткие ответы количества и quantity primitives.
- `parsing/packaging.py` — фасовка, диапазоны и catalog measurement parsing.
- `parsing/comment_scope.py` — явная область общего комментария.
- `services/text.py` — лексическая нормализация, единицы, числа и диапазоны.
- `integrations/openai_parsing.py` — structured schemas и reconciliation
  источника, комментариев, количеств и shadow items.
- `integrations/openai_prompts.py` — неизменяемые prompt-контракты.

### Каталог и диалог

- `services/matching.py` — canonical tokens, evidence, scoring, ranking и
  safety gates.
- `services/catalog_resolver.py` — поиск кандидатов в пределах заведения и
  чистое решение CatalogDecision без изменения ConversationState.
- `services/conversation_handlers/` — quantity, candidate, comment scope,
  review, navigation и единая StateCompatibilityPolicy.
- `services/engine.py` — текущая state machine, применение решения каталога,
  изменение черновика, comments, quantity, duplicate flow и подготовка submission.
- `services/replies.py` — карточки, клавиатуры и пользовательские тексты.

### Внешние эффекты

- `services/submission.py` — запись, checkpoints, пересчёт, dispatch и статусы.
- `services/venue_registration.py` — каталог заведений, доступ и регистрацию.
- `integrations/telegram.py` — Telegram transport.
- `integrations/google_sheets.py` — каталог и листы заведения.
- `integrations/cache.py` — catalog cache и Redis locks.
- `workers/tasks.py` — только Celery delivery и фоновые границы.

## 5. Неприкосновенные инварианты

1. AI предлагает структуру, а источник пользователя и каталог подтверждают её.
2. `product_query` и supplier `comment` могут намеренно содержать одну
   характеристику одновременно.
3. Количество заказа отдельно от фасовки, веса упаковки, диапазона и размеров.
4. Retrieval, ranking и auto-select — разные решения; слабый токен не даёт
   право выбрать другой товар.
5. Текст и голос после транскрибации используют один semantic pipeline.
6. Сначала выполняется global parsing, затем contextual modal fallback.
7. Сильное независимое намерение может прервать старый modal state, не перенося
   quantity/comment в новый товар.
8. Комментарий поставщику требует подтверждённого происхождения.
9. Draft mutation и submission идемпотентны и привязаны к заведению.
10. Старый callback revision не изменяет новый черновик.
11. Ненайденный товар не заменяется похожим автоматически.

## 6. Защищённые границы

Без отдельного разрешения не менять Docker, deployment, CI/CD, `.env`,
секреты, миграции, схему БД, serialized state, Telegram UX/callback,
prompts, matching thresholds, submission contracts, Google Sheets,
Redis locking, workers и API entrypoints.

GitLab не используется. В этой задаче разрешён только GitHub.

## 7. Известные ручные acceptance-проблемы

Это текущие нерешённые acceptance-проблемы:

- после вопроса о количестве для «хлеб» фраза «новый товар» не должна стать
  количеством хлеба;
- «удали все комментарии» не должна очищать товары;
- исправление комментария «не X, а Y» должно менять только исправленную часть.

## 8. Статус декомпозиции

Block 0 завершён: зафиксированы владельцы, dependency rules, compatibility
facades и порядок миграции в `docs/ARCHITECTURE_DECOMPOSITION.md`.

Block 1 завершён механически: реализация product parser находится в
`parsing/products.py`, а `services/parser.py` импортирует его напрямую.
Поведение, prompts, state machine, matching, UX, persistence и deployment не
менялись. Focused и полный regression baseline проходят.

Block 2A завершён: из `products.py` вынесены три доказанных независимых
кластера — quantities, packaging и explicit global comment scope. Старый
`services/product_parser.py` удалён после полного import audit: callers старого
пути не найдены.

Block 2B завершён механически: text command parsing разделён по ответственностям
в `parsing/commands/`, а `services/parser.py` стал facade/dispatcher на 224 строки.
`parse_callback()` оставлен отдельным channel contract. Поведение подтверждено
сравнением на 62 существующих случаях и полным baseline `1362 passed`.

Block 3 завершён механически: structured AI schemas и reconciliation разделены
по ответственности в `parsing/ai/`. `integrations/openai_parsing.py` оставлен
совместимым re-export facade на 39 строк; OpenAI transport остаётся в
`integrations/openai_client.py`. Поведение подтверждено focused AI suite
`220 passed` и полным baseline `1362 passed`.

Сохранены инварианты: Google Sheets остаётся source of truth; PostgreSQL,
pgvector, catalog migrations и Sheets sync в Block 3 не реализовывались.
AI предлагает структуру, source phrase подтверждает, catalog уточняет,
детерминированный код выполняет действие.

Постоянное правило: перед каждым `MOVE`/`MERGE`/`DELETE` выполняются
repository-wide usage и duplicate audit. Мёртвый или дублирующий код не
переносится; для одной ответственности остаётся одна реализация. Временный
facade допустим только как явный re-export при подтверждённых callers.

## 9. Block 2B

Документационный commit с постоянными правилами создан отдельно. Text command
parsing механически разделён по доказанным ответственностям в `parsing/commands/`.
`services/parser.py` оставлен компактным facade/dispatcher: `infer_intent`,
`parse_callback` и public compatibility exports. Callback mapping не смешан с
channel-agnostic text parsing. Product parsing Block 2A не изменялся.

До code commit выполнены повторные usage/duplicate/dead-code audit и semantic
comparison на 62 существующих тестовых строках: расхождений нет. Полный
regression baseline остаётся `1362 passed`; code migration прошёл все quality
gates.

## 10. Следующий блок

Следующий функционально-архитектурный блок — catalog evidence/scoring/safety
и resolver. Он начинается только после отдельного review завершённого Block 3.

## 11. Проверки

Для Block 2A обязательны:

```text
focused pytest:
tests/input/test_parser.py
tests/input/test_input_edge_cases.py
tests/input/test_voice_quantity_recovery.py
tests/conversation/test_comment_handling.py
tests/quantity/test_quantity_contract.py
tests/conversation/test_quantity_state_preemption.py

full pytest: python -m pytest -q --tb=short
ruff check src tests
ruff format --check <изменённые Python-файлы>
mypy src
python scripts/check_markdown_links.py
git diff --check
```

Также проверяются импорты `restaurant_bot.parsing.products`,
`restaurant_bot.parsing.quantities`, `restaurant_bot.parsing.packaging`,
`restaurant_bot.parsing.comment_scope`, `restaurant_bot.services.parser` и
отсутствие циклических импортов.

## 11. Правила передачи

Текущий код, тесты и Git-diff важнее старых заметок. Не удалять пользовательские
файлы, не использовать destructive Git commands и force push. Не читать и не
выводить `.env`. После каждого блока обновлять этот короткий handoff одной
актуальной записью и отдельно указывать подтверждённые факты, ограничения и
следующий блок.

Критическое состояние проекта не должно существовать только в истории ChatGPT
или Codex-сессии. Для восстановления работы достаточно AGENTS.md,
PROJECT_HANDOFF.md, `.agents/DECISIONS.md`, `.agents/PROJECT_MAP.md`,
архитектурной документации, тестов и текущего Git state. История обсуждений
в handoff не копируется.
