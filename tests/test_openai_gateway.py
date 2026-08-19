from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.openai_errors import (
    OpenAIEmptyOutputError,
    OpenAIRefusalError,
    OpenAISchemaError,
)
from app.openai_gateway import (
    CanaryOpenAIGateway,
    ChatCompletionsGateway,
    FallbackOpenAIGateway,
    ResponsesGateway,
    ShadowOpenAIGateway,
    build_openai_gateway,
    extract_function_calls,
    extract_output_text,
    messages_to_responses_parts,
    reset_openai_gateway,
)


class _SampleOut(BaseModel):
    label: str


class _FakeParsedMessage:
    def __init__(self, parsed=None, content=None, refusal=None):
        self.parsed = parsed
        self.content = content
        self.refusal = refusal


class _FakeChoice:
    def __init__(self, message):
        self.message = message


class _FakeChatResponse:
    def __init__(self, message):
        self.choices = [_FakeChoice(message)]
        self.usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1)


class _FakeResponses:
    def __init__(self):
        self.parse_kwargs = None
        self.create_kwargs = None

    async def parse(self, **kwargs):
        self.parse_kwargs = kwargs
        return SimpleNamespace(
            output_parsed=_SampleOut(label="ok"),
            output_text='{"label":"ok"}',
            status="completed",
            output=[],
            usage=SimpleNamespace(input_tokens=2, output_tokens=2),
        )

    async def create(self, **kwargs):
        self.create_kwargs = kwargs
        return SimpleNamespace(
            output_text="olá",
            status="completed",
            output=[
                SimpleNamespace(
                    type="message",
                    content=[SimpleNamespace(type="output_text", text="olá")],
                )
            ],
            usage=SimpleNamespace(input_tokens=2, output_tokens=2),
        )


class _FakeChatCompletions:
    def __init__(self):
        self.parse_kwargs = None
        self.create_kwargs = None

    async def parse(self, **kwargs):
        self.parse_kwargs = kwargs
        return _FakeChatResponse(
            _FakeParsedMessage(parsed=_SampleOut(label="chat"), content='{"label":"chat"}')
        )

    async def create(self, **kwargs):
        self.create_kwargs = kwargs
        return _FakeChatResponse(_FakeParsedMessage(content="resposta chat"))


class _FakeClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=_FakeChatCompletions())
        self.responses = _FakeResponses()


@pytest.fixture(autouse=True)
def _reset_gateway():
    reset_openai_gateway()
    yield
    reset_openai_gateway()


def test_build_gateway_chat_completions_when_explicitly_allowed(monkeypatch):
    monkeypatch.setattr(
        "app.openai_gateway.get_settings",
        lambda: SimpleNamespace(
            openai_api_mode="chat_completions",
            openai_chat_completions_primary_allowed=True,
        ),
    )
    gateway = build_openai_gateway()
    assert isinstance(gateway, ChatCompletionsGateway)


def test_build_gateway_responses_and_shadow(monkeypatch):
    monkeypatch.setattr(
        "app.openai_gateway.get_settings",
        lambda: SimpleNamespace(
            openai_api_mode="responses",
            openai_responses_fallback_to_chat=True,
            openai_chat_completions_primary_allowed=False,
        ),
    )
    assert isinstance(build_openai_gateway("responses"), FallbackOpenAIGateway)
    assert isinstance(build_openai_gateway("shadow"), ShadowOpenAIGateway)
    assert isinstance(build_openai_gateway("canary"), CanaryOpenAIGateway)


def test_build_responses_without_fallback(monkeypatch):
    monkeypatch.setattr(
        "app.openai_gateway.get_settings",
        lambda: SimpleNamespace(
            openai_api_mode="responses",
            openai_responses_fallback_to_chat=False,
            openai_chat_completions_primary_allowed=False,
        ),
    )
    assert isinstance(build_openai_gateway("responses"), ResponsesGateway)


def test_messages_to_responses_parts_splits_system():
    instructions, items = messages_to_responses_parts(
        [
            {"role": "system", "content": "persona"},
            {"role": "user", "content": "oi"},
        ]
    )
    assert instructions == "persona"
    assert items == [{"role": "user", "content": "oi"}]


def test_extract_output_text_from_multi_items():
    response = SimpleNamespace(
        output_text="",
        output=[
            SimpleNamespace(type="reasoning", content=[]),
            SimpleNamespace(
                type="message",
                content=[
                    SimpleNamespace(type="output_text", text="parte1"),
                    SimpleNamespace(type="output_text", text=" parte2"),
                ],
            ),
        ],
    )
    assert extract_output_text(response) == "parte1 parte2"


def test_extract_function_calls_preserves_call_id():
    call = SimpleNamespace(type="function_call", call_id="call_123", name="search")
    response = SimpleNamespace(output=[call, SimpleNamespace(type="message")])
    calls = extract_function_calls(response)
    assert len(calls) == 1
    assert calls[0].call_id == "call_123"


@pytest.mark.asyncio
async def test_chat_gateway_parse_structured():
    client = _FakeClient()
    gateway = ChatCompletionsGateway(client=client)
    result = await gateway.parse_structured(
        model="gpt-4.1-mini",
        text_format=_SampleOut,
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.0,
    )
    assert result.api_mode == "chat_completions"
    assert result.parsed.label == "chat"
    assert client.chat.completions.parse_kwargs["model"] == "gpt-4.1-mini"
    assert "store" not in client.chat.completions.parse_kwargs


