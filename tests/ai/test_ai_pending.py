from restaurant_bot.domain.models import (
    Candidate,
    CartItem,
    CatalogProduct,
    ConversationState,
    EngineResult,
    InputKind,
    ItemStatus,
    TelegramEvent,
)
from restaurant_bot.integrations.openai_client import ProductMatchDecision
from restaurant_bot.services.engine import ConversationEngine
from restaurant_bot.services.matching import rank_candidates
from restaurant_bot.services.orchestrator import UpdateOrchestrator
from restaurant_bot.services.replies import issue_reply


class _Matcher:
    """Имитирует сопоставление товара с кандидатом каталога."""

    def __init__(self, decision: ProductMatchDecision):
        """Инициализирует тестовый двойник зависимости."""
        self.decision = decision
        self.calls: list[tuple[str, list[dict[str, object]], str]] = []

    def choose_catalog_candidate(
        self,
        query: str,
        candidates: list[dict[str, object]],
        product_context: str = "",
    ) -> ProductMatchDecision:
        """Возвращает настроенное решение сопоставления товара."""
        self.calls.append((query, candidates, product_context))
        return self.decision


def _orchestrator(settings, matcher: _Matcher) -> UpdateOrchestrator:  # type: ignore[no-untyped-def]
    """Создаёт настроенный тестовый оркестратор."""
    orchestrator = object.__new__(UpdateOrchestrator)
    orchestrator.engine = ConversationEngine(settings)
    orchestrator.openai = matcher
    return orchestrator


