"""Offline direct engine contracts; no OpenAI, commerce or channel network."""
import json
import time
from contextlib import contextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.config import Settings
from app.direct.agent import DirectOpenAIAgent, session_scope
from app.direct.tools import DirectTools, tool_schemas
from app.models import IncomingMessage, AgentResult

WORKSPACE = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


def settings(**kwargs):
    return Settings(_env_file=None, CHATBO_WORKSPACE_ID=WORKSPACE, **kwargs)


def persona():
    return NS(id=1, workspace_id=WORKSPACE, tenant_id="xnamai", persona_key="xnamai_commercial",
              status="active", name="XNamai", instructions="Ajude os clientes da XNamai.", metadata={})


def incoming(**kwargs):
    return IncomingMessage(workspace_id=WORKSPACE, provider="brevo", channel="whatsapp",
                           conversation_id="conversation-1", sender_key="person-1", **kwargs)


def response(*, text="Olá!", calls=None, status="completed"):
    return NS(status=status, output=calls or [NS(type="message", id="message-1")],
              output_text=text, usage=NS(input_tokens=10, output_tokens=5))


def client(responses):
    value = NS(responses=NS(create=AsyncMock(side_effect=responses)),
        conversations=NS(create=AsyncMock(return_value=NS(id="conv-remote")),
                         items=NS(list=AsyncMock(return_value=NS(data=[NS(id="message-1")])))))
    value.with_options = Mock(return_value=value)
    return value


def tools(message=None):
    return DirectTools(provider=NS(available=True, execute=AsyncMock(return_value={"ok": True})),
                       incoming=message or incoming(text="Oi"), documents=[])


@pytest.mark.asyncio
async def test_direct_bypasses_legacy(monkeypatch):
    from app import message_pipeline
    from app.direct import pipeline
    monkeypatch.setattr(message_pipeline, "get_settings", lambda: settings(XNAMAI_AGENT_ENGINE="direct"))
    direct = AsyncMock(return_value=AgentResult(reply_text="direto"))
    monkeypatch.setattr(pipeline, "process_direct_message", direct)
    result = await message_pipeline.process_incoming_message(incoming(text="Oi"), {})
    assert result.reply_text == "direto"
    direct.assert_awaited_once()


def test_defaults_and_invalid_engine():
    assert settings().xnamai_agent_engine == "legacy"
    with pytest.raises(ValueError):
        settings(XNAMAI_AGENT_ENGINE="wrong")
    with pytest.raises(ValueError):
        settings(DIRECT_MAX_ROUNDS=0)


@pytest.mark.asyncio
async def test_loop_calls_read_tool_then_final_and_preserves_runtime_budget():
    from app.runtime_context import set_current_turn, reset_current_turn
    from app.turn_runtime import TurnRuntimeContext, LLMCallBudget
    call = NS(type="function_call", name="get_product", arguments='{"product_id":"123"}', call_id="call-1")
    ai = client([response(calls=[call]), response(text="Cabo disponível para consulta.")])
    toolbox = tools()
    runtime = TurnRuntimeContext(trace_id="test", llm_budget=LLMCallBudget(max_calls=1, enforce=True))
    token = set_current_turn(runtime)
    try:
        result = await DirectOpenAIAgent(ai, settings()).run_turn(
            incoming=incoming(text="Cabo"), workspace_id=WORKSPACE, history=[], previous={},
            tools=toolbox, content=[{"type": "input_text", "text": "Cabo"}], persona=persona())
    finally:
        reset_current_turn(token)
    assert result.response_metadata["direct_agent"]["calls"] == 2
    assert runtime.llm_budget.used_calls == 2
    second = ai.responses.create.call_args_list[1].kwargs
    assert second["input"][0]["call_id"] == "call-1"
    assert second["conversation"] == "conv-remote"
    assert ai.with_options.call_args.kwargs["max_retries"] == 0
    assert "temperature" not in second


@pytest.mark.asyncio
async def test_final_round_disallows_tools():
    call = NS(type="function_call", name="get_product", arguments='{"product_id":"1"}', call_id="c")
    ai = client([response(calls=[call]), response(calls=[call])])
    with pytest.raises(RuntimeError, match="tool_limit"):
        await DirectOpenAIAgent(ai, settings(DIRECT_MAX_ROUNDS=2)).run_turn(
            incoming=incoming(), workspace_id=WORKSPACE, history=[], previous={}, tools=tools(),
            content=[], persona=persona())
    assert ai.responses.create.call_args.kwargs["tool_choice"] == "none"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,text", [("incomplete", "partial"), ("completed", ""), ("completed", "x" * 4001)])
