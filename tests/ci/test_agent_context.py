"""Проверяет безопасный генератор локального контекста AI-агента."""

from __future__ import annotations

from pathlib import Path

from scripts.build_agent_context import (
    collect_python_symbols,
    file_digest,
    symbols_from_body,
)


def test_symbol_index_contains_classes_methods_and_functions(tmp_path: Path) -> None:
    """Индексирует объявления с квалифицированными именами и строками."""
    source = tmp_path / "sample.py"
    source.write_text(
        'class Service:\n    """Сервис."""\n\n    def run(self):\n        """Запускает."""\n\n\ndef helper():\n    """Помогает."""\n',
        encoding="utf-8",
    )

    symbols, failures = collect_python_symbols(tmp_path, [source])

    assert failures == []
    assert [(symbol.kind, symbol.name) for symbol in symbols] == [
        ("class", "Service"),
        ("def", "Service.run"),
        ("def", "helper"),
    ]
    assert all(symbol.line <= symbol.end_line for symbol in symbols)


def test_invalid_python_is_reported_without_stopping_other_files(tmp_path: Path) -> None:
    """Сохраняет полезный индекс, даже если один новый файл пока синтаксически неполон."""
    valid = tmp_path / "valid.py"
    invalid = tmp_path / "invalid.py"
    valid.write_text('def ready():\n    """Готово."""\n', encoding="utf-8")
    invalid.write_text("def broken(:\n", encoding="utf-8")

    symbols, failures = collect_python_symbols(tmp_path, [valid, invalid])

    assert [symbol.name for symbol in symbols] == ["ready"]
    assert len(failures) == 1
    assert failures[0].startswith("invalid.py:")


def test_file_digest_is_content_sensitive_and_does_not_expose_content(tmp_path: Path) -> None:
    """Публикует только короткий отпечаток, а не содержимое потенциально чувствительного файла."""
    path = tmp_path / "value.txt"
    path.write_text("first-private-value", encoding="utf-8")
    first = file_digest(path)
    path.write_text("second-private-value", encoding="utf-8")
    second = file_digest(path)

    assert first != second
    assert len(first) == len(second) == 12
    assert "private" not in first + second


def test_symbols_from_empty_module_is_empty() -> None:
    """Возвращает пустой индекс для модуля без объявлений."""
    assert symbols_from_body("empty.py", []) == []
