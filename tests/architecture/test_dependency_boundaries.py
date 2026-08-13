"""Проверяет зафиксированные границы пакетов проекта."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2] / "src" / "restaurant_bot"
CORE_PACKAGES = ("domain", "conversation", "catalog", "orders", "parsing")
CHANNEL_NEUTRAL_APPLICATION_FILES = (
    ROOT / "application" / "conversation" / "contracts.py",
    ROOT / "application" / "conversation" / "use_case.py",
)
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


def test_conversation_application_contract_has_no_channel_adapters() -> None:
    """Не допускает Telegram и инфраструктуру в общем conversation use case."""
    forbidden = {
        "presentation",
        "integrations",
        "repositories",
        "workers",
        "api",
        "services",
    }
    violations = [
        f"{path}: {name}"
        for path in CHANNEL_NEUTRAL_APPLICATION_FILES
        for name in sorted(_imports(path) & forbidden)
    ]
    assert violations == []


def test_conversation_application_source_has_no_telegram_protocol_types() -> None:
    """Проверяет, что общий use case не требует TelegramEvent или callback-протокола."""
    for path in CHANNEL_NEUTRAL_APPLICATION_FILES:
        source = path.read_text(encoding="utf-8")
        assert "TelegramEvent" not in source
        assert "presentation.telegram" not in source
        assert "services.orchestrator" not in source
