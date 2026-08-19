"""Готовит детерминированные представления фотографии для vision-вызова."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError

_DENSE_TABLE_MIN_WIDTH = 1200
_DENSE_TABLE_MIN_HEIGHT = 700
_DENSE_TABLE_MIN_ASPECT_RATIO = 1.25
_PRODUCT_VIEW_START = 0.0
_PRODUCT_VIEW_END = 0.65
_ORDER_VIEW_START = 0.35
_ORDER_VIEW_END = 0.98
_CROP_UPSCALE_FACTOR = 2


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
    """Содержит исходное изображение и дополнительные view для vision."""

    views: tuple[PhotoImageView, ...]
    original_width: int
    original_height: int
    upscale_factor: int
    dense_table_views_used: bool

    @property
    def product_view_size(self) -> tuple[int, int] | None:
        """Возвращает размер увеличенного товарного view."""
        return _view_size(self.views, "product")

    @property
    def order_view_size(self) -> tuple[int, int] | None:
        """Возвращает размер увеличенного order view."""
        return _view_size(self.views, "order")


def prepare_photo_views(path: Path, mime_type: str) -> PhotoImagePreparation:
    """Готовит максимум три view, сохраняя исходное изображение без изменений."""
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

            product_view = _build_crop_view(
                image,
                "product",
                _PRODUCT_VIEW_START,
                _PRODUCT_VIEW_END,
            )
            order_view = _build_crop_view(
                image,
                "order",
                _ORDER_VIEW_START,
                _ORDER_VIEW_END,
            )
    except (UnidentifiedImageError, OSError):
        return PhotoImagePreparation(
            views=(PhotoImageView("original", original_data, mime_type, 0, 0),),
            original_width=0,
            original_height=0,
            upscale_factor=1,
            dense_table_views_used=False,
        )

    return PhotoImagePreparation(
        views=(
            PhotoImageView("original", original_data, mime_type, width, height),
            product_view,
            order_view,
        ),
        original_width=width,
        original_height=height,
        upscale_factor=_CROP_UPSCALE_FACTOR,
        dense_table_views_used=True,
    )


def _is_dense_table(width: int, height: int) -> bool:
    """Определяет широкое изображение, для которого нужны crops таблицы."""
    return (
        width >= _DENSE_TABLE_MIN_WIDTH
        and height >= _DENSE_TABLE_MIN_HEIGHT
        and width / height >= _DENSE_TABLE_MIN_ASPECT_RATIO
    )


def _build_crop_view(
    image: Image.Image,
    name: str,
    start_ratio: float,
    end_ratio: float,
) -> PhotoImageView:
    """Вырезает перекрывающийся регион и увеличивает его без изменения пропорций."""
    start = max(0, min(image.width - 1, round(image.width * start_ratio)))
    end = max(start + 1, min(image.width, round(image.width * end_ratio)))
    crop = image.crop((start, 0, end, image.height))
    enlarged = crop.resize(
        (crop.width * _CROP_UPSCALE_FACTOR, crop.height * _CROP_UPSCALE_FACTOR),
        resample=Image.Resampling.LANCZOS,
    )
    output = BytesIO()
    enlarged.save(output, format="PNG", optimize=False)
    return PhotoImageView(
        name=name,
        data=output.getvalue(),
        mime_type="image/png",
        width=enlarged.width,
        height=enlarged.height,
    )


def _view_size(views: tuple[PhotoImageView, ...], name: str) -> tuple[int, int] | None:
    """Возвращает размеры view по имени для компактной диагностики."""
    for view in views:
        if view.name == name:
            return view.width, view.height
    return None
