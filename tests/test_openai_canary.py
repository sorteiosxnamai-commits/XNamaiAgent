from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.openai_errors import OpenAIGatewayError
from app.openai_gateway import (
    CanaryOpenAIGateway,
    ChatCompletionsGateway,
    ResponsesGateway,
    build_openai_gateway,
    reset_openai_gateway,
)
from app.openai_routing import bucket_for_key, select_api_route
from app.runtime_context import reset_current_turn, set_current_turn
from app.turn_runtime import TurnRuntimeContext


class _Out(BaseModel):
    n: int


@pytest.fixture(autouse=True)
def _reset_gateway():
    reset_openai_gateway()
    yield
    reset_openai_gateway()


def test_build_gateway_canary(monkeypatch):
    monkeypatch.setattr(
        "app.openai_gateway.get_settings",
        lambda: SimpleNamespace(openai_api_mode="canary"),
    )
    assert isinstance(build_openai_gateway("canary"), CanaryOpenAIGateway)


def test_sticky_routing_is_deterministic():
    key = "whatsapp:5511999999999"
    a = bucket_for_key(key)
    b = bucket_for_key(key)
    assert a == b
    assert 0 <= a < 10_000


def test_select_api_route_percent_bounds(monkeypatch):
    monkeypatch.setattr(
        "app.openai_routing.get_settings",
        lambda: SimpleNamespace(
            openai_responses_traffic_percent=0.0,
            openai_canary_sticky_routing=True,
        ),
    )
    assert select_api_route(routing_key="k1") == "chat_completions"

    monkeypatch.setattr(
        "app.openai_routing.get_settings",
        lambda: SimpleNamespace(
            openai_responses_traffic_percent=1.0,
            openai_canary_sticky_routing=True,
        ),
    )
    assert select_api_route(routing_key="k1") == "responses"


def test_same_conversation_stays_on_same_route(monkeypatch):
    monkeypatch.setattr(
        "app.openai_routing.get_settings",
        lambda: SimpleNamespace(
            openai_responses_traffic_percent=0.5,
            openai_canary_sticky_routing=True,
        ),
    )
    key = "conversation-sticky-42"
    first = select_api_route(routing_key=key)
    second = select_api_route(routing_key=key)
    assert first == second


@pytest.mark.asyncio
async def test_canary_uses_responses_when_selected(monkeypatch):
    writes: list[str] = []

    monkeypatch.setattr(
        "app.openai_gateway.get_settings",
        lambda: SimpleNamespace(
            openai_responses_traffic_percent=1.0,
            openai_responses_fallback_to_chat=True,
            openai_canary_sticky_routing=True,
            openai_store_responses=False,
            openai_responses_structured_enabled=True,
        ),
    )
    monkeypatch.setattr(
        "app.openai_routing.get_settings",
        lambda: SimpleNamespace(
            openai_responses_traffic_percent=1.0,
            openai_canary_sticky_routing=True,
        ),
    )

    class _Chat:
        async def parse(self, **kwargs):
            writes.append("chat")
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            parsed=_Out(n=1),
                            content='{"n":1}',
                            refusal=None,
                        )
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            )

    class _Resp:
        async def parse(self, **kwargs):
            writes.append("responses")
            return SimpleNamespace(
                output_parsed=_Out(n=2),
                output_text='{"n":2}',
                status="completed",
                refusal=None,
                output=[],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=_Chat()),
        responses=_Resp(),
    )
    gateway = CanaryOpenAIGateway(
        chat=ChatCompletionsGateway(client=client),
        responses=ResponsesGateway(client=client),
    )
    result = await gateway.parse_structured(
        model="gpt-4.1-mini",
        text_format=_Out,
        messages=[{"role": "user", "content": "x"}],
    )
    assert writes == ["responses"]
    assert result.parsed.n == 2
    assert result.api_mode == "canary_responses"


