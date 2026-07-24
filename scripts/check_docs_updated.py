"""Проверяет, что значимые изменения кода сопровождаются документацией."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Iterable

ZERO_SHA = "0" * 40

CODE_PREFIXES = (
    "src/restaurant_bot/",
    "alembic/",
)
CODE_FILES = {
    ".env.example",
    ".gitlab-ci.yml",
    "Dockerfile",
    "Makefile",
    "docker-compose.yml",
    "docker-compose.prod.yml",
    "pyproject.toml",
    "scripts/check_docs_updated.py",
    "scripts/check_markdown_links.py",
}
DOCUMENTATION_FILES = {
    "README.md",
    "README-DEVOPS.md",
    "SECURITY.md",
}
DOCUMENTATION_PREFIXES = ("docs/",)


def run_git(*args: str) -> str:
    """Выполняет Git-команду и возвращает стандартный вывод."""
    result = subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def git_object_exists(reference: str) -> bool:
    """Проверяет, существует ли Git-объект."""
    if not reference or reference == ZERO_SHA:
        return False
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{reference}^{{commit}}"],
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def resolve_base(explicit_base: str | None, head: str) -> str:
    """Определяет базовый commit для merge request, push или локального запуска."""
    candidates = (
        explicit_base,
        os.getenv("CI_MERGE_REQUEST_DIFF_BASE_SHA"),
        os.getenv("CI_COMMIT_BEFORE_SHA"),
    )
    for candidate in candidates:
        if candidate and git_object_exists(candidate):
            return candidate

    parent = f"{head}^"
    if git_object_exists(parent):
        return parent

    return run_git("hash-object", "-t", "tree", os.devnull)


def changed_files(base: str, head: str) -> set[str]:
    """Возвращает изменённые пути между двумя Git-объектами."""
    output = run_git("diff", "--name-only", "--diff-filter=ACDMRT", base, head)
    return {line.strip().replace("\\", "/") for line in output.splitlines() if line.strip()}


def is_code_change(path: str) -> bool:
    """Определяет, влияет ли файл на поведение, сборку или эксплуатацию."""
    return path in CODE_FILES or path.startswith(CODE_PREFIXES)


def is_documentation_change(path: str) -> bool:
    """Определяет, относится ли изменённый файл к документации."""
    return path in DOCUMENTATION_FILES or path.startswith(DOCUMENTATION_PREFIXES)


def evaluate_documentation_impact(paths: Iterable[str]) -> tuple[set[str], set[str]]:
    """Разделяет изменения на значимый код и документацию."""
    normalized = {path.replace("\\", "/") for path in paths}
    code = {path for path in normalized if is_code_change(path)}
    docs = {path for path in normalized if is_documentation_change(path)}
    return code, docs


def parse_args() -> argparse.Namespace:
    """Читает аргументы командной строки."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="Базовый commit; по умолчанию определяется из GitLab CI.")
    parser.add_argument("--head", default="HEAD", help="Проверяемый commit, по умолчанию HEAD.")
    return parser.parse_args()


def main() -> int:
    """Запускает проверку документационного влияния."""
    args = parse_args()
    base = resolve_base(args.base, args.head)
    paths = changed_files(base, args.head)
    code_changes, docs_changes = evaluate_documentation_impact(paths)

    print(f"Documentation impact range: {base}..{args.head}")
    if not code_changes:
        print("No documentation-impacting code changes detected.")
        return 0

    if docs_changes:
        print("Documentation impact check passed.")
        print("Code changes:")
        for path in sorted(code_changes):
            print(f"  - {path}")
        print("Documentation changes:")
        for path in sorted(docs_changes):
            print(f"  - {path}")
        return 0

    print(
        "Documentation impact check failed: code or infrastructure changed, "
        "but README.md, README-DEVOPS.md, SECURITY.md or docs/ was not updated.",
        file=sys.stderr,
    )
    for path in sorted(code_changes):
        print(f"  - {path}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
