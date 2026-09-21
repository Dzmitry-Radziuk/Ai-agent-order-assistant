"""Готовит детерминированные представления фотографии для vision-вызова."""

from __future__ import annotations

import math
from dataclasses import dataclass
from io import BytesIO
from itertools import pairwise
from pathlib import Path
from typing import cast

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError

_CROP_UPSCALE_FACTOR = 2
_SHEET_HEADER_SEARCH_MAX_RATIO = 0.45
_SHEET_HEADER_MIN_WARM_RATIO = 0.08
_SHEET_HEADER_MIN_GREEN_RATIO = 0.08
_SHEET_HEADER_MIN_WARM_SPAN_RATIO = 0.08
_SHEET_HEADER_CONTEXT_MARGIN_RATIO = 0.10
_SHEET_FOCUS_RIGHT_MARGIN_RATIO = 0.03
_SHEET_LAYOUT_MIN_WIDTH = 80
_SHEET_LAYOUT_MIN_HEIGHT = 60
_TABLE_READING_TARGET_WIDTH = 2400
_TABLE_READING_TARGET_HEIGHT = 1000
_MAX_TABLE_UPSCALE_FACTOR = 5
_VISION_MAX_IMAGE_EDGE = 6000
_VISION_MAX_IMAGE_PATCHES = 10_000
_VISION_PATCH_EDGE = 32
_LIGHT_SHEET_HEADER_MIN_RATIO = 0.35
_LIGHT_SHEET_GRID_VERTICAL_MIN_RATIO = 0.55
_LIGHT_SHEET_GRID_HORIZONTAL_MIN_RATIO = 0.70
_LIGHT_SHEET_MAX_GRID_GROUP_WIDTH = 3
_LIGHT_SHEET_UI_SEPARATOR_MIN_WIDTH = 6
_LIGHT_SHEET_MIN_CELL_INK_PIXELS = 4
_LIGHT_SHEET_DETECTION_MAX_EDGE = 1400

_LightSheetGeometry = tuple[
    tuple[int, int] | None,
    tuple[int, int, int, int, int],
    list[int],
]


@dataclass(frozen=True, slots=True)
class PhotoImageView:
    """Описывает одно представление исходного изображения."""

    name: str
    data: bytes
    mime_type: str
    width: int
    height: int
    expected_quantity_cell_count: int | None = None


@dataclass(frozen=True, slots=True)
class PhotoImagePreparation:
    """Содержит каноническое представление изображения для vision."""

    views: tuple[PhotoImageView, ...]
    original_width: int
    original_height: int
    upscale_factor: int
    dense_table_views_used: bool
    spreadsheet_layout_detected: bool
    detected_filled_order_row_count: int | None = None

    @property
    def table_focus_view_size(self) -> tuple[int, int] | None:
        """Возвращает размер увеличенного полноширинного вида таблицы."""
        return _view_size(self.views, "table_focus")