async def test_incomplete_empty_oversized_never_accepted(status, text):
    ai = client([response(status=status, text=text)])
    with pytest.raises(RuntimeError):
        await DirectOpenAIAgent(ai, settings()).run_turn(incoming=incoming(), workspace_id=WORKSPACE,
            history=[], previous={}, tools=tools(), content=[], persona=persona())


@pytest.mark.asyncio
async def test_remote_reuse_requires_exact_delivered_tail_scope_and_persona():
    ai = client([])
    agent = DirectOpenAIAgent(ai, settings())
    state = {"direct_agent": {"scope": "scope", "conversation_id": "old", "last_item_id": "message-1",
                              "turn_count": 2, "persona_sha256": "hash"}}
    assert await agent._conversation("scope", [], state, "hash") == ("old", 2)
    ai.conversations.create.assert_not_awaited()
    ai.conversations.items.list.return_value = NS(data=[NS(id="undelivered")])
    history = [{"role": "user", "content": "Entregue"}, {"role": "assistant", "content": "Sim"}]
    assert await agent._conversation("scope", history, state, "hash") == ("conv-remote", 0)
    assert ai.conversations.create.call_args.kwargs["items"] == history
    await agent._conversation("other-scope", [], state, "hash")
    await agent._conversation("scope", [], state, "changed-persona")
    assert ai.conversations.create.await_count == 3
    assert session_scope(WORKSPACE, incoming()) != session_scope(OTHER, incoming())


@pytest.mark.asyncio
async def test_tools_allowlist_validation_and_freshness():
    toolbox = tools()
    assert not (await toolbox.execute("create_order", "{}"))["ok"]
    assert not (await toolbox.execute("get_product", '{"product_id":"1", "tenant_id":"other"}'))["ok"]
    assert not (await toolbox.execute("search_products", '{"query":"cabo","limit":100,"offset":0}'))["ok"]
    toolbox.provider.execute.assert_not_awaited()
    fact = {"ok": True, "product": {"id": "1", "price": None, "stock": 0,
                                    "freshness": {"price_confirmed": False, "stock_confirmed": False}}}
    toolbox.provider.execute.return_value = fact
    assert await toolbox.execute("get_product", '{"product_id":"1"}') == fact
    assert "stock" not in toolbox.products[0]
    toolbox.provider.available = False
    assert (await toolbox.execute("get_product", '{"product_id":"1"}'))["error"] == "catalog_unavailable"
    assert {s["name"] for s in tool_schemas()} == {
        "get_product", "search_products", "compare_products", "check_inventory", "search_knowledge", "request_human"}


@pytest.mark.asyncio
async def test_comparison_reads_beyond_first_page_and_fails_on_missing_page():
    toolbox = tools()
    first = {"ok": True, "products": [{"id": str(i), "name": f"Cabo {i}"} for i in range(20)],
             "paging": {"has_more": True}}
    last = {"ok": True, "products": [{"id": "20", "name": "Melhor cabo"}], "paging": {"has_more": False}}
    toolbox.provider.execute.side_effect = [first, last]
    result = await toolbox.execute("compare_products", '{"query":"cabo"}')
    assert result["complete"] and result["count"] == 21
    assert result["candidates"][-1]["name"] == "Melhor cabo"
    toolbox.provider.execute.side_effect = [first, {"ok": False}]
    result = await toolbox.execute("compare_products", '{"query":"cabo"}')
    assert result["ok"] is False and "candidates" not in result
    toolbox.provider.execute.side_effect = [first, first]
    assert (await toolbox.execute("compare_products", '{"query":"cabo"}'))["error"] == "catalog_changed_retry_search"


@pytest.mark.asyncio
async def test_handoff_requires_backend_consent():
    assert not (await tools().execute("request_human", "{}"))["ok"]
    toolbox = tools(incoming(text="Quero falar com um atendente humano"))
    result = await toolbox.execute("request_human", "{}")
    assert result["ok"] and not result["assigned"] and toolbox.handoff


