from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.models import AgentResult, IncomingMessage, SalesInterpretation
from app.context_builder import detect_customer_intents, gather_customer_facts, _primary_intent
from openai_test_utils import install_fake_openai_client


def _settings(**overrides):
    values = {
        "openai_api_key": "",
        "openai_model": "gpt-test",
        "tray_adapter_url": "https://tray-adapter.test",
        "tray_adapter_token": "secret-that-must-not-leak",
        "app_name": "test",
        "dry_run": True,
        "environment": "test",
        "database_url": "",
        "brevo_api_key": "",
        "brevo_agent_id": "",
        "brevo_agent_email": "",
        "brevo_agent_name": "",
        "brevo_sender_number": "",
        "brevo_reply_mode": "dry_run",
        "brevo_webhook_secret": "",
        "audio_inbound_enabled": True,
        "audio_outbound_enabled": True,
        "supabase_url": "",
        "supabase_service_key": "",
        "max_reply_chars": 900,
        "admin_api_token": "admin-secret",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_commerce_intents_and_local_intents_remain_distinct():
    assert _primary_intent(detect_customer_intents("Vocês têm Tissot Seastar?")) == "commerce"
    assert _primary_intent(detect_customer_intents("Tem estoque desse relógio?")) == "commerce"
    assert _primary_intent(detect_customer_intents("Quanto custa?")) == "commerce"
    assert _primary_intent(detect_customer_intents("Quanto fica no Pix?")) == "commerce"
    assert _primary_intent(detect_customer_intents("saldo")) == "balance"
    assert "commerce" not in detect_customer_intents("saldo do João")


def test_semantic_sales_plan_is_generic_and_preserves_constraints():
    from app.sales_agent import _normalize_semantic_plan

    plan = _normalize_semantic_plan({
        "domain": "commerce",
        "goal": "recommend",
        "subject": {"product_type": "relógio", "query": "Citizen elegante", "brand": "Citizen"},
        "constraints": {"budget_max": 3000, "attributes": ["elegante"]},
        "information_needed": ["catalog"],
    })
    assert plan["goal"] == "recommend"
    assert plan["subject"]["brand"] == "Citizen"
    assert plan["constraints"]["budget_max"] == 3000
    assert "Citizen elegante" in plan["query"]


@pytest.mark.asyncio
async def test_third_party_balance_remains_blocked(monkeypatch):
    from app import openai_agent

    monkeypatch.setattr(openai_agent, "get_settings", lambda: _settings(openai_api_key=""))
    result = await openai_agent.generate_agent_reply_async(
        IncomingMessage(sender_phone="5511999999999", text="saldo do João"),
        {},
    )
    assert result.intent == "security_refusal"


@pytest.mark.asyncio
async def test_greeting_does_not_lookup_account_or_handoff(monkeypatch):
    from app import openai_agent

    monkeypatch.setattr(openai_agent, "get_settings", lambda: _settings(openai_api_key=""))
    monkeypatch.setattr(openai_agent, "find_coupon_balance_by_phone", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("greeting must not lookup account")))
    result = await openai_agent.generate_agent_reply_async(IncomingMessage(text="olá"), {})
    assert result.intent == "general"
    assert result.handoff_required is False
    assert result.reply_text


def test_commerce_facts_do_not_lookup_personal_balance(monkeypatch):
    def fail_account_lookup(*args, **kwargs):
        raise AssertionError("commerce must not query local account")

    monkeypatch.setattr("app.context_builder.find_coupon_balance_by_phone", fail_account_lookup)
    facts = gather_customer_facts(
        IncomingMessage(sender_phone="5511999999999", text="Vocês têm Tissot Seastar?"),
        {"found": True, "name": "Cliente"},
    )
    assert facts["primary_intent"] == "commerce"
    assert facts["account"] == {"found": False}
    assert facts["display_name"] == "Cliente"


