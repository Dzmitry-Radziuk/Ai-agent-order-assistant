from restaurant_bot.integrations.openai_client import _PHOTO_SYSTEM


def test_photo_prompt_excludes_packaging_and_stock_from_order_quantity() -> None:
    assert "остатки" in _PHOTO_SYSTEM
    assert "фактического заказа" in _PHOTO_SYSTEM
    assert "рукописные исправления" in _PHOTO_SYSTEM
    assert "Зал" in _PHOTO_SYSTEM
    assert "Бар" in _PHOTO_SYSTEM
    assert "Кухня" in _PHOTO_SYSTEM
    assert "зачёркнутое" in _PHOTO_SYSTEM
    assert "крайней правой ячейке" in _PHOTO_SYSTEM
    assert "hall, bar, kitchen" in _PHOTO_SYSTEM
