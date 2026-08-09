import pytest

from restaurant_bot.config import Settings
from restaurant_bot.domain.models import (
    Candidate,
    CartItem,
    CatalogProduct,
    ConversationState,
    ItemStatus,
)
from restaurant_bot.services.engine import ConversationEngine
from restaurant_bot.services.matching import (
    can_auto_select,
    has_complete_query_evidence,
    has_conflicting_catalog_qualifiers,
    is_broad_category_query,
    is_safe_catalog_name_equivalent,
    rank_candidates,
    unverified_product_terms,
)


def test_complete_query_evidence_rejects_category_only_match() -> None:
    """Проверяет, что полный query evidence отклоняет категория только сопоставление."""
    assert has_complete_query_evidence("сироп роза", "Сироп Роза, 1л")
    assert not has_complete_query_evidence("сироп роза", "Сироп Тархун, 1л")


def test_safe_catalog_name_equivalence_accepts_inflection_and_word_order() -> None:
    """Принимает только другое написание того же самого товара."""
    assert is_safe_catalog_name_equivalent("сироп роз", "Сироп Роза, 1л")
    assert is_safe_catalog_name_equivalent("филе форели", "Форель филе, кг")


def test_safe_catalog_name_equivalence_rejects_related_different_products() -> None:
    """Не подменяет товар похожей категорией или другим видом продукта."""
    assert not is_safe_catalog_name_equivalent(
        "кукуруза",
        "Крупа кукурузная Алина 700г 1/7, шт",
    )
    assert not is_safe_catalog_name_equivalent("свинина", "Сало свиное")
    assert not is_safe_catalog_name_equivalent("сливки", "Сливки 33%, 1л")


def test_safe_equivalence_requires_user_named_size_range() -> None:
    """Не считает товар эквивалентным, если в каталоге отсутствует размер из запроса."""
    assert not is_safe_catalog_name_equivalent(
        "Филе форели 0,9-1,3 килограмма",
        "Филе форели",
    )
    assert is_safe_catalog_name_equivalent(
        "Филе форели 0,9-1,3 килограмма",
        "Филе форели 0.9–1.3 кг",
    )
    assert is_safe_catalog_name_equivalent(
        "Филе форели 0,9 -- 1,3 килограмма",
        "Филе форели 0.9–1.3 кг",
    )


def test_exact_product_is_auto_selected_but_category_query_is_not() -> None:
    """Проверяет, что точный товар является auto выбранный but категория query является не."""
    catalog = [
        CatalogProduct(product_id="rose", name="Сироп Роза", supplier="Сиропы", unit="шт"),
        CatalogProduct(product_id="feijoa", name="Сироп Фейхоа", supplier="Сиропы", unit="шт"),
    ]

    assert can_auto_select(rank_candidates("сироп роза", catalog))
    assert not can_auto_select(rank_candidates("сироп", catalog))


def test_size_range_requires_an_equivalent_catalog_row(settings: Settings) -> None:
    """Не выбирает единственный базовый товар вместо позиции с другим размером."""
    engine = ConversationEngine(settings)
    item = CartItem(
        id="trout",
        source_query="Филе форели 0,9-1,3 килограмма",
        quantity=None,
        source_line="Филе форели 0,9-1,3 килограмма",
    )

    engine._match_item(
        item,
        [CatalogProduct(product_id="plain-trout", name="Филе форели", unit="кг")],
    )

    assert item.catalog_product_id == ""
    assert item.status is ItemStatus.AMBIGUOUS


def test_size_range_matches_the_same_catalog_variant_without_becoming_comment(
    settings: Settings,
) -> None:
    """Сохраняет диапазон в поиске и выбирает только вариант с теми же характеристиками."""
    engine = ConversationEngine(settings)
    source = "Филе форели 0,9-1,3 килограмма"
    item = CartItem(id="trout", source_query=source, source_line=source)
    catalog = [
        CatalogProduct(
            product_id="range-trout",
            name="Филе форели 0,9-1,3 кг",
            unit="кг",
        ),
    ]

    engine._match_item(item, catalog)

    assert item.catalog_product_id == "range-trout"
    assert item.comment == ""
    assert item.quantity is None
    assert item.status is ItemStatus.MISSING_QTY


