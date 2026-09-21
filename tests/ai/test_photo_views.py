"""Проверяет подготовку детерминированных view для фото заявки."""

from io import BytesIO
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


def _save_lettered_sheet_like_image(path: Path) -> None:
    """Добавляет полосу N/O/P/Q выше цветных заголовков таблицы."""
    _save_sheet_like_image(path, (1000, 500))
    with Image.open(path) as source:
        image = source.convert("RGB")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 48, 720, 70), fill=(205, 225, 210))
    for x, letter in ((500, "N"), (550, "O"), (600, "P"), (650, "Q")):
        draw.text((x, 52), letter, fill=(20, 20, 20))
    image.save(path, format="PNG")


def _save_lettered_department_sheet(path: Path) -> None:
    """Создаёт лист с тремя буквенными колонками и разными значениями в каждой."""
    image = Image.new("RGB", (1000, 500), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 40, 740, 62), fill=(205, 225, 210))
    for x, letter in ((330, "N"), (420, "O"), (510, "P"), (650, "Q")):
        draw.text((x, 44), letter, fill=(20, 20, 20))
    for x in (300, 390, 480, 570, 740):
        draw.line((x, 63, x, 400), fill=(185, 185, 185))
    for y in range(100, 401, 25):
        draw.line((0, y, 740, y), fill=(215, 215, 215))
    for y, product in (
        (104, "N and P product"),
        (129, "O product"),
        (154, "N and P product two"),
    ):
        draw.text((100, y), product, fill=(20, 20, 20))
    for y, x, quantity in (
        (104, 330, "2"),
        (104, 510, "4"),
        (129, 420, "3"),
        (154, 330, "5"),
        (154, 510, "6"),
    ):
        draw.text((x, y), quantity, fill=(20, 20, 20))
    image.save(path, format="PNG")


