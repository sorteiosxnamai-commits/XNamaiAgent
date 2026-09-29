"""B7 — taxonomia de erros LLM: logs/metricas distinguem, o cliente nao ve nada."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import openai
import pytest

from app.openai_errors import (
    OpenAIGatewayError,
    OpenAIQuotaExhaustedError,
    OpenAIRateLimitGatewayError,
    classify_llm_error,
    gateway_error_for,
)

_REQUEST = httpx.Request("POST", "https://api.openai.com/v1/responses")


def _status_error(cls, status: int, code: str | None = None):
    body = {"error": {"message": "x", "code": code}} if code else {"error": {"message": "x"}}
    return cls("boom", response=httpx.Response(status, request=_REQUEST), body=body)


@pytest.mark.parametrize(
    ("exc", "category"),
    [
        (_status_error(openai.RateLimitError, 429), "rate_limited"),
        (_status_error(openai.RateLimitError, 429, "insufficient_quota"), "quota_exhausted"),
        (openai.APITimeoutError(request=_REQUEST), "timeout"),
        (asyncio.TimeoutError(), "timeout"),
        (openai.APIConnectionError(request=_REQUEST), "provider_unavailable"),
        (_status_error(openai.InternalServerError, 503), "provider_unavailable"),
        (_status_error(openai.BadRequestError, 400), "invalid_request"),
        (_status_error(openai.UnprocessableEntityError, 422), "invalid_request"),
        (_status_error(openai.AuthenticationError, 401), "auth_error"),
        (_status_error(openai.PermissionDeniedError, 403), "auth_error"),
        (KeyError("bug"), "internal_error"),
    ],
)
def test_every_failure_mode_has_a_distinct_category(exc, category):
    assert classify_llm_error(exc) == category
    mapped = gateway_error_for(exc)
    assert isinstance(mapped, OpenAIGatewayError)
    assert mapped.category == category


def test_quota_and_rate_limit_are_different_errors():
    quota = gateway_error_for(_status_error(openai.RateLimitError, 429, "insufficient_quota"))
    burst = gateway_error_for(_status_error(openai.RateLimitError, 429))
    assert isinstance(quota, OpenAIQuotaExhaustedError) and quota.code == "openai_quota_exhausted"
    assert isinstance(burst, OpenAIRateLimitGatewayError) and burst.code == "openai_rate_limit"


def test_gateway_uses_the_taxonomy():
    from app.openai_gateway import _map_api_error

    assert _map_api_error(openai.APITimeoutError(request=_REQUEST)).category == "timeout"


@pytest.mark.asyncio
async def test_runtime_records_the_category_for_metrics(monkeypatch):
    import app.openai_runtime as runtime

    seen = {}
    monkeypatch.setattr(runtime, "record_openai_observation", lambda **kwargs: seen.update(kwargs))

    async def quota():
        raise _status_error(openai.RateLimitError, 429, "insufficient_quota")

    with pytest.raises(openai.RateLimitError):
        await runtime.execute_openai_call(call_type="decision", operation=quota)
    assert seen["error_category"] == "quota_exhausted"
    assert seen["ok"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [OpenAIQuotaExhaustedError, OpenAIRateLimitGatewayError])
async def test_customer_never_sees_internal_error_details(monkeypatch, error):
    import app.sales_agent as sales_agent
    from app.models import IncomingMessage, SalesInterpretation

    monkeypatch.setattr(
        sales_agent,
        "get_settings",
        lambda: SimpleNamespace(openai_api_key="sk-test", openai_model="m", agent_db_persona_enabled=False),
    )

    async def failing(**_kwargs):
        raise error("insufficient_quota sk-proj-SECRET billing detail")

    monkeypatch.setattr("app.openai_gateway.generate_text_output", failing)
    interpretation = SalesInterpretation(
        domain="commerce", goal="discover", subject={"product_type": "capa"}, preferences={},
        references_previous_context=False, needs_clarification=True, confidence=0.9,
    )
    interpretation._source = "fallback"

    result = await sales_agent.generate_clarification_reply(
        message=IncomingMessage(text="quero uma capa"),
        interpretation=interpretation,
        discovery_state={"force_retrieval": False},
    )

    for leaked in ("quota", "sk-", "SECRET", "billing", "rate"):
        assert leaked not in result.reply_text
    assert result.response_metadata.get("response_source") == "deterministic_fallback"
