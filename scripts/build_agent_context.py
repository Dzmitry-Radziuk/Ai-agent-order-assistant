"""Создаёт безопасный локальный снимок текущего состояния проекта для AI-агента."""

from __future__ import annotations

import argparse
import ast
import hashlib
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

OUTPUT_PATH = Path(".agents/runtime/CURRENT_CONTEXT.md")
FINGERPRINT_PATTERN = re.compile(r"<!-- context-fingerprint: ([0-9a-f]{64}) -->")
TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
SPECIAL_TEXT_FILES = {"Dockerfile", "Makefile"}


@dataclass(frozen=True, slots=True)
class PythonSymbol:
    """Описывает объявление Python и его положение в исходном файле."""

    path: str
    kind: str
    name: str
    line: int
    end_line: int


@dataclass(frozen=True, slots=True)
class MigrationInfo:
    """Хранит идентификаторы одной миграции Alembic."""

    path: str
    revision: str
    down_revision: str


def run_git(root: Path, *args: str, binary: bool = False) -> str | bytes:
    """Выполняет Git-команду в проекте и возвращает её стандартный вывод."""
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=not binary,
        encoding=None if binary else "utf-8",
    )
    return cast(str | bytes, result.stdout)


def repository_root(start: Path) -> Path:
    """Находит корень Git-репозитория от указанного каталога."""
    output = run_git(start.resolve(), "rev-parse", "--show-toplevel")
    return Path(str(output).strip()).resolve()


def repository_files(root: Path) -> list[Path]:
    """Возвращает отслеживаемые и новые неигнорируемые файлы проекта."""
    output = run_git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return sorted(
        (root / item).resolve()
        for item in str(output).split("\0")
        if item and (root / item).is_file()
    )


def safe_relative(root: Path, path: Path) -> str:
    """Возвращает единообразный относительный путь с прямыми слешами."""
    return path.relative_to(root).as_posix()


def is_text_file(path: Path) -> bool:
    """Определяет текстовый файл по безопасному списку расширений и имён."""
    return path.suffix.lower() in TEXT_SUFFIXES or path.name in SPECIAL_TEXT_FILES