@pytest.mark.asyncio
async def test_async_agent_does_not_preload_account_for_commerce(monkeypatch):
    from app import openai_agent

    monkeypatch.setattr(openai_agent, "get_settings", lambda: _settings(openai_api_key=""))
    monkeypatch.setattr(openai_agent, "find_coupon_balance_by_phone", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not lookup account")))
    result = await openai_agent.generate_agent_reply_async(
        IncomingMessage(text="Quanto fica no Pix?"),
        {},
    )
    assert result is not None


@pytest.mark.asyncio
async def test_ean_is_commerce_not_third_party(monkeypatch):
    from app import openai_agent

    monkeypatch.setattr(openai_agent, "get_settings", lambda: _settings(openai_api_key=""))
    calls = []

    async def fake_execute(name, arguments):
        calls.append((name, arguments))
        return {"products": []}

    monkeypatch.setattr("app.commerce_router.execute_tool", fake_execute)
    monkeypatch.setattr("app.sales_agent.execute_tool", fake_execute)
    result = await openai_agent.generate_agent_reply_async(
        IncomingMessage(text="Tem o EAN 7611608287637?"),
        {},
    )
    assert result.intent == "commerce"
    assert result.safety_reason == "product_not_found"
    assert calls == [("search_products", {"ean": "7611608287637", "limit": 20, "page": 1})]


@pytest.mark.asyncio
async def test_general_does_not_send_tray_tools_or_commercial_fallback(monkeypatch):
    from app import openai_agent

    monkeypatch.setattr(openai_agent, "get_settings", lambda: _settings(openai_api_key=""))
    monkeypatch.setattr("app.commerce_router.execute_tool", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("general must not call Tray")))
    result = await openai_agent.generate_agent_reply_async(IncomingMessage(text="oi"), {})
    assert result.intent == "general"
    assert result.reply_text == "Ol\u00e1! Como posso ajudar?"
    assert "informa\u00e7\u00f5es da loja" not in result.reply_text


@pytest.mark.asyncio
async def test_out_of_scope_is_refused_without_openai_answer_or_tray(monkeypatch):
    from app import openai_agent
    import app.sales_agent as sales_agent

    settings = _settings(openai_api_key="")
    monkeypatch.setattr(openai_agent, "get_settings", lambda: settings)
    monkeypatch.setattr(sales_agent, "get_settings", lambda: settings)
    monkeypatch.setattr("app.commerce_router.execute_tool", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("out of scope must not call Tray")))
    result = await openai_agent.generate_agent_reply_async(IncomingMessage(text="quem ganhou o jogo ontem?"), {})
    assert result.intent == "out_of_scope"
    assert "NewStore" in result.reply_text


@pytest.mark.asyncio
async def test_purchase_intent_uses_product_entity_not_full_sentence(monkeypatch):
    import app.sales_agent as sales_agent

    settings = _settings(openai_api_key="test-key")
    monkeypatch.setattr(sales_agent, "get_settings", lambda: settings)
    calls = []

    class FakeCompletions:
        async def create(self, **kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Qual preferência é mais importante para você?"))])

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    async def fake_execute(name, arguments):
        calls.append((name, arguments))
        return {"products": [{"id": "1", "name": "Relógio esportivo", "current_price": 1000}]}

    monkeypatch.setattr("app.commerce_router.execute_tool", fake_execute)
    install_fake_openai_client(monkeypatch, FakeClient)
    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="quero comprar um relógio"),
        {"primary_intent": "commerce"},
        {},
        SalesInterpretation(
            domain="commerce",
            goal="discover",
            subject={"product_type": "relógio"},
            preferences={},
            references_previous_context=False,
            needs_clarification=True,
            clarification_question=None,
            confidence=0.95,
        ),
    )
    assert calls == []
    assert result.safety_reason == "commerce_clarification"
    assert result.reply_text == "Qual preferência é mais importante para você?"
    assert result.response_metadata["used_openai_responder"] is True
    assert result.intent == "commerce"