def prepare_photo_views(
    path: Path,
    mime_type: str,
    *,
    include_original: bool = False,
    include_focus_view: bool = True,
    prefer_filled_order_rows: bool = False,
    include_department_column_check: bool = False,
) -> PhotoImagePreparation:
    """Готовит reading-view и детерминированную проверку заполненных строк заказа."""
    original_data = path.read_bytes()
    try:
        with Image.open(BytesIO(original_data)) as source:
            source_format = source.format or ""
            source_orientation = source.getexif().get(274, 1)
            image = ImageOps.exif_transpose(source)
            original_view, original_resize_factor = _build_ordinary_reading_view(
                image,
                original_data=original_data,
                mime_type=mime_type,
                source_format=source_format,
                source_orientation=source_orientation,
            )
            width, height = image.size
            light_sheet_geometry = _detect_light_sheet_geometry(image)
            filled_rows = _filled_order_row_ranges_for_geometry(image, light_sheet_geometry)
            if not include_focus_view:
                return PhotoImagePreparation(
                    views=(original_view,),
                    original_width=width,
                    original_height=height,
                    upscale_factor=original_resize_factor,
                    dense_table_views_used=False,
                    spreadsheet_layout_detected=light_sheet_geometry is not None,
                    detected_filled_order_row_count=(
                        len(filled_rows) if light_sheet_geometry is not None else None
                    ),
                )

            reading_box = _detect_spreadsheet_reading_box(image)
            if reading_box is None:
                reading_box = _light_spreadsheet_reading_box(image, light_sheet_geometry)

            filled_order_view: PhotoImageView | None = None
            detected_filled_order_row_count: int | None = None
            filled_order_upscale_factor = 1
            if reading_box is not None:
                (
                    filled_order_view,
                    detected_filled_order_row_count,
                    filled_order_upscale_factor,
                ) = _build_filled_order_rows_view(
                    image,
                    geometry=light_sheet_geometry,
                    filled_rows=filled_rows,
                )
            table_focus_view: PhotoImageView | None = None
            focus_upscale_factor = 1
            if reading_box is not None:
                table_focus_view, focus_upscale_factor = _build_table_focus_view(
                    image,
                    reading_box=reading_box,
                )
                if prefer_filled_order_rows and filled_order_view is not None:
                    table_focus_view = filled_order_view
                    focus_upscale_factor = filled_order_upscale_factor
            department_check_views = (
                _build_department_column_check_views(
                    image,
                    geometry=light_sheet_geometry,
                    filled_rows=filled_rows,
                )
                if include_department_column_check
                else ()
            )
    except (UnidentifiedImageError, OSError):
        return PhotoImagePreparation(
            views=(PhotoImageView("original", original_data, mime_type, 0, 0),),
            original_width=0,
            original_height=0,
            upscale_factor=1,
            dense_table_views_used=False,
            spreadsheet_layout_detected=False,
        )

    views: tuple[PhotoImageView, ...] = (
        *((table_focus_view,) if table_focus_view is not None else ()),
        *department_check_views,
    )
    if include_original:
        views += (original_view,)
    if not views:
        views = (original_view,)
    return PhotoImagePreparation(
        views=views,
        original_width=width,
        original_height=height,
        upscale_factor=focus_upscale_factor,
        dense_table_views_used=table_focus_view is not None,
        spreadsheet_layout_detected=reading_box is not None,
        detected_filled_order_row_count=detected_filled_order_row_count,
    )


def _build_ordinary_reading_view(
    image: Image.Image,
    *,
    original_data: bytes,
    mime_type: str,
    source_format: str,
    source_orientation: int,
) -> tuple[PhotoImageView, int]:
    """Сохраняет весь кадр и ориентацию EXIF без искусственного увеличения."""
    normalized_image, resize_factor = _fit_image_to_vision_budget(image)
    supported_formats = {
        "JPEG": "image/jpeg",
        "PNG": "image/png",
        "WEBP": "image/webp",
    }
    output_format = source_format.upper()
    output_mime_type = supported_formats.get(output_format, "image/png")
    if resize_factor == 1 and source_orientation in {0, 1} and output_mime_type == mime_type:
        data = original_data
    else:
        if output_format not in supported_formats:
            output_format = "PNG"
            output_mime_type = "image/png"
        if (output_format == "JPEG" and normalized_image.mode not in {"RGB", "L"}) or (
            output_format == "WEBP" and normalized_image.mode not in {"RGB", "RGBA", "L"}
        ):
            normalized_image = normalized_image.convert("RGB")
        output = BytesIO()
        save_options: dict[str, object] = {}
        if output_format == "JPEG":
            save_options = {"quality": 95, "optimize": True, "subsampling": 0}
        elif output_format == "WEBP":
            save_options = {"quality": 95, "method": 4}
        else:
            save_options = {"optimize": True}
        normalized_image.save(output, format=output_format, **save_options)
        data = output.getvalue()
    return (
        PhotoImageView(
            "original",
            data,
            output_mime_type,
            normalized_image.width,
            normalized_image.height,
        ),
        max(1, round(1 / resize_factor)),
    )


def _fit_image_to_vision_budget(image: Image.Image) -> tuple[Image.Image, float]:
    """Снижает слишком большой кадр до ограничений vision API, но не увеличивает его."""
    pixel_budget = _VISION_MAX_IMAGE_PATCHES * _VISION_PATCH_EDGE**2
    scale = min(
        1.0,
        _VISION_MAX_IMAGE_EDGE / max(image.width, image.height, 1),
        math.sqrt(pixel_budget / max(image.width * image.height, 1)),
    )
    if scale >= 1:
        return image, 1.0
    size = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
    return image.resize(size, resample=Image.Resampling.LANCZOS), scale