def test_conflicting_product_qualifier_blocks_automatic_catalog_selection(
    settings: Settings,
) -> None:
    """Не превращает противоречивую характеристику товара в комментарий."""
    assert has_conflicting_catalog_qualifiers(
        "Свинина уши копчёные",
        "Свинина Уши Свежие, (кг)",
    )
    assert has_conflicting_catalog_qualifiers(
        "Свинина рулька задняя",
        "Свинина Рулька Передняя",
    )

    item = CartItem(
        id="pork",
        source_query="Свинина уши копчёные",
        quantity=5,
        unit="кг",
    )
    ConversationEngine(settings)._match_item(
        item,
        [
            CatalogProduct(
                product_id="fresh-ears",
                name="Свинина Уши Свежие, (кг)",
                unit="кг",
            )
        ],
    )

    assert item.catalog_product_id == ""
    assert item.status is ItemStatus.AMBIGUOUS
    assert item.comment == ""


def test_full_name_term_missing_from_catalog_is_not_saved_as_comment(
    settings: Settings,
) -> None:
    """Не подставляет единственный вариант, если каталог потерял признак товара."""
    item = CartItem(
        id="mustard",
        source_query="Горчица Дижонская CHATEL, ведро, 1 кг, Франция",
        quantity=1,
        unit="шт",
    )
    catalog = [
        CatalogProduct(
            product_id="mustard-chatel",
            name="Горчица Дижонская CHATEL, ведро, 1 кг",
            unit="шт",
        )
    ]

    ConversationEngine(settings)._match_item(item, catalog)

    assert item.catalog_product_id == ""
    assert item.status is ItemStatus.AMBIGUOUS
    assert item.comment == ""


@pytest.mark.parametrize(
    ("query", "catalog_name"),
    [
        ("Свинина сало обжаренное", "Свинина Сало копченое. (кг)"),
        ("Рис квадратный", "Рис круглый, 1 кг"),
    ],
)
def test_product_variant_is_not_replaced_by_the_only_similar_catalog_row(
    settings: Settings,
    query: str,
    catalog_name: str,
) -> None:
    """Не выбирает единственную похожую строку при несовпадении характеристики."""
    item = CartItem(id="variant", source_query=query, quantity=1, unit="кг")

    ConversationEngine(settings)._match_item(
        item,
        [CatalogProduct(product_id="similar", name=catalog_name, unit="кг")],
    )

    assert item.source_query == query
    assert item.comment == ""
    assert item.catalog_product_id == ""
    assert item.status is ItemStatus.AMBIGUOUS


def test_short_unknown_term_is_not_treated_as_a_voice_typo_without_catalog_evidence() -> None:
    """Не разрешает короткий неизвестный признак только из-за сильной категории."""
    assert unverified_product_terms("свинина сало икс", "Свинина Сало копченое") == ["икс"]
    assert unverified_product_terms("сироп рза", "Сироп Роза") == []


def test_catalog_packaging_attribute_must_match_candidate(settings: Settings) -> None:
    """Не игнорирует обязательную фасовку при выборе из каталога."""
    item = CartItem(
        id="trout",
        source_query="Форель филе",
        packaging_text="0,8-1,3 кг",
        packaging_role="catalog_attribute",
        packaging_confidence=0.9,
        quantity=5,
        unit="кг",
    )

    ConversationEngine(settings)._match_item(
        item,
        [CatalogProduct(product_id="trout", name="Форель филе 1,5-2 кг", unit="кг")],
    )

    assert item.status is ItemStatus.AMBIGUOUS
    assert item.catalog_product_id == ""


def test_spoken_catalog_packaging_is_not_used_as_order_quantity(settings: Settings) -> None:
    """Не превращает фасовку из голосового названия в количество заказа."""
    item = CartItem(
        id="marzipan",
        source_query="Марципан",
        source_line="Марципан 65 грамм",
        quantity=65,
        unit="г",
    )
    catalog = [
        CatalogProduct(product_id="small", name="Марципан 65гр", unit="кг"),
        CatalogProduct(product_id="large", name="Марципан 1/5кг", unit="кг"),
    ]

    ConversationEngine(settings)._match_item(item, catalog)

    assert item.quantity is None
    assert item.unit == ""
    assert item.packaging_text == "65 г"
    assert item.packaging_role == "catalog_attribute"
    assert item.status is ItemStatus.AMBIGUOUS