def file_digest(path: Path) -> str:
    """Возвращает короткий SHA-256 файла без публикации его содержимого."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def file_line_count(path: Path) -> int | None:
    """Считает строки текстового файла или возвращает отсутствие значения."""
    if not is_text_file(path):
        return None
    return len(path.read_text(encoding="utf-8", errors="replace").splitlines())


def working_tree_fingerprint(root: Path, files: list[Path]) -> str:
    """Вычисляет отпечаток HEAD и актуального содержимого рабочих файлов."""
    digest = hashlib.sha256()
    head = str(run_git(root, "rev-parse", "HEAD")).strip()
    digest.update(head.encode())
    for path in files:
        relative = safe_relative(root, path)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def git_value(root: Path, *args: str, default: str = "нет") -> str:
    """Читает необязательное Git-значение без падения всего снимка."""
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip() or default


def git_summary(root: Path) -> dict[str, str]:
    """Собирает ветку, commit, upstream, расхождение и рабочий diff."""
    branch = git_value(root, "branch", "--show-current", default="detached HEAD")
    upstream = git_value(root, "rev-parse", "--abbrev-ref", "@{upstream}")
    divergence = "не определено"
    if upstream != "нет":
        divergence = git_value(root, "rev-list", "--left-right", "--count", f"HEAD...{upstream}")
    return {
        "branch": branch,
        "head": git_value(root, "log", "-1", "--format=%H %s"),
        "upstream": upstream,
        "divergence": divergence,
        "status": git_value(root, "status", "--short", "--untracked-files=all", default="чисто"),
    }


def node_name(node: ast.AST, parents: tuple[str, ...]) -> str:
    """Строит квалифицированное имя Python-объявления."""
    name = getattr(node, "name", "<anonymous>")
    return ".".join((*parents, name))


def symbols_from_body(
    path: str,
    body: list[ast.stmt],
    parents: tuple[str, ...] = (),
) -> list[PythonSymbol]:
    """Рекурсивно извлекает классы, функции и методы из AST-модуля."""
    symbols: list[PythonSymbol] = []
    for node in body:
        if isinstance(node, ast.ClassDef):
            symbols.append(
                PythonSymbol(
                    path,
                    "class",
                    node_name(node, parents),
                    node.lineno,
                    node.end_lineno or node.lineno,
                )
            )
            symbols.extend(symbols_from_body(path, node.body, (*parents, node.name)))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            symbols.append(
                PythonSymbol(
                    path,
                    kind,
                    node_name(node, parents),
                    node.lineno,
                    node.end_lineno or node.lineno,
                )
            )
    return symbols


def collect_python_symbols(root: Path, files: list[Path]) -> tuple[list[PythonSymbol], list[str]]:
    """Индексирует Python-объявления и сообщает о файлах с ошибкой разбора."""
    symbols: list[PythonSymbol] = []
    failures: list[str] = []
    for path in files:
        if path.suffix != ".py":
            continue
        relative = safe_relative(root, path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (SyntaxError, UnicodeError) as exc:
            failures.append(f"{relative}: {exc}")
            continue
        symbols.extend(symbols_from_body(relative, tree.body))
    return symbols, failures


def assigned_literal(tree: ast.Module, name: str) -> object | None:
    """Находит простое литеральное присваивание верхнего уровня модуля."""
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == name for target in targets):
            continue
        value = node.value
        if value is None:
            return None
        try:
            return cast(object, ast.literal_eval(value))
        except (ValueError, TypeError):
            return "<dynamic>"
    return None


def collect_migrations(root: Path, files: list[Path]) -> list[MigrationInfo]:
    """Извлекает цепочку revision/down_revision без выполнения миграций."""
    migrations: list[MigrationInfo] = []
    for path in files:
        relative = safe_relative(root, path)
        if not relative.startswith("alembic/versions/") or path.suffix != ".py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        revision = assigned_literal(tree, "revision")
        down_revision = assigned_literal(tree, "down_revision")
        migrations.append(
            MigrationInfo(relative, str(revision or "?"), str(down_revision or "base"))
        )
    return migrations


def collect_setting_names(root: Path) -> list[str]:
    """Читает только имена полей Settings, не раскрывая значения окружения."""
    path = root / "src/restaurant_bot/config.py"
    if not path.exists():
        return []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            return sorted(
                child.target.id
                for child in node.body
                if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name)
            )
    return []


def collect_compose_services(root: Path) -> list[str]:
    """Извлекает имена верхнеуровневых Compose-сервисов без подстановки env."""
    path = root / "docker-compose.yml"
    if not path.exists():
        return []
    services: list[str] = []
    inside_services = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line == "services:":
            inside_services = True
            continue
        if inside_services and line and not line.startswith(" "):
            break
        match = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line) if inside_services else None
        if match:
            services.append(match.group(1))
    return services


def render_file_inventory(root: Path, files: list[Path]) -> list[str]:
    """Формирует таблицу файлов с размером, строками и отпечатком."""
    lines = ["| Файл | Строки | Байт | SHA-256 |", "|---|---:|---:|---|"]
    for path in files:
        count = file_line_count(path)
        lines.append(
            f"| `{safe_relative(root, path)}` | {count if count is not None else 'binary'} "
            f"| {path.stat().st_size} | `{file_digest(path)}` |"
        )
    return lines


def render_symbol_inventory(symbols: list[PythonSymbol]) -> list[str]:
    """Формирует индекс Python-символов с точными диапазонами строк."""
    lines = ["| Файл | Тип | Символ | Строки |", "|---|---|---|---:|"]
    for symbol in symbols:
        lines.append(
            f"| `{symbol.path}` | `{symbol.kind}` | `{symbol.name}` | "
            f"{symbol.line}–{symbol.end_line} |"
        )
    return lines


def render_context(root: Path) -> str:
    """Создаёт Markdown-снимок из актуального состояния репозитория."""
    files = repository_files(root)
    fingerprint = working_tree_fingerprint(root, files)
    git = git_summary(root)
    symbols, parse_failures = collect_python_symbols(root, files)
    migrations = collect_migrations(root, files)
    settings = collect_setting_names(root)
    services = collect_compose_services(root)
    test_symbols = [
        symbol
        for symbol in symbols
        if symbol.path.startswith("tests/") and symbol.name.split(".")[-1].startswith("test_")
    ]
    sections = [
        f"<!-- context-fingerprint: {fingerprint} -->",
        "# Текущий контекст проекта",
        "",
        "> Файл создан автоматически. Не редактировать вручную и не коммитить.",
        "",
        f"- Создан: `{datetime.now(UTC).isoformat(timespec='seconds')}`.",
        f"- Корень: `{root}`.",
        f"- Ветка: `{git['branch']}`.",
        f"- HEAD: `{git['head']}`.",
        f"- Upstream: `{git['upstream']}`.",
        f"- Расхождение HEAD/upstream (слева/справа): `{git['divergence']}`.",
        f"- Файлов в снимке: `{len(files)}`.",
        f"- Python-символов: `{len(symbols)}`.",
        f"- Тестовых функций: `{len(test_symbols)}`.",
        "",
        "## Незакоммиченные изменения",
        "",
        "```text",
        git["status"],
        "```",
        "",
        "## Compose-сервисы",
        "",
        ", ".join(f"`{service}`" for service in services) or "Не найдены.",
        "",
        "## Настройки приложения",
        "",
        "> Только имена полей `Settings`; значения и секреты намеренно не читаются.",
        "",
        ", ".join(f"`{name}`" for name in settings) or "Не найдены.",
        "",
        "## Цепочка миграций",
        "",
        "| Файл | Revision | Down revision |",
        "|---|---|---|",
        *(f"| `{item.path}` | `{item.revision}` | `{item.down_revision}` |" for item in migrations),
        "",
        "## Индекс файлов",
        "",
        *render_file_inventory(root, files),
        "",
        "## Индекс Python-символов",
        "",
        *render_symbol_inventory(symbols),
    ]
    if parse_failures:
        sections.extend(
            ["", "## Ошибки AST-разбора", "", *(f"- {failure}" for failure in parse_failures)]
        )
    sections.append("")
    return "\n".join(sections)


def current_fingerprint(root: Path) -> str:
    """Вычисляет отпечаток актуального дерева для проверки снимка."""
    files = repository_files(root)
    return working_tree_fingerprint(root, files)


def check_snapshot(root: Path, output_path: Path) -> int:
    """Проверяет, соответствует ли существующий снимок рабочему дереву."""
    if not output_path.exists():
        print(f"Agent context is missing: {output_path}", file=sys.stderr)
        return 1
    match = FINGERPRINT_PATTERN.search(output_path.read_text(encoding="utf-8"))
    if match is None or match.group(1) != current_fingerprint(root):
        print("Agent context is stale. Run scripts/build_agent_context.py.", file=sys.stderr)
        return 1
    print(f"Agent context is current: {output_path}")
    return 0


def parse_args() -> argparse.Namespace:
    """Разбирает режим генерации или проверки снимка."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Проверить актуальность без записи.")
    return parser.parse_args()


def main() -> int:
    """Создаёт или проверяет локальный контекст проекта."""
    args = parse_args()
    root = repository_root(Path.cwd())
    output_path = root / OUTPUT_PATH
    if args.check:
        return check_snapshot(root, output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_context(root), encoding="utf-8")
    print(f"Agent context written: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
