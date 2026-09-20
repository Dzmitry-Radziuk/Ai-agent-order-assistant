"""Готовит детерминированные представления фотографии для vision-вызова."""

from __future__ import annotations

import math
from dataclasses import dataclass
from io import BytesIO
from itertools import pairwise
from pathlib import Path
from typing import cast

from PIL import Image, ImageOps, UnidentifiedImageError

_DENSE_TABLE_MIN_WIDTH = 900
_DENSE_TABLE_MIN_HEIGHT = 200
_DENSE_TABLE_MIN_ASPECT_RATIO = 1.25
_COMPACT_TABLE_MIN_WIDTH = 320
_COMPACT_TABLE_MIN_HEIGHT = 100
_COMPACT_TABLE_MIN_ASPECT_RATIO = 1.8
_CROP_UPSCALE_FACTOR = 2
_SHEET_HEADER_SEARCH_MAX_RATIO = 0.45
_SHEET_HEADER_MIN_WARM_RATIO = 0.08
_SHEET_HEADER_MIN_GREEN_RATIO = 0.08
_SHEET_HEADER_MIN_WARM_SPAN_RATIO = 0.08
_SHEET_FOCUS_RIGHT_MARGIN_RATIO = 0.03
_SHEET_LAYOUT_MIN_WIDTH = 80
_SHEET_LAYOUT_MIN_HEIGHT = 60
_ORDINARY_READING_TARGET_LONG_EDGE = 1600
_MAX_READING_UPSCALE_FACTOR = 5
_TABLE_READING_TARGET_WIDTH = 2400
_TABLE_READING_TARGET_HEIGHT = 1000
_MAX_TABLE_UPSCALE_FACTOR = 5
_LIGHT_SHEET_HEADER_MIN_RATIO = 0.35
_LIGHT_SHEET_HEADER_SEARCH_MAX_RATIO = 0.55
_LIGHT_SHEET_GRID_VERTICAL_MIN_RATIO = 0.55
_LIGHT_SHEET_GRID_HORIZONTAL_MIN_RATIO = 0.70
_LIGHT_SHEET_MAX_GRID_GROUP_WIDTH = 3
_LIGHT_SHEET_UI_SEPARATOR_MIN_WIDTH = 6
_LIGHT_SHEET_MIN_CELL_INK_PIXELS = 4


@dataclass(frozen=True, slots=True)
class PhotoImageView:
    """Описывает одно представление исходного изображения."""

    name: str
    data: bytes
    mime_type: str
    width: int
    height: int


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
    prefer_filled_order_rows: bool = False,
) -> PhotoImagePreparation:
    """Готовит reading-view и детерминированную проверку заполненных строк заказа."""
    original_data = path.read_bytes()
    try:
        with Image.open(BytesIO(original_data)) as image:
            image = ImageOps.exif_transpose(image)
            width, height = image.size
            reading_box = _detect_spreadsheet_reading_box(image)
            if reading_box is None:
                reading_box = _detect_light_spreadsheet_reading_box(image)
            if reading_box is None and not _is_dense_table(width, height):
                reading_view, upscale_factor = _build_ordinary_reading_view(
                    image,
                    original_data=original_data,
                    mime_type=mime_type,
                )
                return PhotoImagePreparation(
                    views=(reading_view,),
                    original_width=width,
                    original_height=height,
                    upscale_factor=upscale_factor,
                    dense_table_views_used=False,
                    spreadsheet_layout_detected=False,
                )

            filled_order_view: PhotoImageView | None = None
            detected_filled_order_row_count: int | None = None
            filled_order_upscale_factor = 1
            if reading_box is not None:
                (
                    filled_order_view,
                    detected_filled_order_row_count,
                    filled_order_upscale_factor,
                ) = _build_filled_order_rows_view(image)
            table_focus_view, upscale_factor = _build_table_focus_view(
                image,
                reading_box=reading_box,
            )
            if prefer_filled_order_rows and filled_order_view is not None:
                table_focus_view = filled_order_view
                upscale_factor = filled_order_upscale_factor
    except (UnidentifiedImageError, OSError):
        return PhotoImagePreparation(
            views=(PhotoImageView("original", original_data, mime_type, 0, 0),),
            original_width=0,
            original_height=0,
            upscale_factor=1,
            dense_table_views_used=False,
            spreadsheet_layout_detected=False,
        )

    views: tuple[PhotoImageView, ...] = (table_focus_view,)
    if include_original:
        views += (PhotoImageView("original", original_data, mime_type, width, height),)
    return PhotoImagePreparation(
        views=views,
        original_width=width,
        original_height=height,
        upscale_factor=upscale_factor,
        dense_table_views_used=True,
        spreadsheet_layout_detected=reading_box is not None,
        detected_filled_order_row_count=detected_filled_order_row_count,
    )


def _is_dense_table(width: int, height: int) -> bool:
    """Определяет широкое изображение с плотной табличной разметкой."""
    large_table = (
        width >= _DENSE_TABLE_MIN_WIDTH
        and height >= _DENSE_TABLE_MIN_HEIGHT
        and width / height >= _DENSE_TABLE_MIN_ASPECT_RATIO
    )
    compact_wide_table = (
        width >= _COMPACT_TABLE_MIN_WIDTH
        and height >= _COMPACT_TABLE_MIN_HEIGHT
        and width / height >= _COMPACT_TABLE_MIN_ASPECT_RATIO
    )
    return large_table or compact_wide_table