def _fit_and_upscale_vision_view(
    image: Image.Image,
    requested_factor: int,
) -> tuple[Image.Image, int]:
    """Увеличивает вспомогательный вид, не превышая лимиты vision-модели."""
    fitted, original_scale = _fit_image_to_vision_budget(image)
    pixel_budget = _VISION_MAX_IMAGE_PATCHES * _VISION_PATCH_EDGE**2
    maximum_factor = math.floor(
        min(
            _VISION_MAX_IMAGE_EDGE / max(fitted.width, fitted.height, 1),
            math.sqrt(pixel_budget / max(fitted.width * fitted.height, 1)),
        )
    )
    factor = max(1, min(requested_factor, maximum_factor))
    if factor > 1:
        fitted = fitted.resize(
            (fitted.width * factor, fitted.height * factor),
            resample=Image.Resampling.LANCZOS,
        )
    return fitted, factor if original_scale == 1 else 1


def _build_table_focus_view(
    image: Image.Image,
    *,
    reading_box: tuple[int, int, int, int] | None,
) -> tuple[PhotoImageView, int]:
    """Строит один reading view таблицы и увеличивает его без потери строк."""
    crop = image.crop(reading_box or (0, 0, image.width, image.height))
    enlarged, upscale_factor = _fit_and_upscale_vision_view(
        crop,
        _table_focus_upscale_factor(crop.width, crop.height),
    )
    output = BytesIO()
    enlarged.save(output, format="PNG", optimize=False)
    return (
        PhotoImageView(
            name="table_focus",
            data=output.getvalue(),
            mime_type="image/png",
            width=enlarged.width,
            height=enlarged.height,
        ),
        upscale_factor,
    )


def _table_focus_upscale_factor(width: int, height: int) -> int:
    """Выбирает увеличение, сохраняющее читаемость коротких широких скриншотов."""
    factor = max(
        math.ceil(_TABLE_READING_TARGET_WIDTH / max(1, width)),
        math.ceil(_TABLE_READING_TARGET_HEIGHT / max(1, height)),
    )
    return min(_MAX_TABLE_UPSCALE_FACTOR, max(_CROP_UPSCALE_FACTOR, factor))


def _light_spreadsheet_reading_box(
    image: Image.Image,
    geometry: _LightSheetGeometry | None,
) -> tuple[int, int, int, int] | None:
    """Строит прямоугольник чтения из уже обнаруженной сетки, не повторяя сканирование."""
    if geometry is None:
        return None
    header_band, order_columns, row_boundaries = geometry
    top = header_band[0] if header_band is not None else row_boundaries[0]
    right = min(image.width, order_columns[-1] + 1)
    bottom = min(image.height, row_boundaries[-1] + 1)
    return 0, top, right, bottom


