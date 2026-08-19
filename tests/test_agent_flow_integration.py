from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.models import AgentResult, BrevoSendResult, IncomingMessage, SalesInterpretation
from openai_test_utils import install_fake_openai_client


def _settings(**overrides):
    values = {
        "openai_api_key": "test-key",
        "openai_model": "gpt-4.1-mini",
        "openai_main_model": "gpt-4.1-mini",
        "openai_fast_model": "gpt-4.1-nano",
        "agent_turn_understanding_enabled": False,
        "audio_inbound_enabled": False,
        "audio_outbound_enabled": False,
        "tray_adapter_url": "https://tray-adapter.test",
        "tray_adapter_token": "secret",
        "max_reply_chars": 900,
        "database_url": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_real_webhook_flow_persists_and_reloads_context_for_followup(monkeypatch, capsys):
    import api.index as index
    import app.message_pipeline as message_pipeline
    import app.openai_agent as openai_agent
    import app.sales_agent as sales_agent

    state = {"inbound": [], "responses": []}
    interpreter_requests = []
    clarification_replies = iter([
        "Você prefere um estilo mais esportivo, social ou casual?",
        "Perfeito. Qual faixa de preço você tem em mente?",
    ])

    def claim(message):
        inbound_id = len(state["inbound"]) + 1
        state["inbound"].append({"id": inbound_id, **message})
        return True, inbound_id

    def load_turns(
        *,
        conversation_id,
        sender_phone,
        before_inbound_id,
        limit=8,
        sender_key=None,
        hard_cap=40,
    ):
        _ = hard_cap
        rows = [
            row for row in state["inbound"]
            if row["id"] < before_inbound_id
            and (
                row.get("conversation_id") == conversation_id
                if conversation_id
                else row.get("sender_phone") == sender_phone
            )
        ]
        turns = []
        for row in rows:
            turns.append({"role": "user", "content": row["text"]})
            delivered = next(
                (
                    response for response in state["responses"]
                    if response["inbound_id"] == row["id"] and response["provider_send_ok"] is True
                ),
                None,
            )
            if delivered:
                assistant_turn = {"role": "assistant", "content": delivered["reply_text"]}
                if delivered.get("safety_reason"):
                    assistant_turn["metadata"] = {"safety_reason": delivered["safety_reason"]}
                turns.append(assistant_turn)
        return turns[-limit:]

    class FakeCompletions:
        async def parse(self, **kwargs):
            interpreter_requests.append(kwargs["messages"])
            current_text = kwargs["messages"][-1]["content"]
            if current_text == "quero comprar um relógio":
                interpretation = SalesInterpretation(
                    domain="commerce",
                    goal="discover",
                    subject={"product_type": "relógio"},
                    preferences={},
                    references_previous_context=False,
                    needs_clarification=True,
                    clarification_question=None,
                    confidence=0.97,
                )
            else:
                interpretation = SalesInterpretation(
                    domain="commerce",
                    goal="discover",
                    subject={"product_type": "relógio"},
                    preferences={"style": "esportivo"},
                    references_previous_context=True,
                    needs_clarification=True,
                    clarification_question=None,
                    confidence=0.98,
                )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(parsed=interpretation, refusal=None))]
            )

        async def create(self, **kwargs):
            if kwargs["messages"][0]["content"] == sales_agent.SALES_RESPONDER_INSTRUCTIONS:
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="Encontrei opções esportivas para você."))]
                )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=next(clarification_replies)))]
            )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    async def fake_send(incoming, result):
        return BrevoSendResult(ok=True, dry_run=False, status_code=200, provider_response={"sent": True})

    monkeypatch.setattr(index, "inbound_message_exists", lambda provider, message_id: False)
    monkeypatch.setattr(index, "claim_inbound_message", claim)
    monkeypatch.setattr(index, "insert_agent_response", lambda data: state["responses"].append(dict(data)))
    monkeypatch.setattr(index, "is_latest_inbound_message", lambda *args: True)
    monkeypatch.setattr(index, "find_customer_profile_by_phone", lambda phone: {})
    monkeypatch.setattr(index, "send_brevo_reply", fake_send)
    monkeypatch.setattr(message_pipeline, "get_settings", lambda: _settings())
    monkeypatch.setattr(openai_agent, "get_settings", lambda: _settings())
    monkeypatch.setattr(openai_agent, "load_recent_conversation_turns", load_turns)
    monkeypatch.setattr(sales_agent, "get_settings", lambda: _settings())
    install_fake_openai_client(monkeypatch, FakeOpenAI)
    async def fake_execute(name, arguments):
        return {"products": [{"id": "1", "name": "Relógio esportivo", "style": "esportivo"}]}

    monkeypatch.setattr(sales_agent, "execute_tool", fake_execute)

    index.app.dependency_overrides[index.verify_brevo_webhook] = lambda: None
    try:
        async with AsyncClient(transport=ASGITransport(app=index.app), base_url="http://test") as client:
            first = await client.post(
                "/api/webhooks/brevo/whatsapp",
                json={
                    "eventName": "conversationFragment",
                    "conversationId": "conversation-1",
                    "visitor": {"id": "visitor-1", "attributes": {"SMS": "5511999999999"}},
                    "messages": [{"id": "message-1", "type": "visitor", "text": "quero comprar um relógio", "createdAt": "2026-07-22T10:00:00Z"}],
                },
            )
            second = await client.post(
                "/api/webhooks/brevo/whatsapp",
                json={
                    "eventName": "conversationFragment",
                    "conversationId": "conversation-1",
                    "visitor": {"id": "visitor-1", "attributes": {"SMS": "5511999999999"}},
                    "messages": [{"id": "message-2", "type": "visitor", "text": "esportivo", "createdAt": "2026-07-22T10:01:00Z"}],
                },
            )
    finally:
        index.app.dependency_overrides.pop(index.verify_brevo_webhook, None)

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(state["inbound"]) == 2
    assert state["responses"][0]["provider_send_ok"] is True
    assert state["responses"][0]["provider_response"]["_agent_context"]["commerce_state"]["active_domain"] == "commerce"
    assert state["responses"][1]["reply_text"] != sales_agent.OUT_OF_SCOPE_REPLY
    second_messages = interpreter_requests[1]
    assert second_messages[2:] == [
        {"role": "user", "content": "quero comprar um relógio"},
        {"role": "assistant", "content": "Você prefere um estilo mais esportivo, social ou casual?"},
        {"role": "user", "content": "esportivo"},
    ]
    assert second_messages[1]["role"] == "system"
    assert second_messages[1]["content"].startswith("COMMERCE_STATE:")
    logs = capsys.readouterr().out
    assert '"event": "sales.context"' in logs or "[agent.obs]" in logs
    assert '"history_user_turns": 1' in logs
    assert '"history_assistant_turns": 1' in logs
    assert '"event": "agent.response"' in logs
    assert '"response_source": "openai"' in logs


