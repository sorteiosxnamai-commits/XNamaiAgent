from copy import deepcopy
from types import SimpleNamespace as NS

import pytest
from openai.types.responses import ResponseReasoningItem, ResponseFunctionToolCall

from app.openai_gateway import ResponsesGateway, _serialize_output_item


@pytest.mark.asyncio
@pytest.mark.parametrize("sdk_objects", [True, False])
async def test_stateless_tool_rounds_preserve_encrypted_reasoning(monkeypatch, sdk_objects):
    monkeypatch.setattr("app.openai_gateway.get_settings", lambda: NS(
        openai_store_responses=False, openai_responses_tool_loop_enabled=True,
        openai_reasoning_effort="low", openai_timeout_seconds=10,
    ))
    calls = []
    reasoning = {"id": "rs_test", "type": "reasoning", "summary": [],
                 "encrypted_content": "opaque-encrypted-state", "status": "completed"}
    function = {"id": "fc_test", "type": "function_call", "call_id": "call_test",
                "name": "search_knowledge", "arguments": '{"query":"cadastro"}', "status": "completed"}

    async def create(**kwargs):
        calls.append(deepcopy(kwargs))
        if len(calls) == 1:
            output = ([ResponseReasoningItem(**reasoning), ResponseFunctionToolCall(**function)]
                      if sdk_objects else [NS(**reasoning), NS(**function)])
            return NS(output=output, status="completed", usage=None)
        assert kwargs["input"][1]["encrypted_content"] == "opaque-encrypted-state"
        assert kwargs["input"][1]["summary"] == []
        assert "status" not in kwargs["input"][1]
        assert "status" not in kwargs["input"][2]
        assert kwargs["input"][2]["call_id"] == "call_test"
        assert kwargs["input"][3]["type"] == "function_call_output"
        assert kwargs["input"][3]["call_id"] == "call_test"
        return NS(output=[], output_text="O cadastro é feito no catálogo.", status="completed", usage=None)

    async def execute(name, arguments):
        assert name == "search_knowledge"
        return {"passages": ["cadastro no catálogo"]}

    result = await ResponsesGateway(client=NS(responses=NS(create=create))).run_tool_loop(
        model="gpt-5.4", tools=[{"type": "function", "name": "search_knowledge",
                                 "parameters": {"type": "object", "properties": {}}}],
        execute_tool=execute, messages=[{"role": "user", "content": "Como me cadastro?"}],
    )
    assert result.text == "O cadastro é feito no catálogo."
    assert len(calls) == 2
    assert all(call["include"] == ["reasoning.encrypted_content"] for call in calls)
    assert all(call["store"] is False and "previous_response_id" not in call for call in calls)


def test_reasoning_dict_replay_drops_lifecycle_fields_without_mutating_output():
    output = {"type": "reasoning", "id": "rs_a", "summary": [],
              "encrypted_content": "opaque", "status": None, "output_only_field": "discard"}
    assert _serialize_output_item(output) == {
        "type": "reasoning", "id": "rs_a", "summary": [], "encrypted_content": "opaque"}
    assert "status" in output