def test_persona_workspace_guard_and_published_knowledge(monkeypatch):
    from app.direct import context
    fake = persona()
    monkeypatch.setattr(context, "get_active_persona", lambda *a: fake)
    assert context.load_persona(settings(), WORKSPACE)[0] == WORKSPACE
    with pytest.raises(ValueError):
        context.load_persona(settings(), OTHER)
    fake.workspace_id = OTHER
    with pytest.raises(ValueError):
        context.load_persona(settings(), WORKSPACE)
    fake.metadata = {"knowledge_documents": [
        {"id": "ok", "content": "Garantia de fábrica", "status": "approved"},
        {"id": "draft", "content": "Segredo", "status": "draft"},
        {"id": "expired", "content": "Vencido", "valid_until": "2000-01-01T00:00:00Z"}]}
    assert [d["id"] for d in context.knowledge_documents(fake)] == ["ok"]


def test_history_query_strictly_scoped_and_delivered(monkeypatch):
    from app.direct import context
    cur = Mock()
    cur.fetchall.return_value = [{"text": "Oi", "reply_text": "Olá", "metadata": {"engine": "direct"}}]
    @contextmanager
    def cursor():
        yield cur
    @contextmanager
    def connection():
        yield NS(cursor=cursor)
    monkeypatch.setattr(context, "get_conn", connection)
    history, previous = context.load_history(incoming(raw={"inbound_id": 10}), WORKSPACE)
    query, params = cur.execute.call_args.args
    assert "provider_send_ok=true" in query
    assert "i.provider=%(provider)s" in query and "i.channel=%(channel)s" in query
    assert "COALESCE(i.sender_key" in query and "i.id < %(before)s" in query
    assert params["workspace"] == WORKSPACE and params["before"] == 10
    assert len(history) == 2 and previous["engine"] == "direct"


@pytest.mark.asyncio
async def test_pipeline_failure_does_not_fallback_or_expose_error(monkeypatch):
    from app.direct import pipeline
    monkeypatch.setattr(pipeline, "run_direct_turn", AsyncMock(side_effect=RuntimeError("secret")))
    result = await pipeline.process_direct_message(incoming(), {})
    assert result.safety_reason == "direct_unavailable"
    assert "secret" not in result.reply_text


def test_preview_auth_scope_signature_expiry_and_no_sends(monkeypatch):
    from app.direct import admin
    from app import security
    cfg = settings(ADMIN_API_TOKEN="test-admin")
    monkeypatch.setattr(admin, "get_settings", lambda: cfg)
    monkeypatch.setattr(security, "get_settings", lambda: cfg)
    metrics = dict(model="fake", calls=1, tools=0, input_tokens=1, output_tokens=1,
                   latency_ms=1, prompt_version="test", knowledge_document_count=0)
    turn = AsyncMock(return_value=AgentResult(reply_text="Preview", response_metadata={"direct_agent": metrics}))
    monkeypatch.setattr(admin, "run_direct_turn", turn)
    app = FastAPI()
    app.include_router(admin.router)
    with TestClient(app) as http:
        payload = {"workspace_id": WORKSPACE, "text": "Oi"}
        assert http.post("/api/test/direct", json=payload).status_code == 401
        headers = {"Authorization": "Bearer test-admin"}
        assert http.post("/api/test/direct", json={**payload, "workspace_id": OTHER}, headers=headers).status_code == 403
        data = http.post("/api/test/direct", json=payload, headers=headers).json()
        assert data["sent_to_customer"] is False and data["active_engine"] == "legacy"
        payload["session"] = data["session"]
        assert http.post("/api/test/direct", json=payload, headers=headers).status_code == 200
        assert len(turn.call_args.kwargs["history"]) == 2
        payload["channel"] = "instagram"
        assert http.post("/api/test/direct", json=payload, headers=headers).status_code == 400
    with pytest.raises(HTTPException):
        admin.decode_session(data["session"] + "tampered", "test-admin", WORKSPACE, "whatsapp")
    expired = admin.encode_session({"workspace": WORKSPACE, "channel": "whatsapp", "expires": time.time() - 1}, "test-admin")
    with pytest.raises(HTTPException):
        admin.decode_session(expired, "test-admin", WORKSPACE, "whatsapp")


@pytest.mark.asyncio
async def test_media_image_failure_and_audio_initial_modality(monkeypatch):
    from app.direct import media
    from app import instagram_story_media
    monkeypatch.setattr(instagram_story_media, "download_story_media", AsyncMock(side_effect=ValueError("blocked")))
    result = await media.input_content(incoming(text="foto", image_url="http://127.0.0.1"))
    assert len(result) == 1 and "Imagem indisponível" in result[0]["text"]
    transcribe = AsyncMock(return_value="Quero cabo USB-C")
    monkeypatch.setattr(media, "transcribe_audio", transcribe)
    message = incoming(audio_url="https://example.test/audio", input_modality="audio")
    result = await media.input_content(message)
    assert result[0]["text"] == "Quero cabo USB-C" and message.text == result[0]["text"]
    transcribe.assert_awaited_once()