def test_explicit_order_quantity_is_not_reclassified_as_packaging(settings: Settings) -> None:
    """Сохраняет количество, если пользователь явно попросил его заказать."""
    item = CartItem(
        id="marzipan",
        source_query="Марципан",
        source_line="Закажи Марципан 65 грамм",
        quantity=65,
        unit="г",
    )
    catalog = [CatalogProduct(product_id="small", name="Марципан 65гр", unit="кг")]

    ConversationEngine(settings)._match_item(item, catalog)

    assert item.quantity == 65
    assert item.unit == "г"
    assert item.packaging_role == "none"


def test_catalog_product_facts_are_not_saved_as_supplier_comments(settings: Settings) -> None:
    """Не записывает подтверждённые слова названия в комментарий поставщику."""
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(
            product_id="petricor",
            name="Вино ПЕТРИКОР МАЛЬВАЗИЯ сухое белое 0,75л",
            unit="шт",
        ),
        CatalogProduct(
            product_id="cuvee-one",
            name="Вино Кюве №1 резерв сухое красное 0,75л",
            unit="шт",
        ),
    ]

    white_wine = CartItem(
        id="petricor",
        source_query="Вино ПЕТРИКОР МАЛЬВАЗИЯ сухое белое",
        comment="белое",
        quantity=2,
        unit="шт",
    )
    cuvee = CartItem(
        id="cuvee-one",
        source_query="Вино Кюве резерв сухое красное",
        comment="номер один",
        quantity=1,
        unit="шт",
    )

    engine._match_item(white_wine, catalog)
    engine._match_item(cuvee, catalog)

    assert white_wine.comment == ""
    assert cuvee.comment == ""


def test_asr_product_residue_is_not_saved_as_supplier_comment(settings: Settings) -> None:
    """Не сохраняет остаток ошибочной расшифровки названия как комментарий."""
    engine = ConversationEngine(settings)
    item = CartItem(
        id="temelion",
        source_query="Абрау-Дюрсо белое т. миллион винтаж 20 0,75 литра",
        quantity=5,
        unit="шт",
    )
    product = CatalogProduct(
        product_id="temelion",
        name="Вино брют белое «Темелион Винтаж 20»0,75л",
        unit="шт",
    )

    engine._apply_catalog(
        item,
        Candidate(
            product_id=product.product_id,
            name=product.name,
            unit=product.unit,
        ),
        [product],
    )

    assert item.comment == ""


def test_catalog_title_facts_are_not_quantity_or_comment(settings: Settings) -> None:
    """Не принимает фасовку, страну и упаковку полного названия за заказ."""
    engine = ConversationEngine(settings)
    product = CatalogProduct(
        product_id="hondashi",
        name='Бульон рыбный Хондаши ТМ "OTOSAN", 1 кг, 10 кг/кор, Китай',
        supplier="Поставщик",
        unit="шт",
    )
    item = CartItem(
        id="hondashi",
        source_query="Бульон рыбный Хондаши ТМ Ото-сан",
        source_line="Бульон рыбный Хондаши ТМ Ото-сан, 1 кг, 10 кг в коробке, Китай",
        quantity=10,
        unit="кг",
        supplier_hint="Китай",
        comment="1 кг, в коробке, китай",
        packaging_text="10 кг в коробке",
        packaging_role="catalog_attribute",
    )

    engine._match_item(item, [product])

    assert item.catalog_product_id == ""
    assert item.candidates[0].product_id == product.product_id
    assert item.quantity is None
    assert item.unit == ""
    assert item.comment == ""
    assert item.supplier_hint == ""
    assert item.status is ItemStatus.AMBIGUOUS


def test_quantity_only_comment_residue_is_removed(settings: Settings) -> None:
    """Удаляет из комментария продублированные количество и единицы товара."""
    engine = ConversationEngine(settings)
    product = CatalogProduct(
        product_id="hazelnut",
        name="Фундук 13/15 1кг",
        unit="кг",
    )
    item = CartItem(
        id="hazelnut",
        source_query="Фундук три пятнадцатых",
        source_line="Фундук три пятнадцатых один килограмм десять килограмм",
        quantity=10,
        unit="кг",
        comment="один килограмм десять килограмм",
    )

    engine._match_item(item, [product])

    assert item.comment == ""


