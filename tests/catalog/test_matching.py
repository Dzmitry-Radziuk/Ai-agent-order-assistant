from restaurant_bot.catalog.evidence import has_catalog_search_evidence
from restaurant_bot.catalog.retrieval import rank_candidates
from restaurant_bot.catalog.safety import can_auto_select
from restaurant_bot.domain.models import CatalogProduct
from restaurant_bot.catalog.evidence import remove_phrase_overlap


def _catalog() -> list[CatalogProduct]:
    """Создаёт тестовый каталог товаров."""
    return [
        CatalogProduct(product_id="rose", name="Сироп Роза, 1л", supplier="Сиропы", unit="л"),
        CatalogProduct(product_id="tarragon", name="Сироп Тархун, 1л", supplier="Сиропы", unit="л"),
    ]


def test_close_product_typo_keeps_only_relevant_candidate() -> None:
    """Проверяет, что close товар опечатка сохраняет только relevant кандидат."""
    candidates = rank_candidates("сироп роза", _catalog())
    assert candidates[0].product_id == "rose"
    assert can_auto_select(candidates)


def test_unrelated_words_do_not_create_false_candidate() -> None:
    """Проверяет, что несвязанные слова do не create false кандидат."""
    assert rank_candidates("пару яблок", _catalog()) == []


def test_beef_typo_keeps_only_beef_candidates() -> None:
    """Проверяет, что говядина опечатка сохраняет только говядина кандидаты."""
    catalog = [
        CatalogProduct(product_id="beef", name="Говядина Тонкий край", supplier="Мясо", unit="кг"),
        CatalogProduct(product_id="syrup", name="Сироп Роза", supplier="Сиропы", unit="шт"),
    ]

    candidates = rank_candidates("гонядина", catalog)

    assert [candidate.product_id for candidate in candidates] == ["beef"]


def test_category_word_shows_related_suggestions_without_auto_replacement() -> None:
    """Проверяет, что категория слово показывает связанные suggestions без auto замена."""
    catalog = [
        CatalogProduct(product_id="rose", name="Сироп Роза", supplier="Сиропы", unit="шт"),
        CatalogProduct(product_id="tarhun", name="Сироп Тархун", supplier="Сиропы", unit="шт"),
        CatalogProduct(product_id="beef", name="Говядина Тонкий край", supplier="Мясо", unit="кг"),
    ]

    candidates = rank_candidates("сироп", catalog)

    assert [candidate.product_id for candidate in candidates] == ["rose", "tarhun"]


def test_vowelless_voice_typo_keeps_only_relevant_syrup_suggestion() -> None:
    """Проверяет, что vowelless голос опечатка сохраняет только relevant syrup предложение."""
    candidates = rank_candidates("српроза", _catalog())

    assert [candidate.product_id for candidate in candidates] == ["rose"]


def test_catalog_packaging_number_is_not_treated_as_ordered_quantity() -> None:
    """Проверяет, что каталог фасовка число является не treated как ordered количество."""
    candidates = rank_candidates(
        "Сироп Роза 1 л",
        [CatalogProduct(product_id="rose", name="Сироп Роза, 1 л", supplier="Сиропы", unit="шт")],
    )

    assert [candidate.product_id for candidate in candidates] == ["rose"]


def test_packaging_only_overlap_does_not_create_unrelated_candidates() -> None:
    """Не показывает товары, совпавшие с запросом только по фасовке."""
    catalog = [
        CatalogProduct(
            product_id="oil", name="Масло растительное 1л", supplier="Продукты", unit="шт"
        ),
        CatalogProduct(
            product_id="bleach",
            name="Средство отбеливающее жидкое, Белизна 1л",
            supplier="Хозтовары",
            unit="шт",
        ),
        CatalogProduct(
            product_id="cream", name="Сливки 33% Натуральные 1л БМК", supplier="Молочные", unit="шт"
        ),
    ]

    assert rank_candidates("Сироп Роза, 1л", catalog) == []


def test_joined_voice_product_name_keeps_safe_catalog_candidate() -> None:
    """Проверяет, что слитное голос товар название сохраняет безопасный каталог кандидат."""
    candidates = rank_candidates("сыропроза", _catalog())

    assert [candidate.product_id for candidate in candidates] == ["rose"]


def test_real_audio_syrovroza_keeps_rose_syrup_candidate() -> None:
    """Проверяет, что реальный аудио syrovroza сохраняет rose syrup кандидат."""
    candidates = rank_candidates("Сыровроза", _catalog())

    assert [candidate.product_id for candidate in candidates] == ["rose"]


def test_short_typo_in_variant_word_selects_unique_catalog_product() -> None:
    """Сохраняет короткую опечатку как основание для нужного кандидата."""
    candidates = rank_candidates("сироп рза", _catalog())

    assert candidates[0].product_id == "rose"
    assert not can_auto_select(candidates)


def test_typo_search_works_in_a_catalog_with_over_one_thousand_products() -> None:
    """Находит вариант с опечаткой без специальных правил для товара."""
    catalog = [
        CatalogProduct(
            product_id=f"bulk-{index}",
            name=f"Товар каталога {index}",
            supplier="Поставщик",
            unit="шт",
        )
        for index in range(1200)
    ]
    catalog.extend(_catalog())

    candidates = rank_candidates("сироп рза", catalog)

    assert candidates[0].product_id == "rose"


def test_search_query_removes_only_the_saved_comment_overlap() -> None:
    """Очищает только временную копию запроса и сохраняет исходные поля."""
    source_query = "свиная шея без костей без кожи без хрящиков"
    comment = "без костей без кожи без хрящиков"

    assert remove_phrase_overlap(source_query, comment) == "свиная шея"
    assert source_query == "свиная шея без костей без кожи без хрящиков"
    assert comment == "без костей без кожи без хрящиков"


def test_constraints_cannot_create_cross_category_candidates() -> None:
    """Не показывает товар по совпадению только с ограничением или общим признаком."""
    catalog = [
        CatalogProduct(product_id="neck", name="Шея свиная, кг"),
        CatalogProduct(product_id="cherry", name="Вишня без косточки"),
        CatalogProduct(product_id="tea", name="Чай зеленый"),
        CatalogProduct(product_id="sauce", name="Соус сливочный"),
    ]

    search_query = remove_phrase_overlap("свиная шея без костей", "без костей")
    assert [candidate.product_id for candidate in rank_candidates(search_query, catalog)] == [
        "neck"
    ]
    assert rank_candidates("лук зеленый", catalog) == []
    assert rank_candidates("сыр сливочный", catalog) == []
    assert not has_catalog_search_evidence(search_query, catalog[1])


def test_core_candidates_keep_single_word_and_preparation_queries() -> None:
    """Сохраняет однословные товары и ищет базу до структурного пожелания."""
    catalog = [
        CatalogProduct(product_id="parmesan", name="Пармезан"),
        CatalogProduct(product_id="dill", name="Укроп"),
        CatalogProduct(product_id="leek", name="Лук порей"),
        CatalogProduct(product_id="potato", name="Картофель"),
    ]

    assert [candidate.product_id for candidate in rank_candidates("пармезан", catalog)] == [
        "parmesan"
    ]
    assert [candidate.product_id for candidate in rank_candidates("укроп", catalog)] == ["dill"]
    assert [candidate.product_id for candidate in rank_candidates("лук порей", catalog)] == ["leek"]
    assert [candidate.product_id for candidate in rank_candidates("картофель", catalog)] == [
        "potato"
    ]