def _build_ordinary_reading_view(
    image: Image.Image,
    *,
    original_data: bytes,
    mime_type: str,
) -> tuple[PhotoImageView, int]:
    """Увеличивает небольшой обычный список до читаемого размера без изменения пропорций."""
    long_edge = max(image.width, image.height)
    upscale_factor = min(
        _MAX_READING_UPSCALE_FACTOR,
        max(1, math.ceil(_ORDINARY_READING_TARGET_LONG_EDGE / max(1, long_edge))),
    )
    if upscale_factor == 1:
        return (
            PhotoImageView(
                "original",
                original_data,
                mime_type,
                image.width,
                image.height,
            ),
            1,
        )
    enlarged = image.resize(
        (image.width * upscale_factor, image.height * upscale_factor),
        resample=Image.Resampling.LANCZOS,
    )
    output = BytesIO()
    enlarged.save(output, format="PNG", optimize=False)
    return (
        PhotoImageView(
            "reading_view",
            output.getvalue(),
            "image/png",
            enlarged.width,
            enlarged.height,
        ),
        upscale_factor,
    )


def _build_table_focus_view(
    image: Image.Image,
    *,
    reading_box: tuple[int, int, int, int] | None,
) -> tuple[PhotoImageView, int]:
    """Строит один reading view таблицы и увеличивает его без потери строк."""
    crop = image.crop(reading_box or (0, 0, image.width, image.height))
    upscale_factor = _table_focus_upscale_factor(crop.width, crop.height)
    enlarged = crop.resize(
        (crop.width * upscale_factor, crop.height * upscale_factor),
        resample=Image.Resampling.LANCZOS,
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


def _detect_light_spreadsheet_reading_box(
    image: Image.Image,
) -> tuple[int, int, int, int] | None:
    """Находит светлую Google Sheets-таблицу по сетке order/comment колонок."""
    geometry = _detect_light_sheet_geometry(image)
    if geometry is None:
        return None
    header_band, order_columns, row_boundaries = geometry
    top = header_band[0] if header_band is not None else row_boundaries[0]
    right = min(image.width, order_columns[-1] + 1)
    bottom = min(image.height, row_boundaries[-1] + 1)
    return 0, top, right, bottom


def _detect_light_sheet_geometry(
    image: Image.Image,
) -> (
    tuple[
        tuple[int, int] | None,
        tuple[int, int, int, int, int],
        list[int],
    ]
    | None
):
    """Находит order-сетку Sheets даже у снимка без строки заголовков."""
    header_band = _light_sheet_header_band(image)
    start_positions = [header_band[1] + 1, 0] if header_band is not None else [0]
    for start_y in start_positions:
        order_columns = _detect_light_sheet_order_columns(image, start_y=start_y)
        if order_columns is None:
            continue
        row_boundaries = _detect_light_sheet_row_boundaries(
            image,
            start_y=start_y,
            right=order_columns[-1],
        )
        if len(row_boundaries) >= 4:
            return header_band, order_columns, row_boundaries
    return None


def _light_sheet_header_band(image: Image.Image) -> tuple[int, int] | None:
    """Ищет светло-зелёную строку букв колонок без привязки к теме оформления."""
    rgb = image.convert("RGB")
    width, height = rgb.size
    max_y = min(height, max(1, round(height * _LIGHT_SHEET_HEADER_SEARCH_MAX_RATIO)))
    step_x = max(1, width // 900)
    candidate_rows: list[int] = []
    for y in range(max_y):
        samples = 0
        matches = 0
        for x in range(0, width, step_x):
            samples += 1
            if _is_light_sheet_header_pixel(cast(tuple[int, int, int], rgb.getpixel((x, y)))):
                matches += 1
        if samples and matches / samples >= _LIGHT_SHEET_HEADER_MIN_RATIO:
            candidate_rows.append(y)
    groups = _contiguous_groups(candidate_rows)
    if not groups:
        return None
    return max(groups, key=lambda group: group[1] - group[0])


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
        and width * 0.30 <= (left + right) / 2 <= width * 0.75
    ]
    best: tuple[float, tuple[int, int, int, int, int]] | None = None
    for index in range(len(centers) - 4):
        candidate = centers[index : index + 5]
        widths = [candidate[offset + 1] - candidate[offset] for offset in range(4)]
        order_widths = widths[:3]
        mean_width = sum(order_widths) / 3
        if mean_width < max(18, width * 0.02) or mean_width > width * 0.12:
            continue
        if min(order_widths) <= 0 or max(order_widths) / min(order_widths) > 1.35:
            continue
        if widths[3] < mean_width * 1.5 or widths[3] > mean_width * 5:
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
) -> tuple[PhotoImageView | None, int | None, int]:
    """Строит второй reading-view только из детерминированно заполненных строк."""
    geometry = _detect_light_sheet_geometry(image)
    if geometry is None:
        return None, None, 1
    header_band, order_columns, row_boundaries = geometry
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
    enlarged = canvas.resize(
        (canvas.width * upscale_factor, canvas.height * upscale_factor),
        resample=Image.Resampling.LANCZOS,
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
        len(filled_rows),
        upscale_factor,
    )


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
    top = max(0, best_y - max(1, band_height * 2))
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