def test_saved_catalog_comment_is_normalized_when_draft_is_reopened() -> None:
    """Очищает ошибочный комментарий в черновике, созданном старой версией."""
    item = CartItem(
        id="hondashi",
        source_query="Бульон рыбный Хондаши ТМ Ото-сан",
        catalog_name='Бульон рыбный Хондаши ТМ "OTOSAN", 1 кг, 10 кг/кор, Китай',
        comment="1 кг, в коробке, китай",
    )
    state = ConversationState(cart=[item])

    ConversationEngine._normalize_existing_catalog_comments(state)

    assert state.cart[0].comment == ""


def test_exact_catalog_title_number_requires_explicit_order_quantity(settings: Settings) -> None:
    """Не считает число из полного названия заказом без явной просьбы пользователя."""
    engine = ConversationEngine(settings)
    product = CatalogProduct(
        product_id="jam",
        name="Джем Клубничный 1кг д/п",
        supplier="Поставщик",
        unit="шт",
    )
    item = CartItem(
        id="jam",
        source_query="Джем клубничный",
        source_line="Джем клубничный один килограмм дп",
        quantity=1,
        unit="кг",
        supplier_hint="дп",
        comment="дп",
    )

    engine._match_item(item, [product])

    assert item.catalog_product_id == product.product_id
    assert item.quantity is None
    assert item.unit == ""
    assert item.comment == ""
    assert item.status is ItemStatus.MISSING_QTY


def test_explicit_quantity_after_catalog_title_is_preserved(settings: Settings) -> None:
    """Сохраняет отдельное количество после полного названия товара."""
    engine = ConversationEngine(settings)
    product = CatalogProduct(
        product_id="jam",
        name="Джем Клубничный 1кг д/п",
        supplier="Поставщик",
        unit="шт",
    )
    item = CartItem(
        id="jam",
        source_query="Джем клубничный",
        source_line="Джем клубничный один килограмм дп — 2 шт",
        quantity=2,
        unit="шт",
    )

    engine._match_item(item, [product])

    assert item.catalog_product_id == product.product_id
    assert item.quantity == 2
    assert item.unit == "шт"
    assert item.comment == ""
    assert item.status is ItemStatus.MATCHED


def test_catalog_apply_preserves_user_provenance_and_is_idempotent(settings: Settings) -> None:
    """Сохраняет исходные поля пользователя при повторном применении каталога."""
    engine = ConversationEngine(settings)
    product = CatalogProduct(
        product_id="parmesan",
        name="Пармезан",
        supplier="Поставщик",
        unit="кг",
        comment="справочное примечание",
    )
    item = CartItem(
        id="parmesan",
        source_query="Пармезан",
        source_line="Пармезан — 2 кг",
        quantity=2,
        unit="кг",
        supplier_hint="Поставщик",
        comment="привезти холодным",
    )

    engine._match_item(item, [product])
    first_snapshot = (
        item.source_query,
        item.source_line,
        item.quantity,
        item.unit,
        item.supplier_hint,
        item.comment,
        item.catalog_name,
        item.catalog_comment,
    )

    engine._apply_catalog(
        item,
        Candidate(product_id=product.product_id, name=product.name, unit=product.unit),
        [product],
    )

    assert (
        item.source_query,
        item.source_line,
        item.quantity,
        item.unit,
        item.supplier_hint,
        item.comment,
        item.catalog_name,
        item.catalog_comment,
    ) == first_snapshot


def test_source_owned_product_fact_comment_survives_catalog_cleanup(settings: Settings) -> None:
    """Не удаляет продублированное в запросе требование пользователя."""
    engine = ConversationEngine(settings)
    product = CatalogProduct(
        product_id="pork-neck",
        name="Шея свиная без костей",
        supplier="Поставщик",
        unit="кг",
    )
    item = CartItem(
        id="pork-neck",
        source_query="Шея свиная без костей",
        source_line="Шея свиная 5 кг без костей",
        quantity=5,
        unit="кг",
        comment="без костей",
    )

    engine._match_item(item, [product])

    assert item.source_query == "Шея свиная без костей"
    assert item.source_line == "Шея свиная 5 кг без костей"
    assert item.comment == "без костей"