def _detect_light_sheet_geometry(
    image: Image.Image,
) -> _LightSheetGeometry | None:
    """Находит order-сетку Sheets даже у снимка без строки заголовков."""
    if image.width < _SHEET_LAYOUT_MIN_WIDTH or image.height < _SHEET_LAYOUT_MIN_HEIGHT:
        return None
    maximum_edge = max(image.width, image.height)
    if maximum_edge > _LIGHT_SHEET_DETECTION_MAX_EDGE:
        scale = _LIGHT_SHEET_DETECTION_MAX_EDGE / maximum_edge
        reduced = image.resize(
            (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
            resample=Image.Resampling.LANCZOS,
        )
        geometry = _detect_light_sheet_geometry(reduced)
        if geometry is None:
            return None
        header_band, order_columns, row_boundaries = geometry
        restored_header: tuple[int, int] | None = None
        if header_band is not None:
            restored_header = cast(
                tuple[int, int],
                tuple(round(value / scale) for value in header_band),
            )
        return (
            restored_header,
            cast(
                tuple[int, int, int, int, int],
                tuple(round(value / scale) for value in order_columns),
            ),
            [round(value / scale) for value in row_boundaries],
        )

    header_bands = _light_sheet_header_bands(image)
    candidate_starts: list[tuple[int, tuple[int, int] | None]] = [
        (band[1] + 1, band) for band in reversed(header_bands)
    ]
    candidate_starts.append((0, None))
    for start_y, header_band in candidate_starts:
        candidate_columns = _detect_light_sheet_order_columns(image, start_y=start_y)
        if candidate_columns is None:
            continue
        row_boundaries = _detect_light_sheet_row_boundaries(
            image,
            start_y=start_y,
            right=candidate_columns[-1],
        )
        if len(row_boundaries) >= 4:
            return header_band, candidate_columns, row_boundaries
    return None


def _light_sheet_header_band(image: Image.Image) -> tuple[int, int] | None:
    """Ищет светло-зелёную строку букв колонок без привязки к теме оформления."""
    bands = _light_sheet_header_bands(image)
    if not bands:
        return None
    return max(bands, key=lambda group: group[1] - group[0])


def _light_sheet_header_bands(image: Image.Image) -> list[tuple[int, int]]:
    """Находит все кандидаты полосы букв колонок по всему кадру."""
    rgb = image.convert("RGB")
    width, height = rgb.size
    step_x = max(1, width // 900)
    candidate_rows: list[int] = []
    for y in range(height):
        samples = 0
        matches = 0
        for x in range(0, width, step_x):
            samples += 1
            if _is_light_sheet_header_pixel(cast(tuple[int, int, int], rgb.getpixel((x, y)))):
                matches += 1
        if samples and matches / samples >= _LIGHT_SHEET_HEADER_MIN_RATIO:
            candidate_rows.append(y)
    groups = _contiguous_groups(candidate_rows)
    return groups


def _is_light_sheet_header_pixel(pixel: tuple[int, int, int]) -> bool:
    """Отличает нейтральный светло-зелёный header от белого фона."""
    red, green, blue = pixel
    return (
        160 <= red <= 245
        and 170 <= green <= 250
        and 160 <= blue <= 245
        and green - red >= 4
        and green - blue >= 2
        and max(pixel) - min(pixel) <= 55
    )


def _is_sheet_grid_pixel(pixel: tuple[int, int, int]) -> bool:
    """Определяет серый или тёмный нейтральный пиксель линии таблицы."""
    red, green, blue = pixel
    intensity = (red + green + blue) / 3
    return max(pixel) - min(pixel) <= 12 and intensity <= 245


def _contiguous_groups(values: list[int]) -> list[tuple[int, int]]:
    """Склеивает соседние координаты одной линии в диапазоны."""
    if not values:
        return []
    groups: list[tuple[int, int]] = []
    start = values[0]
    previous = values[0]
    for value in values[1:]:
        if value - previous <= 1:
            previous = value
            continue
        groups.append((start, previous))
        start = previous = value
    groups.append((start, previous))
    return groups


def _detect_light_sheet_vertical_groups(
    image: Image.Image,
    *,
    start_y: int,
) -> list[tuple[int, int]]:
    """Находит устойчивые вертикальные линии сетки ниже заголовка."""
    rgb = image.convert("RGB")
    sample_y = list(range(max(0, start_y), rgb.height, 2))
    if not sample_y:
        return []
    candidates: list[int] = []
    for x in range(rgb.width):
        matches = sum(
            _is_sheet_grid_pixel(cast(tuple[int, int, int], rgb.getpixel((x, y)))) for y in sample_y
        )
        if matches / len(sample_y) >= _LIGHT_SHEET_GRID_VERTICAL_MIN_RATIO:
            candidates.append(x)
    return _contiguous_groups(candidates)


def _detect_light_sheet_order_columns(
    image: Image.Image,
    *,
    start_y: int,
) -> tuple[int, int, int, int, int] | None:
    """Находит три узкие order-колонки и следующую широкую comment-колонку."""
    groups = _detect_light_sheet_vertical_groups(image, start_y=start_y)
    width = image.width
    centers = [
        (left + right) / 2
        for left, right in groups
        if right - left <= _LIGHT_SHEET_MAX_GRID_GROUP_WIDTH
    ]
    best: tuple[float, tuple[int, int, int, int, int]] | None = None
    for index in range(len(centers) - 4):
        candidate = centers[index : index + 5]
        widths = [candidate[offset + 1] - candidate[offset] for offset in range(4)]
        order_widths = widths[:3]
        mean_width = sum(order_widths) / 3
        if mean_width < max(8, width * 0.01) or mean_width > width * 0.18:
            continue
        if min(order_widths) <= 0 or max(order_widths) / min(order_widths) > 1.8:
            continue
        if widths[3] < mean_width * 1.25 or widths[3] > mean_width * 10:
            continue
        score = sum(abs(value - mean_width) for value in order_widths)
        rounded = cast(
            tuple[int, int, int, int, int],
            tuple(round(value) for value in candidate),
        )
        if best is None or score < best[0]:
            best = score, rounded
    return None if best is None else best[1]


def _detect_light_sheet_horizontal_groups(
    image: Image.Image,
    *,
    start_y: int,
    right: int,
) -> list[tuple[int, int]]:
    """Находит горизонтальные границы строк внутри товарной и order-зоны."""
    rgb = image.convert("RGB")
    sample_x = list(range(0, min(rgb.width, right + 1), 2))
    if not sample_x:
        return []
    candidates: list[int] = []
    for y in range(max(0, start_y), rgb.height):
        matches = sum(
            _is_sheet_grid_pixel(cast(tuple[int, int, int], rgb.getpixel((x, y)))) for x in sample_x
        )
        if matches / len(sample_x) >= _LIGHT_SHEET_GRID_HORIZONTAL_MIN_RATIO:
            candidates.append(y)
    return _contiguous_groups(candidates)


def _detect_light_sheet_row_boundaries(
    image: Image.Image,
    *,
    start_y: int,
    right: int,
) -> list[int]:
    """Возвращает границы видимых строк и отсекает нижний интерфейс Sheets."""
    groups = _detect_light_sheet_horizontal_groups(
        image,
        start_y=start_y,
        right=right,
    )
    points: list[int] = []
    for top, bottom in groups:
        group_width = bottom - top + 1
        if len(points) >= 3 and group_width >= _LIGHT_SHEET_UI_SEPARATOR_MIN_WIDTH:
            break
        if bottom - top <= _LIGHT_SHEET_MAX_GRID_GROUP_WIDTH:
            points.append(round((top + bottom) / 2))
    if len(points) < 3:
        return points
    gaps = [right_y - left_y for left_y, right_y in pairwise(points)]
    positive_gaps = sorted(gap for gap in gaps if gap > 0)
    if not positive_gaps:
        return points
    typical_gap = positive_gaps[len(positive_gaps) // 2]
    maximum_row_gap = max(30, round(typical_gap * 2.2))
    for index, gap in enumerate(gaps):
        if index >= 2 and gap > maximum_row_gap:
            return points[: index + 1]
    return points


def _sheet_cell_ink_pixels(
    image: Image.Image,
    *,
    left: int,
    right: int,
    top: int,
    bottom: int,
) -> int:
    """Считает тёмные пиксели внутри ячейки без её рамки."""
    count = 0
    for y in range(top + 2, max(top + 2, bottom - 2)):
        for x in range(left + 2, max(left + 2, right - 2)):
            red, green, blue = cast(tuple[int, int, int], image.getpixel((x, y)))
            if max(red, green, blue) < 170:
                count += 1
    return count


def _sheet_row_has_order_header_fill(
    image: Image.Image,
    *,
    order_columns: tuple[int, int, int, int, int],
    top: int,
    bottom: int,
) -> bool:
    """Отличает тёплую строку заголовков Зал/Бар/Кухня от заказа."""
    rgb = image.convert("RGB")
    samples = 0
    warm = 0
    step_y = max(1, (bottom - top) // 8)
    for y in range(top + 2, max(top + 3, bottom - 2), step_y):
        for x in range(order_columns[0] + 2, order_columns[3] - 2, 3):
            red, green, blue = cast(tuple[int, int, int], rgb.getpixel((x, y)))
            samples += 1
            if (
                red >= 190
                and green >= 140
                and blue <= 235
                and red - green >= 8
                and red - blue >= 20
            ):
                warm += 1
    return bool(samples) and warm / samples >= 0.25


def _filled_order_row_ranges(
    image: Image.Image,
    *,
    order_columns: tuple[int, int, int, int, int],
    row_boundaries: list[int],
) -> list[tuple[int, int]]:
    """Выбирает строки с реальным ink в одной из трёх order-ячеек."""
    rgb = image.convert("RGB")
    filled: list[tuple[int, int]] = []
    for top, bottom in pairwise(row_boundaries):
        if bottom - top < 5:
            continue
        if _sheet_row_has_order_header_fill(
            rgb,
            order_columns=order_columns,
            top=top,
            bottom=bottom,
        ):
            continue
        cell_counts = [
            _sheet_cell_ink_pixels(
                rgb,
                left=order_columns[index],
                right=order_columns[index + 1],
                top=top,
                bottom=bottom,
            )
            for index in range(3)
        ]
        if max(cell_counts, default=0) >= _LIGHT_SHEET_MIN_CELL_INK_PIXELS:
            filled.append((top, bottom))
    return filled


def _filled_order_row_ranges_for_geometry(
    image: Image.Image,
    geometry: _LightSheetGeometry | None,
) -> list[tuple[int, int]]:
    """Считает заполненные строки, если сеточный детектор подтвердил структуру."""
    if geometry is None:
        return []
    _, order_columns, row_boundaries = geometry
    return _filled_order_row_ranges(
        image,
        order_columns=order_columns,
        row_boundaries=row_boundaries,
    )


def _relevant_sheet_row_ranges(
    image: Image.Image,
    *,
    order_columns: tuple[int, int, int, int, int],
    row_boundaries: list[int],
) -> list[tuple[int, int]]:
    """Сохраняет строки с заказом и отдельные строки комментариев."""
    rgb = image.convert("RGB")
    relevant: list[tuple[int, int]] = []
    for top, bottom in pairwise(row_boundaries):
        if bottom - top < 5:
            continue
        cell_counts = [
            _sheet_cell_ink_pixels(
                rgb,
                left=order_columns[index],
                right=order_columns[index + 1],
                top=top,
                bottom=bottom,
            )
            for index in range(4)
        ]
        if max(cell_counts, default=0) >= _LIGHT_SHEET_MIN_CELL_INK_PIXELS:
            relevant.append((top, bottom))
    return relevant


def _build_filled_order_rows_view(
    image: Image.Image,
    *,
    geometry: _LightSheetGeometry | None = None,
    filled_rows: list[tuple[int, int]] | None = None,
) -> tuple[PhotoImageView | None, int | None, int]:
    """Строит второй reading-view только из детерминированно заполненных строк."""
    geometry = geometry or _detect_light_sheet_geometry(image)
    if geometry is None:
        return None, None, 1
    header_band, order_columns, row_boundaries = geometry
    if filled_rows is None:
        filled_rows = _filled_order_row_ranges(
            image,
            order_columns=order_columns,
            row_boundaries=row_boundaries,
        )
    if not filled_rows:
        return None, 0, 1
    relevant_rows = _relevant_sheet_row_ranges(
        image,
        order_columns=order_columns,
        row_boundaries=row_boundaries,
    )

    source = image.convert("RGB")
    right = min(source.width, order_columns[-1] + 1)
    header = (
        source.crop((0, header_band[0], right, row_boundaries[0]))
        if header_band is not None
        else None
    )
    separator = 2
    canvas_height = (header.height if header is not None else 0) + sum(
        bottom - top + separator for top, bottom in relevant_rows
    )
    canvas = Image.new("RGB", (right, canvas_height), "white")
    cursor_y = 0
    full_row_set = set(filled_rows)
    full_row_set.update(
        (top, bottom)
        for top, bottom in relevant_rows
        if _sheet_row_has_order_header_fill(
            source,
            order_columns=order_columns,
            top=top,
            bottom=bottom,
        )
    )
    if header is not None:
        canvas.paste(header, (0, cursor_y))
        cursor_y += header.height
    for top, bottom in relevant_rows:
        if (top, bottom) in full_row_set:
            row = source.crop((0, top, right, bottom))
        else:
            row = Image.new("RGB", (right, bottom - top), "white")
            comment = source.crop((order_columns[3], top, right, bottom))
            row.paste(comment, (order_columns[3], 0))
        canvas.paste(row, (0, cursor_y))
        cursor_y += row.height + separator

    upscale_factor = _table_focus_upscale_factor(canvas.width, canvas.height)
    enlarged, upscale_factor = _fit_and_upscale_vision_view(canvas, upscale_factor)
    output = BytesIO()
    enlarged.save(output, format="PNG", optimize=False)
    return (
        PhotoImageView(
            name="table_focus",
            data=output.getvalue(),
            mime_type="image/png",
            width=enlarged.width,
            height=enlarged.height,
        ),
        len(filled_rows),
        upscale_factor,
    )


def _build_department_column_check_views(
    image: Image.Image,
    *,
    geometry: _LightSheetGeometry | None = None,
    filled_rows: list[tuple[int, int]] | None = None,
) -> tuple[PhotoImageView, ...]:
    """Готовит отдельные выровненные виды колонок заказа вместе с названием товара."""
    geometry = geometry or _detect_light_sheet_geometry(image)
    if geometry is None:
        return ()
    header_band, order_columns, row_boundaries = geometry
    if filled_rows is None:
        filled_rows = _filled_order_row_ranges(
            image,
            order_columns=order_columns,
            row_boundaries=row_boundaries,
        )
    if not filled_rows:
        return ()

    source = image.convert("RGB")
    left_end = order_columns[0]
    header_top, header_bottom = (
        (header_band[0], row_boundaries[0]) if header_band is not None else (0, row_boundaries[0])
    )
    header_height = max(0, header_bottom - header_top)
    separator = max(2, round(image.width * 0.002))
    title_height = max(28, round(image.width * 0.024))
    labels = (
        ("n", (224, 237, 255)),
        ("o", (255, 237, 210)),
        ("p", (223, 244, 225)),
    )
    views: list[PhotoImageView] = []
    for column_index, (letter, title_color) in enumerate(labels):
        column_left = order_columns[column_index]
        column_right = order_columns[column_index + 1]
        panel_width = left_end + separator + column_right - column_left
        panel_height = (
            title_height
            + header_height
            + sum(bottom - top + separator for top, bottom in filled_rows)
        )
        panel = Image.new("RGB", (panel_width, panel_height), "white")
        draw = ImageDraw.Draw(panel)
        draw.rectangle((0, 0, panel_width, title_height), fill=title_color)
        column_label = (
            f"ORDER COLUMN {column_index + 1}"
            if header_band is None
            else f"COLUMN {letter.upper()}"
        )
        draw.text((8, 5), column_label, fill=(20, 20, 20))

        cursor_y = title_height
        if header_height:
            panel.paste(source.crop((0, header_top, left_end, header_bottom)), (0, cursor_y))
            panel.paste(
                source.crop((column_left, header_top, column_right, header_bottom)),
                (left_end + separator, cursor_y),
            )
            cursor_y += header_height
        for top, bottom in filled_rows:
            panel.paste(source.crop((0, top, left_end, bottom)), (0, cursor_y))
            panel.paste(
                source.crop((column_left, top, column_right, bottom)),
                (left_end + separator, cursor_y),
            )
            cursor_y += bottom - top + separator

        requested_factor = min(3, max(2, math.ceil(1200 / max(1, panel.width))))
        enlarged, _ = _fit_and_upscale_vision_view(panel, requested_factor)
        output = BytesIO()
        enlarged.save(output, format="PNG", optimize=False)
        views.append(
            PhotoImageView(
                name=f"department_column_{letter}",
                data=output.getvalue(),
                mime_type="image/png",
                width=enlarged.width,
                height=enlarged.height,
                expected_quantity_cell_count=sum(
                    _sheet_cell_ink_pixels(
                        source,
                        left=column_left,
                        right=column_right,
                        top=top,
                        bottom=bottom,
                    )
                    >= _LIGHT_SHEET_MIN_CELL_INK_PIXELS
                    for top, bottom in filled_rows
                ),
            )
        )
    return tuple(views)


def _detect_spreadsheet_reading_box(image: Image.Image) -> tuple[int, int, int, int] | None:
    """Находит область Google Sheets по цветной строке заголовков, иначе возвращает None."""
    width = int(image.width)
    height = int(image.height)
    if width < _SHEET_LAYOUT_MIN_WIDTH or height < _SHEET_LAYOUT_MIN_HEIGHT:
        return None

    rgb = image.convert("RGB")
    sample_step = max(1, width // 900)
    max_y = min(height, max(1, round(height * _SHEET_HEADER_SEARCH_MAX_RATIO)))
    best_y = 0
    best_score = 0.0
    best_warm_ratio = 0.0
    best_green_ratio = 0.0

    for y in range(0, max_y, max(1, height // 240)):
        warm = 0
        green = 0
        total = 0
        for x in range(0, width, sample_step):
            red, g, blue = cast(tuple[int, int, int], rgb.getpixel((x, y)))
            total += 1
            if red >= 150 and g >= 105 and red - blue >= 25 and red - g >= 10:
                warm += 1
            if g >= 45 and g >= red * 1.25 and g >= blue * 0.9 and red <= 110:
                green += 1
        if not total:
            continue
        warm_ratio = warm / total
        green_ratio = green / total
        score = warm_ratio + green_ratio
        if score > best_score:
            best_y = y
            best_score = score
            best_warm_ratio = warm_ratio
            best_green_ratio = green_ratio

    if (
        best_warm_ratio < _SHEET_HEADER_MIN_WARM_RATIO
        or best_green_ratio < _SHEET_HEADER_MIN_GREEN_RATIO
    ):
        return None

    band_height = max(1, height // 240)
    warm_columns: list[int] = []
    for x in range(width):
        matches = 0
        for y in range(max(0, best_y - band_height), min(height, best_y + band_height + 1)):
            red, g, blue = cast(tuple[int, int, int], rgb.getpixel((x, y)))
            if red >= 150 and g >= 105 and red - blue >= 25 and red - g >= 10:
                matches += 1
        if matches >= 1:
            warm_columns.append(x)

    if not warm_columns:
        return None
    warm_start = min(warm_columns)
    warm_end = max(warm_columns)
    if warm_end - warm_start < width * _SHEET_HEADER_MIN_WARM_SPAN_RATIO:
        return None

    right_of_warm_green = 0
    for x in range(warm_end + 1, width):
        red, g, blue = cast(tuple[int, int, int], rgb.getpixel((x, best_y)))
        if g >= 45 and g >= red * 1.25 and g >= blue * 0.9 and red <= 110:
            right_of_warm_green += 1
    if right_of_warm_green < width * 0.04:
        return None

    right = min(
        width,
        warm_end + max(1, round(width * _SHEET_FOCUS_RIGHT_MARGIN_RATIO)),
    )
    top = max(
        0,
        best_y
        - max(
            1,
            band_height * 2,
            round(height * _SHEET_HEADER_CONTEXT_MARGIN_RATIO),
        ),
    )
    column_letter_band = _light_sheet_header_band(image)
    if column_letter_band is not None and column_letter_band[1] < best_y:
        top = min(top, column_letter_band[0])
    bottom = _detect_order_content_bottom(
        rgb,
        width=width,
        height=height,
        start_y=min(height, best_y + band_height * 3),
        left=warm_start,
        right=warm_end,
        sample_step=sample_step,
    )
    if right <= warm_start or right <= 0 or top >= height:
        return None
    return 0, top, right, bottom


def _detect_order_content_bottom(
    image: Image.Image,
    *,
    width: int,
    height: int,
    start_y: int,
    left: int,
    right: int,
    sample_step: int,
) -> int:
    """Обрезает только подтверждённый пустой хвост после order/comment области."""
    dark_row_threshold = max(3, (right - left) // 160)
    last_content_y: int | None = None
    for y in range(start_y, height):
        dark_pixels = 0
        for x in range(left, right + 1, sample_step):
            red, green, blue = cast(tuple[int, int, int], image.getpixel((x, y)))
            if max(red, green, blue) < 170:
                dark_pixels += 1
        if dark_pixels >= dark_row_threshold:
            last_content_y = y

    if last_content_y is None:
        return height
    blank_tail = height - last_content_y
    minimum_tail = max(24, height // 12)
    if blank_tail <= minimum_tail:
        return height
    return min(height, last_content_y + max(12, height // 30))


def _view_size(views: tuple[PhotoImageView, ...], name: str) -> tuple[int, int] | None:
    """Возвращает размеры view по имени для компактной диагностики."""
    for view in views:
        if view.name == name:
            return view.width, view.height
    return None
