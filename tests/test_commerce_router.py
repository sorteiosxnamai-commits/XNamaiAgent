import pytest

from app.commerce_router import clear_commerce_memory, extract_product_query, handle_commerce_message, resolve_commerce_action
from app.models import IncomingMessage


def test_extract_product_query_removes_commercial_prefixes():
    assert extract_product_query("Tem Tissot Seastar?") == "Tissot Seastar"
    assert extract_product_query("Vocês têm o T120.417.11.051.00?") == "T120.417.11.051.00"
    assert extract_product_query("Quanto custa o Citizen?") == "Citizen"
    assert extract_product_query("Tem estoque de EAN 7611608287637?") == "EAN 7611608287637"


def test_resolve_commerce_actions():
    assert resolve_commerce_action("Tem Citizen?") == "product_search"
    assert resolve_commerce_action("Quanto custa o Tissot?") == "product_price"
    assert resolve_commerce_action("Tem estoque do Tissot?") == "product_inventory"
    assert resolve_commerce_action("Tem algum cupom disponível?") == "coupon_search"


@pytest.mark.asyncio
async def test_product_search_is_deterministic_and_does_not_need_openai(monkeypatch):
    calls = []

    async def fake_execute(name, arguments):
        calls.append((name, arguments))
        return {"products": [{"id": "641", "name": "Tissot Seastar", "reference": "T120.417.11.051.00", "current_price": 6399.99}]}

    monkeypatch.setattr("app.commerce_router.execute_tool", fake_execute)
    result = await handle_commerce_message(IncomingMessage(text="Tem Tissot Seastar?"), {"primary_intent": "commerce"}, {})
    assert calls == [("search_products", {"query": "Tissot Seastar", "limit": 3})]
    assert result.handoff_required is False
    assert "Tissot Seastar" in result.reply_text
    assert "R$ 6.399,99" in result.reply_text


@pytest.mark.asyncio
async def test_agent_commerce_calls_tray_before_openai(monkeypatch):
    from app import openai_agent

    calls = []

    async def fake_execute(name, arguments):
        calls.append((name, arguments))
        return {"products": [{"id": "1", "name": "Tissot Seastar", "current_price": 100}]}

    async def openai_must_not_run(*args, **kwargs):
        raise AssertionError("OpenAI must not decide the first commerce lookup")

    monkeypatch.setattr("app.sales_agent.execute_tool", fake_execute)
    monkeypatch.setattr(openai_agent, "generate_openai_reply_async", openai_must_not_run)
    result = await openai_agent.generate_agent_reply_async(IncomingMessage(text="Tem Tissot Seastar?"), {})
    assert calls, "Tray must be consulted before OpenAI responder"
    assert calls[0][0] == "search_products"
    # Parallel retrieval may start with token_and_search; OpenAI responder must not run first.
    assert result.intent == "commerce"


@pytest.mark.asyncio
async def test_inventory_searches_then_checks_single_product(monkeypatch):
    calls = []

    async def fake_execute(name, arguments):
        calls.append((name, arguments))
        if name == "search_products":
            return {"products": [{"id": "641", "name": "Tissot Seastar"}]}
        return {"product_id": "641", "stock": 4, "available_for_purchase": True}

    monkeypatch.setattr("app.commerce_router.execute_tool", fake_execute)
    result = await handle_commerce_message(IncomingMessage(text="Tem estoque do T120.417.11.051.00?"), {"primary_intent": "commerce"}, {})
    assert calls == [
        ("search_products", {"query": "T120.417.11.051.00", "limit": 3}),
        ("check_inventory", {"product_id": "641"}),
    ]
    assert "Estoque: 4" in result.reply_text


