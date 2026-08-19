"""Проверяет поведение, связанное с модулем «test photo prompt contract»."""

from restaurant_bot.integrations.openai_client import _PHOTO_SYSTEM
from restaurant_bot.integrations.openai_prompts import _PHOTO_OBSERVATION_CONTRACT


def test_photo_prompt_excludes_packaging_and_stock_from_order_quantity() -> None:
    """Проверяет, что photo инструкция модели исключает фасовка и остаток из заказ количество."""
    prompt = _PHOTO_SYSTEM.lower()
    assert "остатки" in _PHOTO_SYSTEM
    assert "фактического заказа" in _PHOTO_SYSTEM
    assert "рукописное исправление" in prompt
    assert "handwritten_correction" in prompt
    assert "заменяет старое значение" in prompt
    assert "никогда не суммируй старое и новое" in prompt
    assert "Зал" in _PHOTO_SYSTEM
    assert "Бар" in _PHOTO_SYSTEM
    assert "Кухня" in _PHOTO_SYSTEM
    assert "зачёркнутое" in _PHOTO_SYSTEM
    assert "крайней правой ячейке" in _PHOTO_SYSTEM
    assert "hall, bar, kitchen" in _PHOTO_SYSTEM


def test_photo_observation_contract_prioritizes_order_area_integrity() -> None:
    """Проверяет, что prompt отличает полноту order-area от пустых строк."""
    assert "order_area_complete" in _PHOTO_OBSERVATION_CONTRACT
    assert "uncertain_order_row_count" in _PHOTO_OBSERVATION_CONTRACT
    assert "blank-row omissions" in _PHOTO_OBSERVATION_CONTRACT


def test_photo_prompt_forbids_moving_quantity_between_neighboring_rows() -> None:
    """Фиксирует горизонтальную привязку количества к строке товара."""
    prompt = _PHOTO_SYSTEM.lower()
    assert "не переноси рукописное число в строку выше или ниже" in prompt
    assert "такую строку полностью пропусти" in prompt
    assert "строка ниже" in prompt
    assert "оно относится только к строке ниже" in prompt
    assert "включая пустые поля заказа" in _PHOTO_SYSTEM
    assert "не отбрасывай её" in _PHOTO_SYSTEM
