"""Проверяет подготовку детерминированных view для фото заявки."""

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from restaurant_bot.input.photo_views import prepare_photo_views


def _save_image(path: Path, size: tuple[int, int]) -> None:
    """Создаёт тестовое изображение заданного размера."""
    Image.new("RGB", size, "white").save(path, format="PNG")


def _save_sheet_like_image(path: Path, size: tuple[int, int] = (1400, 600)) -> None:
    """Создаёт минимальный скриншот с товарной, order и summary областями."""
    width, height = size
    header_y = round(height / 6)
    header_bottom = header_y + max(20, round(height * 0.06))
    product_end = round(width * 0.43)
    order_end = round(width * 0.71)
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, header_y, product_end, header_bottom), fill=(45, 105, 78))
    draw.rectangle(
        (product_end, header_y, order_end, header_bottom),
        fill=(255, 220, 170),
    )
    draw.rectangle(
        (order_end, header_y, width - 1, header_bottom),
        fill=(45, 105, 78),
    )
    column_step = max(40, round(width / 14))
    row_step = max(16, round(height / 24))
    for x in range(0, width, column_step):
        draw.line((x, header_bottom + 1, x, height - 1), fill=(190, 190, 190))
    for y in range(header_bottom + 1, height, row_step):
        draw.line((0, y, width - 1, y), fill=(220, 220, 220))
    draw.text((round(width * 0.54), header_bottom + row_step * 2), "3", fill=(20, 20, 20))
    image.save(path, format="PNG")


def _save_light_sheet_with_order_rows(path: Path) -> None:
    """Создаёт светлый Sheets-скриншот с двумя заполненными order-строками."""
    image = Image.new("RGB", (1000, 500), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 100, 700, 118), fill=(205, 225, 210))
    verticals = [20, 120, 320, 400, 450, 500, 550, 700, 760, 820]
    for x in verticals:
        line_color = (0, 0, 0) if x in {550, 700} else (185, 185, 185)
        draw.line((x, 119, x, 400), fill=line_color)
    boundaries = list(range(120, 401, 20))
    for y in boundaries:
        draw.line((0, y, 700, y), fill=(215, 215, 215))
    draw.text((415, 143), "20", fill=(20, 20, 20))
    draw.text((515, 283), "6", fill=(20, 20, 20))
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
    assert preparation.upscale_factor == 2


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
    assert preparation.spreadsheet_layout_detected is True


@pytest.mark.parametrize("size", [(971, 477), (480, 236)])
def test_telegram_compressed_sheet_uses_same_adaptive_table_pipeline(
    tmp_path: Path,
    size: tuple[int, int],
) -> None:
    """Распознаёт сжатый Telegram-скриншот таблицы без абсолютного порога ширины."""
    photo = tmp_path / "telegram-sheet.png"
    _save_sheet_like_image(photo, size)

    preparation = prepare_photo_views(photo, "image/png")

    assert preparation.spreadsheet_layout_detected is True
    assert preparation.dense_table_views_used is True
    assert preparation.table_focus_view_size is not None
    assert preparation.table_focus_view_size[0] < size[0] * preparation.upscale_factor
    assert preparation.table_focus_view_size[1] < size[1] * preparation.upscale_factor


def test_non_sheet_wide_photo_keeps_full_frame(tmp_path: Path) -> None:
    """Не обрезает широкое фото без распознаваемой строки заголовков таблицы."""
    photo = tmp_path / "free-list.png"
    _save_image(photo, (1400, 600))

    preparation = prepare_photo_views(photo, "image/png")

    assert preparation.table_focus_view_size == (2800, 1200)
    assert preparation.spreadsheet_layout_detected is False


def test_small_ordinary_photo_gets_adaptive_reading_scale(tmp_path: Path) -> None:
    """Увеличивает небольшой обычный список без применения табличной обрезки."""
    photo = tmp_path / "small.png"
    _save_image(photo, (800, 600))

    preparation = prepare_photo_views(photo, "image/png")

    assert len(preparation.views) == 1
    assert preparation.views[0].name == "reading_view"
    assert preparation.views[0].width == 1600
    assert preparation.views[0].height == 1200
    assert preparation.upscale_factor == 2
    assert preparation.dense_table_views_used is False


def test_light_sheet_detects_independent_filled_order_row_count(tmp_path: Path) -> None:
    """Считает заполненные order-строки без тёмно-зелёного или тёплого заголовка."""
    photo = tmp_path / "light-sheet.png"
    _save_light_sheet_with_order_rows(photo)

    preparation = prepare_photo_views(photo, "image/png")

    assert preparation.spreadsheet_layout_detected is True
    assert preparation.detected_filled_order_row_count == 2
    assert preparation.table_focus_view_size is not None
    assert preparation.table_focus_view_size[0] < 1000 * preparation.upscale_factor
