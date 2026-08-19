"""Structured fake OpenAI gateway for offline deterministic evals.

Observations must come from the agent result — never from ``expected``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from app.openai_errors import (
    OpenAIEmptyOutputError,
    OpenAIIncompleteError,
    OpenAIRateLimitGatewayError,
    OpenAIRefusalError,
    OpenAITimeoutGatewayError,
)
from app.openai_gateway import (
    StructuredParseResult,
    TextGenerationResult,
    ToolLoopResult,
)


@dataclass
class FakeScript:
    """One canned response keyed by call_type (or '*')."""

    call_type: str = "*"
    kind: str = "text"  # text | structured | refusal | empty | timeout | rate_limit | incomplete | tool
    text: str = ""
    parsed: BaseModel | None = None
    tool_results: list[dict[str, Any]] = field(default_factory=list)


class FakeOpenAIGateway:
    def __init__(self, scripts: list[FakeScript] | None = None) -> None:
        self.scripts = list(scripts or [])
        self.calls: list[dict[str, Any]] = []

    def _next(self, call_type: str) -> FakeScript:
        for index, script in enumerate(self.scripts):
            if script.call_type in {call_type, "*"}:
                return self.scripts.pop(index)
        return FakeScript(call_type=call_type, kind="text", text="")

    def _raise_for(self, script: FakeScript) -> None:
        if script.kind == "timeout":
            raise OpenAITimeoutGatewayError("fake_timeout")
        if script.kind == "rate_limit":
            raise OpenAIRateLimitGatewayError("fake_rate_limit")
        if script.kind == "refusal":
            raise OpenAIRefusalError("fake_refusal")
        if script.kind == "empty":
            raise OpenAIEmptyOutputError("fake_empty")
        if script.kind == "incomplete":
            raise OpenAIIncompleteError("fake_incomplete")

    async def parse_structured(self, **kwargs: Any) -> StructuredParseResult:
        call_type = str(kwargs.get("call_type") or "structured")
        self.calls.append({"method": "parse_structured", "call_type": call_type})
        script = self._next(call_type)
        self._raise_for(script)
        if script.parsed is None:
            text_format = kwargs.get("text_format")
            if text_format is not None and issubclass(text_format, BaseModel):
                script.parsed = text_format.model_validate({})
            else:
                raise OpenAIEmptyOutputError("fake_structured_missing")
        return StructuredParseResult(
            parsed=script.parsed,
            raw_response=None,
            api_mode="fake",
            output_text=script.text or None,
            model=str(kwargs.get("model") or "fake"),
        )

    async def generate_text(self, **kwargs: Any) -> TextGenerationResult:
        call_type = str(kwargs.get("call_type") or "text")
        self.calls.append({"method": "generate_text", "call_type": call_type})
        script = self._next(call_type)
        self._raise_for(script)
        return TextGenerationResult(
            text=script.text,
            raw_response=None,
            api_mode="fake",
            model=str(kwargs.get("model") or "fake"),
        )

    async def run_tool_loop(self, **kwargs: Any) -> ToolLoopResult:
        call_type = str(kwargs.get("call_type") or "tool_loop")
        self.calls.append({"method": "run_tool_loop", "call_type": call_type})
        script = self._next(call_type)
        self._raise_for(script)
        return ToolLoopResult(
            text=script.text,
            raw_response=None,
            api_mode="fake",
            tool_results=list(script.tool_results),
            model=str(kwargs.get("model") or "fake"),
        )
