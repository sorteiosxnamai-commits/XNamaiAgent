from types import SimpleNamespace

import pytest

from app.openai_errors import OpenAIUnknownToolError
from app.openai_gateway import (
    ChatCompletionsGateway,
    ResponsesGateway,
    to_chat_tools,
    to_responses_tools,
)


CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": "search",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        },
    }
]


def test_tool_schema_conversion_preserves_name():
    chat = to_chat_tools(
        [{"type": "function", "name": "search_products", "parameters": {}}]
    )
    assert chat[0]["function"]["name"] == "search_products"
    responses = to_responses_tools(CHAT_TOOLS)
    assert responses[0]["name"] == "search_products"
    assert responses[0]["type"] == "function"


@pytest.mark.asyncio
async def test_chat_tool_loop_preserves_tool_call_id_and_returns_text():
    calls: list[tuple[str, dict]] = []

    class FakeCompletions:
        def __init__(self):
            self.round = 0

        async def create(self, **kwargs):
            self.round += 1
            if self.round == 1:
                tool_call = SimpleNamespace(
                    id="call_abc",
                    function=SimpleNamespace(
                        name="search_products",
                        arguments='{"query":"tissot"}',
                    ),
                    model_dump=lambda: {
                        "id": "call_abc",
                        "function": {
                            "name": "search_products",
                            "arguments": '{"query":"tissot"}',
                        },
                    },
                )
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content=None,
                                tool_calls=[tool_call],
                            )
                        )
                    ],
                    usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
                )
            # Ensure tool result was fed back with tool_call_id (not renamed).
            assert any(
                msg.get("role") == "tool" and msg.get("tool_call_id") == "call_abc"
                for msg in kwargs["messages"]
            )
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="Achei opções.", tool_calls=None)
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))

    async def execute(name, arguments):
        calls.append((name, arguments))
        return {"products": [{"id": "1"}]}

    gateway = ChatCompletionsGateway(client=client)
    result = await gateway.run_tool_loop(
        model="gpt-4.1-mini",
        tools=CHAT_TOOLS,
        execute_tool=execute,
        messages=[{"role": "user", "content": "tem tissot?"}],
        max_rounds=3,
    )
    assert result.text == "Achei opções."
    assert result.call_ids == ["call_abc"]
    assert calls == [("search_products", {"query": "tissot"})]
    assert result.limit_reached is False


@pytest.mark.asyncio
async def test_chat_tool_loop_blocks_unknown_tool():
    class FakeCompletions:
        async def create(self, **kwargs):
            tool_call = SimpleNamespace(
                id="call_x",
                function=SimpleNamespace(name="drop_database", arguments="{}"),
                model_dump=lambda: {
                    "id": "call_x",
                    "function": {"name": "drop_database", "arguments": "{}"},
                },
            )
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=None, tool_calls=[tool_call])
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            )

    gateway = ChatCompletionsGateway(
        client=SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    )

    async def execute(name, arguments):
        raise AssertionError("must not execute unknown tool")

    with pytest.raises(OpenAIUnknownToolError):
        await gateway.run_tool_loop(
            model="gpt-4.1-mini",
            tools=CHAT_TOOLS,
            execute_tool=execute,
            messages=[{"role": "user", "content": "hack"}],
        )


@pytest.mark.asyncio
async def test_responses_tool_loop_preserves_call_id(monkeypatch):
    monkeypatch.setattr(
        "app.openai_gateway.get_settings",
        lambda: SimpleNamespace(
            openai_store_responses=False,
            openai_responses_tool_loop_enabled=True,
        ),
    )
    create_kwargs: list[dict] = []

    class FakeResponses:
        def __init__(self):
            self.round = 0

        async def create(self, **kwargs):
            self.round += 1
            create_kwargs.append(kwargs)
            assert kwargs["store"] is False
            assert "previous_response_id" not in kwargs
            if self.round == 1:
                return SimpleNamespace(
                    status="completed",
                    output_text="",
                    output=[
                        SimpleNamespace(
                            type="function_call",
                            call_id="call_resp_1",
                            name="search_products",
                            arguments='{"query":"prx"}',
                        )
                    ],
                    usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                )
            # Second round must include function_call_output with same call_id.
            assert any(
                isinstance(item, dict)
                and item.get("type") == "function_call_output"
                and item.get("call_id") == "call_resp_1"
                for item in kwargs["input"]
            )
            return SimpleNamespace(
                status="completed",
                output_text="PRX encontrado.",
                output=[
                    SimpleNamespace(
                        type="message",
                        content=[
                            SimpleNamespace(type="output_text", text="PRX encontrado.")
                        ],
                    )
                ],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )

    async def execute(name, arguments):
        assert name == "search_products"
        return {"ok": True}

    gateway = ResponsesGateway(client=SimpleNamespace(responses=FakeResponses()))
    result = await gateway.run_tool_loop(
        model="gpt-4.1-mini",
        tools=CHAT_TOOLS,
        execute_tool=execute,
        instructions="ajuda comercial",
        input_items="tem prx?",
        max_rounds=3,
        parallel_tool_calls=False,
    )
    assert result.text == "PRX encontrado."
    assert result.call_ids == ["call_resp_1"]
    assert create_kwargs[0]["parallel_tool_calls"] is False
