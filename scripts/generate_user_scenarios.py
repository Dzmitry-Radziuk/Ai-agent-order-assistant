"""Генерирует Markdown и автономный HTML-каталог пользовательских сценариев."""

from __future__ import annotations

import argparse
import ast
import html
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = ROOT / "docs" / "user-scenarios" / "scenarios.json"
MARKDOWN_PATH = ROOT / "docs" / "USER_SCENARIOS.md"
HTML_PATH = ROOT / "docs" / "user-scenarios" / "index.html"

PRIORITY_LABELS = {
    "critical": "Критический",
    "important": "Важный",
    "edge": "Защитный",
}

STATUS_LABELS = {
    "live": "Работает сейчас",
    "prepared": "Подготовлено, но выключено",
}


def _strip_trailing_whitespace(text: str) -> str:
    """Удаляет пробелы в концах строк, сохраняя финальный перевод строки."""
    cleaned = "\n".join(line.rstrip() for line in text.splitlines())
    return f"{cleaned}\n" if text.endswith("\n") else cleaned


def load_catalog(path: Path = SOURCE_PATH) -> dict[str, Any]:
    """Загружает и проверяет единый JSON-источник пользовательских сценариев."""
    catalog = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    validate_catalog(catalog, path.parent)
    return catalog


def _test_functions(path: Path) -> set[str]:
    """Возвращает имена тестовых функций верхнего уровня в Python-файле."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


def validate_catalog(catalog: dict[str, Any], source_directory: Path) -> None:
    """Проверяет структуру каталога и существование связанных pytest-тестов."""
    required_root = {
        "title",
        "version",
        "rules",
        "categories",
        "journeys",
        "system_flows",
        "scenarios",
    }
    missing_root = required_root - catalog.keys()
    if missing_root:
        raise ValueError(f"В каталоге отсутствуют поля: {sorted(missing_root)}")

    categories = catalog["categories"]
    category_ids = [category["id"] for category in categories]
    if len(category_ids) != len(set(category_ids)):
        raise ValueError("Идентификаторы категорий должны быть уникальными")

    known_categories = set(category_ids)
    known_priorities = set(PRIORITY_LABELS)
    scenario_ids: set[str] = set()
    test_cache: dict[Path, set[str]] = {}

    required_contract = {"permissions", "entities", "confirmation", "recovery"}
    for category in categories:
        missing_contract = required_contract - category.get("contract", {}).keys()
        if missing_contract:
            raise ValueError(
                f"Категория {category['id']}: в contract отсутствуют {sorted(missing_contract)}"
            )
        if not category["contract"]["entities"]:
            raise ValueError(f"Категория {category['id']}: нужен список сущностей")

    for scenario in catalog["scenarios"]:
        required = {
            "id",
            "category",
            "title",
            "priority",
            "channels",
            "precondition",
            "user_action",
            "bot_response",
            "result",
            "examples",
            "test_refs",
        }
        missing = required - scenario.keys()
        if missing:
            raise ValueError(f"{scenario.get('id', 'UNKNOWN')}: отсутствуют {sorted(missing)}")
        if scenario["id"] in scenario_ids:
            raise ValueError(f"Повторяющийся сценарий: {scenario['id']}")
        scenario_ids.add(scenario["id"])
        if scenario["category"] not in known_categories:
            raise ValueError(f"{scenario['id']}: неизвестная категория")
        if scenario["priority"] not in known_priorities:
            raise ValueError(f"{scenario['id']}: неизвестный приоритет")
        if scenario.get("status", "live") not in STATUS_LABELS:
            raise ValueError(f"{scenario['id']}: неизвестный статус доступности")
        if scenario["priority"] == "critical" and not scenario["test_refs"]:
            raise ValueError(f"{scenario['id']}: критический сценарий должен иметь тест")
        if not scenario["channels"]:
            raise ValueError(f"{scenario['id']}: нужен хотя бы один канал")

        for reference in scenario["test_refs"]:
            if "::" not in reference:
                raise ValueError(f"{scenario['id']}: некорректная ссылка на тест {reference}")
            relative_path, function_name = reference.split("::", maxsplit=1)
            test_path = (ROOT / relative_path).resolve()
            if ROOT not in test_path.parents or not test_path.is_file():
                raise ValueError(f"{scenario['id']}: тестовый файл не найден {relative_path}")
            functions = test_cache.setdefault(test_path, _test_functions(test_path))
            if function_name not in functions:
                raise ValueError(f"{scenario['id']}: тест не найден {reference}")

    journey_titles: set[str] = set()
    for journey in [*catalog["journeys"], *catalog["system_flows"]]:
        required = {"title", "description", "steps", "diagram", "scenario_ids"}
        missing = required - journey.keys()
        if missing:
            raise ValueError(
                f"Маршрут {journey.get('title', 'UNKNOWN')}: отсутствуют {sorted(missing)}"
            )
        if journey["title"] in journey_titles:
            raise ValueError(f"Повторяющийся маршрут: {journey['title']}")
        journey_titles.add(journey["title"])
        if not journey["steps"] or not journey["diagram"].strip():
            raise ValueError(f"Маршрут {journey['title']}: нужны шаги и Mermaid-схема")
        if not journey["scenario_ids"]:
            raise ValueError(f"Маршрут {journey['title']}: не связан со сценариями")
        unknown_scenarios = set(journey["scenario_ids"]) - scenario_ids
        if unknown_scenarios:
            raise ValueError(
                f"Маршрут {journey['title']}: неизвестные сценарии {sorted(unknown_scenarios)}"
            )

    for rule in catalog["rules"]:
        if not rule.get("title") or not rule.get("description"):
            raise ValueError("Каждому правилу нужны title и description")

    if source_directory.resolve() != SOURCE_PATH.parent.resolve():
        return


def _coverage_summary(catalog: dict[str, Any]) -> tuple[int, int, Counter[str]]:
    """Возвращает количество сценариев, связанных тестов и приоритетов."""
    scenarios = catalog["scenarios"]
    linked = sum(bool(scenario["test_refs"]) for scenario in scenarios)
    priorities = Counter(scenario["priority"] for scenario in scenarios)
    return len(scenarios), linked, priorities


def _test_markdown(reference: str) -> str:
    """Формирует Markdown-ссылку на тестовый файл и имя функции."""
    path, function_name = reference.split("::", maxsplit=1)
    return f"[`{path}`](../{path}) → `{function_name}`"


def _scenario_contract(scenario: dict[str, Any], category: dict[str, Any]) -> dict[str, Any]:
    """Объединяет правила раздела с уточнениями конкретного сценария."""
    contract = dict(category["contract"])
    contract.update(scenario.get("contract", {}))
    contract.setdefault("state_change", scenario["result"])
    return contract


def render_markdown(catalog: dict[str, Any]) -> str:
    """Создаёт человекочитаемый Markdown-каталог с Mermaid-схемами."""
    total, linked, priorities = _coverage_summary(catalog)
    category_by_id = {category["id"]: category for category in catalog["categories"]}
    lines = [
        f"# {catalog['title']}",
        "",
        "<!-- Сгенерировано из docs/user-scenarios/scenarios.json. Не редактируйте вручную. -->",
        "",
        "Этот документ показывает поведение бота языком повара и сотрудника кафе.",
        "Он не заменяет тесты: ссылки в карточках ведут к проверкам реального кода.",
        "",
        "## Как пользоваться",
        "",
        "1. Найдите нужную ситуацию по разделу или идентификатору.",
        "2. Повторите действие пользователя в Telegram.",
        "3. Сравните ответ бота и итог с ожидаемым поведением.",
        "4. Для удобного просмотра откройте [`user-scenarios/index.html`](user-scenarios/index.html).",
        "",
        "## Главные правила",
        "",
    ]

    for rule in catalog["rules"]:
        lines.extend(
            [
                f"### {rule['title']}",
                "",
                rule["description"],
                "",
            ]
        )

    lines.extend(
        [
            "## Покрытие",
            "",
            "| Показатель | Значение |",
            "|---|---:|",
            f"| Всего сценариев | {total} |",
            f"| Связаны с pytest | {linked} |",
            f"| Критические | {priorities['critical']} |",
            f"| Важные | {priorities['important']} |",
            f"| Защитные | {priorities['edge']} |",
            "",
            "## Основные маршруты",
            "",
        ]
    )

    for journey in catalog["journeys"]:
        lines.extend(
            [
                f"### {journey['title']}",
                "",
                journey["description"],
                "",
                "```mermaid",
                journey["diagram"].strip(),
                "```",
                "",
                "**Проверяется сценариями:** "
                + ", ".join(f"`{scenario_id}`" for scenario_id in journey["scenario_ids"]),
                "",
            ]
        )

    lines.extend(["## Как бот принимает решения", ""])
    for flow in catalog["system_flows"]:
        lines.extend(
            [
                f"### {flow['title']}",
                "",
                flow["description"],
                "",
                "```mermaid",
                flow["diagram"].strip(),
                "```",
                "",
                "**Связанные сценарии:** "
                + ", ".join(f"`{scenario_id}`" for scenario_id in flow["scenario_ids"]),
                "",
            ]
        )

    lines.extend(["## Каталог сценариев", ""])
    for category in catalog["categories"]:
        category_contract = category["contract"]
        lines.extend(
            [
                f"### {category['title']}",
                "",
                category["description"],
                "",
                f"**Доступ:** {category_contract['permissions']}  ",
                "**Что распознаём:** " + ", ".join(category_contract["entities"]) + "  ",
                f"**Подтверждение:** {category_contract['confirmation']}  ",
                f"**Если произошла ошибка:** {category_contract['recovery']}",
                "",
            ]
        )
        for scenario in (
            item for item in catalog["scenarios"] if item["category"] == category["id"]
        ):
            channels = ", ".join(scenario["channels"])
            priority = PRIORITY_LABELS[scenario["priority"]]
            status = STATUS_LABELS[scenario.get("status", "live")]
            contract = _scenario_contract(scenario, category)
            lines.extend(
                [
                    f"#### {scenario['id']} · {scenario['title']}",
                    "",
                    f"**Приоритет:** {priority}<br>",
                    f"**Статус:** {status}<br>",
                    f"**Канал:** {channels}<br>",
                    f"**Предусловие:** {scenario['precondition']}",
                    "",
                    f"**Действие пользователя:** {scenario['user_action']}",
                    "",
                    f"**Ответ бота:** {scenario['bot_response']}",
                    "",
                    f"**Результат:** {scenario['result']}",
                    "",
                    "<details>",
                    "<summary><strong>Что важно для системы</strong></summary>",
                    "",
                    f"- **Доступ:** {contract['permissions']}",
                    "- **Распознаваем:** " + ", ".join(contract["entities"]),
                    f"- **Подтверждение:** {contract['confirmation']}",
                    f"- **Изменение состояния:** {contract['state_change']}",
                    f"- **Восстановление:** {contract['recovery']}",
                    "",
                    "</details>",
                    "",
                    "**Примеры фраз:**",
                    "",
                    *[f"- «{example}»" for example in scenario["examples"]],
                    "",
                    "**Автоматическая проверка:**",
                    "",
                    *(
                        [f"- {_test_markdown(reference)}" for reference in scenario["test_refs"]]
                        or ["- Пока нет прямой автоматической проверки."]
                    ),
                    "",
                ]
            )

    lines.extend(
        [
            "## Обозначения",
            "",
            "- **Критический** — ошибка приводит к неверной заявке, потере данных или отправке против воли пользователя.",
            "- **Важный** — основной рабочий путь сотрудника кафе.",
            "- **Защитный** — обработка неоднозначности, сбоя или ошибочного действия.",
            "",
            f"_Версия каталога: {catalog['version']}. Категорий: {len(category_by_id)}._",
            "",
        ]
    )
    return _strip_trailing_whitespace("\n".join(lines))


def _scenario_card(scenario: dict[str, Any], category: dict[str, Any]) -> str:
    """Создаёт одну HTML-карточку сценария."""
    priority = scenario["priority"]
    status = scenario.get("status", "live")
    category_title = category["title"]
    contract = _scenario_contract(scenario, category)
    search_text = " ".join(
        [
            scenario["id"],
            scenario["title"],
            category_title,
            scenario["user_action"],
            scenario["bot_response"],
            contract["permissions"],
            *contract["entities"],
            *scenario["examples"],
        ]
    ).lower()
    examples = "".join(f"<li>«{html.escape(item)}»</li>" for item in scenario["examples"])
    tests = "".join(
        (
            f'<li><a href="../../{html.escape(reference.split("::", 1)[0])}">'
            f"{html.escape(reference.split('::', 1)[0])}</a>"
            f"<span>{html.escape(reference.split('::', 1)[1])}</span></li>"
        )
        for reference in scenario["test_refs"]
    )
    if not tests:
        tests = "<li>Прямая автоматическая проверка пока не привязана.</li>"
    channels = "".join(f"<span>{html.escape(channel)}</span>" for channel in scenario["channels"])
    return f"""
      <article id="scenario-{html.escape(scenario["id"])}" class="scenario-card"
        data-category="{html.escape(scenario["category"])}"
        data-priority="{html.escape(priority)}" data-search="{html.escape(search_text)}">
        <div class="card-top">
          <span class="scenario-id">{html.escape(scenario["id"])}</span>
          <span class="badges">
            <span class="status status-{html.escape(status)}">{STATUS_LABELS[status]}</span>
            <span class="priority priority-{html.escape(priority)}">{PRIORITY_LABELS[priority]}</span>
          </span>
        </div>
        <h3>{html.escape(scenario["title"])}</h3>
        <p class="category-label">{html.escape(category_title)}</p>
        <div class="channels">{channels}</div>
        <dl>
          <div><dt>Перед началом</dt><dd>{html.escape(scenario["precondition"])}</dd></div>
          <div><dt>Пользователь</dt><dd>{html.escape(scenario["user_action"])}</dd></div>
          <div><dt>Бот</dt><dd>{html.escape(scenario["bot_response"])}</dd></div>
          <div class="result"><dt>Итог</dt><dd>{html.escape(scenario["result"])}</dd></div>
        </dl>
        <details>
          <summary>Что важно</summary>
          <dl class="contract">
            <div><dt>Доступ</dt><dd>{html.escape(contract["permissions"])}</dd></div>
            <div><dt>Распознаём</dt><dd>{html.escape(", ".join(contract["entities"]))}</dd></div>
            <div><dt>Подтверждение</dt><dd>{html.escape(contract["confirmation"])}</dd></div>
            <div><dt>Что изменится</dt><dd>{html.escape(contract["state_change"])}</dd></div>
            <div><dt>Если произошла ошибка</dt><dd>{html.escape(contract["recovery"])}</dd></div>
          </dl>
        </details>
        <details>
          <summary>Примеры фраз</summary>
          <ul>{examples}</ul>
        </details>
        <details>
          <summary>Связанные pytest-тесты · {len(scenario["test_refs"])}</summary>
          <ul class="tests">{tests}</ul>
        </details>
      </article>
    """


def render_html(catalog: dict[str, Any]) -> str:
    """Создаёт автономную HTML-страницу с поиском и фильтрами."""
    total, linked, priorities = _coverage_summary(catalog)
    category_by_id = {category["id"]: category for category in catalog["categories"]}
    category_buttons = "".join(
        f'<button type="button" data-category="{html.escape(category["id"])}">'
        f"{html.escape(category['title'])}</button>"
        for category in catalog["categories"]
    )
    journey_cards = "".join(
        (
            '<section class="journey"><h3>'
            f"{html.escape(journey['title'])}</h3><p>{html.escape(journey['description'])}</p>"
            '<div class="journey-steps">'
            + "".join(
                f"<span><b>{index}</b>{html.escape(step)}</span>"
                for index, step in enumerate(journey["steps"], start=1)
            )
            + '</div><div class="journey-tests"><b>Проверяется сценариями:</b> '
            + " ".join(
                f'<a href="#scenario-{html.escape(scenario_id)}">{html.escape(scenario_id)}</a>'
                for scenario_id in journey["scenario_ids"]
            )
            + "</div></section>"
        )
        for journey in catalog["journeys"]
    )
    rule_cards = "".join(
        '<article class="rule"><h3>'
        f"{html.escape(rule['title'])}</h3><p>{html.escape(rule['description'])}</p></article>"
        for rule in catalog["rules"]
    )
    flow_cards = "".join(
        (
            '<details class="system-flow"><summary>'
            f"{html.escape(flow['title'])}</summary><p>{html.escape(flow['description'])}</p>"
            '<div class="journey-steps">'
            + "".join(
                f"<span><b>{index}</b>{html.escape(step)}</span>"
                for index, step in enumerate(flow["steps"], start=1)
            )
            + '</div><div class="journey-tests"><b>Связанные сценарии:</b> '
            + " ".join(
                f'<a href="#scenario-{html.escape(scenario_id)}">{html.escape(scenario_id)}</a>'
                for scenario_id in flow["scenario_ids"]
            )
            + "</div></details>"
        )
        for flow in catalog["system_flows"]
    )
    cards = "".join(
        _scenario_card(scenario, category_by_id[scenario["category"]])
        for scenario in catalog["scenarios"]
    )
    source_json = json.dumps(
        {
            "version": catalog["version"],
            "total": total,
            "linked": linked,
        },
        ensure_ascii=False,
    ).replace("</", "<\\/")

    page = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(catalog["title"])}</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #16201c;
      --muted: #607068;
      --paper: #f4f7f4;
      --card: #ffffff;
      --green: #1d6b4c;
      --green-soft: #e7f3ec;
      --line: #dce5df;
      --critical: #b42318;
      --important: #9a6700;
      --edge: #475467;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--paper); color: var(--ink); }}
    header {{
      padding: 48px max(24px, calc((100vw - 1180px) / 2));
      background: linear-gradient(135deg, #143d2d, #1f7654);
      color: white;
    }}
    header p {{ max-width: 760px; margin: 12px 0 0; color: #dceee5; font-size: 18px; line-height: 1.55; }}
    h1 {{ margin: 0; font-size: clamp(32px, 5vw, 54px); line-height: 1.05; }}
    main {{ width: min(1180px, calc(100% - 32px)); margin: 0 auto 64px; }}
    .summary {{
      display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px;
      margin: -25px 0 34px;
    }}
    .metric {{ background: var(--card); padding: 18px 20px; border-radius: 16px; box-shadow: 0 10px 30px #10271c14; }}
    .metric b {{ display: block; font-size: 28px; color: var(--green); }}
    .metric span {{ color: var(--muted); font-size: 14px; }}
    .section-heading {{ margin: 34px 0 16px; }}
    .section-heading h2 {{ margin: 0 0 6px; font-size: 28px; }}
    .section-heading p {{ margin: 0; color: var(--muted); }}
    .rules {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }}
    .rule {{ padding: 18px; border-radius: 16px; background: var(--green-soft); }}
    .rule h3 {{ margin: 0 0 6px; font-size: 17px; }}
    .rule p {{ margin: 0; color: #315c48; line-height: 1.5; }}
    .journeys {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }}
    .journey {{ background: var(--card); border: 1px solid var(--line); border-radius: 18px; padding: 22px; }}
    .journey h3 {{ margin: 0 0 6px; }}
    .journey p {{ color: var(--muted); margin: 0 0 18px; line-height: 1.5; }}
    .journey-steps {{ display: flex; align-items: stretch; gap: 8px; overflow-x: auto; padding-bottom: 4px; }}
    .journey-steps span {{
      min-width: 120px; flex: 1; padding: 12px; border-radius: 12px; background: var(--green-soft);
      color: #214c39; font-size: 13px; position: relative;
    }}
    .journey-steps b {{
      display: block; width: 22px; height: 22px; margin-bottom: 8px; border-radius: 50%;
      background: var(--green); color: white; text-align: center; line-height: 22px;
    }}
    .journey-tests {{ margin-top: 16px; color: var(--muted); font-size: 13px; line-height: 1.8; }}
    .journey-tests a {{
      display: inline-block; margin-left: 5px; padding: 1px 7px; border-radius: 7px;
      background: var(--green-soft); text-decoration: none;
    }}
    .system-flows {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
    .system-flow {{ margin: 0; padding: 18px; border: 1px solid var(--line); border-radius: 16px; background: var(--card); }}
    .system-flow summary {{ font-size: 17px; }}
    .system-flow p {{ color: var(--muted); line-height: 1.5; }}
    .toolbar {{
      position: sticky; top: 0; z-index: 5; margin: 24px 0 18px; padding: 14px;
      background: #f4f7f4ed; backdrop-filter: blur(12px); border: 1px solid var(--line); border-radius: 16px;
    }}
    .toolbar-row {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    input {{
      flex: 1 1 320px; border: 1px solid #bdcbc2; border-radius: 12px; padding: 12px 14px;
      background: white; font: inherit;
    }}
    button {{
      border: 1px solid #bdcbc2; border-radius: 999px; padding: 9px 13px; background: white;
      color: var(--ink); cursor: pointer; font: inherit; font-size: 13px;
    }}
    button:hover, button.active {{ border-color: var(--green); background: var(--green); color: white; }}
    .category-filters {{ margin-top: 10px; display: flex; gap: 7px; overflow-x: auto; padding-bottom: 2px; }}
    #visible-count {{ color: var(--muted); font-size: 13px; margin: 10px 2px 0; }}
    .scenario-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }}
    .scenario-card {{ background: var(--card); border: 1px solid var(--line); border-radius: 18px; padding: 22px; }}
    .scenario-card[hidden] {{ display: none; }}
    .card-top {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; }}
    .badges {{ display: flex; align-items: center; justify-content: flex-end; gap: 6px; flex-wrap: wrap; }}
    .scenario-id {{ color: var(--green); font-weight: 750; letter-spacing: .04em; }}
    .priority {{ font-size: 12px; border-radius: 999px; padding: 5px 9px; font-weight: 700; }}
    .priority-critical {{ color: var(--critical); background: #fee4e2; }}
    .priority-important {{ color: var(--important); background: #fef0c7; }}
    .priority-edge {{ color: var(--edge); background: #eaecf0; }}
    .status {{ font-size: 11px; border-radius: 999px; padding: 5px 8px; font-weight: 700; }}
    .status-live {{ color: #146c43; background: #d1fadf; }}
    .status-prepared {{ color: #93370d; background: #ffead5; }}
    .scenario-card h3 {{ margin: 13px 0 4px; font-size: 21px; }}
    .category-label {{ margin: 0 0 12px; color: var(--muted); font-size: 13px; }}
    .channels {{ display: flex; gap: 6px; margin-bottom: 16px; }}
    .channels span {{ color: #315c48; background: var(--green-soft); border-radius: 8px; padding: 5px 8px; font-size: 12px; }}
    dl {{ margin: 0; }}
    dl div {{ border-top: 1px solid #edf1ee; padding: 12px 0; }}
    dt {{ color: var(--muted); font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: .04em; }}
    dd {{ margin: 5px 0 0; line-height: 1.48; }}
    dl .result {{ border-radius: 12px; border: 0; background: var(--green-soft); padding: 12px; margin: 4px 0 12px; }}
    .contract div {{ padding: 9px 0; }}
    details {{ border-top: 1px solid var(--line); padding: 11px 0 0; margin-top: 10px; }}
    summary {{ cursor: pointer; color: var(--green); font-weight: 650; }}
    li {{ margin: 7px 0; line-height: 1.4; }}
    .tests span {{ display: block; color: var(--muted); font-family: ui-monospace, monospace; font-size: 11px; }}
    a {{ color: var(--green); }}
    footer {{ color: var(--muted); text-align: center; padding: 24px; }}
    @media (max-width: 820px) {{
      .summary, .rules, .journeys, .system-flows, .scenario-grid {{ grid-template-columns: 1fr; }}
      header {{ padding-top: 36px; padding-bottom: 50px; }}
      .toolbar {{ position: static; }}
    }}
    @media print {{
      .toolbar {{ display: none; }}
      .scenario-grid {{ grid-template-columns: 1fr; }}
      .scenario-card {{ break-inside: avoid; }}
      body {{ background: white; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>{html.escape(catalog["title"])}</h1>
    <p>Понятная карта поведения Telegram-бота: что делает сотрудник кафе, как отвечает бот и каким тестом подтверждён результат.</p>
  </header>
  <main>
    <section class="summary" aria-label="Сводка">
      <div class="metric"><b>{total}</b><span>сценариев</span></div>
      <div class="metric"><b>{linked}</b><span>связаны с pytest</span></div>
      <div class="metric"><b>{priorities["critical"]}</b><span>критических</span></div>
      <div class="metric"><b>{len(catalog["categories"])}</b><span>разделов</span></div>
    </section>

    <div class="section-heading">
      <h2>Главные правила</h2>
      <p>Пять принципов, которые защищают пользователя от неверного действия.</p>
    </div>
    <section class="rules">{rule_cards}</section>

    <div class="section-heading">
      <h2>Как проходит заявка</h2>
      <p>Основные маршруты без технических деталей.</p>
    </div>
    <div class="journeys">{journey_cards}</div>

    <div class="section-heading">
      <h2>Как бот принимает решения</h2>
      <p>Откройте нужную схему — внутри только основные шаги.</p>
    </div>
    <section class="system-flows">{flow_cards}</section>

    <div class="section-heading">
      <h2>Все пользовательские сценарии</h2>
      <p>Ищите по фразе, действию, названию или идентификатору.</p>
    </div>
    <section class="toolbar" aria-label="Фильтры">
      <div class="toolbar-row">
        <input id="search" type="search" placeholder="Например: не добавляй, фото, новый заказ…" autocomplete="off">
        <button type="button" class="active" data-priority="all">Все</button>
        <button type="button" data-priority="critical">Критические</button>
        <button type="button" data-priority="important">Важные</button>
        <button type="button" data-priority="edge">Защитные</button>
      </div>
      <div class="category-filters">
        <button type="button" class="active" data-category="all">Все разделы</button>
        {category_buttons}
      </div>
      <p id="visible-count"></p>
    </section>
    <section class="scenario-grid" id="scenario-grid">{cards}</section>
  </main>
  <footer>Локальный каталог · версия {html.escape(str(catalog["version"]))} · работает без интернета</footer>
  <script id="catalog-meta" type="application/json">{source_json}</script>
  <script>
    (() => {{
      const cards = [...document.querySelectorAll(".scenario-card")];
      const search = document.querySelector("#search");
      const count = document.querySelector("#visible-count");
      let priority = "all";
      let category = "all";

      const applyFilters = () => {{
        const query = search.value.trim().toLocaleLowerCase("ru");
        let visible = 0;
        cards.forEach((card) => {{
          const matches = (priority === "all" || card.dataset.priority === priority)
            && (category === "all" || card.dataset.category === category)
            && (!query || card.dataset.search.includes(query));
          card.hidden = !matches;
          if (matches) visible += 1;
        }});
        count.textContent = `Показано: ${{visible}} из ${{cards.length}}`;
      }};

      document.querySelectorAll("[data-priority]").forEach((button) => {{
        button.addEventListener("click", () => {{
          document.querySelectorAll("[data-priority]").forEach((item) => item.classList.remove("active"));
          button.classList.add("active");
          priority = button.dataset.priority;
          applyFilters();
        }});
      }});
      document.querySelectorAll(".category-filters [data-category]").forEach((button) => {{
        button.addEventListener("click", () => {{
          document.querySelectorAll(".category-filters [data-category]").forEach((item) => item.classList.remove("active"));
          button.classList.add("active");
          category = button.dataset.category;
          applyFilters();
        }});
      }});
      search.addEventListener("input", applyFilters);
      applyFilters();
    }})();
  </script>
</body>
</html>
"""
    return _strip_trailing_whitespace(page)


