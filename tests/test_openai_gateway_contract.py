"""Contract tests for the OpenAI gateway (Responses primary path).

These tests pin SDK 2.7.2 behaviour with fakes — run before any openai bump.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openai import APITimeoutError, RateLimitError
from pydantic import BaseModel

from app.openai_errors import (
    OpenAIEmptyOutputError,
    OpenAIGatewayError,
    OpenAIIncompleteError,
    OpenAIInvalidToolArgumentsError,
    OpenAIRateLimitGatewayError,
    OpenAIRefusalError,
    OpenAISchemaError,
    OpenAITimeoutGatewayError,
    OpenAIUnknownToolError,
)
from app.openai_gateway import (
    ChatCompletionsGateway,
    FallbackOpenAIGateway,
    ResponsesGateway,
    build_openai_gateway,
    extract_usage_metrics,
    model_capabilities,
    reset_openai_gateway,
)
from app.openai_models import resolve_openai_model


class _Label(BaseModel):
    label: str


def _settings(**overrides):
    base = dict(
        openai_api_mode="responses",
        openai_store_responses=False,
        openai_use_previous_response_id=False,
        openai_responses_structured_enabled=True,
        openai_responses_tool_loop_enabled=True,
        openai_responses_fallback_to_chat=True,
        openai_chat_completions_primary_allowed=False,
        openai_responses_traffic_percent=1.0,
        openai_reasoning_effort="medium",
        openai_text_verbosity="medium",
        openai_max_output_tokens=None,
        openai_timeout_seconds=30.0,
        openai_max_retries=2,
        openai_model="gpt-4.1-mini",
        openai_main_model="gpt-4.1-mini",
        openai_fast_model="gpt-4.1-nano",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _reset_gateway():
    reset_openai_gateway()
    yield
    reset_openai_gateway()


@pytest.fixture
def responses_settings(monkeypatch):
    monkeypatch.setattr(
        "app.openai_gateway.get_settings",
        lambda: _settings(),
    )
    monkeypatch.setattr(
        "app.openai_models.get_settings",
        lambda: _settings(),
    )


class _FakeResponses:
    def __init__(self, *, create_fn=None, parse_fn=None):
        self.create_kwargs_list: list[dict] = []
        self.parse_kwargs_list: list[dict] = []
        self._create_fn = create_fn
        self._parse_fn = parse_fn

    async def parse(self, **kwargs):
        self.parse_kwargs_list.append(kwargs)
        if self._parse_fn:
            return await self._parse_fn(**kwargs)
        return SimpleNamespace(
            output_parsed=_Label(label="ok"),
            output_text='{"label":"ok"}',
            status="completed",
            output=[],
            usage=SimpleNamespace(
                input_tokens=10,
                output_tokens=4,
                input_tokens_details=SimpleNamespace(cached_tokens=3),
                output_tokens_details=SimpleNamespace(reasoning_tokens=2),
            ),
        )

    async def create(self, **kwargs):
        self.create_kwargs_list.append(kwargs)
        if self._create_fn:
            return await self._create_fn(**kwargs)
        return SimpleNamespace(
            output_text="olá",
            status="completed",
            output=[
                SimpleNamespace(
                    type="message",
                    content=[SimpleNamespace(type="output_text", text="olá")],
                )
            ],
            usage=SimpleNamespace(
                input_tokens=5,
                output_tokens=2,
                input_tokens_details=SimpleNamespace(cached_tokens=1),
                output_tokens_details=SimpleNamespace(reasoning_tokens=0),
            ),
        )


class _FakeChat:
    def __init__(self):
        self.parse_kwargs = None
        self.create_kwargs = None

    async def parse(self, **kwargs):
        self.parse_kwargs = kwargs
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        parsed=_Label(label="chat"),
                        content='{"label":"chat"}',
                        refusal=None,
                    )
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    async def create(self, **kwargs):
        self.create_kwargs = kwargs
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="chat text", refusal=None)
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )


def test_sdk_pin_is_2_7_2():
    import openai

    assert openai.__version__ == "2.7.2"


def test_build_gateway_defaults_to_responses(monkeypatch):
    monkeypatch.setattr(
        "app.openai_gateway.get_settings",
        lambda: _settings(openai_api_mode="responses"),
    )
    gateway = build_openai_gateway()
    assert isinstance(gateway, FallbackOpenAIGateway)


def test_resolve_openai_model_main_and_fast(responses_settings):
    assert resolve_openai_model("main") == "gpt-4.1-mini"
    assert resolve_openai_model("fast") == "gpt-4.1-nano"


def test_model_capabilities_reasoning_models():
    caps = model_capabilities("o3-mini")
    assert caps.supports_reasoning_effort is True
    caps_std = model_capabilities("gpt-4.1-mini")
    assert caps_std.supports_reasoning_effort is False
    assert caps_std.supports_text_verbosity is True


def test_extract_usage_metrics_includes_cache_and_reasoning():
    response = SimpleNamespace(
        status="completed",
        usage=SimpleNamespace(
            input_tokens=20,
            output_tokens=8,
            input_tokens_details=SimpleNamespace(cached_tokens=7),
            output_tokens_details=SimpleNamespace(reasoning_tokens=5),
        ),
    )
    metrics = extract_usage_metrics(response, model="gpt-5", latency_ms=12.5)
    assert metrics.input_tokens == 20
    assert metrics.output_tokens == 8
    assert metrics.cached_tokens == 7
    assert metrics.reasoning_tokens == 5
    assert metrics.model == "gpt-5"
    assert metrics.status == "completed"
    assert metrics.call_id
    assert metrics.latency_ms == 12.5


@pytest.mark.asyncio
async def test_responses_structured_valid_and_metrics(responses_settings):
    client = SimpleNamespace(responses=_FakeResponses())
    gateway = ResponsesGateway(client=client)
    result = await gateway.parse_structured(
        model="gpt-4.1-mini",
        text_format=_Label,
        instructions="sys",
        input_items="hi",
    )
    assert result.parsed.label == "ok"
    assert result.metrics is not None
    assert result.metrics.input_tokens == 10
    assert result.metrics.cached_tokens == 3
    assert result.metrics.reasoning_tokens == 2
    assert result.metrics.status == "completed"
    kwargs = client.responses.parse_kwargs_list[0]
    assert kwargs["store"] is False
    assert "previous_response_id" not in kwargs
    assert kwargs["instructions"] == "sys"
    assert kwargs["text"]["verbosity"] == "medium"
    assert "reasoning" not in kwargs  # gpt-4.1-mini is not a reasoning model


@pytest.mark.asyncio
async def test_responses_attaches_reasoning_for_o_models(monkeypatch):
    monkeypatch.setattr(
        "app.openai_gateway.get_settings",
        lambda: _settings(openai_reasoning_effort="high"),
    )
    client = SimpleNamespace(responses=_FakeResponses())
    gateway = ResponsesGateway(client=client)
    await gateway.generate_text(
        model="o3-mini",
        instructions="sys",
        input_items="q",
    )
    kwargs = client.responses.create_kwargs_list[0]
    assert kwargs["reasoning"] == {"effort": "high"}
    assert kwargs.get("text", {}).get("verbosity") == "medium"


@pytest.mark.asyncio
async def test_responses_max_output_tokens(monkeypatch):
    monkeypatch.setattr(
        "app.openai_gateway.get_settings",
        lambda: _settings(openai_max_output_tokens=512),
    )
    client = SimpleNamespace(responses=_FakeResponses())
    gateway = ResponsesGateway(client=client)
    await gateway.generate_text(
        model="gpt-4.1-mini",
        input_items="x",
    )
    assert client.responses.create_kwargs_list[0]["max_output_tokens"] == 512


@pytest.mark.asyncio
async def test_structured_invalid_missing_parsed(responses_settings):
    async def _parse(**_kwargs):
        return SimpleNamespace(
            output_parsed=None,
            output_text="",
            status="completed",
            refusal=None,
            output=[],
            usage=None,
        )

    gateway = ResponsesGateway(
        client=SimpleNamespace(responses=_FakeResponses(parse_fn=_parse))
    )
    with pytest.raises(OpenAISchemaError):
        await gateway.parse_structured(
            model="gpt-4.1-mini",
            text_format=_Label,
            input_items="x",
        )


@pytest.mark.asyncio
async def test_refusal_raises(responses_settings):
    async def _parse(**_kwargs):
        return SimpleNamespace(
            output_parsed=None,
            output_text="",
            status="completed",
            refusal="não posso",
            output=[],
            usage=None,
        )

    gateway = ResponsesGateway(
        client=SimpleNamespace(responses=_FakeResponses(parse_fn=_parse))
    )
    with pytest.raises(OpenAIRefusalError):
        await gateway.parse_structured(
            model="gpt-4.1-mini",
            text_format=_Label,
            input_items="x",
        )


@pytest.mark.asyncio
async def test_empty_output_raises(responses_settings):
    async def _create(**_kwargs):
        return SimpleNamespace(
            output_text="",
            status="completed",
            output=[],
            usage=None,
        )

    gateway = ResponsesGateway(
        client=SimpleNamespace(responses=_FakeResponses(create_fn=_create))
    )
    with pytest.raises(OpenAIEmptyOutputError):
        await gateway.generate_text(model="gpt-4.1-mini", input_items="x")


@pytest.mark.asyncio
async def test_incomplete_status_raises(responses_settings):
    async def _create(**_kwargs):
        return SimpleNamespace(
            output_text="parcial",
            status="incomplete",
            output=[],
            usage=None,
        )

    gateway = ResponsesGateway(
        client=SimpleNamespace(responses=_FakeResponses(create_fn=_create))
    )
    with pytest.raises(OpenAIIncompleteError):
        await gateway.generate_text(model="gpt-4.1-mini", input_items="x")


@pytest.mark.asyncio
async def test_timeout_normalized(responses_settings, monkeypatch):
    async def _create(**_kwargs):
        raise APITimeoutError(request=None)

    monkeypatch.setattr(
        "app.openai_gateway.execute_openai_call",
        lambda **kwargs: (_ for _ in ()).throw(APITimeoutError(request=None)),
    )
    # Bypass execute wrapper — call mapping path via real gateway with exploding client
    class _Boom:
        async def create(self, **kwargs):
            raise APITimeoutError(request=None)

    # Use direct exception path: monkeypatch execute to raise
    async def _exec(**kwargs):
        raise APITimeoutError(request=None)

    monkeypatch.setattr("app.openai_gateway.execute_openai_call", _exec)
    gateway = ResponsesGateway(client=SimpleNamespace(responses=_Boom()))
    with pytest.raises(OpenAITimeoutGatewayError):
        await gateway.generate_text(model="gpt-4.1-mini", input_items="x")


@pytest.mark.asyncio
async def test_rate_limit_normalized(monkeypatch, responses_settings):
    from httpx import Request, Response

    async def _exec(**kwargs):
        request = Request("POST", "https://api.openai.com/v1/responses")
        response = Response(429, request=request)
        raise RateLimitError(message="rate", response=response, body=None)

    monkeypatch.setattr("app.openai_gateway.execute_openai_call", _exec)
    gateway = ResponsesGateway(client=SimpleNamespace(responses=SimpleNamespace()))
    with pytest.raises(OpenAIRateLimitGatewayError):
        await gateway.generate_text(model="gpt-4.1-mini", input_items="x")


@pytest.mark.asyncio
async def test_tool_loop_preserves_call_ids_and_outputs(responses_settings):
    round_state = {"n": 0}

    async def _create(**kwargs):
        round_state["n"] += 1
        if round_state["n"] == 1:
            # Assert prior round inputs are list
            assert isinstance(kwargs["input"], list)
            return SimpleNamespace(
                status="completed",
                output_text="",
                output=[
                    SimpleNamespace(
                        type="function_call",
                        call_id="call_a",
                        name="search_products",
                        arguments='{"q":"seiko"}',
                    ),
                    SimpleNamespace(
                        type="function_call",
                        call_id="call_b",
                        name="get_stock",
                        arguments='{"id":"1"}',
                    ),
                ],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )
        # Second round: must include function_call items + outputs with matching ids
        items = kwargs["input"]
        types = [
            (i.get("type") if isinstance(i, dict) else getattr(i, "type", None))
            for i in items
        ]
        assert "function_call" in types
        assert "function_call_output" in types
        outputs = [
            i
            for i in items
            if (i.get("type") if isinstance(i, dict) else None) == "function_call_output"
        ]
        assert {o["call_id"] for o in outputs} == {"call_a", "call_b"}
        return SimpleNamespace(
            status="completed",
            output_text="pronto",
            output=[
                SimpleNamespace(
                    type="message",
                    content=[SimpleNamespace(type="output_text", text="pronto")],
                )
            ],
            usage=SimpleNamespace(input_tokens=2, output_tokens=2),
        )

    tools = [
        {
            "type": "function",
            "name": "search_products",
            "description": "search",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "type": "function",
            "name": "get_stock",
            "description": "stock",
            "parameters": {"type": "object", "properties": {}},
        },
    ]
    seen: list[str] = []

    async def execute_tool(name, args):
        seen.append(name)
        return {"ok": True, "name": name, "args": args}

    gateway = ResponsesGateway(
        client=SimpleNamespace(responses=_FakeResponses(create_fn=_create))
    )
    result = await gateway.run_tool_loop(
        model="gpt-4.1-mini",
        tools=tools,
        execute_tool=execute_tool,
        instructions="sys",
        input_items="quero seiko",
        max_rounds=3,
    )
    assert result.text == "pronto"
    assert result.call_ids == ["call_a", "call_b"]
    assert seen == ["search_products", "get_stock"]
    assert result.metrics is not None


@pytest.mark.asyncio
async def test_tool_loop_rejects_unknown_tool_and_missing_call_id(responses_settings):
    async def _create_unknown(**_kwargs):
        return SimpleNamespace(
            status="completed",
            output_text="",
            output=[
                SimpleNamespace(
                    type="function_call",
                    call_id="call_x",
                    name="evil_tool",
                    arguments="{}",
                )
            ],
            usage=None,
        )

    gateway = ResponsesGateway(
        client=SimpleNamespace(
            responses=_FakeResponses(create_fn=_create_unknown)
        )
    )
    with pytest.raises(OpenAIUnknownToolError):
        await gateway.run_tool_loop(
            model="gpt-4.1-mini",
            tools=[
                {
                    "type": "function",
                    "name": "search_products",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
            execute_tool=lambda *_a, **_k: None,
            input_items="x",
        )

    async def _create_no_id(**_kwargs):
        return SimpleNamespace(
            status="completed",
            output_text="",
            output=[
                SimpleNamespace(
                    type="function_call",
                    call_id="",
                    name="search_products",
                    arguments="{}",
                )
            ],
            usage=None,
        )

    gateway2 = ResponsesGateway(
        client=SimpleNamespace(responses=_FakeResponses(create_fn=_create_no_id))
    )
    with pytest.raises(OpenAIInvalidToolArgumentsError):
        await gateway2.run_tool_loop(
            model="gpt-4.1-mini",
            tools=[
                {
                    "type": "function",
                    "name": "search_products",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
            execute_tool=lambda *_a, **_k: None,
            input_items="x",
        )


@pytest.mark.asyncio
async def test_fallback_on_responses_failure(responses_settings):
    async def _boom(**_kwargs):
        raise OpenAIGatewayError("down", code="down")

    client = SimpleNamespace(
        responses=_FakeResponses(parse_fn=_boom),
        chat=SimpleNamespace(completions=_FakeChat()),
    )
    gateway = FallbackOpenAIGateway(
        primary=ResponsesGateway(client=client),
        fallback=ChatCompletionsGateway(client=client),
    )
    result = await gateway.parse_structured(
        model="gpt-4.1-mini",
        text_format=_Label,
        messages=[{"role": "user", "content": "x"}],
    )
    assert result.api_mode == "responses_fallback_chat"
    assert result.parsed.label == "chat"


@pytest.mark.asyncio
async def test_never_sends_previous_response_id_even_if_flag_true(monkeypatch):
    monkeypatch.setattr(
        "app.openai_gateway.get_settings",
        lambda: _settings(openai_use_previous_response_id=True),
    )
    client = SimpleNamespace(responses=_FakeResponses())
    gateway = ResponsesGateway(client=client)
    await gateway.generate_text(model="gpt-4.1-mini", input_items="x")
    assert "previous_response_id" not in client.responses.create_kwargs_list[0]


@pytest.mark.asyncio
async def test_default_timeout_from_settings(monkeypatch):
    captured = {}

    async def _exec(**kwargs):
        captured["timeout"] = kwargs.get("timeout_seconds")
        return SimpleNamespace(
            output_text="ok",
            status="completed",
            output=[
                SimpleNamespace(
                    type="message",
                    content=[SimpleNamespace(type="output_text", text="ok")],
                )
            ],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )

    monkeypatch.setattr(
        "app.openai_gateway.get_settings",
        lambda: _settings(openai_timeout_seconds=17.0),
    )
    monkeypatch.setattr("app.openai_gateway.execute_openai_call", _exec)
    gateway = ResponsesGateway(client=SimpleNamespace(responses=_FakeResponses()))
    await gateway.generate_text(model="gpt-4.1-mini", input_items="x")
    assert captured["timeout"] == 17.0
