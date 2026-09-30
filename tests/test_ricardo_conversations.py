"""Conversational contracts from Ricardo's failures, without calling OpenAI.

These exercise the model boundary and tool authority, not the quality of a canned
mock reply. The model must receive enough conversation to resolve continuations
and must be able to explain without searching the catalog.
"""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.commerce_context import CommerceConversationState
from app.config import Settings
from app.consultative_agent import consult
from app.models import IncomingMessage, SalesInterpretation
from app.turn_understanding import RequestedAction, TurnUnderstanding, attach_turn_understanding


def understood():
    return attach_turn_understanding(
        SalesInterpretation(domain="store_general", goal="discover", confidence=.99,
                            references_previous_context=True, needs_clarification=False),
        TurnUnderstanding(primary_intent="store_general", confidence=.99,
                          references_previous_context=True,
                          requested_action=RequestedAction(kind="none")),
    )


@pytest.fixture
def consultation(monkeypatch):
    import app.persona_repository as repository
    import app.prompt_compiler as compiler
    import app.commerce.tools as commerce
    monkeypatch.setattr(repository, "get_active_persona", lambda *a: NS(metadata={}))
    monkeypatch.setattr(compiler, "resolve_system_instructions",
                        lambda **kwargs: "Você é a Mai, assistente da XNamai.")
    monkeypatch.setattr(commerce, "tool_schemas_for_model", lambda: commerce.TOOL_SCHEMAS)
    return Settings(OPENAI_API_KEY="test", AGENT_HISTORY_LIMIT=24,
                    AGENT_CONSULTATIVE_ENABLED=True, AGENT_CONSULTATIVE_TRAFFIC_PERCENT=100)


@pytest.mark.asyncio
@pytest.mark.parametrize("history,text", [
    ([{"role": "user", "content": "Queria abastecer minha loja"}],
     "Por onde devo começar?"),
    ([{"role": "user", "content": "Queria abastecer minha loja"},
      {"role": "assistant", "content": "Você quer receber o link do catálogo?"}], "sim"),
    ([{"role": "user", "content": "#95805"},
      {"role": "assistant", "content": 'Seu pedido está como "Pedido gerado — não faturado".'}],
     "Okay e o que faço agora?"),
    ([{"role": "assistant", "content": "Não encontrei esse produto no catálogo agora."}],
     "Não foi isso que perguntei, quero entender como comprar"),
])
async def test_ricardo_continuations_keep_the_actual_question_and_history(consultation, history, text):
    execute = AsyncMock(side_effect=AssertionError("Informational guidance must not search products"))

    async def gateway(**kwargs):
        conversational = [m for m in kwargs["messages"] if m["role"] in {"user", "assistant"}]
        assert conversational[-(len(history) + 1):] == [*history, {"role": "user", "content": text}]
        assert {t["function"]["name"] for t in kwargs["tools"]} == {"search_knowledge"}
        return NS(text="Posso explicar as etapas com você. 😊", limit_reached=False)

    result = await consult(IncomingMessage(text=text, workspace_id="workspace-a", conversation_id="ricardo"),
                           {"_conversation_turns": history}, understood(), settings=consultation,
                           informational_only=True, execute=execute, gateway=gateway)
    execute.assert_not_awaited()
    assert not result.handoff_required and not result.safety_reason
    assert result.response_metadata["used_openai_responder"] is True
    assert not result.response_metadata["used_commerce_provider"]


@pytest.mark.asyncio
async def test_multiple_questions_reach_model_intact_with_approved_policy_evidence(consultation, monkeypatch):
    import app.knowledge_search as knowledge
    policy = {"source": "minimum-order", "content": "O pedido mínimo é de R$ 800,00."}
    retrieve = AsyncMock(return_value=NS(passages=[policy], status="lexical"))
    monkeypatch.setattr(knowledge, "prepare_knowledge", retrieve)
    text = "Quero abastecer minha loja com fones e mouses. Como compro e qual o pedido mínimo?"
    execute = AsyncMock(side_effect=AssertionError("No concrete catalog request"))

    async def gateway(**kwargs):
        assert kwargs["messages"][-1] == {"role": "user", "content": text}
        evidence = await kwargs["execute_tool"]("search_knowledge", {"query": "cadastro compra pedido mínimo"})
        assert evidence["passages"] == [policy]
        return NS(text="O pedido mínimo é de R$ 800,00.", limit_reached=False)

    result = await consult(IncomingMessage(text=text, workspace_id="workspace-a", conversation_id="ricardo"),
                           {}, understood(), settings=consultation, informational_only=True,
                           execute=execute, gateway=gateway)
    retrieve.assert_awaited_once()
    execute.assert_not_awaited()
    assert result.response_metadata["knowledge_sources"] == ["minimum-order"]


@pytest.mark.asyncio
async def test_explaining_with_existing_cart_never_authorizes_mutation(consultation):
    execute = AsyncMock(side_effect=AssertionError("Read-only explanation must not mutate"))
    state = CommerceConversationState(cart_id="existing-cart", order_id="95805")

    async def gateway(**kwargs):
        for name, arguments in [("create_cart", {}), ("create_order", {}),
                                ("checkout_create", {}), ("get_customer", {}),
                                ("search_products", {"query": "okay"})]:
            assert await kwargs["execute_tool"](name, arguments) == {"error": "tool_not_allowed"}
        return NS(text="Vamos esclarecer sua dúvida sobre as próximas etapas.", limit_reached=False)

    result = await consult(IncomingMessage(text="O que significa não faturado?", workspace_id="workspace-a",
                                          conversation_id="ricardo"), {}, understood(), settings=consultation,
                           state=state, informational_only=True, execute=execute, gateway=gateway)
    execute.assert_not_awaited()
    assert state.cart_id == "existing-cart" and state.order_id == "95805"
    assert not result.handoff_required
