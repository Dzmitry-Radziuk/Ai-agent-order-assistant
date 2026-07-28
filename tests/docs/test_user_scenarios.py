"""Проверяет пользовательский каталог сценариев и его сгенерированные представления."""

from scripts.generate_user_scenarios import (
    HTML_PATH,
    MARKDOWN_PATH,
    check_outputs,
    load_catalog,
)


def test_catalog_references_only_existing_pytest_tests() -> None:
    """Проверяет загрузку каталога вместе со всеми ссылками на pytest."""
    catalog = load_catalog()

    assert len(catalog["scenarios"]) >= 30


def test_every_critical_scenario_has_an_automated_check() -> None:
    """Проверяет наличие автоматической защиты у каждого критического сценария."""
    catalog = load_catalog()
    critical = [scenario for scenario in catalog["scenarios"] if scenario["priority"] == "critical"]

    assert critical
    assert all(scenario["test_refs"] for scenario in critical)


def test_generated_scenario_views_are_current() -> None:
    """Проверяет соответствие Markdown и HTML единому JSON-источнику."""
    catalog = load_catalog()

    assert check_outputs(catalog) == []


def test_markdown_contains_mermaid_and_all_scenario_ids() -> None:
    """Проверяет наличие схем и каждой карточки в Markdown-представлении."""
    catalog = load_catalog()
    markdown = MARKDOWN_PATH.read_text(encoding="utf-8")

    assert markdown.count("```mermaid") == len(catalog["journeys"])
    assert all(f"#### {scenario['id']} ·" in markdown for scenario in catalog["scenarios"])


def test_every_journey_is_linked_to_tested_scenarios() -> None:
    """Связывает каждый шаговый маршрут с покрытыми pytest сценариями."""
    catalog = load_catalog()
    scenarios = {scenario["id"]: scenario for scenario in catalog["scenarios"]}

    for journey in catalog["journeys"]:
        assert journey["scenario_ids"]
        assert all(scenarios[scenario_id]["test_refs"] for scenario_id in journey["scenario_ids"])


def test_html_catalog_is_autonomous_and_filterable() -> None:
    """Проверяет автономность HTML и наличие поиска по карточкам."""
    catalog = load_catalog()
    page = HTML_PATH.read_text(encoding="utf-8")

    assert "https://cdn." not in page
    assert 'id="search"' in page
    assert 'class="scenario-card"' in page
    assert page.count('class="scenario-card"') == len(catalog["scenarios"])
    assert all(
        f'href="#scenario-{scenario_id}"' in page
        for journey in catalog["journeys"]
        for scenario_id in journey["scenario_ids"]
    )
