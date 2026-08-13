"""Проверяет зафиксированные границы пакетов проекта."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2] / "src" / "restaurant_bot"
CORE_PACKAGES = ("domain", "conversation", "catalog", "orders", "parsing")
FORBIDDEN_CORE_IMPORTS = {
    "services",
    "workers",
    "api",
    "repositories",
    "integrations",
    "presentation",
}


def _imports(path: Path) -> set[str]:
    """Возвращает верхнеуровневые импорты restaurant_bot из файла."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        else:
            continue
        for name in names:
            parts = name.split(".")
            if len(parts) > 1 and parts[0] == "restaurant_bot":
                result.add(parts[1])
    return result


def test_core_packages_do_not_import_infrastructure_or_presentation() -> None:
    """Не допускает обратных зависимостей из core-пакетов."""
    violations: list[str] = []
    for package in CORE_PACKAGES:
        for path in (ROOT / package).rglob("*.py"):
            forbidden = _imports(path) & FORBIDDEN_CORE_IMPORTS
            violations.extend(f"{path}: {name}" for name in sorted(forbidden))
    assert violations == []


def test_venue_application_contract_has_no_infrastructure_imports() -> None:
    """Проверяет нейтральность контракта регистрации заведения."""
    contract = ROOT / "application" / "venue_registration" / "contracts.py"
    assert _imports(contract) <= {"domain"}