def write_outputs(catalog: dict[str, Any]) -> None:
    """Записывает сгенерированные Markdown и HTML в документацию проекта."""
    MARKDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
    HTML_PATH.parent.mkdir(parents=True, exist_ok=True)
    MARKDOWN_PATH.write_text(render_markdown(catalog), encoding="utf-8", newline="\n")
    HTML_PATH.write_text(render_html(catalog), encoding="utf-8", newline="\n")


def check_outputs(catalog: dict[str, Any]) -> list[Path]:
    """Возвращает отсутствующие или устаревшие сгенерированные файлы."""
    expected = {
        MARKDOWN_PATH: render_markdown(catalog),
        HTML_PATH: render_html(catalog),
    }
    return [
        path
        for path, content in expected.items()
        if not path.exists() or path.read_text(encoding="utf-8") != content
    ]


def parse_args() -> argparse.Namespace:
    """Разбирает аргументы командной строки генератора."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Проверить актуальность артефактов без перезаписи.",
    )
    return parser.parse_args()


def main() -> int:
    """Запускает генерацию либо проверку актуальности каталога."""
    args = parse_args()
    catalog = load_catalog()
    if args.check:
        stale = check_outputs(catalog)
        if stale:
            print("Каталог пользовательских сценариев устарел:", file=sys.stderr)
            for path in stale:
                print(f"  - {path.relative_to(ROOT)}", file=sys.stderr)
            return 1
        print(f"Каталог актуален: {len(catalog['scenarios'])} сценариев.")
        return 0

    write_outputs(catalog)
    print(f"Создано сценариев: {len(catalog['scenarios'])}.")
    print(f"Markdown: {MARKDOWN_PATH.relative_to(ROOT)}")
    print(f"HTML: {HTML_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
