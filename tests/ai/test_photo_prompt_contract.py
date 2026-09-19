"""Проверяет контракт распознавания фотографии заявки."""

from restaurant_bot.integrations.openai_client import _PHOTO_SYSTEM
from restaurant_bot.integrations.openai_prompts import _PHOTO_OBSERVATION_CONTRACT


def test_photo_prompt_excludes_packaging_and_stock_from_order_quantity() -> None:
    """Проверяет, что vision получает контракт наблюдения, а не готовой заявки."""
    prompt = f"{_PHOTO_SYSTEM}\n{_PHOTO_OBSERVATION_CONTRACT}"

    assert "PhotoDocumentObservation" in prompt
    assert "PhotoRowObservation" in prompt
    assert "intent=add_items" not in prompt
    assert "Верни только структуру" not in prompt


def test_photo_prompt_prioritizes_filled_department_cells() -> None:
    """Проверяет приоритет заполненных ячеек и явную диагностику непривязанных строк."""
    prompt = f"{_PHOTO_SYSTEM}\n{_PHOTO_OBSERVATION_CONTRACT}"

    assert "заполненные ячейки" in prompt
    assert "hall_quantity" in prompt
    assert "bar_quantity" in prompt
    assert "kitchen_quantity" in prompt
    assert "обязательно помещай в соответствующее department-поле" in prompt
    assert "не заменяй его только на explicit_order_quantity" in prompt
    assert "браузер" in prompt
    assert "скриншот" in prompt
    assert "панели чата" in prompt
    assert "соседнюю строку" in prompt
    assert "order_area_complete=false" in prompt
    assert "uncertain_order_row_count" in prompt


def test_photo_prompt_uses_filled_rows_without_losing_table_geometry() -> None:
    """Проверяет краткий ответ с сохранением горизонтальной привязки каждой ячейки."""
    prompt = f"{_PHOTO_SYSTEM}\n{_PHOTO_OBSERVATION_CONTRACT}"

    assert "Возвращай только строки с фактически заполненной ячейкой заказа" in prompt
    assert "установи горизонтальную полосу заполненной ячейки" in prompt
    assert "прочитай товар в этой же полосе" in prompt
    assert "точность важнее полноты" not in prompt


def test_photo_prompt_preserves_same_row_quantity_and_corrections() -> None:
    """Проверяет запрет переноса количества и суммирования исправлений."""
    prompt = f"{_PHOTO_SYSTEM}\n{_PHOTO_OBSERVATION_CONTRACT}"

    assert "той же" in prompt
    assert "строке" in prompt
    assert "не переноси между строками" in prompt.lower()
    assert "crossed_out_quantity_text" in prompt
    assert "corrected_quantity_text" in prompt
    assert "не суммируй их" in prompt


def test_photo_observation_contract_keeps_order_area_fields() -> None:
    """Проверяет наличие диагностических полей полноты области заказа."""
    assert "order_area_complete" in _PHOTO_OBSERVATION_CONTRACT
    assert "uncertain_order_row_count" in _PHOTO_OBSERVATION_CONTRACT
    assert "visible_product_row_count" in _PHOTO_OBSERVATION_CONTRACT
    assert "visible_filled_order_row_count" in _PHOTO_OBSERVATION_CONTRACT
    assert "sheet_row_numbers_visible" in _PHOTO_OBSERVATION_CONTRACT
    assert "sheet_row_number_confidence" in _PHOTO_OBSERVATION_CONTRACT


def test_photo_prompt_supports_headerless_table_fragments() -> None:
    """Требует разбирать фрагмент без заголовков только по доказательствам одной строки."""
    prompt = f"{_PHOTO_SYSTEM}\n{_PHOTO_OBSERVATION_CONTRACT}"

    assert "фрагмент таблицы без заголовков" in prompt
    assert "не придумывай" in prompt
    assert "explicit_order_quantity" in prompt
    assert "order_entry_text" in prompt


def test_photo_prompt_returns_only_filled_sheet_rows() -> None:
    """Не расходует vision-ответ на пустые строки электронной таблицы."""
    prompt = f"{_PHOTO_SYSTEM}\n{_PHOTO_OBSERVATION_CONTRACT}"

    assert "Возвращай только строки с фактически заполненной ячейкой заказа" in prompt
    assert "пустые товарные" in prompt
    assert "строки не возвращай" in prompt
    assert "могут иметь пропуски из-за пустых строк" in prompt