def _save_sheet_below_browser_chrome(path: Path, size: tuple[int, int]) -> None:
    """Создаёт сжатый снимок таблицы ниже панели приложения."""
    table_path = path.with_name(f"{path.stem}-table.png")
    _save_lettered_department_sheet(table_path)
    width, height = size
    with Image.open(table_path) as source:
        table = source.convert("RGB").resize(
            (width, round(width / 2)),
            resample=Image.Resampling.LANCZOS,
        )
    image = Image.new("RGB", size, (242, 244, 246))
    draw = ImageDraw.Draw(image)
    for y in range(round(height * 0.08), round(height * 0.55), max(12, height // 18)):
        draw.line((0, y, width - 1, y), fill=(210, 214, 218), width=1)
    image.paste(table, (0, round(height * 0.58)))
    image.save(path, format="JPEG", quality=68)


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


def _save_unlabeled_department_sheet(path: Path) -> None:
    """Создаёт обрезанный снимок с тремя колонками заказа без шапки и букв."""
    image = Image.new("RGB", (764, 340), "white")
    draw = ImageDraw.Draw(image)
    verticals = [20, 110, 310, 350, 390, 430, 550]
    for x in verticals:
        draw.line((x, 40, x, 280), fill=(185, 185, 185))
    for y in range(40, 281, 40):
        draw.line((0, y, 550, y), fill=(215, 215, 215))
    for y, product in (
        (47, "Hot mustard"),
        (87, "Dijon mustard"),
        (127, "Fried onion"),
        (167, "Miso"),
    ):
        draw.text((120, y), product, fill=(20, 20, 20))
    for y, x, quantity in (
        (47, 313, "3"),
        (87, 353, "2"),
        (127, 393, "5"),
        (167, 313, "1"),
        (167, 393, "4"),
    ):
        draw.text((x, y), quantity, fill=(20, 20, 20))
    draw.text((440, 47), "first note", fill=(20, 20, 20))
    image.save(path, format="PNG")


def _save_headerless_sheet_with_bottom_ui(path: Path) -> None:
    """Создаёт обрезанную таблицу с комментариями и нижней панелью приложения."""
    image = Image.new("RGB", (1000, 420), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 700, 19), fill=(250, 225, 190))
    verticals = [20, 120, 320, 400, 450, 500, 550, 700, 760, 820]
    for x in verticals:
        line_color = (0, 0, 0) if x in {550, 700} else (185, 185, 185)
        draw.line((x, 0, x, 340), fill=line_color)
    for y in range(0, 341, 20):
        draw.line((0, y, 700, y), fill=(215, 215, 215))
    draw.text((410, 3), "Hall", fill=(20, 20, 20))
    draw.text((460, 3), "Bar", fill=(20, 20, 20))
    draw.text((510, 3), "Kitchen", fill=(20, 20, 20))
    draw.text((415, 43), "20", fill=(20, 20, 20))
    draw.text((515, 283), "6", fill=(20, 20, 20))
    draw.text((130, 123), "Product without order", fill=(20, 20, 20))
    draw.text((565, 123), "bring tomorrow", fill=(20, 20, 20))
    draw.rectangle((0, 348, 999, 357), fill=(190, 190, 190))
    draw.text((415, 373), "9", fill=(20, 20, 20))
    image.save(path, format="PNG")


def test_wide_photo_preserves_the_full_source_without_guessing_its_type(tmp_path: Path) -> None:
    """Сохраняет широкий кадр целиком, не классифицируя его только по размеру."""
    photo = tmp_path / "wide.png"
    _save_image(photo, (1600, 900))

    preparation = prepare_photo_views(photo, "image/png")

    assert [view.name for view in preparation.views] == ["original"]
    assert preparation.original_width == 1600
    assert preparation.original_height == 900
    assert preparation.dense_table_views_used is False
    assert preparation.upscale_factor == 1
    assert preparation.table_focus_view_size is None
    assert preparation.views[0].mime_type == "image/png"
    assert all(view.width > 0 and view.height > 0 for view in preparation.views)


def test_primary_view_preserves_full_width_and_full_height(tmp_path: Path) -> None:
    """Передаёт весь исходный документ без искусственного увеличения."""
    photo = tmp_path / "wide.png"
    _save_image(photo, (1600, 900))

    preparation = prepare_photo_views(photo, "image/png")
    original = preparation.views[0]

    assert original.width == 1600
    assert original.height == 900
    assert original.width / original.height == pytest.approx(1600 / 900)


def test_wide_screenshot_at_680px_height_keeps_native_dimensions(tmp_path: Path) -> None:
    """Не принимает широкий снимок за таблицу и не увеличивает его по порогу размера."""
    photo = tmp_path / "screenshot.png"
    _save_image(photo, (1280, 680))

    preparation = prepare_photo_views(photo, "image/png")

    assert preparation.dense_table_views_used is False
    assert [view.name for view in preparation.views] == ["original"]
    assert (preparation.views[0].width, preparation.views[0].height) == (1280, 680)
    assert preparation.upscale_factor == 1


def test_short_wide_screenshot_keeps_full_height_without_upscaling(tmp_path: Path) -> None:
    """Не обрезает исходный узкий кадр и не пытается восстановить отсутствующие пиксели."""
    photo = tmp_path / "short-screenshot.png"
    _save_image(photo, (1280, 350))

    preparation = prepare_photo_views(photo, "image/png")

    assert preparation.dense_table_views_used is False
    assert (preparation.views[0].width, preparation.views[0].height) == (1280, 350)
    assert preparation.upscale_factor == 1


def test_very_short_wide_screenshot_is_not_misclassified_by_aspect_ratio(tmp_path: Path) -> None:
    """Сохраняет очень широкий снимок целиком и не выдаёт интерполяцию за детали."""
    photo = tmp_path / "very-short-screenshot.png"
    _save_image(photo, (1280, 210))

    preparation = prepare_photo_views(photo, "image/png")

    assert preparation.dense_table_views_used is False
    assert (preparation.views[0].width, preparation.views[0].height) == (1280, 210)
    assert preparation.upscale_factor == 1


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


def test_sheet_focus_preserves_column_letter_band_above_header(tmp_path: Path) -> None:
    """Не обрезает видимые буквы N/O/P/Q перед заголовком таблицы."""
    photo = tmp_path / "lettered-sheet.png"
    _save_lettered_sheet_like_image(photo)

    preparation = prepare_photo_views(photo, "image/png")

    assert preparation.spreadsheet_layout_detected is True
    with Image.open(BytesIO(preparation.views[0].data)) as focused:
        scale = preparation.upscale_factor
        letter_band_rows = [
            y
            for y in range(focused.height)
            if all(
                abs(channel - expected) <= 10
                for channel, expected in zip(
                    focused.getpixel((10 * scale, y))[:3],
                    (205, 225, 210),
                    strict=True,
                )
            )
        ]
        assert letter_band_rows
        letters = focused.crop(
            (490 * scale, min(letter_band_rows), 670 * scale, max(letter_band_rows) + 1)
        )
        assert any(max(pixel) < 100 for pixel in letters.convert("RGB").getdata())


def test_lettered_department_check_views_isolate_each_order_column(tmp_path: Path) -> None:
    """Строит отдельные выровненные виды N, O и P для перепроверки количеств."""
    photo = tmp_path / "department-columns.png"
    _save_lettered_department_sheet(photo)

    preparation = prepare_photo_views(
        photo,
        "image/png",
        prefer_filled_order_rows=True,
        include_department_column_check=True,
    )

    assert preparation.spreadsheet_layout_detected is True
    assert [view.name for view in preparation.views] == [
        "table_focus",
        "department_column_n",
        "department_column_o",
        "department_column_p",
    ]
    for view in preparation.views:
        assert max(view.width, view.height) <= 6000
        assert view.width * view.height <= 10_000 * 32**2
    expected_colors = ((224, 237, 255), (255, 237, 210), (223, 244, 225))
    for view, expected_color in zip(preparation.views[1:], expected_colors, strict=True):
        with Image.open(BytesIO(view.data)) as focused:
            assert focused.getpixel((0, 0))[:3] == expected_color
            assert focused.width > 500
            assert focused.height > 100
    assert [view.expected_quantity_cell_count for view in preparation.views[1:]] == [2, 1, 2]


@pytest.mark.parametrize("size", [(1000, 1300), (640, 832)])
def test_lettered_sheet_below_application_chrome_survives_resizing_and_compression(
    tmp_path: Path,
    size: tuple[int, int],
) -> None:
    """Находит таблицу ниже панели приложения при разном размере JPEG-снимка."""
    photo = tmp_path / f"chrome-sheet-{size[0]}.jpg"
    _save_sheet_below_browser_chrome(photo, size)

    preparation = prepare_photo_views(
        photo,
        "image/jpeg",
        prefer_filled_order_rows=True,
        include_department_column_check=True,
    )

    assert preparation.spreadsheet_layout_detected is True
    assert preparation.detected_filled_order_row_count == 3
    assert [view.name for view in preparation.views] == [
        "table_focus",
        "department_column_n",
        "department_column_o",
        "department_column_p",
    ]


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
    """Не превращает обычный широкий снимок в сфокусированную таблицу."""
    photo = tmp_path / "free-list.png"
    _save_image(photo, (1400, 600))

    preparation = prepare_photo_views(photo, "image/png")

    assert preparation.views[0].name == "original"
    assert (preparation.views[0].width, preparation.views[0].height) == (1400, 600)
    assert preparation.table_focus_view_size is None
    assert preparation.spreadsheet_layout_detected is False


def test_small_ordinary_photo_keeps_original_pixels(tmp_path: Path) -> None:
    """Не увеличивает небольшой обычный список без подтверждённой области документа."""
    photo = tmp_path / "small.png"
    _save_image(photo, (800, 600))

    preparation = prepare_photo_views(photo, "image/png")

    assert len(preparation.views) == 1
    assert preparation.views[0].name == "original"
    assert preparation.views[0].width == 800
    assert preparation.views[0].height == 600
    assert preparation.upscale_factor == 1
    assert preparation.dense_table_views_used is False


def test_ordinary_retry_does_not_send_duplicate_upscaled_full_frame(tmp_path: Path) -> None:
    """Не отправляет один и тот же кадр дважды после искусственного увеличения."""
    photo = tmp_path / "ordinary.png"
    _save_image(photo, (800, 600))

    preparation = prepare_photo_views(photo, "image/png", include_original=True)

    assert [view.name for view in preparation.views] == ["original"]
    assert preparation.views[0].width == 800


@pytest.mark.parametrize("size", [(320, 180), (1272, 690), (900, 2500), (4032, 3024)])
def test_primary_view_preserves_full_documents_at_varied_dimensions(
    tmp_path: Path,
    size: tuple[int, int],
) -> None:
    """Сохраняет весь список или документ при произвольной ориентации и размере."""
    photo = tmp_path / f"document-{size[0]}x{size[1]}.png"
    _save_image(photo, size)

    preparation = prepare_photo_views(photo, "image/png", include_focus_view=False)
    view = preparation.views[0]

    assert len(preparation.views) == 1
    assert view.name == "original"
    assert view.width <= size[0]
    assert view.height <= size[1]
    assert view.width / view.height == pytest.approx(size[0] / size[1], rel=0.002)
    assert preparation.upscale_factor == 1


def test_primary_view_keeps_context_around_document_in_desktop_screenshot(
    tmp_path: Path,
) -> None:
    """Не вырезает вложенную таблицу из исходного снимка рабочего стола."""
    photo = tmp_path / "desktop.png"
    image = Image.new("RGB", (1000, 1200), (232, 235, 238))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 999, 150), fill=(45, 55, 70))
    draw.rectangle((80, 400, 920, 900), fill="white", outline=(80, 80, 80), width=2)
    draw.line((80, 500, 920, 500), fill=(180, 180, 180), width=2)
    image.save(photo, format="PNG")

    preparation = prepare_photo_views(photo, "image/png", include_focus_view=False)

    with Image.open(BytesIO(preparation.views[0].data)) as view:
        assert view.size == (1000, 1200)
        assert view.getpixel((10, 10)) == (45, 55, 70)
        assert view.getpixel((50, 300)) == (232, 235, 238)


