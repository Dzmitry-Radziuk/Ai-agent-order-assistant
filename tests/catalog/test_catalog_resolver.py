from restaurant_bot.catalog.evidence import remove_phrase_overlap
from restaurant_bot.catalog.resolver import CatalogDecision, CatalogResolver
from restaurant_bot.domain.models import CatalogProduct, SearchScope


def _catalog() -> list[CatalogProduct]:
    """Создаёт каталог с пересекающимися товарами и поставщиками."""
    return [
        CatalogProduct(
            product_id="rose-a",
            name="Сироп Роза, 1л",
            supplier="Поставщик А",
            unit="шт",
        ),
        CatalogProduct(
            product_id="rose-b",
            name="Сироп Роза, 1л",
            supplier="Поставщик Б",
            unit="шт",
        ),
        CatalogProduct(
            product_id="tarhun",
            name="Сироп Тархун, 1л",
            supplier="Поставщик А",
            unit="шт",
        ),
        CatalogProduct(
            product_id="corn",
            name="Кукуруза свежая",
            supplier="Овощи",
            unit="шт",
        ),
    ]


def test_supplier_scope_returns_outside_rows_only_as_locked_suggestions() -> None:
    """Не выбирает товар другого поставщика при строгой области поиска."""
    result = CatalogResolver().search(
        "кукуруза свежая",
        _catalog(),
        supplier_hint="Поставщик А",
        search_scope=SearchScope.SUPPLIER_ONLY,
    )

    assert result.found_in_scope is False
    assert result.supplier_search_locked is True
    assert [candidate.product_id for candidate in result.candidates] == ["corn"]


def test_any_supplier_scope_can_find_the_same_product() -> None:
    """Разрешает поиск во всём каталоге после явного выбора пользователя."""
    result = CatalogResolver().search(
        "кукуруза свежая",
        _catalog(),
        supplier_hint="Поставщик А",
        search_scope=SearchScope.ANY_SUPPLIER,
    )

    assert result.found_in_scope is True
    assert result.supplier_search_locked is False
    assert result.candidates[0].product_id == "corn"


def test_equal_products_from_different_suppliers_require_clarification() -> None:
    """Не выбирает первую из одинаковых строк разных поставщиков."""
    resolver = CatalogResolver()
    result = resolver.search("Сироп Роза, 1л", _catalog())

    assert resolver.decide("Сироп Роза, 1л", result.candidates) is CatalogDecision.CLARIFY


def test_exact_unique_product_can_be_selected_automatically() -> None:
    """Разрешает точное уникальное совпадение после hard veto."""
    resolver = CatalogResolver()
    result = resolver.search("Сироп Тархун, 1л", _catalog())

    assert resolver.decide("Сироп Тархун, 1л", result.candidates) is CatalogDecision.AUTO_SELECT


def test_unknown_variant_blocks_single_similar_candidate() -> None:
    """Не подменяет неизвестный вариант единственным похожим товаром."""
    resolver = CatalogResolver()
    result = resolver.search("кукуруза замороженная", _catalog())

    assert result.candidates[0].product_id == "corn"
    assert resolver.decide("кукуруза замороженная", result.candidates) is CatalogDecision.CLARIFY


def test_explicit_supplier_instruction_is_split_from_catalog_query() -> None:
    """Отделяет просьбу поставщику, но сохраняет товарные признаки."""
    resolver = CatalogResolver()
    result = resolver.search("сироп роза привезти холодным", _catalog())

    split = resolver.split_explicit_supplier_comment(
        "сироп роза привезти холодным",
        result.candidates,
    )

    assert split is not None
    assert split.product_query == "сироп роза"
    assert split.supplier_comment == "привезти холодным"


def test_unmarked_attribute_is_not_split_as_supplier_instruction() -> None:
    """Оставляет неоднозначный признак в поисковом названии товара."""
    resolver = CatalogResolver()
    result = resolver.search("сироп роза холодным", _catalog())

    assert (
        resolver.split_explicit_supplier_comment(
            "сироп роза холодным",
            result.candidates,
        )
        is None
    )


def test_search_copy_ignores_a_comment_but_source_data_stays_duplicated() -> None:
    """Исключает комментарий только из поисковой копии названия товара."""
    source_query = "Шея без кости без кожи без хрящиков"

    assert remove_phrase_overlap(source_query, "без кости, без кожи, без хрящиков") == "Шея"
    assert source_query == "Шея без кости без кожи без хрящиков"