@pytest.mark.asyncio
async def test_valid_commerce_interpretation_reaches_openai_sales_responder(monkeypatch):
    import app.openai_agent as openai_agent
    import app.sales_agent as sales_agent

    interpretation = SalesInterpretation(
        domain="commerce",
        goal="find",
        subject={"brand": "Tissot", "model": "Seastar"},
        preferences={},
        references_previous_context=False,
        needs_clarification=False,
        clarification_question=None,
        confidence=0.99,
    )
    tool_calls = []

    async def fake_interpret(message, *, recent_turns=None, commerce_state=None):
        return interpretation

    async def fake_execute(name, arguments):
        tool_calls.append((name, arguments))
        return {"products": [{"id": "1", "name": "Tissot Seastar", "brand": "Tissot", "model": "Seastar", "current_price": 4999}]}

    class FakeCompletions:
        async def create(self, **kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="Encontrei um Tissot Seastar que combina com o que você procura."))]
            )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(openai_agent, "get_settings", lambda: _settings())
    monkeypatch.setattr(openai_agent, "load_recent_conversation_turns", lambda **kwargs: [])
    monkeypatch.setattr(openai_agent, "interpret_message", fake_interpret)
    monkeypatch.setattr(sales_agent, "get_settings", lambda: _settings())
    install_fake_openai_client(monkeypatch, FakeOpenAI)
    monkeypatch.setattr(sales_agent, "execute_tool", fake_execute)
    monkeypatch.setattr(
        "app.commerce_router.resolve_commerce_action",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("structured action must not be reclassified")),
    )
    monkeypatch.setattr(
        "app.commerce_router.extract_product_query",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("structured query must not be extracted again")),
    )

    result = await openai_agent.generate_agent_reply_async(
        IncomingMessage(text="Tem Tissot Seastar?"),
        {},
    )

    assert result.reply_text == "Encontrei um Tissot Seastar que combina com o que você procura."
    search_calls = [call for call in tool_calls if call[0] == "search_products"]
    assert search_calls
    assert any(
        call[1].get("name") == "Seastar" and call[1].get("brand") == "Tissot"
        for call in search_calls
    )
    assert ("get_product", {"product_id": "1"}) in tool_calls
    assert result.response_metadata["used_openai_interpreter"] is True
    assert result.response_metadata["used_openai_responder"] is True
    assert result.response_metadata["used_tray"] is True