@pytest.mark.asyncio
async def test_canary_falls_back_to_chat_on_responses_error(monkeypatch):
    writes: list[str] = []
    runtime = TurnRuntimeContext(trace_id="t1", conversation_key="c1")
    token = set_current_turn(runtime)
    try:
        monkeypatch.setattr(
            "app.openai_gateway.get_settings",
            lambda: SimpleNamespace(
                openai_responses_traffic_percent=1.0,
                openai_responses_fallback_to_chat=True,
                openai_canary_sticky_routing=True,
                openai_store_responses=False,
                openai_responses_structured_enabled=True,
            ),
        )
        monkeypatch.setattr(
            "app.openai_routing.get_settings",
            lambda: SimpleNamespace(
                openai_responses_traffic_percent=1.0,
                openai_canary_sticky_routing=True,
            ),
        )

        class _Chat:
            async def parse(self, **kwargs):
                writes.append("chat")
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                parsed=_Out(n=9),
                                content='{"n":9}',
                                refusal=None,
                            )
                        )
                    ],
                    usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
                )

        class _Resp:
            async def parse(self, **kwargs):
                writes.append("responses")
                raise OpenAIGatewayError("boom", code="responses_down")

        client = SimpleNamespace(
            chat=SimpleNamespace(completions=_Chat()),
            responses=_Resp(),
        )
        gateway = CanaryOpenAIGateway(
            chat=ChatCompletionsGateway(client=client),
            responses=ResponsesGateway(client=client),
        )
        result = await gateway.parse_structured(
            model="gpt-4.1-mini",
            text_format=_Out,
            messages=[{"role": "user", "content": "x"}],
        )
        assert writes == ["responses", "chat"]
        assert result.parsed.n == 9
        assert result.api_mode == "canary_fallback_chat"
        assert runtime.openai_api_fallback is True
    finally:
        reset_current_turn(token)


@pytest.mark.asyncio
async def test_canary_chat_bucket_never_calls_responses(monkeypatch):
    writes: list[str] = []
    monkeypatch.setattr(
        "app.openai_gateway.get_settings",
        lambda: SimpleNamespace(
            openai_responses_traffic_percent=0.0,
            openai_responses_fallback_to_chat=True,
            openai_canary_sticky_routing=True,
            openai_store_responses=False,
            openai_responses_structured_enabled=True,
        ),
    )
    monkeypatch.setattr(
        "app.openai_routing.get_settings",
        lambda: SimpleNamespace(
            openai_responses_traffic_percent=0.0,
            openai_canary_sticky_routing=True,
        ),
    )

    class _Chat:
        async def parse(self, **kwargs):
            writes.append("chat")
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            parsed=_Out(n=3),
                            content='{"n":3}',
                            refusal=None,
                        )
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            )

    class _Resp:
        async def parse(self, **kwargs):
            writes.append("responses")
            raise AssertionError("responses should not be called")

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=_Chat()),
        responses=_Resp(),
    )
    gateway = CanaryOpenAIGateway(
        chat=ChatCompletionsGateway(client=client),
        responses=ResponsesGateway(client=client),
    )
    result = await gateway.parse_structured(
        model="gpt-4.1-mini",
        text_format=_Out,
        messages=[{"role": "user", "content": "x"}],
    )
    assert writes == ["chat"]
    assert result.api_mode == "canary_chat"


@pytest.mark.asyncio
async def test_canary_tool_loop_never_falls_back_to_chat(monkeypatch):
    writes: list[str] = []
    monkeypatch.setattr(
        "app.openai_routing.get_settings",
        lambda: SimpleNamespace(
            openai_responses_traffic_percent=1.0,
            openai_canary_sticky_routing=True,
        ),
    )
    monkeypatch.setattr(
        "app.openai_gateway.get_settings",
        lambda: SimpleNamespace(
            openai_responses_fallback_to_chat=True,
            openai_responses_tool_loop_enabled=True,
            openai_store_responses=False,
        ),
    )

    class _ChatLoop:
        async def run_tool_loop(self, **kwargs):
            writes.append("chat")
            raise AssertionError("chat tool loop must not run as fallback")

    class _RespLoop:
        async def run_tool_loop(self, **kwargs):
            writes.append("responses")
            raise OpenAIGatewayError("tool_loop_down", code="responses_down")

    gateway = CanaryOpenAIGateway(
        chat=_ChatLoop(),  # type: ignore[arg-type]
        responses=_RespLoop(),  # type: ignore[arg-type]
    )
    context = TurnRuntimeContext(trace_id="canary-tool-loop")
    token = set_current_turn(context)
    try:
        with pytest.raises(OpenAIGatewayError, match="tool_loop_down"):
            await gateway.run_tool_loop(
                model="gpt-4.1-mini",
                tools=[],
                execute_tool=lambda *_a, **_k: {},
                messages=[{"role": "user", "content": "x"}],
            )
    finally:
        reset_current_turn(token)

    assert writes == ["responses"]
    assert context.openai_api_fallback is False
