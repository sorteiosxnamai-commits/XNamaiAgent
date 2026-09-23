"""Replay the generic XNamai flow with catalog facts supplied by a fake provider."""
import pytest

from app.business_policy import bind_policy, reset_policy
from app.commerce_context import CommerceConversationState, evolve_commerce_state
from app.models import IncomingMessage
from app.sales_agent import _generic_catalog_fast_path

pytestmark = [pytest.mark.offline_eval, pytest.mark.asyncio]

PRODUCTS = [
    {"id": "usb-c", "name": "FKT-110C - Carregador Tipo C", "price": 14.91, "stock": 490},
    {"id": "v8", "name": "FKT-1108 - Carregador V8", "price": 14.04, "stock": 101},
    {"id": "cabo", "name": "CB702V - Cabo USB V8", "price": 5.25, "stock": 69},
]


@pytest.fixture
def catalog(monkeypatch):
    calls = []
    async def execute(tool, args):
        calls.append((tool, args))
        if tool == "search_products":
            query = str(args.get("query") or "").lower()
            return {"ok": True, "products": [p for p in PRODUCTS if not query or query in p["name"].lower()]}
        product = next(p for p in PRODUCTS if p["id"] == args.get("product_id"))
        if tool == "get_product":
            return {"ok": True, "product": product}
        if tool == "check_inventory":
            return {"ok": True, "stock": product["stock"], "stock_confirmed": True}
        raise AssertionError(f"Unexpected mutation or integration: {tool}")
    monkeypatch.setattr("app.commerce.tools.commerce_tools_available", lambda: True)
    monkeypatch.setattr("app.commerce.tools.execute_tool", execute)
    return calls


async def turn(text, state):
    result = await _generic_catalog_fast_path(IncomingMessage(text=text), state=state)
    assert result is not None
    return result, evolve_commerce_state(state, result)


async def test_published_browse_limit_then_selection_price_and_stock(catalog):
    token = bind_policy({"business_policies": {"catalog_browse_limit": 2}})
    try:
        _, state = await turn("me mostre alguns produtos", CommerceConversationState())
        assert len(state.last_presented_products) == 2
        _, state = await turn("o segundo", state)
        price, state = await turn("quanto custa?", state)
        assert "14,04" in price.reply_text
        _, state = await turn("tem estoque?", state)
        assert state.active_product.product_id == "v8"
        assert catalog[-1] == ("check_inventory", {"product_id": "v8"})
    finally:
        reset_policy(token)


async def test_misunderstanding_repeats_safe_query_then_hands_off(catalog):
    _, state = await turn("FKT-110C", CommerceConversationState())
    assert state.last_catalog_query
    repaired, state = await turn("não foi isso que perguntei", state)
    assert repaired.response_metadata["conversation_repair"]["attempt"] == 1
    assert state.active_product.product_id == "usb-c"
    assert not repaired.handoff_required
    before = len(catalog)
    handoff, state = await turn("você não entendeu", state)
    assert handoff.handoff_required
    assert "XNamai" in handoff.reply_text
    assert len(catalog) == before
    assert state.pending_commerce_action is None


async def test_purchase_guidance_answers_how_to_buy_without_searching(catalog):
    result, state = await turn(
        "como faço para comprar com vocês?",
        CommerceConversationState(),
    )

    assert "https://xnamai.meuspedidos.com.br/" in result.reply_text
    assert "tipo de produto" in result.reply_text
    assert "modelo ou estilo" not in result.reply_text.lower()
    assert result.response_metadata["active_topic"] == "purchase_guidance"
    assert result.response_metadata["used_commerce_provider"] is False
    assert state.pending_commerce_action is None
    assert catalog == []


async def test_model_of_what_resumes_purchase_guidance_without_catalog_search(catalog):
    _, state = await turn(
        "como faço para comprar com vocês?",
        CommerceConversationState(),
    )
    result, state = await turn("modelo de quê?", state)

    assert "catálogo oficial da XNamai" in result.reply_text
    assert "Não encontrei" not in result.reply_text
    assert state.pending_commerce_action is None
    assert catalog == []
