import json
from types import SimpleNamespace
from typing import Literal

import httpx
import pytest
from openai import BadRequestError
from openai.lib._pydantic import to_strict_json_schema
from pydantic import BaseModel

from app.commerce_context import CommerceConversationState
from app.models import IncomingMessage, SalesInterpretation
from openai_test_utils import install_fake_openai_client


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        openai_api_key="test-key",
        openai_model="gpt-4.1-mini",
        openai_main_model="gpt-4.1-mini",
        openai_fast_model="gpt-4.1-nano",
        # Legacy interpreter path for contract tests against SalesInterpretation.
        agent_turn_understanding_enabled=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _interpretation() -> SalesInterpretation:
    return SalesInterpretation(
        domain="commerce",
        goal="discover",
        subject={"product_type": "relógio"},
        preferences={"style": "esportivo"},
        references_previous_context=True,
        needs_clarification=False,
        clarification_question=None,
        confidence=0.96,
    )


def _schema_paths_with_keyword(value, keyword: str, path: str = "$") -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        if keyword in value:
            paths.append(path)
        for key, child in value.items():
            paths.extend(_schema_paths_with_keyword(child, keyword, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.extend(_schema_paths_with_keyword(child, keyword, f"{path}[{index}]"))
    return paths


def test_full_structured_schema_is_strict_and_has_no_default_keywords():
    class MinimalInterpretation(BaseModel):
        domain: Literal["commerce", "raffle", "store_general", "greeting", "out_of_scope"]

    class InvalidDefaultInterpretation(BaseModel):
        domain: Literal["commerce", "raffle"]
        references_previous_context: bool = False

    minimal_schema = to_strict_json_schema(MinimalInterpretation)
    invalid_schema = to_strict_json_schema(InvalidDefaultInterpretation)
    full_schema = to_strict_json_schema(SalesInterpretation)

    assert _schema_paths_with_keyword(minimal_schema, "default") == []
    assert _schema_paths_with_keyword(invalid_schema, "default") == [
        "$.properties.references_previous_context"
    ]
    assert _schema_paths_with_keyword(full_schema, "default") == []
    assert full_schema["additionalProperties"] is False
    assert set(full_schema["required"]) == set(full_schema["properties"])
    for definition in full_schema["$defs"].values():
        assert definition["additionalProperties"] is False
        assert set(definition["required"]) == set(definition["properties"])


@pytest.mark.asyncio
async def test_interpreter_request_uses_gpt_4_1_mini_and_normalized_messages(monkeypatch):
    import app.sales_agent as sales_agent

    captured = {}

    class FakeCompletions:
        async def parse(self, **kwargs):
            captured.update(kwargs)
            message = SimpleNamespace(parsed=_interpretation(), refusal=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(sales_agent, "get_settings", _settings)
    install_fake_openai_client(monkeypatch, FakeClient)
    monkeypatch.setattr(
        sales_agent,
        "_fallback_interpretation",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("success must not call fallback")),
    )

    result = await sales_agent.interpret_message(
        IncomingMessage(text="  esportivo  "),
        recent_turns=[
            {"role": "user", "content": " quero comprar um relógio ", "metadata": {"ignored": True}},
            {"role": "assistant", "content": "   "},
            {"role": "tool", "content": "invalid role"},
            {"role": "assistant", "content": None},
            SimpleNamespace(role="assistant", content="not a dict"),
        ],
    )

    assert result._source == "openai"
    assert captured["model"] == "gpt-4.1-mini"
    assert captured["response_format"] is SalesInterpretation
    assert captured["temperature"] == 0
    assert "max_tokens" not in captured
    assert "max_completion_tokens" not in captured
    assert "tools" not in captured
    assert "tool_choice" not in captured
    assert "parallel_tool_calls" not in captured
    from app.capability_catalog import format_capability_catalog_for_prompt

    empty_state = CommerceConversationState()
    assert captured["messages"] == [
        {"role": "system", "content": sales_agent.SALES_INTERPRETER_INSTRUCTIONS},
        {
            "role": "system",
            "content": (
                "COMMERCE_STATE:\n"
                + json.dumps(empty_state.interpreter_payload(), ensure_ascii=False)
                + "\n\nWORKING_MEMORY:\n"
                + json.dumps(
                    sales_agent.build_working_memory(empty_state),
                    ensure_ascii=False,
                )
                + "\n"
                + sales_agent.WORKING_MEMORY_USAGE_POLICY
                + "\n\n"
                + format_capability_catalog_for_prompt()
            ),
        },
        {"role": "user", "content": "quero comprar um relógio"},
        {"role": "user", "content": "esportivo"},
    ]


@pytest.mark.asyncio
async def test_bad_request_logs_safe_details_and_observable_fallback(monkeypatch, capsys):
    import app.sales_agent as sales_agent

    body = {
        "error": {
            "message": (
                "Invalid schema for response_format 'SalesInterpretation': "
                "In context=('properties', 'references_previous_context'), "
                "'default' is not permitted. Authorization: Bearer sk-proj-supersecret"
            ),
            "type": "invalid_request_error",
            "param": "response_format",
            "code": "invalid_json_schema",
        }
    }
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(400, request=request)

    class FakeCompletions:
        async def parse(self, **kwargs):
            raise BadRequestError(body["error"]["message"], response=response, body=body)

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(sales_agent, "get_settings", _settings)
    install_fake_openai_client(monkeypatch, FakeClient)

    result = await sales_agent.interpret_message(IncomingMessage(text="esportivo"))
    logs = capsys.readouterr().out

    assert result._source == "deterministic_fallback"
    assert "[sales.interpreter.error]" in logs
    assert "BadRequestError" in logs
    assert "400" in logs
    assert "invalid_json_schema" in logs
    assert "response_format" in logs
    assert "default' is not permitted" in logs
    assert "sk-proj-supersecret" not in logs
    assert "Authorization: Bearer ***" in logs
    assert "fallback_reason" in logs
    assert "openai_bad_request" in logs
