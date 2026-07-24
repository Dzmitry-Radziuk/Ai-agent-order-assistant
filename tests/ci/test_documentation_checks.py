from pathlib import Path

from scripts.check_docs_updated import evaluate_documentation_impact
from scripts.check_markdown_links import broken_links, normalize_target


def test_code_change_without_docs_is_detected() -> None:
    code, docs = evaluate_documentation_impact({"src/restaurant_bot/services/engine.py"})

    assert code == {"src/restaurant_bot/services/engine.py"}
    assert docs == set()


def test_code_and_docs_change_passes_impact_classification() -> None:
    code, docs = evaluate_documentation_impact(
        {
            "docker-compose.yml",
            "README-DEVOPS.md",
            "docs/architecture/c4/workspace.dsl",
        }
    )

    assert code == {"docker-compose.yml"}
    assert docs == {
        "README-DEVOPS.md",
        "docs/architecture/c4/workspace.dsl",
    }


def test_tests_only_do_not_require_documentation() -> None:
    code, docs = evaluate_documentation_impact({"tests/services/test_engine.py"})

    assert code == set()
    assert docs == set()


def test_normalize_target_ignores_external_and_anchor_links() -> None:
    assert normalize_target("https://example.com/docs") is None
    assert normalize_target("#local-section") is None
    assert normalize_target("docs/guide.md#start") == "docs/guide.md"


def test_broken_links_reports_missing_target(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("[Missing](docs/missing.md)\n", encoding="utf-8")

    assert broken_links(tmp_path) == ["README.md:1 -> docs/missing.md"]