@pytest.mark.asyncio
async def test_responses_gateway_parse_sends_store_false(monkeypatch):
    monkeypatch.setattr(
        "app.openai_gateway.get_settings",
        lambda: SimpleNamespace(
            openai_store_responses=False,
            openai_responses_structured_enabled=True,
            openai_use_previous_response_id=False,
            openai_reasoning_effort="",
            openai_text_verbosity="",
            openai_max_output_tokens=None,
            openai_timeout_seconds=45.0,
        ),
    )
    client = _FakeClient()
    gateway = ResponsesGateway(client=client)
    result = await gateway.parse_structured(
        model="gpt-4.1-mini",
        text_format=_SampleOut,
        messages=[
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "parse"},
        ],
    )
    assert result.api_mode == "responses"
    assert result.parsed.label == "ok"
    assert client.responses.parse_kwargs["store"] is False
    assert "previous_response_id" not in client.responses.parse_kwargs
    assert client.responses.parse_kwargs["instructions"] == "rules"
    assert client.responses.parse_kwargs["input"] == [
        {"role": "user", "content": "parse"}
    ]


@pytest.mark.asyncio
async def test_responses_gateway_generate_text_uses_output_text(monkeypatch):
    monkeypatch.setattr(
        "app.openai_gateway.get_settings",
        lambda: SimpleNamespace(
            openai_store_responses=False,
            openai_reasoning_effort="",
            openai_text_verbosity="",
            openai_max_output_tokens=None,
            openai_timeout_seconds=45.0,
        ),
    )
    client = _FakeClient()
    gateway = ResponsesGateway(client=client)
    result = await gateway.generate_text(
        model="gpt-4.1-mini",
        instructions="sys",
        input_items="olá cliente",
    )
    assert result.text == "olá"
    assert result.api_mode == "responses"
    assert client.responses.create_kwargs["store"] is False


@pytest.mark.asyncio
async def test_responses_refusal_raises(monkeypatch):
    monkeypatch.setattr(
        "app.openai_gateway.get_settings",
        lambda: SimpleNamespace(
            openai_store_responses=False,
            openai_responses_structured_enabled=True,
            openai_reasoning_effort="",
            openai_text_verbosity="",
            openai_max_output_tokens=None,
            openai_timeout_seconds=45.0,
        ),
    )

    class _Refusing:
        async def parse(self, **kwargs):
            return SimpleNamespace(
                output_parsed=None,
                output_text="",
                status="completed",
                refusal="não posso",
                output=[],
            )

    client = SimpleNamespace(responses=_Refusing())
    gateway = ResponsesGateway(client=client)
    with pytest.raises(OpenAIRefusalError):
        await gateway.parse_structured(
            model="gpt-4.1-mini",
            text_format=_SampleOut,
            input_items="x",
            instructions="y",
        )


@pytest.mark.asyncio
async def test_responses_missing_parsed_raises(monkeypatch):
    monkeypatch.setattr(
        "app.openai_gateway.get_settings",
        lambda: SimpleNamespace(
            openai_store_responses=False,
            openai_responses_structured_enabled=True,
            openai_reasoning_effort="",
            openai_text_verbosity="",
            openai_max_output_tokens=None,
            openai_timeout_seconds=45.0,
        ),
    )

    class _Empty:
        async def parse(self, **kwargs):
            return SimpleNamespace(
                output_parsed=None,
                output_text="",
                status="completed",
                refusal=None,
                output=[],
            )

    gateway = ResponsesGateway(client=SimpleNamespace(responses=_Empty()))
    with pytest.raises(OpenAISchemaError):
        await gateway.parse_structured(
            model="gpt-4.1-mini",
            text_format=_SampleOut,
            input_items="x",
            instructions="y",
        )


@pytest.mark.asyncio
async def test_chat_empty_text_raises():
    class _EmptyChat:
        async def create(self, **kwargs):
            return _FakeChatResponse(_FakeParsedMessage(content=""))

    gateway = ChatCompletionsGateway(
        client=SimpleNamespace(chat=SimpleNamespace(completions=_EmptyChat()))
    )
    with pytest.raises(OpenAIEmptyOutputError):
        await gateway.generate_text(
            model="gpt-4.1-mini",
            messages=[{"role": "user", "content": "x"}],
        )


@pytest.mark.asyncio
async def test_shadow_returns_primary_and_does_not_require_shadow_success(monkeypatch):
    monkeypatch.setattr(
        "app.openai_gateway.get_settings",
        lambda: SimpleNamespace(
            openai_shadow_sample_rate=1.0,
            openai_store_responses=False,
            openai_responses_structured_enabled=True,
        ),
    )

    class _BrokenResponses:
        async def parse(self, **kwargs):
            raise RuntimeError("shadow_boom")

    client = _FakeClient()
    client.responses = _BrokenResponses()
    gateway = ShadowOpenAIGateway(
        primary=ChatCompletionsGateway(client=client),
        shadow=ResponsesGateway(client=client),
    )
    result = await gateway.parse_structured(
        model="gpt-4.1-mini",
        text_format=_SampleOut,
        messages=[{"role": "user", "content": "hi"}],
    )
    assert result.parsed.label == "chat"
    assert result.api_mode == "shadow"


@pytest.mark.asyncio
async def test_tool_loop_empty_allowlist_raises():
    from app.openai_errors import OpenAIGatewayError

    gateway = ChatCompletionsGateway(client=_FakeClient())
    with pytest.raises(OpenAIGatewayError):
        await gateway.run_tool_loop(
            model="gpt-4.1-mini",
            tools=[],
            execute_tool=lambda *_a, **_k: None,
        )
