"""Готовит детерминированные представления фотографии для vision-вызова."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import cast

from PIL import Image, UnidentifiedImageError

_DENSE_TABLE_MIN_WIDTH = 900
_DENSE_TABLE_MIN_HEIGHT = 200
_DENSE_TABLE_MIN_ASPECT_RATIO = 1.25
_CROP_UPSCALE_FACTOR = 2
_VERY_SHORT_TABLE_FOCUS_MAX_HEIGHT = 320
_SHORT_TABLE_FOCUS_MAX_HEIGHT = 700
_VERY_SHORT_CROP_UPSCALE_FACTOR = 5
_SHORT_CROP_UPSCALE_FACTOR = 3
_SHEET_HEADER_SEARCH_MAX_RATIO = 0.45
_SHEET_HEADER_MIN_WARM_RATIO = 0.08
_SHEET_HEADER_MIN_GREEN_RATIO = 0.08
_SHEET_HEADER_MIN_WARM_SPAN_RATIO = 0.08
_SHEET_FOCUS_RIGHT_MARGIN_RATIO = 0.03


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

    @property
    def table_focus_view_size(self) -> tuple[int, int] | None:
        """Возвращает размер увеличенного полноширинного вида таблицы."""
        return _view_size(self.views, "table_focus")


def prepare_photo_views(path: Path, mime_type: str) -> PhotoImagePreparation:
    """Готовит один полный увеличенный кадр плотной таблицы без потери строк."""
    original_data = path.read_bytes()
    try:
        with Image.open(BytesIO(original_data)) as image:
            width, height = image.size
            if not _is_dense_table(width, height):
                return PhotoImagePreparation(
                    views=(PhotoImageView("original", original_data, mime_type, width, height),),
                    original_width=width,
                    original_height=height,
                    upscale_factor=1,
                    dense_table_views_used=False,
                )

            table_focus_view, upscale_factor = _build_table_focus_view(image)
    except (UnidentifiedImageError, OSError):
        return PhotoImagePreparation(
            views=(PhotoImageView("original", original_data, mime_type, 0, 0),),
            original_width=0,
            original_height=0,
            upscale_factor=1,
            dense_table_views_used=False,
        )

    return PhotoImagePreparation(
        views=(table_focus_view,),
        original_width=width,
        original_height=height,
        upscale_factor=upscale_factor,
        dense_table_views_used=True,
    )


def _is_dense_table(width: int, height: int) -> bool:
    """Определяет широкое изображение с плотной табличной разметкой."""
    return (
        width >= _DENSE_TABLE_MIN_WIDTH
        and height >= _DENSE_TABLE_MIN_HEIGHT
        and width / height >= _DENSE_TABLE_MIN_ASPECT_RATIO
    )


def _build_table_focus_view(image: Image.Image) -> tuple[PhotoImageView, int]:
    """Строит один reading view таблицы и увеличивает его без потери строк."""
    crop = image.crop(_detect_spreadsheet_reading_box(image) or (0, 0, image.width, image.height))
    upscale_factor = _table_focus_upscale_factor(crop.height)
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


def _table_focus_upscale_factor(height: int) -> int:
    """Выбирает увеличение, сохраняющее читаемость коротких широких скриншотов."""
    if height < _VERY_SHORT_TABLE_FOCUS_MAX_HEIGHT:
        return _VERY_SHORT_CROP_UPSCALE_FACTOR
    if height < _SHORT_TABLE_FOCUS_MAX_HEIGHT:
        return _SHORT_CROP_UPSCALE_FACTOR
    return _CROP_UPSCALE_FACTOR


def _detect_spreadsheet_reading_box(image: Image.Image) -> tuple[int, int, int, int] | None:
    """Находит область Google Sheets по цветной строке заголовков, иначе возвращает None."""
    width = int(image.width)
    height = int(image.height)
    if width < 1000 or height < 160:
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
