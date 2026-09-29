"""B6 — orcamento LLM por turno: fallback e retry interno tambem consomem."""

from __future__ import annotations

import httpx
import pytest

from app.turn_runtime import LLMCallBudget, LLMCallBudgetExceeded, TurnRuntimeContext


def test_budget_exposes_limit_used_remaining():
    budget = LLMCallBudget(max_calls=3, max_transport_attempts=8, enforce=True)
    budget.reserve("decision")
    assert (budget.limit, budget.used, budget.remaining) == (3, 1, 2)


def test_remaining_is_bounded_by_the_transport_ceiling():
    budget = LLMCallBudget(max_calls=4, max_transport_attempts=2, transport_attempts=2, enforce=True)
    assert budget.remaining == 0
    with pytest.raises(LLMCallBudgetExceeded, match="llm_transport_budget_exceeded"):
        budget.reserve("response_composition")


def test_responses_to_chat_fallback_consumes_transport_budget():
    runtime = TurnRuntimeContext(
        trace_id="t", llm_budget=LLMCallBudget(max_calls=2, max_transport_attempts=2, enforce=True)
    )
    runtime.register_openai_call("decision", transport="responses")
    # Responses falhou: o gateway devolve a operacao LOGICA e tenta no Chat.
    runtime.release_failed_openai_attempt("decision")
    runtime.register_openai_call("decision", transport="chat_completions")

    assert runtime.llm_budget.used_calls == 1  # mesma operacao logica
    assert runtime.llm_budget.transport_attempts == 2  # mas duas tentativas reais
    with pytest.raises(LLMCallBudgetExceeded):
        runtime.register_openai_call("response_composition", transport="responses")
    assert "llm_budget_exceeded" in runtime.fallback_reasons


def test_sdk_requests_and_gateway_attempts_are_not_double_counted():
    runtime = TurnRuntimeContext(trace_id="t", llm_budget=LLMCallBudget(max_calls=3, enforce=True))
    runtime.register_openai_call("decision", transport="responses")
    runtime.register_sdk_http_request()  # a mesma requisicao vista pelo hook
    assert runtime.llm_budget.transport_attempts == 1
    runtime.register_sdk_http_request()  # retry interno do SDK
    assert runtime.llm_budget.transport_attempts == 2


@pytest.mark.asyncio
async def test_sdk_internal_retries_are_charged_to_the_turn(monkeypatch):
    """O SDK repete 5xx sozinho; cada repeticao real entra no orcamento."""
    from types import SimpleNamespace

    import app.openai_client as clients
    from app.runtime_context import reset_current_turn, set_current_turn

    monkeypatch.setattr(
        clients,
        "get_settings",
        lambda: SimpleNamespace(openai_api_key="sk-test", openai_timeout_seconds=5.0, openai_max_retries=2),
    )
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500, json={"error": {"message": "boom"}})
        return httpx.Response(200, json={"object": "list", "data": []})

    real_factory = clients.DefaultAsyncHttpxClient
    monkeypatch.setattr(
        clients,
        "DefaultAsyncHttpxClient",
        lambda **kwargs: real_factory(transport=httpx.MockTransport(handler), **kwargs),
    )
    clients.reset_openai_clients()
    client = clients.get_async_openai_client()
    monkeypatch.setattr("openai._base_client.AsyncAPIClient._calculate_retry_timeout", lambda *a, **k: 0)

    runtime = TurnRuntimeContext(trace_id="t", llm_budget=LLMCallBudget(max_calls=3, enforce=True))
    token = set_current_turn(runtime)
    try:
        await client.models.list()
    finally:
        reset_current_turn(token)
        clients.reset_openai_clients()

    assert calls["n"] == 3
    assert runtime.sdk_http_requests == 3
    assert runtime.llm_budget.transport_attempts == 3


def test_budget_config_carries_the_transport_ceiling(monkeypatch):
    from types import SimpleNamespace

    import app.llm_call_policy as policy

    monkeypatch.setattr(
        policy,
        "get_settings",
        lambda: SimpleNamespace(
            agent_max_llm_calls_per_turn=2,
            agent_max_llm_calls_per_turn_complex=4,
            agent_llm_budget_enabled=True,
            agent_max_llm_transport_attempts_per_turn=5,
        ),
    )
    assert policy.build_llm_call_budget(execution_path="normal")["max_transport_attempts"] == 5
    assert policy.build_llm_call_budget(execution_path="fast")["max_transport_attempts"] == 5