@pytest.mark.asyncio
async def test_valid_commerce_domain_is_not_overridden_by_local_raffle_classifier(monkeypatch):
    import app.openai_agent as openai_agent

    interpretation = SalesInterpretation(
        domain="commerce",
        goal="find",
        subject={"product_type": "relógio"},
        preferences={},
        information_needed=["catalog"],
        references_previous_context=False,
        needs_clarification=False,
        clarification_question=None,
        confidence=0.96,
    )

    async def fake_interpret(message, *, recent_turns=None, commerce_state=None):
        return interpretation

    async def fake_sales_handler(message, facts, customer_context, semantic_plan, recent_turns=None, commerce_state=None):
        assert semantic_plan is interpretation
        return AgentResult(reply_text="Resposta comercial contextual.", intent="commerce")

    monkeypatch.setattr(openai_agent, "get_settings", lambda: _settings())
    monkeypatch.setattr(openai_agent, "load_recent_conversation_turns", lambda **kwargs: [])
    monkeypatch.setattr(openai_agent, "interpret_message", fake_interpret)
    monkeypatch.setattr(openai_agent, "gather_customer_facts", lambda *args, **kwargs: {"primary_intent": "rules", "intents": ["rules"]})
    monkeypatch.setattr(openai_agent, "handle_sales_message", fake_sales_handler)
    monkeypatch.setattr(
        openai_agent,
        "_local_raffle_reply",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("raffle handler must respect the interpreted domain")),
    )

    result = await openai_agent.generate_agent_reply_async(
        IncomingMessage(text="Como funciona esse relógio?"),
        {},
    )

    assert result.reply_text == "Resposta comercial contextual."
    assert result.response_metadata["domain"] == "commerce"


@pytest.mark.asyncio
async def test_interpreter_fallback_is_observable_and_only_then_controls_scope(monkeypatch):
    import app.openai_agent as openai_agent
    import app.sales_agent as sales_agent

    settings = _settings(openai_api_key="")
    monkeypatch.setattr(openai_agent, "get_settings", lambda: settings)
    monkeypatch.setattr(openai_agent, "load_recent_conversation_turns", lambda **kwargs: [])
    monkeypatch.setattr(sales_agent, "get_settings", lambda: settings)

    result = await openai_agent.generate_agent_reply_async(IncomingMessage(text="esportivo"), {})

    assert result.intent == "out_of_scope"
    assert result.response_metadata["response_source"] == "deterministic_fallback"
    assert result.response_metadata["used_openai_interpreter"] is False
    assert result.response_metadata["fallback_reason"] == "openai_api_key_missing"
