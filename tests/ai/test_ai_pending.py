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
from restaurant_bot.services.orchestrator import UpdateOrchestrator
from restaurant_bot.services.replies import issue_reply


class _Matcher:
    def __init__(self, decision: ProductMatchDecision):
        self.decision = decision
        self.calls: list[tuple[str, list[dict[str, object]]]] = []

    def choose_catalog_candidate(
        self, query: str, candidates: list[dict[str, object]]
    ) -> ProductMatchDecision:
        self.calls.append((query, candidates))
        return self.decision


def _orchestrator(settings, matcher: _Matcher) -> UpdateOrchestrator:  # type: ignore[no-untyped-def]
    orchestrator = object.__new__(UpdateOrchestrator)
    orchestrator.engine = ConversationEngine(settings)
    orchestrator.openai = matcher
    return orchestrator


def test_ai_can_apply_only_catalog_candidate_from_its_shortlist(settings) -> None:  # type: ignore[no-untyped-def]
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
    matcher = _Matcher(ProductMatchDecision(action="select", selected_product_id="rose"))
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


def test_ai_not_found_keeps_candidate_choice_for_user(settings) -> None:  # type: ignore[no-untyped-def]
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
    matcher = _Matcher(ProductMatchDecision(action="not_found"))
    result = EngineResult(state=state, reply=issue_reply(item, 0))
    event = TelegramEvent(update_id=1, chat_id="123456", input_type=InputKind.VOICE)

    resolved = _orchestrator(settings, matcher)._resolve_ai_pending(event, result, [])

    assert resolved.state.cart[0].status is ItemStatus.AMBIGUOUS
    assert resolved.state.cart[0].candidates[0].product_id == "rose"


def test_ai_reranker_never_replaces_a_one_word_category_query(settings) -> None:  # type: ignore[no-untyped-def]
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
