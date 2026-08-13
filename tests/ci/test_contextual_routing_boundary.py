from __future__ import annotations

import ast
from pathlib import Path


def test_contextual_policy_does_not_depend_on_telegram_protocol() -> None:
    """Проверяет, что contextual policy не знает транспорт и callback-протокол Telegram."""
    module_path = (
        Path(__file__).parents[2]
        / "src"
        / "restaurant_bot"
        / "conversation"
        / "routing"
        / "contextual_commands.py"
    )
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_modules.update(
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    )

    assert not any(
        module.startswith(
            (
                "restaurant_bot.input",
                "restaurant_bot.presentation",
                "restaurant_bot.integrations",
                "restaurant_bot.services",
            )
        )
        for module in imported_modules
    )
    assert "TelegramEvent" not in source
    assert "v2:" not in source