def test_one_word_category_never_auto_selects_the_only_catalog_candidate(
    settings: Settings,
) -> None:
    """Проверяет, что один слово категория никогда не auto выбирает только каталог кандидат."""
    engine = ConversationEngine(settings)
    cases = [
        ("говядина", CatalogProduct(product_id="beef", name="Говядина Тонкий край", unit="кг")),
        ("сироп", CatalogProduct(product_id="rose", name="Сироп Роза, 1л", unit="шт")),
    ]

    for query, product in cases:
        item = CartItem(id=query, source_query=query, quantity=10, unit=product.unit)
        engine._match_item(item, [product])

        assert item.status is ItemStatus.AMBIGUOUS
        assert item.catalog_product_id == ""
        assert [candidate.product_id for candidate in item.candidates] == [product.product_id]


def test_inflected_one_word_category_never_auto_selects_catalog_variant(
    settings: Settings,
) -> None:
    """Проверяет, что склонённая один слово категория никогда не auto выбирает каталог вариант."""
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(product_id="thin", name="Говядина Тонкий край", unit="кг"),
        CatalogProduct(product_id="bones", name="Говядина Кости ПРОДОЛЬНЫЙ распил", unit="кг"),
    ]
    item = CartItem(id="beef", source_query="говядины", quantity=10, unit="кг")

    engine._match_item(item, catalog)

    assert item.status is ItemStatus.AMBIGUOUS
    assert item.catalog_product_id == ""
    assert [candidate.product_id for candidate in item.candidates] == ["thin", "bones"]


def test_exact_one_word_catalog_product_is_not_treated_as_a_category() -> None:
    """Проверяет, что точный один слово каталог товар является не treated как a категория."""
    product = CatalogProduct(product_id="milk", name="Молоко", unit="л")

    assert not is_broad_category_query("молоко", rank_candidates("молоко", [product]))


def test_unsupported_beef_qualifier_cannot_choose_between_equal_category_matches() -> None:
    """Проверяет, что неподдерживаемый говядина qualifier не может choose between equal категория соответствует."""
    catalog = [
        CatalogProduct(product_id="thin", name="Говядина Тонкий край", unit="кг"),
        CatalogProduct(product_id="bones", name="Говядина Кости ПРОДОЛЬНЫЙ распил", unit="кг"),
    ]
    candidates = rank_candidates("говядина мраморная", catalog)

    assert is_broad_category_query("говядина мраморная", candidates)


def test_unsupported_beef_qualifier_becomes_comment_before_candidate_choice(
    settings: Settings,
) -> None:
    """Проверяет, что неподдерживаемый говядина qualifier becomes комментарий до кандидат выбор."""
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(product_id="thin", name="Говядина Тонкий край", unit="кг"),
        CatalogProduct(product_id="bones", name="Говядина Кости ПРОДОЛЬНЫЙ распил", unit="кг"),
    ]
    item = CartItem(
        id="beef",
        source_query="говядина мраморная",
        quantity=10,
        unit="кг",
    )

    engine._match_item(item, catalog)

    assert item.source_query == "говядина мраморная"
    assert item.comment == ""
    assert item.status is ItemStatus.AMBIGUOUS
    assert item.catalog_product_id == ""


def test_misspelled_product_variant_is_not_moved_to_supplier_comment(
    settings: Settings,
) -> None:
    """Сохраняет опечатку варианта частью поискового запроса."""
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(product_id="rose", name="Сироп Роза, 1л", unit="шт"),
        CatalogProduct(product_id="tarhun", name="Сироп Тархун, 1л", unit="шт"),
        CatalogProduct(product_id="sangria", name="Сироп Сангрия, 1л", unit="шт"),
    ]
    item = CartItem(id="syrup", source_query="Сироп Снгря", quantity=2, unit="шт")

    engine._match_item(item, catalog)

    assert item.source_query == "Сироп Снгря"
    assert item.comment == ""
    assert item.status is ItemStatus.AMBIGUOUS
    assert item.candidates[0].product_id == "sangria"