def test_primary_view_applies_exif_orientation_before_recognition(tmp_path: Path) -> None:
    """Поворачивает снятый боком лист по EXIF до отправки модели."""
    photo = tmp_path / "rotated-paper.jpg"
    image = Image.new("RGB", (100, 60), "white")
    ImageDraw.Draw(image).rectangle((0, 0, 25, 20), fill="black")
    exif = Image.Exif()
    exif[274] = 6
    image.save(photo, format="JPEG", exif=exif)

    preparation = prepare_photo_views(photo, "image/jpeg", include_focus_view=False)

    with Image.open(BytesIO(preparation.views[0].data)) as normalized:
        assert normalized.size == (60, 100)
        assert normalized.getexif().get(274, 1) == 1


def test_primary_view_respects_vision_dimension_and_patch_budgets(tmp_path: Path) -> None:
    """Сжимает только изображения сверх лимита API, сохраняя их пропорции."""
    photo = tmp_path / "very-large-screen.png"
    _save_image(photo, (6200, 2500))

    preparation = prepare_photo_views(photo, "image/png", include_focus_view=False)
    view = preparation.views[0]

    assert max(view.width, view.height) <= 6000
    assert view.width * view.height <= 10_000 * 32**2
    assert view.width / view.height == pytest.approx(6200 / 2500, rel=0.002)


