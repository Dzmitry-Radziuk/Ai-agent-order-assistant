from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOTS = ("src", "tests", "scripts", "alembic")


def _missing_docstrings() -> list[str]:
    """Возвращает определения классов и функций без docstring."""
    missing: list[str] = []
    definition_types = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    for root_name in PYTHON_ROOTS:
        for path in sorted((PROJECT_ROOT / root_name).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, definition_types)
                    and ast.get_docstring(node, clean=False) is None
                ):
                    relative_path = path.relative_to(PROJECT_ROOT)
                    missing.append(f"{relative_path}:{node.lineno}:{node.name}")
    return missing


def test_all_classes_functions_and_tests_have_docstrings() -> None:
    """Проверяет наличие docstring у всех классов, функций и тестов проекта."""
    assert _missing_docstrings() == []