@pytest.mark.asyncio
async def test_follow_up_inventory_refreshes_tray_using_identity_only(monkeypatch):
    clear_commerce_memory()
    calls = []

    async def fake_execute(name, arguments):
        calls.append((name, arguments))
        if name == "search_products":
            return {"products": [{"id": "641", "name": "Tissot Seastar", "reference": "T120"}]}
        return {"product_id": "641", "stock": 38, "availability": "Disponível"}

    monkeypatch.setattr("app.commerce_router.execute_tool", fake_execute)
    context = {"conversation_id": "conversation-1"}
    await handle_commerce_message(IncomingMessage(conversation_id="conversation-1", text="Tem Tissot Seastar?"), {"primary_intent": "commerce"}, context)
    result = await handle_commerce_message(IncomingMessage(conversation_id="conversation-1", text="E tem estoque?"), {"primary_intent": "commerce"}, context)
    assert calls == [
        ("search_products", {"query": "Tissot Seastar", "limit": 3}),
        ("check_inventory", {"product_id": "641"}),
    ]
    assert "Estoque: 38" in result.reply_text


@pytest.mark.asyncio
async def test_follow_up_price_refreshes_product_and_does_not_use_old_price(monkeypatch):
    clear_commerce_memory()
    calls = []

    async def fake_execute(name, arguments):
        calls.append((name, arguments))
        if name == "search_products":
            return {"products": [{"id": "641", "name": "Tissot Seastar", "current_price": 100}]}
        return {"id": "641", "name": "Tissot Seastar", "current_price": 200, "payment_option_details": "Pix"}

    monkeypatch.setattr("app.commerce_router.execute_tool", fake_execute)
    await handle_commerce_message(IncomingMessage(sender_phone="5511999999999", text="Tem Tissot Seastar?"), {"primary_intent": "commerce"}, {})
    result = await handle_commerce_message(IncomingMessage(sender_phone="5511999999999", text="E quanto custa?"), {"primary_intent": "commerce"}, {})
    assert calls == [
        ("search_products", {"query": "Tissot Seastar", "limit": 3}),
        ("get_product", {"product_id": "641"}),
    ]
    assert "R$ 200,00" in result.reply_text
    assert "R$ 100,00" not in result.reply_text


@pytest.mark.asyncio
async def test_follow_up_tray_error_does_not_reuse_previous_dynamic_data(monkeypatch):
    clear_commerce_memory()
    calls = []

    async def fake_execute(name, arguments):
        calls.append((name, arguments))
        if name == "search_products":
            return {"products": [{"id": "641", "name": "Tissot Seastar", "current_price": 100, "stock": 38}]}
        return {"error": "unavailable"}

    monkeypatch.setattr("app.commerce_router.execute_tool", fake_execute)
    await handle_commerce_message(IncomingMessage(sender_phone="5511888888888", text="Tem Tissot Seastar?"), {"primary_intent": "commerce"}, {})
    result = await handle_commerce_message(IncomingMessage(sender_phone="5511888888888", text="E quanto custa?"), {"primary_intent": "commerce"}, {})
    assert result.handoff_required is False
    assert "R$ 100,00" not in result.reply_text
    assert "informações da loja" in result.reply_text


@pytest.mark.asyncio
async def test_follow_up_without_product_identity_does_not_fall_through_to_openai(monkeypatch):
    async def unexpected_tool(name, arguments):
        raise AssertionError("No Tray lookup is possible without a product identity")

    monkeypatch.setattr("app.commerce_router.execute_tool", unexpected_tool)
    result = await handle_commerce_message(IncomingMessage(text="E tem estoque?"), {"primary_intent": "commerce"}, {})
    assert result.handoff_required is False
    assert result.safety_reason == "product_context_missing"
    assert "produto" in result.reply_text.lower()


@pytest.mark.asyncio
async def test_not_found_is_not_technical_error_and_tray_error_is_neutral(monkeypatch):
    async def no_products(name, arguments):
        return {"products": []}

    monkeypatch.setattr("app.commerce_router.execute_tool", no_products)
    not_found = await handle_commerce_message(IncomingMessage(text="Tem produto inexistente?"), {"primary_intent": "commerce"}, {})
    assert not_found.handoff_required is False
    assert not_found.safety_reason == "product_not_found"

    async def tray_error(name, arguments):
        return {"error": "technical"}

    monkeypatch.setattr("app.commerce_router.execute_tool", tray_error)
    failed = await handle_commerce_message(IncomingMessage(text="Tem Tissot?"), {"primary_intent": "commerce"}, {})
    assert failed.handoff_required is False
    assert failed.safety_reason == "tray_adapter_unavailable"
    assert "Tray" not in failed.reply_text