@pytest.mark.asyncio
async def test_broad_recommendation_with_budget_starts_retrieval(monkeypatch):
    import app.sales_agent as sales_agent

    settings = _settings(openai_api_key="test-key")
    monkeypatch.setattr(sales_agent, "get_settings", lambda: settings)
    calls = []

    class FakeCompletions:
        async def parse(self, **kwargs):
            selection = SimpleNamespace(selected_product_ids=["2"])
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=selection))])

        async def create(self, **kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Encontrei uma opção dentro da faixa informada."))])

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    async def fake_execute(name, arguments):
        calls.append((name, arguments))
        return {"products": [{"id": "2", "name": "Relógio esportivo preto", "current_price": 4500}]}

    monkeypatch.setattr(sales_agent, "execute_tool", fake_execute)
    install_fake_openai_client(monkeypatch, FakeClient)
    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="quero comprar um relógio por menos de 5 mil"),
        {"primary_intent": "commerce"},
        {},
        SalesInterpretation(
            domain="commerce",
            goal="recommend",
            subject={"product_type": "relógio"},
            preferences={"budget_max": 5000},
            references_previous_context=False,
            needs_clarification=False,
            clarification_question=None,
            confidence=0.94,
        ),
    )
    search_calls = [call for call in calls if call[0] == "search_products"]
    assert search_calls == [("search_products", {"name": "relógio", "available": True, "available_in_store": True, "limit": 20, "page": 1})]
    assert result.reply_text == "Encontrei uma opção dentro da faixa informada."
    assert result.safety_reason != "recommendation_not_found"
    assert result.response_metadata["used_tray"] is True


@pytest.mark.asyncio
async def test_product_search_uses_progressive_strategies(monkeypatch):
    import app.sales_agent as sales_agent

    settings = _settings(openai_api_key="")
    monkeypatch.setattr(sales_agent, "get_settings", lambda: settings)
    calls = []

    async def fake_execute(name, arguments):
        calls.append(arguments)
        # Match any exact/token probe for Seastar/Tissot.
        tokens = [str(t).casefold() for t in (arguments.get("tokens") or [])]
        name_arg = str(arguments.get("name") or "").casefold()
        brand = str(arguments.get("brand") or "").casefold()
        if (
            "seastar" in name_arg
            or ("seastar" in tokens and "tissot" in tokens)
            or (brand == "tissot" and "seastar" in name_arg)
        ):
            return {"products": [{"id": "3", "name": "Tissot Seastar"}]}
        return {"products": []}

    monkeypatch.setattr(sales_agent, "execute_tool", fake_execute)
    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="Tem Tissot Seastar?"),
        {"primary_intent": "commerce"},
        {},
        SalesInterpretation(
            domain="commerce",
            goal="find",
            subject={"brand": "Tissot", "model": "Seastar"},
            preferences={},
            references_previous_context=False,
            needs_clarification=False,
            clarification_question=None,
            confidence=0.98,
        ),
    )
    # Production uses parallel multi-strategy probes (token + exact), not a single call.
    assert len(calls) >= 1
    assert any(
        ("tokens" in arguments and "seastar" in [str(t).casefold() for t in arguments.get("tokens") or []])
        or str(arguments.get("name") or "").casefold() == "seastar"
        for arguments in calls
    )
    assert "Tissot Seastar" in result.reply_text


@pytest.mark.asyncio
async def test_ranking_removes_incompatible_brand_model_candidate(monkeypatch):
    import app.sales_agent as sales_agent

    settings = _settings(openai_api_key="")
    monkeypatch.setattr(sales_agent, "get_settings", lambda: settings)

    async def fake_execute(name, arguments):
        return {"products": [
            {"id": "1", "name": "Tissot Seastar preto", "brand": "Tissot", "model": "Seastar", "current_price": 5000},
            {"id": "2", "name": "Tissot Tradition", "brand": "Tissot", "model": "Tradition", "current_price": 4000},
        ]}

    monkeypatch.setattr("app.commerce_router.execute_tool", fake_execute)
    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="Tem Tissot Seastar?"),
        {"primary_intent": "commerce"},
        {},
        {"domain": "commerce", "intent": "product_search", "goal": "find", "query": "Tissot Seastar", "subject": {"query": "Tissot Seastar", "brand": "Tissot", "model": "Seastar"}, "constraints": {}, "filters": {"brand": "Tissot", "model": "Seastar"}, "_source": "openai"},
    )
    assert "Tissot Seastar" in result.reply_text
    assert "Tissot Tradition" not in result.reply_text


@pytest.mark.asyncio
async def test_rules_use_local_flow_without_tray(monkeypatch):
    from app import openai_agent
    import app.sales_agent as sales_agent

    settings = _settings(openai_api_key="")
    monkeypatch.setattr(openai_agent, "get_settings", lambda: settings)
    monkeypatch.setattr(sales_agent, "get_settings", lambda: settings)
    monkeypatch.setattr("app.commerce_router.execute_tool", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("rules must not call Tray")))
    result = await openai_agent.generate_agent_reply_async(IncomingMessage(text="como funciona o sorteio?"), {})
    assert result.intent == "rules_faq"
    assert "Lotomania" in result.reply_text