def test_tall_table_views_stay_within_vision_limits(tmp_path: Path) -> None:
    """Не увеличивает длинную таблицу выше лимитов API GPT-5.4 Mini."""
    photo = tmp_path / "tall-sheet.png"
    _save_sheet_like_image(photo, (900, 3000))

    preparation = prepare_photo_views(
        photo,
        "image/png",
        include_original=True,
        prefer_filled_order_rows=True,
    )

    assert preparation.spreadsheet_layout_detected is True
    assert preparation.table_focus_view_size is not None
    for view in preparation.views:
        assert max(view.width, view.height) <= 6000
        assert view.width * view.height <= 10_000 * 32**2


def test_light_sheet_detects_independent_filled_order_row_count(tmp_path: Path) -> None:
    """Считает заполненные order-строки без тёмно-зелёного или тёплого заголовка."""
    photo = tmp_path / "light-sheet.png"
    _save_light_sheet_with_order_rows(photo)

    preparation = prepare_photo_views(photo, "image/png")

    assert preparation.spreadsheet_layout_detected is True
    assert preparation.detected_filled_order_row_count == 2
    assert preparation.table_focus_view_size is not None
    assert preparation.table_focus_view_size[0] < 1000 * preparation.upscale_factor


def test_headerless_sheet_focuses_order_and_comment_rows_before_bottom_ui(
    tmp_path: Path,
) -> None:
    """Читает обрезанную сетку без заголовка и не принимает нижнюю панель за товар."""
    photo = tmp_path / "headerless-sheet.png"
    _save_headerless_sheet_with_bottom_ui(photo)

    preparation = prepare_photo_views(
        photo,
        "image/png",
        prefer_filled_order_rows=True,
    )

    assert preparation.spreadsheet_layout_detected is True
    assert preparation.detected_filled_order_row_count == 2
    assert preparation.table_focus_view_size is not None
    assert preparation.table_focus_view_size[1] == 88 * preparation.upscale_factor
    with Image.open(BytesIO(preparation.views[0].data)) as focused:
        scale = preparation.upscale_factor
        product_area = focused.crop((120 * scale, 44 * scale, 320 * scale, 64 * scale))
        assert all(max(pixel) > 245 for pixel in product_area.convert("RGB").getdata())


