# PROJECT HANDOFF

Актуально для ветки `decompose_bot` после независимого engineering audit
2026-08-13. Git SHA текущей версии документа нужно получать командой
`git rev-parse HEAD`; production-код аудита совпадает с исходным SHA
`4dc67430336bb90cfad731aca583f51a19948a48`.

## Текущая задача

Завершить документный audit, опубликовать обновлённые current docs и каталог
пользовательских сценариев, затем перейти только к подготовке контролируемого
Telegram pilot. Новую общую декомпозицию и production refactor не начинать.

Подробный verdict и scores:
[`docs/INDEPENDENT_ENGINEERING_AUDIT.md`](docs/INDEPENDENT_ENGINEERING_AUDIT.md).

## Подтверждённые факты

- Ветка: `decompose_bot`.
- Remote: только GitHub `origin` → `Dzmitry-Radziuk/test_bot`.
- Исходный baseline аудита: `1391 collected / 1391 passed`.
- Повторный post-doc suite: `1391 passed` за `18.78 s`.
- Mypy: `139 source files, no issues`.
- Ruff check: pass; Ruff format: `252 files already formatted`.
- Compileall и `git diff --check`: pass.
- После refresh Markdown checker проверил 36 файлов.
- Scenario catalog вырос с 34 до 40 содержательных сценариев; все mappings
  проверяются официальным generator.
- `.env` не tracked; значения не читались.
- Repository scan не нашёл строк, похожих на Telegram/OpenAI keys.
- Production executable code в audit не изменяется.

Финальные post-doc проверки зелёные. Documentation impact checker выполняется после
commit, потому что принимает только Git-объекты; его результат нужно сверить перед
push.

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
- Локальный Docker preflight 2026-08-13: контейнер `migrate` не завершает запуск
  из-за импорта `restaurant_bot.db_models` в `alembic/env.py`; текущий ручной
  runtime поднят поверх существующей схемы через `docker compose up --no-deps`.
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
