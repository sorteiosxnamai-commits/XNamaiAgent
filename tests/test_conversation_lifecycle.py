"""Final reply/delivery own dialogue; completed operations retain their facts."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.commerce_context import CommerceConversationState
from app.conversation_lifecycle import finalize_dialogue, operation_snapshot, commit_delivered_conversation
from app.models import AgentResult, IncomingMessage


def test_rejected_answer_does_not_persist_unsent_question_but_retains_order():
    old = CommerceConversationState(active_topic="commercial_guidance", conversation_goal="Escolher o mix")
    proposed = old.model_copy(update={"order_id": "12345", "active_topic": "order_status",
        "conversation_goal": "Pagar o pedido", "pending_followup": {"question": "Quer pagar?"}})
    result = AgentResult(reply_text="Não consegui confirmar o valor.", intent="commerce",
        safety_reason="factual_validation_failed", response_metadata={"conversation_summary_delta": {"commitments": ["Pagamento confirmado"]}})
    final = finalize_dialogue(result, proposed, old)
    assert final.order_id == "12345"
    assert final.active_topic == old.active_topic
    assert final.conversation_goal == old.conversation_goal
    assert final.pending_followup is None
    assert "conversation_summary_delta" not in result.response_metadata


def test_operational_snapshot_never_presents_unsent_products():
    from app.commerce_context import evolve_commerce_state
    old = CommerceConversationState(active_topic="commercial_guidance")
    result = AgentResult(reply_text="Veja esses fones", intent="commerce",
        commercial_data={"products": [{"id": "new", "name": "Fone novo"}]},
        response_metadata={"presented_products": True, "active_topic": "product_catalog"})
    proposed = evolve_commerce_state(old, result)
    snapshot = operation_snapshot(proposed, old)
    assert snapshot.last_presented_products == []
    assert snapshot.active_topic == "commercial_guidance"


def test_all_paths_capture_final_question_and_clear_it_when_answered():
    state = CommerceConversationState(active_topic="order_status")
    first = finalize_dialogue(AgentResult(reply_text="Qual o número do pedido?", intent="commerce"), state, state)
    assert first.pending_followup == {"question": "Qual o número do pedido?"}
    assert finalize_dialogue(AgentResult(reply_text="Consultei o pedido.", intent="commerce"), first, first).pending_followup is None


def test_failed_delivery_never_commits_memory(monkeypatch):
    persist = Mock(side_effect=AssertionError("must not commit unsent dialogue"))
    monkeypatch.setattr("app.conversation_lifecycle.persist_state", persist)
    commit_delivered_conversation({"provider_send_ok": False,
        "response_metadata": {"conversation_commit": {"version": 1}}})
    persist.assert_not_called()


def test_delivery_commits_scoped_final_state_and_summary(monkeypatch):
    from app.memory_scope import stamp_state
    workspace = "aa774d20-509f-4d54-865b-7a5de22b6d30"
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(chatbo_workspace_id=workspace))
    persist, summary = Mock(), Mock()
    monkeypatch.setattr("app.conversation_lifecycle.persist_state", persist)
    monkeypatch.setattr("app.conversation_memory.update_conversation_memory", summary)
    commit_delivered_conversation({"provider_send_ok": True, "workspace_id": workspace,
        "channel": "whatsapp", "sender_key": "test", "inbound_id": 3, "reply_text": "Qual categoria?",
        "response_metadata": {"conversation_commit": {"version": 1, "conversation_id": "test-dialog"},
            "commerce_state": stamp_state({"active_topic": "product_catalog", "pending_followup": {"question": "Qual categoria?"}}, workspace)}})
    assert persist.call_args.args[0].conversation_id == "test-dialog"
    assert persist.call_args.args[1].pending_followup == {"question": "Qual categoria?"}
    summary.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["Você recebeu um número de pedido, ou ainda está no carrinho?", "Qual o número do seu pedido?"])
async def test_numeric_order_reply_uses_verified_order_tool_without_product_search(monkeypatch, question):
    from app import openai_agent
    calls = []
    async def get_order(**kwargs):
        calls.append(kwargs["order_id"])
        return AgentResult(reply_text="Pedido localizado.", intent="commerce")
    monkeypatch.setattr(openai_agent, "get_order_facts", get_order)
    interpreter = AsyncMock(side_effect=AssertionError("numeric answer already resolved"))
    monkeypatch.setattr(openai_agent, "interpret_message", interpreter)
    result = await openai_agent.generate_agent_reply_async(IncomingMessage(text="95805"), {
        "_evaluation_history": [{"role": "assistant", "content": question}]})
    assert calls == ["95805"]
    assert result.response_metadata["active_topic"] == "order_status"
    interpreter.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_cart_report_keeps_order_context_before_general_advice(monkeypatch):
    from app import openai_agent
    interpreter = AsyncMock(side_effect=AssertionError("Known report should ask for the order reference"))
    monkeypatch.setattr(openai_agent, "interpret_message", interpreter)
    result = await openai_agent.generate_agent_reply_async(IncomingMessage(text="Fiz um pedido no carrinho."), {})
    assert result.response_metadata["active_topic"] == "order_status"
    assert result.response_metadata["response_source"] == "existing_cart_clarification"
    assert "número de pedido" in result.reply_text
    interpreter.assert_not_awaited()


@pytest.mark.asyncio
async def test_semantic_order_intent_cannot_fall_through_to_catalog(monkeypatch):
    from app import openai_agent
    from app.config import get_settings
    from app.models import SalesInterpretation
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("AGENT_CONVERSATION_FIRST_ENABLED", "true")
    monkeypatch.setenv("AGENT_CONSULTATIVE_ENABLED", "true")
    monkeypatch.setenv("AGENT_CONSULTATIVE_EMERGENCY_OFF", "false")
    get_settings.cache_clear()
    interpretation = SalesInterpretation(domain="commerce", goal="after_sales", confidence=.99,
        references_previous_context=True, needs_clarification=False, order_action="get_order_status", order_id="99999")
    interpretation._source = "openai"
    monkeypatch.setattr(openai_agent, "interpret_message", AsyncMock(return_value=interpretation))
    lookup = AsyncMock(return_value=AgentResult(reply_text="Pedido consultado.", intent="commerce"))
    monkeypatch.setattr(openai_agent, "get_order_facts", lookup)
    try:
        result = await openai_agent.generate_agent_reply_async(IncomingMessage(text="95805"), {
            "_evaluation_history": [{"role": "assistant", "content": "Você já finalizou no catálogo?"}]})
        assert lookup.await_args.kwargs["order_id"] == "95805"
        assert result.response_metadata["response_source"] == "verified_order_query"
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_pipeline_persists_final_question_after_rewrite(monkeypatch):
    from tests.test_chatbo_pipeline_replay import Replay
    from app import message_pipeline
    replay = Replay(monkeypatch)
    # Customer enrichment can return a new mapping for an existing customer.
    monkeypatch.setattr(message_pipeline, "enrich_customer_context", lambda context: dict(context))
    monkeypatch.setattr(message_pipeline, "generate_agent_reply_async", AsyncMock(return_value=AgentResult(
        reply_text="Qual categoria?", intent="commerce", response_metadata={"domain": "commerce", "active_topic": "product_catalog"})))
    async def enrich(incoming, result):
        result.reply_text = "Já tenho todas as informações necessárias."
        return result
    monkeypatch.setattr(message_pipeline, "enrich_agent_result", enrich)
    result, row = await replay.say("quero o catálogo")
    assert row["active_topic"] == "product_catalog"
    assert result.response_metadata["commerce_state"]["pending_followup"] is None
    assert replay.states["wa:5511988880001"]["pending_followup"] is None


@pytest.mark.asyncio
async def test_pipeline_references_products_in_final_rewritten_answer(monkeypatch):
    from tests.test_chatbo_pipeline_replay import Replay
    from app import message_pipeline
    replay = Replay(monkeypatch)
    monkeypatch.setattr(message_pipeline, "generate_agent_reply_async", AsyncMock(return_value=AgentResult(
        reply_text="1. Fone antigo\n2. Fone correto", intent="commerce",
        commercial_data={"products": [{"id": "old", "name": "Fone antigo"}, {"id": "new", "name": "Fone correto"}]},
        response_metadata={"domain": "commerce", "active_topic": "product_catalog", "presented_products": True})))
    async def rewrite(*, result, **kwargs):
        result.reply_text = "1. Fone correto"
        result.commercial_data = {"products": [{"id": "new", "name": "Fone correto"}]}
        return result, SimpleNamespace(applied_handoff=False)
    monkeypatch.setattr(message_pipeline, "apply_response_critique_loop", rewrite)
    result, _ = await replay.say("quero fones")
    saved = replay.states["wa:5511988880001"]
    assert [p["product_id"] for p in saved["last_presented_products"]] == ["new"]
    assert result.response_metadata["commerce_state"]["last_presented_products"] == saved["last_presented_products"]
