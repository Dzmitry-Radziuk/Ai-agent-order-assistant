"""Проверяет локальные ссылки во всех Markdown-файлах проекта."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

LINK_PATTERN = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
EXTERNAL_SCHEMES = ("http://", "https://", "mailto:", "tel:")
IGNORED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".planning",
    ".pytest-run",
    ".pytest_cache",
    ".ruff_cache",
    ".tmp",
    ".venv",
    "test-artifacts",
}


def markdown_files(root: Path) -> list[Path]:
    """Возвращает Markdown-файлы проекта без локальных служебных каталогов."""
    return sorted(
        path
        for path in root.rglob("*.md")
        if not any(part in IGNORED_DIRECTORIES for part in path.relative_to(root).parts)
    )


def normalize_target(raw_target: str) -> str | None:
    """Преобразует Markdown target в проверяемый локальный путь."""
    target = raw_target.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    if not target or target.startswith("#") or target.startswith(EXTERNAL_SCHEMES):
        return None

    target = target.split(maxsplit=1)[0]
    target = unquote(target.split("#", maxsplit=1)[0])
    if not target:
        return None
    return target


def broken_links(root: Path) -> list[str]:
    """Возвращает список несуществующих локальных ссылок."""
    failures: list[str] = []
    for markdown_path in markdown_files(root):
        content = markdown_path.read_text(encoding="utf-8")
        for line_number, line in enumerate(content.splitlines(), start=1):
            for match in LINK_PATTERN.finditer(line):
                target = normalize_target(match.group(1))
                if target is None:
                    continue
                resolved = (
                    root / target.lstrip("/")
                    if target.startswith("/")
                    else markdown_path.parent / target
                ).resolve()
                if not resolved.exists():
                    relative_file = markdown_path.relative_to(root).as_posix()
                    failures.append(f"{relative_file}:{line_number} -> {target}")
    return failures


def main() -> int:
    """Запускает проверку ссылок."""
    root = Path.cwd().resolve()
    failures = broken_links(root)
    if failures:
        print("Broken local Markdown links:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print(f"Markdown links check passed ({len(markdown_files(root))} files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