@pytest.mark.asyncio
async def test_pipeline_full_composition_uses_published_persona_and_offline_client(monkeypatch):
    from app.direct import pipeline
    from app import openai_client
    from app.commerce import provider
    cfg = settings()
    active = persona()
    active.metadata["knowledge_documents"] = [{"id": "policy", "title": "Garantia",
                                               "content": "Consulte a nota fiscal.", "status": "approved"}]
    ai = client([response(text="Olá, sou o assistente da XNamai.")])
    monkeypatch.setattr(pipeline, "get_settings", lambda: cfg)
    monkeypatch.setattr(pipeline, "load_persona", lambda *a: (WORKSPACE, active))
    monkeypatch.setattr(pipeline, "load_history", lambda *a: ([], {}))
    monkeypatch.setattr(openai_client, "get_async_openai_client", lambda: ai)
    monkeypatch.setattr(provider, "get_commerce_provider", lambda: NS(available=True))
    result = await pipeline.run_direct_turn(incoming(text="Oi"))
    assert result.response_metadata["persona_runtime"]["workspace_id"] == WORKSPACE
    assert result.response_metadata["direct_agent"]["knowledge_document_count"] == 1
    assert "Ajude os clientes da XNamai." in ai.responses.create.call_args.kwargs["instructions"]


@pytest.mark.asyncio
async def test_sdk_conversations_responses_wire_contract():
    import httpx
    from openai import AsyncOpenAI
    requests = []
    def handler(request):
        requests.append((request.url.path, json.loads(request.content)))
        if request.url.path == "/v1/conversations":
            return httpx.Response(200, json={"id": "conv_wire", "object": "conversation", "created_at": 1, "metadata": {}})
        return httpx.Response(200, json={"id": "resp_wire", "object": "response", "created_at": 1,
            "status": "completed", "model": "gpt-5.4", "output": [{"id": "msg_wire", "type": "message",
                "role": "assistant", "status": "completed", "content": [
                    {"type": "output_text", "text": "Olá da XNamai", "annotations": []}]}],
            "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        ai = AsyncOpenAI(api_key="test-key", http_client=http)
        result = await DirectOpenAIAgent(ai, settings()).run_turn(incoming=incoming(text="Oi"),
            workspace_id=WORKSPACE, history=[], previous={}, tools=tools(), content=[{"type": "input_text", "text": "Oi"}], persona=persona())
    assert result.reply_text == "Olá da XNamai"
    assert requests[1][1]["conversation"] == "conv_wire"
    assert requests[1][1]["parallel_tool_calls"] is False
    assert result.response_metadata["direct_agent"]["last_item_id"] == "msg_wire"


@pytest.mark.asyncio
async def test_tool_exception_redacts_and_never_retries():
    toolbox = tools()
    toolbox.provider.execute.side_effect = RuntimeError("sensitive payload")
    result = await toolbox.execute("get_product", '{"product_id":"1"}')
    assert result == {"ok": False, "error": "tool_unavailable"}
    assert toolbox.provider.execute.await_count == 1


@pytest.mark.asyncio
async def test_comparison_at_limit_refuses_partial_recommendation():
    toolbox = tools()
    async def page(_, args):
        return {"ok": True, "products": [{"id": str(i), "name": "Cabo"}
                for i in range(args["offset"], args["offset"] + 20)], "paging": {"has_more": True}}
    toolbox.provider.execute.side_effect = page
    result = await toolbox.execute("compare_products", '{"query":""}')
    assert result["error"] == "too_many_candidates" and "candidates" not in result
    assert toolbox.provider.execute.await_count == 25


@pytest.mark.asyncio
async def test_audio_blocks_private_url_before_download():
    from app.direct.media import transcribe_audio
    with pytest.raises(Exception, match="scheme_not_https"):
        await transcribe_audio("http://127.0.0.1/private")


@pytest.mark.asyncio
async def test_video_does_not_claim_to_see_frames():
    from app.direct.media import input_content
    result = await input_content(incoming(instagram_story=NS(media_type="video")))
    assert len(result) == 1 and "peça uma foto" in result[0]["text"]
