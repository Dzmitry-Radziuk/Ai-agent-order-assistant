"""Проверяет поведение, связанное с модулем «test services cleanup»."""

from __future__ import annotations

import ast
from pathlib import Path


def test_parsing_and_integrations_do_not_import_removed_services_text() -> None:
    """Проверяет, что удалённый transitional numeric owner не возвращается."""
    source_root = Path(__file__).parents[2] / "src" / "restaurant_bot"
    forbidden = "restaurant_bot.services.text"
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = {
            node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        }
        imported.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert forbidden not in imported, path

    assert not (source_root / "services" / "text.py").exists()
