"""Проверяет подготовку детерминированных view для фото заявки."""

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from restaurant_bot.input.photo_views import prepare_photo_views


def _save_image(path: Path, size: tuple[int, int]) -> None:
    """Создаёт тестовое изображение заданного размера."""
    Image.new("RGB", size, "white").save(path, format="PNG")


def _save_sheet_like_image(path: Path) -> None:
    """Создаёт минимальный скриншот с товарной, order и summary областями."""
    image = Image.new("RGB", (1400, 600), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 100, 600, 135), fill=(45, 105, 78))
    draw.rectangle((600, 100, 1000, 135), fill=(255, 220, 170))
    draw.rectangle((1000, 100, 1399, 135), fill=(45, 105, 78))
    for x in range(0, 1400, 100):
        draw.line((x, 136, x, 599), fill=(190, 190, 190))
    for y in range(136, 600, 25):
        draw.line((0, y, 1399, y), fill=(220, 220, 220))
    draw.text((760, 190), "3", fill=(20, 20, 20))
    image.save(path, format="PNG")


def test_wide_table_prepares_one_full_table_focus(tmp_path: Path) -> None:
    """Готовит одно полное увеличенное представление таблицы."""
    photo = tmp_path / "wide.png"
    _save_image(photo, (1600, 900))

    preparation = prepare_photo_views(photo, "image/png")

    assert [view.name for view in preparation.views] == ["table_focus"]
    assert preparation.original_width == 1600
    assert preparation.original_height == 900
    assert preparation.dense_table_views_used is True
    assert preparation.upscale_factor == 2
    assert preparation.table_focus_view_size == (3200, 1800)
    assert preparation.views[0].mime_type == "image/png"
    assert all(view.width > 0 and view.height > 0 for view in preparation.views)


def test_table_focus_preserves_full_width_and_full_height(tmp_path: Path) -> None:
    """Сохраняет всю ширину и высоту документа."""
    photo = tmp_path / "wide.png"
    _save_image(photo, (1600, 900))

    preparation = prepare_photo_views(photo, "image/png")
    table_focus = preparation.views[0]

    assert table_focus.width == 1600 * preparation.upscale_factor
    assert table_focus.height == 1800
    assert table_focus.width / table_focus.height == pytest.approx(1600 / 900)
    assert table_focus.height == 900 * preparation.upscale_factor


def test_wide_screenshot_at_680px_height_uses_table_focus(tmp_path: Path) -> None:
    """Считает широкое Telegram-изображение 1280x680 плотным скриншотом таблицы."""
    photo = tmp_path / "screenshot.png"
    _save_image(photo, (1280, 680))

    preparation = prepare_photo_views(photo, "image/png")

    assert preparation.dense_table_views_used is True
    assert [view.name for view in preparation.views] == ["table_focus"]
    assert preparation.upscale_factor == 3


def test_short_wide_screenshot_keeps_full_height_in_table_focus(tmp_path: Path) -> None:
    """Не обрезает верхние строки уже обрезанного широкого скриншота."""
    photo = tmp_path / "short-screenshot.png"
    _save_image(photo, (1280, 350))

    preparation = prepare_photo_views(photo, "image/png")

    assert preparation.dense_table_views_used is True
    assert preparation.table_focus_view_size == (3840, 1050)
    assert preparation.upscale_factor == 3


def test_very_short_wide_screenshot_gets_extra_reading_scale(tmp_path: Path) -> None:
    """Увеличивает короткий широкий скриншот достаточно для чтения строк таблицы."""
    photo = tmp_path / "very-short-screenshot.png"
    _save_image(photo, (1280, 210))

    preparation = prepare_photo_views(photo, "image/png")

    assert preparation.dense_table_views_used is True
    assert preparation.table_focus_view_size == (6400, 1050)
    assert preparation.upscale_factor == 5


def test_sheet_screenshot_focus_excludes_summary_columns(tmp_path: Path) -> None:
    """Ограничивает reading view после order-зоны у скриншота Google Sheets."""
    photo = tmp_path / "sheet.png"
    _save_sheet_like_image(photo)

    preparation = prepare_photo_views(photo, "image/png")

    assert preparation.dense_table_views_used is True
    assert preparation.upscale_factor == 5
    assert preparation.table_focus_view_size is not None
    assert preparation.table_focus_view_size[0] < 1400 * preparation.upscale_factor
    assert preparation.table_focus_view_size[1] < 600 * preparation.upscale_factor


def test_non_sheet_wide_photo_keeps_full_frame(tmp_path: Path) -> None:
    """Не обрезает широкое фото без распознаваемой строки заголовков таблицы."""
    photo = tmp_path / "free-list.png"
    _save_image(photo, (1400, 600))

    preparation = prepare_photo_views(photo, "image/png")

    assert preparation.table_focus_view_size == (4200, 1800)


def test_small_ordinary_photo_keeps_original_only(tmp_path: Path) -> None:
    """Не применяет table crops к обычному небольшому изображению."""
    photo = tmp_path / "small.png"
    _save_image(photo, (800, 600))

    preparation = prepare_photo_views(photo, "image/png")

    assert len(preparation.views) == 1
    assert preparation.views[0].name == "original"
    assert preparation.views[0].data == photo.read_bytes()
    assert preparation.dense_table_views_used is False