def test_primary_pass_detects_headerless_grid_without_assigning_departments(
    tmp_path: Path,
) -> None:
    """Распознаёт сетку, но не выводит подразделение из одной геометрии колонок."""
    photo = tmp_path / "unlabeled-departments.png"
    _save_unlabeled_department_sheet(photo)

    preparation = prepare_photo_views(photo, "image/png", include_focus_view=False)

    assert len(preparation.views) == 1
    assert preparation.views[0].name == "original"
    assert preparation.spreadsheet_layout_detected is True
    assert preparation.detected_filled_order_row_count == 4


def test_headerless_column_check_views_preserve_header_context_without_guessing_departments(
    tmp_path: Path,
) -> None:
    """Сохраняет верхний контекст сетки, не добавляя невидимые буквы или названия отделов."""
    photo = tmp_path / "unlabeled-departments.png"
    _save_unlabeled_department_sheet(photo)

    preparation = prepare_photo_views(
        photo,
        "image/png",
        prefer_filled_order_rows=True,
        include_department_column_check=True,
    )

    assert [view.name for view in preparation.views] == [
        "table_focus",
        "department_column_n",
        "department_column_o",
        "department_column_p",
    ]
    assert [view.expected_quantity_cell_count for view in preparation.views[1:]] == [2, 1, 2]
    with Image.open(BytesIO(preparation.views[1].data)) as first_position:
        assert first_position.getpixel((0, 0))[:3] == (224, 237, 255)
        assert first_position.size[0] > 500
        assert first_position.height > 100


@pytest.mark.parametrize("size", [(764, 340), (382, 170), (320, 142)])
def test_light_sheet_geometry_handles_small_images_without_fixed_x_window(
    tmp_path: Path,
    size: tuple[int, int],
) -> None:
    """Обнаруживает табличную сетку после сильного уменьшения кадра."""
    source_path = tmp_path / "source.png"
    _save_unlabeled_department_sheet(source_path)
    image_path = tmp_path / f"scaled-{size[0]}.png"
    with Image.open(source_path) as source:
        scaled = source.resize(size, resample=Image.Resampling.LANCZOS)
        scaled.save(image_path, format="PNG")

    preparation = prepare_photo_views(
        image_path,
        "image/png",
        include_focus_view=False,
    )

    assert preparation.spreadsheet_layout_detected is True


def test_two_column_order_table_is_not_mistaken_for_three_departments(
    tmp_path: Path,
) -> None:
    """Не навязывает отделы обычной таблице с одним столбцом количества."""
    photo = tmp_path / "product-quantity.png"
    image = Image.new("RGB", (764, 340), "white")
    draw = ImageDraw.Draw(image)
    for x in (20, 310, 430, 550):
        draw.line((x, 40, x, 280), fill=(185, 185, 185))
    for y in range(40, 281, 40):
        draw.line((0, y, 550, y), fill=(215, 215, 215))
    draw.text((120, 47), "Mushrooms", fill=(20, 20, 20))
    draw.text((450, 47), "3", fill=(20, 20, 20))
    image.save(photo, format="PNG")

    preparation = prepare_photo_views(photo, "image/png", include_focus_view=False)

    assert preparation.spreadsheet_layout_detected is False
