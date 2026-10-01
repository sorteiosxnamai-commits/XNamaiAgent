from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.commerce_context import CommerceConversationState
from app.config import Settings
from app.consultative_agent import eligible, consult
from app.models import IncomingMessage, SalesInterpretation
from app.turn_understanding import TurnUnderstanding, RequestedAction, attach_turn_understanding


def interpretation(kind="search", intent="commerce_find"):
    result = SalesInterpretation(domain="commerce", goal="find", confidence=.99,
                                 references_previous_context=False, needs_clarification=False)
    return attach_turn_understanding(result, TurnUnderstanding(primary_intent=intent,
        confidence=.99, requested_action=RequestedAction(kind=kind)))


def settings():
    return Settings(OPENAI_API_KEY="test", AGENT_CONSULTATIVE_ENABLED=True,
                    AGENT_CONSULTATIVE_TRAFFIC_PERCENT=100)


def message():
    return IncomingMessage(text="Tem fones? E qual o pedido mínimo?", workspace_id="workspace-a",
                           conversation_id="conversation-a")


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["Leonardo", None])
async def test_catalog_intro_has_customer_name_only_when_provided(monkeypatch, name):
    import json
    import app.persona_repository as repository
    import app.prompt_compiler as compiler
    monkeypatch.setattr(repository, "get_active_persona", lambda *a: NS(metadata={}))
    monkeypatch.setattr(compiler, "resolve_system_instructions", lambda **k: "safe")
    async def gateway(**kwargs):
        data_messages = [m["content"] for m in kwargs["messages"] if m["content"].startswith("Contexto operacional")]
        hints = json.loads(data_messages[0].split("\n", 1)[1]) if data_messages else {}
        assert hints.get("customer_display_name") == name
        return NS(text="Claro! Nosso catálogo completo está disponível online.", limit_reached=False)
    incoming = message().model_copy(update={"sender_name": name, "text": "Pode enviar o catálogo?"})
    result = await consult(incoming, {}, interpretation("none", "store_general"), settings=settings(), gateway=gateway, informational_only=True)
    assert result.safety_reason is None


def test_rollout_is_sticky_scoped_and_can_be_disabled():
    configured = settings()
    assert eligible(message(), interpretation(), CommerceConversationState(), configured)
    configured.agent_consultative_traffic_percent = 0
    assert not eligible(message(), interpretation(), CommerceConversationState(), configured)
    configured.agent_consultative_traffic_percent = 100
    configured.agent_consultative_emergency_off = True
    assert not eligible(message(), interpretation(), CommerceConversationState(), configured)


@pytest.mark.parametrize("kind", ["create_cart", "checkout_create", "shipping_quote", "handoff"])
def test_transactional_intent_never_enters_public_consultation(kind):
    assert not eligible(message(), interpretation(kind), CommerceConversationState(), settings())


@pytest.mark.parametrize("field,value", [
    ("pending_commerce_action", "show_catalog"), ("cart_id", "cart-a"),
    ("customer_registration", {"status": "collecting"}), ("order_id", "order-a"),
    ("pending_followup", {"question": "confirma?"}),
])
def test_pending_customer_work_is_preserved(field, value):
    state = CommerceConversationState(**{field: value})
    assert not eligible(message(), interpretation(), state, settings())


@pytest.mark.asyncio
async def test_bounded_search_reformulation_revalidates_and_blocks_private_tools(monkeypatch):
    import app.persona_repository as repository
    import app.prompt_compiler as compiler
    import app.commerce.tools as commerce
    active = NS(metadata={"knowledge_documents": []})
    monkeypatch.setattr(repository, "get_active_persona", lambda *a: active)
    monkeypatch.setattr(compiler, "resolve_system_instructions", lambda **k: "safe")
    monkeypatch.setattr(commerce, "tool_schemas_for_model", lambda: commerce.TOOL_SCHEMAS)
    calls = []

    async def execute(name, args):
        calls.append((name, args))
        if name == "search_products":
            return {"products": [] if args["query"] == "headphones" else [{"id": "1", "name": "Fone"}]}
        assert name == "get_product"
        return {"id": "1", "name": "Fone", "price": 25}

    async def gateway(**kwargs):
        run = kwargs["execute_tool"]
        assert {t["function"]["name"] for t in kwargs["tools"]} == {"search_products", "search_knowledge"}
        assert await run("create_order", {}) == {"error": "tool_not_allowed"}
        assert await run("get_customer", {}) == {"error": "tool_not_allowed"}
        first = await run("search_products", {"query": "headphones"})
        assert first["products"] == []
        assert await run("search_products", {"query": "headphones"}) == first
        second = await run("search_products", {"query": "fone"})
        assert second["products"][0]["_revalidated"] is True
        assert await run("search_products", {"query": "audio"}) == {"error": "search_limit"}
        return NS(text="Encontrei o Fone.", limit_reached=False)

    result = await consult(message(), {}, interpretation(), settings=settings(), execute=execute, gateway=gateway)
    assert len(calls) == 3
    assert result.commercial_data["products"][0]["price"] == 25


@pytest.mark.asyncio
async def test_failed_revalidation_does_not_supply_stale_prices(monkeypatch):
    import app.persona_repository as repository
    import app.prompt_compiler as compiler
    import app.commerce.tools as commerce
    monkeypatch.setattr(repository, "get_active_persona", lambda *a: NS(metadata={}))
    monkeypatch.setattr(compiler, "resolve_system_instructions", lambda **k: "safe")
    monkeypatch.setattr(commerce, "tool_schemas_for_model", lambda: commerce.TOOL_SCHEMAS)
    execute = AsyncMock(side_effect=[{"products": [{"id": "1", "name": "Fone", "price": 5}]}, {"error": "unavailable"}])

    async def gateway(**kwargs):
        response = await kwargs["execute_tool"]("search_products", {"query": "fone"})
        assert response["products"] == []
        return NS(text="Não consegui confirmar o preço.", limit_reached=False)

    result = await consult(message(), {}, interpretation(), settings=settings(), execute=execute, gateway=gateway)
    assert result.commercial_data["products"] == []
