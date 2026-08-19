"""Проверяет подготовку детерминированных view для фото заявки."""

from pathlib import Path

import pytest
from PIL import Image

from restaurant_bot.input.photo_views import prepare_photo_views


def _save_image(path: Path, size: tuple[int, int]) -> None:
    """Создаёт тестовое изображение заданного размера."""
    Image.new("RGB", size, "white").save(path, format="PNG")


def test_wide_table_prepares_original_and_table_focus_views(tmp_path: Path) -> None:
    """Готовит оригинал и полноширинный table-focus для плотной таблицы."""
    photo = tmp_path / "wide.png"
    _save_image(photo, (1600, 900))

    preparation = prepare_photo_views(photo, "image/png")

    assert [view.name for view in preparation.views] == ["original", "table_focus"]
    assert preparation.original_width == 1600
    assert preparation.original_height == 900
    assert preparation.dense_table_views_used is True
    assert preparation.upscale_factor == 2
    assert preparation.table_focus_view_size == (3200, 1440)
    assert preparation.views[1].mime_type == "image/png"
    assert all(view.width > 0 and view.height > 0 for view in preparation.views)


def test_table_focus_preserves_full_width_and_reduces_vertical_margins(tmp_path: Path) -> None:
    """Сохраняет всю ширину таблицы и уменьшает вертикальные поля."""
    photo = tmp_path / "wide.png"
    _save_image(photo, (1600, 900))

    preparation = prepare_photo_views(photo, "image/png")
    table_focus = preparation.views[1]

    assert table_focus.width == 1600 * preparation.upscale_factor
    assert table_focus.height == 1440
    assert table_focus.width / table_focus.height == pytest.approx(1600 / 720)
    assert table_focus.height < 900 * preparation.upscale_factor


def test_small_ordinary_photo_keeps_original_only(tmp_path: Path) -> None:
    """Не применяет table crops к обычному небольшому изображению."""
    photo = tmp_path / "small.png"
    _save_image(photo, (800, 600))

    preparation = prepare_photo_views(photo, "image/png")

    assert len(preparation.views) == 1
    assert preparation.views[0].name == "original"
    assert preparation.views[0].data == photo.read_bytes()
    assert preparation.dense_table_views_used is False
