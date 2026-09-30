"""Inbound routing regressions for explanatory conversation and tool authority."""
from unittest.mock import AsyncMock

import pytest

from app.commerce_context import CommerceConversationState
from app.conversation_routing import owns_advice
from app.models import AgentResult, SalesInterpretation
from app.turn_understanding import RequestedAction, TurnUnderstanding, attach_turn_understanding


def advice(*, source="openai", domain="store_general", mode="advice", confidence=.99,
           action=None, tools=None):
    interpretation = SalesInterpretation(domain=domain, goal="discover", confidence=confidence,
                                        references_previous_context=True, needs_clarification=False)
    interpretation._source = source
    return attach_turn_understanding(interpretation, TurnUnderstanding(
        primary_intent="store_general", conversation_mode=mode, confidence=confidence,
        references_previous_context=True, requested_action=RequestedAction(**(action or {"kind": "none"})),
        required_tools=tools or ["none"],
    ))


@pytest.mark.parametrize("kwargs,state", [
    ({"mode": "operational"}, {}),
    ({"source": "fallback"}, {}),
    ({"domain": "out_of_scope"}, {}),
    ({"confidence": .50}, {}),
    ({"action": {"kind": "create_cart"}}, {}),
    ({"action": {"kind": "checkout_create"}}, {}),
    ({"action": {"kind": "get_order_status"}}, {}),
    ({"action": {"kind": "none", "confirmation": "confirm"}}, {}),
    ({"action": {"kind": "none", "confirmation": "reject"}}, {}),
    ({"action": {"kind": "none", "image_request": True}}, {}),
    ({"action": {"kind": "none", "shipping_selection_id": "shipping-1"}}, {}),
    ({"action": {"kind": "none", "payment_option_id": "payment-1"}}, {}),
    ({"tools": ["order"]}, {}),
    ({"tools": ["search_products"]}, {}),
    ({"action": {"kind": "checkout_update"}}, {"customer_registration": {"status": "collecting"}}),
    ({"action": {"kind": "none", "confirmation": "confirm"}}, {"club_flow_stage": "awaiting_account"}),
])
def test_advice_label_cannot_bypass_operational_or_data_collection_ownership(kwargs, state):
    assert not owns_advice(advice(**kwargs), CommerceConversationState(**state))


def test_existing_order_and_cart_do_not_prevent_read_only_explanation():
    assert owns_advice(advice(), CommerceConversationState(order_id="95805", cart_id="cart-1"))


@pytest.mark.asyncio
@pytest.mark.parametrize("catalog_question,expected_query", [
    ("Liste caixas de som 120w", "caixa de som 120w"),
    ("Quais caixas de som de 120w estão disponíveis?", "caixa de som de 120w"),
])
async def test_mixed_catalog_keeps_filters_and_answers_policy_without_registration(monkeypatch, catalog_question, expected_query):
    from types import SimpleNamespace
    from app.conversation_routing import mixed_catalog_reply
    from app.models import IncomingMessage
    from app.turn_understanding import get_turn_understanding
    interpreted = advice(mode="operational", action={"kind": "search"}, tools=["search_products"])
    get_turn_understanding(interpreted).questions = [catalog_question, "Posso comprar com CPF?", "Qual o pedido mínimo?"]
    execute = AsyncMock(return_value={"ok": True, "products": [{"id": "a", "name": "Caixa de som 120W"}]})
    consult = AsyncMock(return_value=AgentResult(reply_text="CPF é atendido pela equipe. O pedido mínimo está na política aprovada.", intent="commerce"))
    monkeypatch.setattr("app.consultative_agent.consult", consult)
    result = await mixed_catalog_reply(IncomingMessage(text="Liste caixas de som 120w. Posso comprar com CPF e qual o pedido mínimo?"),
        {}, interpreted, CommerceConversationState(), SimpleNamespace(mercos_adaptor_configured=True), execute)
    assert "Caixa de som 120W" in result.reply_text and "CPF" in result.reply_text
    assert execute.await_args.args[0] == "search_products"
    assert execute.await_args.args[1]["query"] == expected_query
    assert execute.await_args.args[1]["strict"] is True
    assert consult.await_args.kwargs["informational_only"] is True
    assert consult.await_args.kwargs["catalog_answered"] is True
    assert result.response_metadata["preserve_complete_list"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("text,history", [
    ("queria abastecer minha loja", []),
    ("sim", [{"role": "user", "content": "Queria abastecer minha loja"},
             {"role": "assistant", "content": "Você quer receber o link do catálogo?"}]),
])
async def test_inbound_advice_and_catalog_offer_acceptance_bypass_legacy_product_search(monkeypatch, text, history):
    from app import openai_agent, consultative_agent, sales_agent
    from app.config import get_settings
    from tests.test_chatbo_pipeline_replay import Replay
    replay = Replay(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("AGENT_CONVERSATION_FIRST_ENABLED", "true")
    monkeypatch.setenv("AGENT_CONSULTATIVE_ENABLED", "true")
    monkeypatch.setenv("AGENT_CONSULTATIVE_EMERGENCY_OFF", "false")
    monkeypatch.setenv("AGENT_CONSULTATIVE_TRAFFIC_PERCENT", "100")
    get_settings.cache_clear()
    replay.history["wa:5511988880001"] = history.copy()
    seen = []

    async def interpret(message, **kwargs):
        assert message.text == text
        assert kwargs["recent_turns"] == history
        return advice()

    async def consult(message, context, interpretation, **kwargs):
        assert kwargs["informational_only"] is True
        assert isinstance(kwargs["state"], CommerceConversationState)
        assert context.get("_model_conversation_turns", context.get("_conversation_turns", [])) == history
        seen.append(message.text)
        return AgentResult(reply_text="Podemos organizar as categorias que você procura. 😊", intent="commerce",
                           response_metadata={"response_source": "consultative_openai"})

    forbidden = AsyncMock(side_effect=AssertionError("Legacy catalog route stole a conversational turn"))
    monkeypatch.setattr(openai_agent, "interpret_message", interpret)
    monkeypatch.setattr(consultative_agent, "consult", consult)
    monkeypatch.setattr(openai_agent, "execute_tool", forbidden)
    monkeypatch.setattr(sales_agent, "execute_tool", forbidden)
    try:
        result, row = await replay.say(text)
        assert seen == [text]
        assert row["response_source"] == "consultative_openai"
        assert row["tools"] == []
        assert not result.handoff_required and not result.safety_reason
        forbidden.assert_not_awaited()
    finally:
        get_settings.cache_clear()