def test_unknown_product_variant_is_not_moved_to_supplier_comment(
    settings: Settings,
) -> None:
    """Сохраняет неизвестный вкус частью товара, а не комментарием поставщику."""
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(product_id="rose", name="Сироп Роза, 1л", unit="шт"),
        CatalogProduct(product_id="tarhun", name="Сироп Тархун, 1л", unit="шт"),
        CatalogProduct(product_id="feijoa", name="Сироп Фейхоа, 1л", unit="шт"),
    ]
    item = CartItem(id="syrup", source_query="Сироп Рамбутан", quantity=3, unit="кг")

    engine._match_item(item, catalog)

    assert item.source_query == "Сироп Рамбутан"
    assert item.comment == ""
    assert item.catalog_product_id == ""
    assert item.status is ItemStatus.AMBIGUOUS


def test_short_variant_typo_is_preserved_for_safe_candidate_resolution(
    settings: Settings,
) -> None:
    """Передаёт короткую опечатку подбору кандидата без потери слова."""
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(product_id="rose", name="Сироп Роза, 1л", unit="шт"),
        CatalogProduct(product_id="tarhun", name="Сироп Тархун, 1л", unit="шт"),
        CatalogProduct(product_id="feijoa", name="Сироп Фейхоа, 1л", unit="шт"),
        CatalogProduct(product_id="hazelnut", name="Сироп Фундук, 1л", unit="шт"),
        CatalogProduct(product_id="sangria", name="Сироп Сангрия, 1л", unit="шт"),
    ]
    item = CartItem(id="syrup", source_query="сироп рза")

    engine._match_item(item, catalog)

    assert item.source_query == "сироп рза"
    assert item.comment == ""
    assert item.catalog_product_id == ""
    assert item.status is ItemStatus.AMBIGUOUS
    assert item.candidates[0].product_id == "rose"


def test_multiword_product_family_requires_user_choice(settings: Settings) -> None:
    """Не выбирает первый вкус по общему названию товарной линейки."""
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(
            product_id="cherry-shiso",
            name="Кордиал ЛЬЮ Вишня/Шисо 1,0л",
            unit="шт",
        ),
        CatalogProduct(
            product_id="orange-vanilla",
            name="Кордиал ЛЬЮ Апельсин/Ваниль 1,0л",
            unit="шт",
        ),
        CatalogProduct(
            product_id="pear-tonka",
            name="Кордиал ЛЬЮ Груша/Тонка 1,0л",
            unit="шт",
        ),
    ]
    item = CartItem(id="cordial", source_query="Кордиал ЛЬЮ")

    engine._match_item(item, catalog)

    assert item.status is ItemStatus.AMBIGUOUS
    assert item.catalog_product_id == ""
    assert {candidate.product_id for candidate in item.candidates} == {
        "cherry-shiso",
        "orange-vanilla",
        "pear-tonka",
    }


def test_exact_product_inside_multiword_family_is_auto_selected(settings: Settings) -> None:
    """Выбирает точное полное название внутри товарной линейки."""
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(
            product_id="cherry-shiso",
            name="Кордиал ЛЬЮ Вишня/Шисо 1,0л",
            unit="шт",
        ),
        CatalogProduct(
            product_id="orange-vanilla",
            name="Кордиал ЛЬЮ Апельсин/Ваниль 1,0л",
            unit="шт",
        ),
    ]
    item = CartItem(
        id="cordial",
        source_query="Кордиал ЛЬЮ Вишня/Шисо 1,0л",
        quantity=2,
        unit="шт",
    )

    engine._match_item(item, catalog)

    assert item.status is ItemStatus.MATCHED
    assert item.catalog_product_id == "cherry-shiso"


def test_product_family_rule_scales_to_large_catalog(settings: Settings) -> None:
    """Сохраняет безопасный выбор при каталоге больше тысячи товаров."""
    engine = ConversationEngine(settings)
    catalog = [
        CatalogProduct(
            product_id=f"other-{index}",
            name=f"Прочий товар {index}",
            unit="шт",
        )
        for index in range(1_200)
    ]
    catalog.extend(
        CatalogProduct(
            product_id=f"cordial-{index}",
            name=f"Кордиал ЛЬЮ Вкус {index} 1,0л",
            unit="шт",
        )
        for index in range(8)
    )
    item = CartItem(id="cordial", source_query="Кордиал ЛЬЮ")

    engine._match_item(item, catalog)

    assert item.status is ItemStatus.AMBIGUOUS
    assert item.catalog_product_id == ""
    assert len(item.candidates) == 5