@pytest.mark.asyncio
async def test_stale_inbound_does_not_send_old_agent_reply(monkeypatch):
    import api.index as index

    monkeypatch.setattr(index, "inbound_message_exists", lambda *args: False)
    monkeypatch.setattr(index, "claim_inbound_message", lambda message: (True, 41))
    monkeypatch.setattr(index, "is_latest_inbound_message", lambda *args: False)
    monkeypatch.setattr(index, "find_customer_profile_by_phone", lambda phone: {})
    monkeypatch.setattr(index, "process_incoming_message", lambda *args: _async_result("commerce"))
    monkeypatch.setattr(index, "send_brevo_reply", lambda *args: (_ for _ in ()).throw(AssertionError("stale reply must not send")))
    recorded = []
    monkeypatch.setattr(index, "insert_agent_response", lambda data: recorded.append(data))
    index.app.dependency_overrides[index.verify_brevo_webhook] = lambda: None
    try:
        async with AsyncClient(transport=ASGITransport(app=index.app), base_url="http://test") as client:
            response = await client.post(
                "/api/webhooks/brevo/whatsapp",
                json={"id": "stale-1", "conversationId": "conv-1", "from": "5511999999999", "text": "Tem Tissot?"},
            )
    finally:
        index.app.dependency_overrides.pop(index.verify_brevo_webhook, None)
    assert response.status_code == 200
    assert response.json()["skipped_reply"] is True
    assert recorded[0]["provider_send_ok"] is False
    assert recorded[0]["provider_response"]["reason"] == "stale_inbound"


async def _async_result(intent):
    return AgentResult(reply_text="ok", intent=intent, handoff_required=False)


@pytest.mark.asyncio
async def test_health_exposes_only_tray_flags(monkeypatch):
    import api.index as index

    settings = _settings()
    monkeypatch.setattr(index, "get_settings", lambda: settings)
    payload = await index.health()
    assert payload["tray_adapter_configured"] is True
    assert payload["tray_tools_enabled"] is True
    assert settings.tray_adapter_token not in str(payload)
    assert settings.tray_adapter_url not in str(payload)


@pytest.mark.asyncio
async def test_tray_diagnostic_uses_client_and_is_admin_protected(monkeypatch):
    import api.index as index
    import app.security as security

    settings = _settings()
    monkeypatch.setattr(index, "get_settings", lambda: settings)
    monkeypatch.setattr(security, "get_settings", lambda: settings)
    calls = []

    class FakeTrayClient:
        async def search_products(self, **kwargs):
            calls.append(kwargs)
            return {"products": []}

    monkeypatch.setattr(index, "TrayAdapterClient", FakeTrayClient)
    from api.index import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/integrations/tray/test",
            headers={"Authorization": "Bearer admin-secret"},
        )
    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "tray_adapter_connected": True,
        "products_accessible": True,
    }
    assert calls == [{"limit": 1}]


@pytest.mark.asyncio
async def test_brevo_duplicate_message_is_skipped_before_processing(monkeypatch):
    import api.index as index

    monkeypatch.setattr(index, "inbound_message_exists", lambda provider, message_id: provider == "brevo" and message_id == "msg-1")
    monkeypatch.setattr(index, "insert_inbound_message", lambda *_: (_ for _ in ()).throw(AssertionError("duplicate must not be inserted")))
    monkeypatch.setattr(index, "process_incoming_message", lambda *_: (_ for _ in ()).throw(AssertionError("duplicate must not be processed")))
    index.app.dependency_overrides[index.verify_brevo_webhook] = lambda: None
    try:
        async with AsyncClient(transport=ASGITransport(app=index.app), base_url="http://test") as client:
            response = await client.post(
                "/api/webhooks/brevo/whatsapp",
                json={"id": "msg-1", "from": "5511999999999", "text": "olá"},
            )
    finally:
        index.app.dependency_overrides.pop(index.verify_brevo_webhook, None)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "skipped": True, "reason": "duplicate_message"}
