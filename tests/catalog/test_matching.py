from restaurant_bot.domain.models import CatalogProduct
from restaurant_bot.services.matching import can_auto_select, rank_candidates


def _catalog() -> list[CatalogProduct]:
    return [
        CatalogProduct(product_id="rose", name="Сироп Роза, 1л", supplier="Сиропы", unit="л"),
        CatalogProduct(product_id="tarragon", name="Сироп Тархун, 1л", supplier="Сиропы", unit="л"),
    ]


def test_close_product_typo_keeps_only_relevant_candidate() -> None:
    candidates = rank_candidates("сироп роза", _catalog())
    assert candidates[0].product_id == "rose"
    assert can_auto_select(candidates)


def test_unrelated_words_do_not_create_false_candidate() -> None:
    assert rank_candidates("пару яблок", _catalog()) == []


def test_beef_typo_keeps_only_beef_candidates() -> None:
    catalog = [
        CatalogProduct(product_id="beef", name="Говядина Тонкий край", supplier="Мясо", unit="кг"),
        CatalogProduct(product_id="syrup", name="Сироп Роза", supplier="Сиропы", unit="шт"),
    ]

    candidates = rank_candidates("гонядина", catalog)

    assert [candidate.product_id for candidate in candidates] == ["beef"]


def test_category_word_shows_related_suggestions_without_auto_replacement() -> None:
    catalog = [
        CatalogProduct(product_id="rose", name="Сироп Роза", supplier="Сиропы", unit="шт"),
        CatalogProduct(product_id="tarhun", name="Сироп Тархун", supplier="Сиропы", unit="шт"),
        CatalogProduct(product_id="beef", name="Говядина Тонкий край", supplier="Мясо", unit="кг"),
    ]

    candidates = rank_candidates("сироп", catalog)

    assert [candidate.product_id for candidate in candidates] == ["rose", "tarhun"]


def test_vowelless_voice_typo_keeps_only_relevant_syrup_suggestion() -> None:
    candidates = rank_candidates("српроза", _catalog())

    assert [candidate.product_id for candidate in candidates] == ["rose"]


def test_catalog_packaging_number_is_not_treated_as_ordered_quantity() -> None:
    candidates = rank_candidates(
        "Сироп Роза 1 л",
        [CatalogProduct(product_id="rose", name="Сироп Роза, 1 л", supplier="Сиропы", unit="шт")],
    )

    assert [candidate.product_id for candidate in candidates] == ["rose"]


def test_joined_voice_product_name_keeps_safe_catalog_candidate() -> None:
    candidates = rank_candidates("сыропроза", _catalog())

    assert [candidate.product_id for candidate in candidates] == ["rose"]


def test_real_audio_syrovroza_keeps_rose_syrup_candidate() -> None:
    candidates = rank_candidates("Сыровроза", _catalog())

    assert [candidate.product_id for candidate in candidates] == ["rose"]