def test_ai_can_apply_only_catalog_candidate_from_its_shortlist(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что ИИ может применяет только каталог кандидат из its shortlist."""
    candidate = Candidate(
        product_id="rose",
        name="Сироп Роза",
        supplier="Сиропы",
        unit="шт",
        score=48.89,
    )
    item = CartItem(
        id="voice",
        source_query="Сыропроза",
        quantity=10,
        unit="шт",
        status=ItemStatus.AMBIGUOUS,
        candidates=[candidate],
    )
    state = ConversationState(cart=[item])
    matcher = _Matcher(
        ProductMatchDecision(
            action="select",
            selected_product_id="rose",
            confidence=0.96,
        )
    )
    result = EngineResult(state=state, reply=issue_reply(item, 0))
    event = TelegramEvent(update_id=1, chat_id="123456", input_type=InputKind.VOICE)

    resolved = _orchestrator(settings, matcher)._resolve_ai_pending(
        event,
        result,
        [CatalogProduct(product_id="rose", name="Сироп Роза", supplier="Сиропы", unit="шт")],
    )

    assert matcher.calls[0][0] == "Сыропроза"
    assert resolved.state.cart[0].status is ItemStatus.MATCHED
    assert resolved.state.cart[0].catalog_product_id == "rose"


def test_low_confidence_ai_not_found_keeps_candidate_choice_for_user(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что ИИ не found сохраняет кандидат выбор for пользователь."""
    candidate = Candidate(product_id="rose", name="Сироп Роза", supplier="Сиропы", unit="шт")
    item = CartItem(
        id="voice",
        source_query="Сыропроза",
        quantity=10,
        unit="шт",
        status=ItemStatus.AMBIGUOUS,
        candidates=[candidate],
    )
    state = ConversationState(cart=[item])
    matcher = _Matcher(ProductMatchDecision(action="not_found", confidence=0.4))
    result = EngineResult(state=state, reply=issue_reply(item, 0))
    event = TelegramEvent(update_id=1, chat_id="123456", input_type=InputKind.VOICE)

    resolved = _orchestrator(settings, matcher)._resolve_ai_pending(event, result, [])

    assert resolved.state.cart[0].status is ItemStatus.AMBIGUOUS
    assert resolved.state.cart[0].candidates[0].product_id == "rose"


def test_safe_inflected_catalog_name_is_selected_without_ai_guess(settings) -> None:  # type: ignore[no-untyped-def]
    """Выбирает уникальное эквивалентное название, не доверяя ошибочному отказу ИИ."""
    catalog = [
        CatalogProduct(product_id="rose", name="Сироп Роза, 1л", unit="шт"),
        CatalogProduct(product_id="tarhun", name="Сироп Тархун, 1л", unit="шт"),
    ]
    item = CartItem(
        id="rose",
        source_query="сироп роз",
        quantity=5,
        unit="шт",
        status=ItemStatus.AMBIGUOUS,
        candidates=rank_candidates("сироп роз", catalog),
    )
    matcher = _Matcher(ProductMatchDecision(action="not_found", confidence=0.99))

    resolved = _orchestrator(settings, matcher)._resolve_ai_pending(
        TelegramEvent(update_id=6, chat_id="123456", input_type=InputKind.VOICE),
        EngineResult(state=ConversationState(cart=[item]), reply=issue_reply(item, 0)),
        catalog,
    )

    assert matcher.calls == []
    assert resolved.state.cart[0].status is ItemStatus.MATCHED
    assert resolved.state.cart[0].catalog_product_id == "rose"


def test_safe_canonical_equivalence_is_not_vetoed_by_ai_not_found(settings) -> None:  # type: ignore[no-untyped-def]
    """Не отдаёт однозначный диапазон и бренд обратно в уточнение из-за AI not_found."""
    product = CatalogProduct(
        product_id="cucumber-maier",
        name="Огурцы Мар. 40/45 Maier 10л/9700г/5600г, Германия",
        supplier="Поставщик",
        unit="шт",
    )
    item = CartItem(
        id="cucumber",
        source_query="Огурцы 40 на 45 Майер",
        quantity=5,
        unit="шт",
        status=ItemStatus.AMBIGUOUS,
        candidates=rank_candidates("Огурцы 40 на 45 Майер", [product]),
    )
    state = ConversationState(cart=[item])
    matcher = _Matcher(ProductMatchDecision(action="not_found", confidence=0.99))
    result = EngineResult(state=state, reply=issue_reply(item, 0))
    event = TelegramEvent(update_id=1, chat_id="123456", input_type=InputKind.TEXT)

    resolved = _orchestrator(settings, matcher)._resolve_ai_pending(
        event,
        result,
        [product],
    )

    assert matcher.calls == []
    assert resolved.state.cart[0].catalog_product_id == "cucumber-maier"


def test_equivalent_names_from_different_suppliers_still_require_user_choice(settings) -> None:  # type: ignore[no-untyped-def]
    """Не выбирает строку сам, если одинаковый товар есть у нескольких поставщиков."""
    catalog = [
        CatalogProduct(product_id="rose-a", name="Сироп Роза, 1л", supplier="А", unit="шт"),
        CatalogProduct(product_id="rose-b", name="Сироп Роза, 1л", supplier="Б", unit="шт"),
    ]
    item = CartItem(
        id="rose",
        source_query="сироп роз",
        quantity=5,
        unit="шт",
        status=ItemStatus.AMBIGUOUS,
        candidates=rank_candidates("сироп роз", catalog),
    )
    matcher = _Matcher(ProductMatchDecision(action="ambiguous", confidence=0.99))

    resolved = _orchestrator(settings, matcher)._resolve_ai_pending(
        TelegramEvent(update_id=7, chat_id="123456", input_type=InputKind.VOICE),
        EngineResult(state=ConversationState(cart=[item]), reply=issue_reply(item, 0)),
        catalog,
    )

    assert matcher.calls == []
    assert resolved.state.cart[0].status is ItemStatus.AMBIGUOUS
    assert resolved.state.cart[0].catalog_product_id == ""


def test_ai_does_not_auto_select_weak_single_corn_candidate(settings) -> None:  # type: ignore[no-untyped-def]
    """Не заменяет свежую кукурузу единственной найденной кукурузной крупой."""
    catalog = [
        CatalogProduct(
            product_id="cornmeal",
            name="Крупа кукурузная Алина 700г 1/7, шт",
            supplier="Бакалея",
            unit="шт",
        )
    ]
    candidates = rank_candidates("кукуруза спелая", catalog)
    item = CartItem(
        id="corn",
        source_query="кукуруза спелая",
        quantity=1,
        unit="шт",
        status=ItemStatus.AMBIGUOUS,
        candidates=candidates,
    )
    matcher = _Matcher(
        ProductMatchDecision(
            action="select",
            selected_product_id="cornmeal",
            confidence=0.99,
        )
    )
    result = EngineResult(state=ConversationState(cart=[item]), reply=issue_reply(item, 0))

    resolved = _orchestrator(settings, matcher)._resolve_ai_pending(
        TelegramEvent(update_id=4, chat_id="123456", input_type=InputKind.VOICE),
        result,
        catalog,
    )

    resolved_item = resolved.state.cart[0]
    assert candidates[0].score < 40
    assert resolved_item.status is ItemStatus.AMBIGUOUS
    assert resolved_item.catalog_product_id == ""
    assert resolved_item.candidates[0].product_id == "cornmeal"
    assert "Точного совпадения не найдено" in resolved.reply.text


def test_ai_not_found_preserves_base_candidate_for_unresolved_qualifier(settings) -> None:  # type: ignore[no-untyped-def]
    """Сохраняет базовый кандидат, если AI отклонил только неизвестный признак."""
    product = CatalogProduct(
        product_id="broccoli",
        name="Брокколи свежая 10 кг",
        supplier="Овощи",
        unit="кг",
    )
    item = CartItem(
        id="broccoli-request",
        source_query="брокколи крупные кочаны",
        quantity=1,
        unit="кг",
        status=ItemStatus.AMBIGUOUS,
        candidates=rank_candidates("брокколи крупные кочаны", [product]),
    )
    matcher = _Matcher(
        ProductMatchDecision(
            action="not_found",
            confidence=0.99,
            contradictions=["размер кочана не подтверждён"],
        )
    )

    resolved = _orchestrator(settings, matcher)._resolve_ai_pending(
        TelegramEvent(update_id=8, chat_id="123456", input_type=InputKind.VOICE),
        EngineResult(state=ConversationState(cart=[item]), reply=issue_reply(item, 0)),
        [product],
    )

    resolved_item = resolved.state.cart[0]
    assert resolved_item.status is ItemStatus.AMBIGUOUS
    assert [candidate.product_id for candidate in resolved_item.candidates] == ["broccoli"]
    assert "Брокколи свежая 10 кг" in resolved.reply.text


def test_ai_rejects_semantically_conflicting_pork_candidate(settings) -> None:  # type: ignore[no-untyped-def]
    """Не предлагает свиное сало вместо свинины с явными требованиями."""
    catalog = [
        CatalogProduct(
            product_id="lard",
            name="Сало свиное",
            supplier="Мясо",
            unit="кг",
        )
    ]
    item = CartItem(
        id="pork",
        source_query="свинина",
        comment="без костей, без шкуры, без хрящей",
        quantity=2,
        unit="кг",
        status=ItemStatus.AMBIGUOUS,
        candidates=[
            Candidate(
                product_id="lard",
                name="Сало свиное",
                supplier="Мясо",
                unit="кг",
                score=36.66,
            )
        ],
    )
    matcher = _Matcher(
        ProductMatchDecision(
            action="not_found",
            confidence=0.97,
            contradictions=["другой продукт"],
        )
    )
    result = EngineResult(state=ConversationState(cart=[item]), reply=issue_reply(item, 0))

    resolved = _orchestrator(settings, matcher)._resolve_ai_pending(
        TelegramEvent(update_id=5, chat_id="123456", input_type=InputKind.VOICE),
        result,
        catalog,
    )

    resolved_item = resolved.state.cart[0]
    assert resolved_item.status is ItemStatus.NOT_FOUND
    assert resolved_item.catalog_product_id == ""
    assert resolved_item.candidates == []
    assert "Сало свиное" not in resolved.reply.text
    assert matcher.calls[0][0] == "свинина"
    assert matcher.calls[0][2] == "без костей, без шкуры, без хрящей"


def test_ai_cannot_override_numeric_range_mismatch(settings) -> None:  # type: ignore[no-untyped-def]
    """Не принимает вариант каталога, даже если AI ошибочно выбрал другой диапазон."""
    product = CatalogProduct(
        product_id="trout",
        name="Форель Филе свежее 0,8-1,2кг 20-22 кг/кор",
        supplier="Рыба",
        unit="кг",
    )
    item = CartItem(
        id="trout",
        source_query="Филе форели 0,8-1,3 килограмма зачищенное трим С",
        quantity=5,
        unit="кг",
        status=ItemStatus.AMBIGUOUS,
        candidates=[
            Candidate(
                product_id=product.product_id,
                name=product.name,
                supplier=product.supplier,
                unit=product.unit,
                score=92,
            )
        ],
    )
    matcher = _Matcher(
        ProductMatchDecision(
            action="select",
            selected_product_id=product.product_id,
            confidence=0.99,
        )
    )
    result = EngineResult(state=ConversationState(cart=[item]), reply=issue_reply(item, 0))

    resolved = _orchestrator(settings, matcher)._resolve_ai_pending(
        TelegramEvent(update_id=8, chat_id="123456", input_type=InputKind.VOICE),
        result,
        [product],
    )

    assert resolved.state.cart[0].status is ItemStatus.AMBIGUOUS
    assert resolved.state.cart[0].catalog_product_id == ""
    assert resolved.state.cart[0].comment == ""


def test_ai_cannot_fill_missing_full_name_term(settings) -> None:  # type: ignore[no-untyped-def]
    """Не принимает кандидата, в названии которого отсутствует обязательный признак."""
    product = CatalogProduct(
        product_id="mustard",
        name="Горчица Дижонская CHATEL, ведро, 1 кг",
        supplier="Соусный поставщик",
        unit="шт",
    )
    item = CartItem(
        id="mustard",
        source_query="Горчица Дижонская CHATEL, ведро, 1 кг, Франция",
        quantity=1,
        unit="шт",
        status=ItemStatus.AMBIGUOUS,
        candidates=[
            Candidate(
                product_id=product.product_id,
                name=product.name,
                supplier=product.supplier,
                unit=product.unit,
                score=96,
            )
        ],
    )
    matcher = _Matcher(
        ProductMatchDecision(
            action="select",
            selected_product_id=product.product_id,
            confidence=0.99,
        )
    )
    result = EngineResult(state=ConversationState(cart=[item]), reply=issue_reply(item, 0))

    resolved = _orchestrator(settings, matcher)._resolve_ai_pending(
        TelegramEvent(update_id=9, chat_id="123456", input_type=InputKind.VOICE),
        result,
        [product],
    )

    assert resolved.state.cart[0].status is ItemStatus.AMBIGUOUS
    assert resolved.state.cart[0].catalog_product_id == ""
    assert resolved.state.cart[0].comment == ""


def test_ai_reranker_never_replaces_a_one_word_category_query(settings) -> None:  # type: ignore[no-untyped-def]
    """Проверяет, что ИИ reranker никогда не replaces a один слово категория query."""
    candidate = Candidate(
        product_id="beef", name="Говядина Тонкий край", supplier="Мясо", unit="кг"
    )
    item = CartItem(
        id="beef",
        source_query="говядина",
        quantity=10,
        unit="кг",
        status=ItemStatus.AMBIGUOUS,
        candidates=[candidate],
    )
    matcher = _Matcher(ProductMatchDecision(action="select", selected_product_id="beef"))
    result = EngineResult(state=ConversationState(cart=[item]), reply=issue_reply(item, 0))

    resolved = _orchestrator(settings, matcher)._resolve_ai_pending(
        TelegramEvent(update_id=1, chat_id="123456", input_type=InputKind.VOICE),
        result,
        [
            CatalogProduct(
                product_id="beef", name="Говядина Тонкий край", supplier="Мясо", unit="кг"
            )
        ],
    )

    assert matcher.calls == []
    assert resolved.state.cart[0].status is ItemStatus.AMBIGUOUS


def test_ai_reranker_receives_full_query_with_short_typo(settings) -> None:  # type: ignore[no-untyped-def]
    """Передаёт ИИ полную опечатку и применяет выбранный товар каталога."""
    catalog = [
        CatalogProduct(product_id="rose", name="Сироп Роза, 1л", unit="шт"),
        CatalogProduct(product_id="tarhun", name="Сироп Тархун, 1л", unit="шт"),
        CatalogProduct(product_id="feijoa", name="Сироп Фейхоа, 1л", unit="шт"),
    ]
    item = CartItem(
        id="typo",
        source_query="сироп рза",
        status=ItemStatus.AMBIGUOUS,
        candidates=rank_candidates("сироп рза", catalog),
    )
    matcher = _Matcher(
        ProductMatchDecision(
            action="select",
            selected_product_id="rose",
            confidence=0.96,
        )
    )
    result = EngineResult(state=ConversationState(cart=[item]), reply=issue_reply(item, 0))

    resolved = _orchestrator(settings, matcher)._resolve_ai_pending(
        TelegramEvent(update_id=2, chat_id="123456", input_type=InputKind.TEXT),
        result,
        catalog,
    )

    assert matcher.calls[0][0] == "сироп рза"
    assert resolved.state.cart[0].source_query == "сироп рза"
    assert resolved.state.cart[0].comment == ""
    assert resolved.state.cart[0].catalog_product_id == "rose"
    assert resolved.state.cart[0].status is ItemStatus.MISSING_QTY


def test_ai_never_selects_first_variant_of_multiword_product_family(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    """Не разрешает ИИ выбирать вкус по общему названию линейки."""
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
    item = CartItem(
        id="cordial",
        source_query="Кордиал ЛЬЮ",
        status=ItemStatus.AMBIGUOUS,
        candidates=rank_candidates("Кордиал ЛЬЮ", catalog),
    )
    matcher = _Matcher(ProductMatchDecision(action="select", selected_product_id="cherry-shiso"))
    result = EngineResult(
        state=ConversationState(cart=[item]),
        reply=issue_reply(item, 0),
    )

    resolved = _orchestrator(settings, matcher)._resolve_ai_pending(
        TelegramEvent(update_id=3, chat_id="123456", input_type=InputKind.TEXT),
        result,
        catalog,
    )

    assert matcher.calls == []
    assert resolved.state.cart[0].status is ItemStatus.AMBIGUOUS
    assert resolved.state.cart[0].catalog_product_id == ""
