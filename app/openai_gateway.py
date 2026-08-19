"""Central OpenAI gateway (Chat Completions + Responses + shadow).

Business modules must use this layer instead of calling
``client.chat.completions`` / ``client.responses`` directly.
Audio transcription/TTS and embeddings stay on dedicated helpers and are
not migrated to Responses in this phase.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from openai import (
    APIError,
    APITimeoutError,
    AsyncOpenAI,
    BadRequestError,
    RateLimitError,
)
from pydantic import BaseModel

from .config import get_settings
from .openai_client import get_async_openai_client
from .openai_errors import (
    OpenAIEmptyOutputError,
    OpenAIGatewayError,
    OpenAIIncompleteError,
    OpenAIInvalidToolArgumentsError,
    OpenAIRateLimitGatewayError,
    OpenAIRefusalError,
    OpenAISchemaError,
    OpenAITimeoutGatewayError,
    OpenAIToolLoopDisabledError,
    OpenAIUnknownToolError,
)
from .openai_runtime import execute_openai_call

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class ModelCapabilities:
    supports_responses: bool = True
    supports_structured_outputs: bool = True
    supports_reasoning_effort: bool = False
    supports_text_verbosity: bool = False
    supports_parallel_tool_calls: bool = True


@dataclass
class GatewayCallMetrics:
    """Normalized per-call telemetry (no PII / no full prompts)."""

    call_id: str
    model: str | None = None
    status: str | None = None
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    error_code: str | None = None
    error_message: str | None = None


@dataclass
class StructuredParseResult:
    parsed: BaseModel
    raw_response: Any
    api_mode: str
    refusal: str | None = None
    output_text: str | None = None
    model: str | None = None
    latency_ms: float = 0.0
    metrics: GatewayCallMetrics | None = None


@dataclass
class TextGenerationResult:
    text: str
    raw_response: Any
    api_mode: str
    refusal: str | None = None
    model: str | None = None
    latency_ms: float = 0.0
    metrics: GatewayCallMetrics | None = None


@dataclass
class ToolLoopResult:
    text: str | None
    raw_response: Any
    api_mode: str
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    call_ids: list[str] = field(default_factory=list)
    model: str | None = None
    latency_ms: float = 0.0
    limit_reached: bool = False
    metrics: GatewayCallMetrics | None = None


class OpenAIGateway(Protocol):
    async def parse_structured(
        self,
        *,
        model: str,
        text_format: type[T],
        messages: list[dict[str, Any]] | None = None,
        instructions: str | None = None,
        input_items: list[dict[str, Any]] | str | None = None,
        temperature: float | None = None,
        call_type: str = "structured",
        timeout_seconds: float | None = None,
        store: bool | None = None,
    ) -> StructuredParseResult: ...

    async def generate_text(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]] | None = None,
        instructions: str | None = None,
        input_items: list[dict[str, Any]] | str | None = None,
        temperature: float | None = None,
        call_type: str = "text",
        timeout_seconds: float | None = None,
        store: bool | None = None,
    ) -> TextGenerationResult: ...

    async def run_tool_loop(
        self,
        *,
        model: str,
        tools: list[dict[str, Any]],
        execute_tool: Any,
        messages: list[dict[str, Any]] | None = None,
        instructions: str | None = None,
        input_items: list[dict[str, Any]] | str | None = None,
        parallel_tool_calls: bool = True,
        call_type: str = "tool_loop",
        timeout_seconds: float | None = None,
        store: bool | None = None,
        max_rounds: int = 4,
        temperature: float | None = 0.3,
    ) -> ToolLoopResult: ...


def model_capabilities(model: str | None = None) -> ModelCapabilities:
    """Capability map used to attach Responses-only controls safely.

    Prefer explicit overrides from settings when present; otherwise use a
    conservative capability table (not only fragile prefixes).
    """
    settings = get_settings()
    name = (model or "").strip().casefold()
    overrides_raw = getattr(settings, "openai_model_capability_overrides", None)
    overrides: dict[str, Any] = {}
    if isinstance(overrides_raw, dict):
        overrides = {
            str(k).casefold(): v
            for k, v in overrides_raw.items()
            if isinstance(v, dict)
        }
    elif isinstance(overrides_raw, str) and overrides_raw.strip():
        try:
            import json

            parsed = json.loads(overrides_raw)
            if isinstance(parsed, dict):
                overrides = {
                    str(k).casefold(): v
                    for k, v in parsed.items()
                    if isinstance(v, dict)
                }
        except Exception:
            overrides = {}

    if name in overrides:
        row = overrides[name]
        return ModelCapabilities(
            supports_responses=bool(row.get("supports_responses", True)),
            supports_structured_outputs=bool(
                row.get("supports_structured_outputs", True)
            ),
            supports_reasoning_effort=bool(row.get("supports_reasoning_effort", False)),
            supports_text_verbosity=bool(row.get("supports_text_verbosity", False)),
            supports_parallel_tool_calls=bool(
                row.get("supports_parallel_tool_calls", True)
            ),
        )

    # Known families (conservative).
    # Reasoning effort: o* and gpt-5 only (not gpt-4.1 / gpt-4o).
    reasoning_families = ("o1", "o3", "o4", "gpt-5")
    verbosity_families = reasoning_families + ("gpt-4.1", "gpt-4o", "chatgpt-4o")
    reasoning = any(name.startswith(prefix) for prefix in reasoning_families)
    verbosity = any(name.startswith(prefix) for prefix in verbosity_families)
    return ModelCapabilities(
        supports_responses=True,
        supports_structured_outputs=True,
        supports_reasoning_effort=reasoning,
        supports_text_verbosity=verbosity,
        supports_parallel_tool_calls=True,
    )


def apply_responses_controls_report(
    kwargs: dict[str, Any],
    *,
    model: str,
) -> dict[str, Any]:
    """Apply Responses controls and return applied/skipped metrics (no prompts)."""
    settings = get_settings()
    caps = model_capabilities(model)
    report: dict[str, Any] = {
        "model": model,
        "configured_reasoning_effort": None,
        "reasoning_effort_applied": False,
        "reasoning_effort_skip_reason": None,
        "configured_text_verbosity": None,
        "text_verbosity_applied": False,
        "text_verbosity_skip_reason": None,
        "max_output_tokens_applied": False,
    }
    effort = str(getattr(settings, "openai_reasoning_effort", "") or "").strip()
    report["configured_reasoning_effort"] = effort or None
    if effort:
        if caps.supports_reasoning_effort:
            kwargs["reasoning"] = {"effort": effort}
            report["reasoning_effort_applied"] = True
        else:
            report["reasoning_effort_skip_reason"] = "model_capability_not_declared"
            print(
                "[openai.responses.control.skipped]",
                {
                    "param": "reasoning.effort",
                    "reason": "model_capability_not_declared",
                    "model": model,
                },
            )
    verbosity = str(getattr(settings, "openai_text_verbosity", "") or "").strip()
    report["configured_text_verbosity"] = verbosity or None
    if verbosity:
        if caps.supports_text_verbosity:
            text_cfg = dict(kwargs.get("text") or {})
            text_cfg["verbosity"] = verbosity
            kwargs["text"] = text_cfg
            report["text_verbosity_applied"] = True
        else:
            report["text_verbosity_skip_reason"] = "model_capability_not_declared"
            print(
                "[openai.responses.control.skipped]",
                {
                    "param": "text.verbosity",
                    "reason": "model_capability_not_declared",
                    "model": model,
                },
            )
    max_tokens = getattr(settings, "openai_max_output_tokens", None)
    if max_tokens is not None:
        try:
            value = int(max_tokens)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            kwargs["max_output_tokens"] = value
            report["max_output_tokens_applied"] = True
    kwargs.pop("previous_response_id", None)
    return report


def _apply_responses_controls(kwargs: dict[str, Any], *, model: str) -> None:
    """Attach reasoning / verbosity / max_output_tokens when configured + supported.

    Never attaches ``previous_response_id`` — conversation state lives in app DB.
    """
    apply_responses_controls_report(kwargs, model=model)


def extract_usage_metrics(
    response: Any,
    *,
    model: str | None = None,
    latency_ms: float = 0.0,
    call_id: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> GatewayCallMetrics:
    usage = getattr(response, "usage", None) if response is not None else None
    input_tokens = 0
    output_tokens = 0
    cached_tokens = 0
    reasoning_tokens = 0
    if usage is not None:
        input_tokens = int(
            getattr(usage, "input_tokens", None)
            or getattr(usage, "prompt_tokens", None)
            or 0
        )
        output_tokens = int(
            getattr(usage, "output_tokens", None)
            or getattr(usage, "completion_tokens", None)
            or 0
        )
        details_in = getattr(usage, "input_tokens_details", None)
        if details_in is not None:
            cached_tokens = int(getattr(details_in, "cached_tokens", 0) or 0)
            if isinstance(details_in, dict):
                cached_tokens = int(details_in.get("cached_tokens") or 0)
        details_out = getattr(usage, "output_tokens_details", None)
        if details_out is not None:
            reasoning_tokens = int(getattr(details_out, "reasoning_tokens", 0) or 0)
            if isinstance(details_out, dict):
                reasoning_tokens = int(details_out.get("reasoning_tokens") or 0)
    status = None
    if response is not None:
        status = getattr(response, "status", None)
        if status is None and getattr(response, "choices", None) is not None:
            status = "completed"
    return GatewayCallMetrics(
        call_id=call_id or uuid.uuid4().hex,
        model=model,
        status=str(status) if status is not None else None,
        latency_ms=float(latency_ms or 0.0),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        reasoning_tokens=reasoning_tokens,
        error_code=error_code,
        error_message=(error_message[:240] if error_message else None),
    )


def _resolve_timeout(timeout_seconds: float | None) -> float | None:
    if timeout_seconds is not None and timeout_seconds > 0:
        return float(timeout_seconds)
    settings = get_settings()
    configured = getattr(settings, "openai_timeout_seconds", None)
    if configured is None:
        return None
    try:
        value = float(configured)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def messages_to_responses_parts(
    messages: list[dict[str, Any]] | None,
) -> tuple[str | None, list[dict[str, Any]] | str]:
    """Split Chat-style messages into Responses ``instructions`` + ``input``."""
    if not messages:
        return None, []
    instruction_parts: list[str] = []
    input_items: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "").strip().lower()
        content = message.get("content")
        if role == "system":
            if isinstance(content, str) and content.strip():
                instruction_parts.append(content.strip())
            elif content is not None:
                instruction_parts.append(json.dumps(content, ensure_ascii=False))
            continue
        if role in {"user", "assistant", "developer"}:
            mapped_role = "user" if role == "developer" else role
            input_items.append({"role": mapped_role, "content": content})
    instructions = "\n\n".join(instruction_parts) if instruction_parts else None
    return instructions, input_items


def extract_output_text(response: Any) -> str:
    """Read Responses ``output_text`` or concatenate text segments."""
    direct = getattr(response, "output_text", None)
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    chunks: list[str] = []
    for item in getattr(response, "output", None) or []:
        item_type = getattr(item, "type", None) or (
            item.get("type") if isinstance(item, dict) else None
        )
        if item_type != "message":
            continue
        content = getattr(item, "content", None)
        if content is None and isinstance(item, dict):
            content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            part_type = getattr(part, "type", None) or (
                part.get("type") if isinstance(part, dict) else None
            )
            if part_type in {"output_text", "text"}:
                text = getattr(part, "text", None)
                if text is None and isinstance(part, dict):
                    text = part.get("text")
                if isinstance(text, str) and text:
                    chunks.append(text)
    return "".join(chunks).strip()


def extract_function_calls(response: Any) -> list[Any]:
    calls: list[Any] = []
    for item in getattr(response, "output", None) or []:
        item_type = getattr(item, "type", None) or (
            item.get("type") if isinstance(item, dict) else None
        )
        if item_type == "function_call":
            calls.append(item)
    return calls


def to_chat_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize tools to Chat Completions ``tools`` shape."""
    normalized: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            normalized.append(tool)
            continue
        if tool.get("type") == "function" and tool.get("name"):
            normalized.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description") or "",
                        "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
                    },
                }
            )
    return normalized


def to_responses_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize tools to Responses API function-tool shape (no arbitrary endpoints)."""
    normalized: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            fn = tool["function"]
            normalized.append(
                {
                    "type": "function",
                    "name": fn["name"],
                    "description": fn.get("description") or "",
                    "parameters": fn.get("parameters")
                    or {"type": "object", "properties": {}},
                }
            )
            continue
        if tool.get("type") == "function" and tool.get("name"):
            normalized.append(
                {
                    "type": "function",
                    "name": tool["name"],
                    "description": tool.get("description") or "",
                    "parameters": tool.get("parameters")
                    or {"type": "object", "properties": {}},
                }
            )
    return normalized


def _tool_allowlist(tools: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function")
        if isinstance(fn, dict) and fn.get("name"):
            names.add(str(fn["name"]))
        elif tool.get("name"):
            names.add(str(tool["name"]))
    return names


def _parse_tool_arguments(raw: Any) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise OpenAIInvalidToolArgumentsError("invalid_tool_arguments_json") from exc
    if not isinstance(parsed, dict):
        raise OpenAIInvalidToolArgumentsError("tool_arguments_must_be_object")
    return parsed


def _serialize_output_item(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="json")
    data: dict[str, Any] = {}
    for key in (
        "type",
        "id",
        "call_id",
        "name",
        "arguments",
        "status",
        "role",
        "content",
    ):
        value = getattr(item, key, None)
        if value is not None:
            data[key] = value
    return data


def _store_flag(explicit: bool | None) -> bool:
    settings = get_settings()
    if explicit is not None:
        return bool(explicit)
    return bool(getattr(settings, "openai_store_responses", False))


def _map_api_error(exc: Exception) -> OpenAIGatewayError:
    if isinstance(exc, OpenAIGatewayError):
        return exc
    if isinstance(exc, (APITimeoutError, asyncio.TimeoutError)):
        return OpenAITimeoutGatewayError(str(exc)[:240])
    if isinstance(exc, RateLimitError):
        return OpenAIRateLimitGatewayError(str(exc)[:240])
    if isinstance(exc, APIError):
        return OpenAIGatewayError(str(exc)[:240], code="openai_api_error")
    return OpenAIGatewayError(str(exc)[:240])


class ChatCompletionsGateway:
    """Chat Completions path retained for rollback / shadow / Responses fallback.

    Phase 8+: prefer ``OPENAI_API_MODE=canary|responses``. Disable Chat as
    primary with ``OPENAI_CHAT_COMPLETIONS_PRIMARY_ALLOWED=false`` after canary
    metrics are green. Do not call ``client.chat.completions`` from business modules.
    """

    def __init__(self, client: AsyncOpenAI | None = None) -> None:
        self._client = client

    @property
    def client(self) -> AsyncOpenAI:
        return self._client or get_async_openai_client()

    async def parse_structured(
        self,
        *,
        model: str,
        text_format: type[T],
        messages: list[dict[str, Any]] | None = None,
        instructions: str | None = None,
        input_items: list[dict[str, Any]] | str | None = None,
        temperature: float | None = None,
        call_type: str = "structured",
        timeout_seconds: float | None = None,
        store: bool | None = None,
    ) -> StructuredParseResult:
        del store  # Chat Completions has no store flag.
        chat_messages = _coerce_chat_messages(
            messages=messages,
            instructions=instructions,
            input_items=input_items,
        )
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": chat_messages,
            "response_format": text_format,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        started = time.perf_counter()
        try:
            response = await execute_openai_call(
                call_type=call_type,
                model=model,
                messages=chat_messages,
                timeout_seconds=_resolve_timeout(timeout_seconds),
                operation=lambda: self.client.chat.completions.parse(**kwargs),
            )
        except BadRequestError:
            raise
        except Exception as exc:
            raise _map_api_error(exc) from exc
        message = response.choices[0].message if response.choices else None
        refusal = getattr(message, "refusal", None) if message is not None else None
        if isinstance(refusal, str) and refusal.strip():
            raise OpenAIRefusalError(refusal.strip())
        parsed = getattr(message, "parsed", None) if message is not None else None
        if parsed is None:
            raise OpenAISchemaError("structured_output_missing")
        content = getattr(message, "content", None) if message is not None else None
        return StructuredParseResult(
            parsed=parsed,
            raw_response=response,
            api_mode="chat_completions",
            refusal=None,
            output_text=str(content).strip() if content else None,
            model=model,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    async def generate_text(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]] | None = None,
        instructions: str | None = None,
        input_items: list[dict[str, Any]] | str | None = None,
        temperature: float | None = None,
        call_type: str = "text",
        timeout_seconds: float | None = None,
        store: bool | None = None,
    ) -> TextGenerationResult:
        del store
        chat_messages = _coerce_chat_messages(
            messages=messages,
            instructions=instructions,
            input_items=input_items,
        )
        kwargs: dict[str, Any] = {"model": model, "messages": chat_messages}
        if temperature is not None:
            kwargs["temperature"] = temperature
        started = time.perf_counter()
        try:
            response = await execute_openai_call(
                call_type=call_type,
                model=model,
                messages=chat_messages,
                timeout_seconds=_resolve_timeout(timeout_seconds),
                operation=lambda: self.client.chat.completions.create(**kwargs),
            )
        except BadRequestError:
            raise
        except Exception as exc:
            raise _map_api_error(exc) from exc
        message = response.choices[0].message if response.choices else None
        refusal = getattr(message, "refusal", None) if message is not None else None
        if isinstance(refusal, str) and refusal.strip():
            raise OpenAIRefusalError(refusal.strip())
        content = getattr(message, "content", None) if message is not None else None
        text = str(content or "").strip()
        if not text:
            raise OpenAIEmptyOutputError("empty_chat_completion_content")
        return TextGenerationResult(
            text=text,
            raw_response=response,
            api_mode="chat_completions",
            model=model,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    async def run_tool_loop(
        self,
        *,
        model: str,
        tools: list[dict[str, Any]],
        execute_tool: Any,
        messages: list[dict[str, Any]] | None = None,
        instructions: str | None = None,
        input_items: list[dict[str, Any]] | str | None = None,
        parallel_tool_calls: bool = True,
        call_type: str = "tool_loop",
        timeout_seconds: float | None = None,
        store: bool | None = None,
        max_rounds: int = 4,
        temperature: float | None = 0.3,
    ) -> ToolLoopResult:
        del store
        chat_tools = to_chat_tools(tools)
        allowlist = _tool_allowlist(chat_tools)
        if not allowlist:
            raise OpenAIGatewayError("tool_allowlist_empty", code="openai_tool_allowlist_empty")
        current_messages = _coerce_chat_messages(
            messages=messages,
            instructions=instructions,
            input_items=input_items,
        )
        tool_results: list[dict[str, Any]] = []
        call_ids: list[str] = []
        last_response: Any = None
        started = time.perf_counter()
        rounds = max(1, int(max_rounds))
        resolved_timeout = _resolve_timeout(timeout_seconds)
        for round_index in range(rounds):
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": current_messages,
                "tools": chat_tools,
                "tool_choice": "auto",
            }
            if temperature is not None:
                kwargs["temperature"] = temperature
            if not parallel_tool_calls:
                kwargs["parallel_tool_calls"] = False
            try:
                response = await execute_openai_call(
                    call_type=(
                        "decision" if round_index == 0 else call_type
                    ),
                    model=model,
                    messages=current_messages,
                    timeout_seconds=resolved_timeout,
                    operation=lambda: self.client.chat.completions.create(**kwargs),
                )
            except BadRequestError:
                raise
            except Exception as exc:
                raise _map_api_error(exc) from exc
            last_response = response
            message = response.choices[0].message if response.choices else None
            tool_calls = getattr(message, "tool_calls", None) if message else None
            if not tool_calls:
                text = str(getattr(message, "content", None) or "").strip() or None
                return ToolLoopResult(
                    text=text,
                    raw_response=response,
                    api_mode="chat_completions",
                    tool_results=tool_results,
                    call_ids=call_ids,
                    model=model,
                    latency_ms=round((time.perf_counter() - started) * 1000, 2),
                )
            current_messages.append(
                {
                    "role": "assistant",
                    "content": getattr(message, "content", None),
                    "tool_calls": [
                        call.model_dump()
                        if hasattr(call, "model_dump")
                        else call
                        for call in tool_calls
                    ],
                }
            )
            for call in tool_calls:
                function = getattr(call, "function", None)
                name = str(getattr(function, "name", None) or "")
                call_id = str(getattr(call, "id", None) or "")
                if name not in allowlist:
                    raise OpenAIUnknownToolError(f"unknown_tool:{name}")
                arguments = _parse_tool_arguments(
                    getattr(function, "arguments", None)
                )
                result = await execute_tool(name, arguments)
                tool_results.append(
                    {"name": name, "call_id": call_id, "result": result}
                )
                if call_id:
                    call_ids.append(call_id)
                current_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": name,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
        return ToolLoopResult(
            text=None,
            raw_response=last_response,
            api_mode="chat_completions",
            tool_results=tool_results,
            call_ids=call_ids,
            model=model,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            limit_reached=True,
        )


class ResponsesGateway:
    """Responses API path (store=False by default; no previous_response_id)."""

    def __init__(self, client: AsyncOpenAI | None = None) -> None:
        self._client = client

    @property
    def client(self) -> AsyncOpenAI:
        return self._client or get_async_openai_client()

    async def parse_structured(
        self,
        *,
        model: str,
        text_format: type[T],
        messages: list[dict[str, Any]] | None = None,
        instructions: str | None = None,
        input_items: list[dict[str, Any]] | str | None = None,
        temperature: float | None = None,
        call_type: str = "structured",
        timeout_seconds: float | None = None,
        store: bool | None = None,
    ) -> StructuredParseResult:
        settings = get_settings()
        if not bool(getattr(settings, "openai_responses_structured_enabled", True)):
            raise OpenAIGatewayError(
                "responses_structured_disabled",
                code="openai_responses_structured_disabled",
            )
        resolved_instructions, resolved_input = _resolve_responses_payload(
            messages=messages,
            instructions=instructions,
            input_items=input_items,
        )
        kwargs: dict[str, Any] = {
            "model": model,
            "text_format": text_format,
            "store": _store_flag(store),
        }
        if resolved_instructions:
            kwargs["instructions"] = resolved_instructions
        if resolved_input is not None:
            kwargs["input"] = resolved_input
        if temperature is not None:
            kwargs["temperature"] = temperature
        _apply_responses_controls(kwargs, model=model)
        started = time.perf_counter()
        call_id = uuid.uuid4().hex
        try:
            response = await execute_openai_call(
                call_type=call_type,
                model=model,
                messages=messages,
                timeout_seconds=_resolve_timeout(timeout_seconds),
                operation=lambda: self.client.responses.parse(**kwargs),
            )
        except Exception as exc:
            raise _map_api_error(exc) from exc
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        metrics = extract_usage_metrics(
            response, model=model, latency_ms=latency_ms, call_id=call_id
        )
        _raise_for_incomplete(response)
        refusal = _extract_refusal(response)
        if refusal:
            raise OpenAIRefusalError(refusal)
        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            raise OpenAISchemaError("responses_output_parsed_missing")
        return StructuredParseResult(
            parsed=parsed,
            raw_response=response,
            api_mode="responses",
            output_text=extract_output_text(response) or None,
            model=model,
            latency_ms=latency_ms,
            metrics=metrics,
        )

    async def generate_text(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]] | None = None,
        instructions: str | None = None,
        input_items: list[dict[str, Any]] | str | None = None,
        temperature: float | None = None,
        call_type: str = "text",
        timeout_seconds: float | None = None,
        store: bool | None = None,
    ) -> TextGenerationResult:
        resolved_instructions, resolved_input = _resolve_responses_payload(
            messages=messages,
            instructions=instructions,
            input_items=input_items,
        )
        kwargs: dict[str, Any] = {
            "model": model,
            "store": _store_flag(store),
        }
        if resolved_instructions:
            kwargs["instructions"] = resolved_instructions
        if resolved_input is not None:
            kwargs["input"] = resolved_input
        if temperature is not None:
            kwargs["temperature"] = temperature
        _apply_responses_controls(kwargs, model=model)
        started = time.perf_counter()
        call_id = uuid.uuid4().hex
        try:
            response = await execute_openai_call(
                call_type=call_type,
                model=model,
                messages=messages,
                timeout_seconds=_resolve_timeout(timeout_seconds),
                operation=lambda: self.client.responses.create(**kwargs),
            )
        except Exception as exc:
            raise _map_api_error(exc) from exc
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        metrics = extract_usage_metrics(
            response, model=model, latency_ms=latency_ms, call_id=call_id
        )
        _raise_for_incomplete(response)
        refusal = _extract_refusal(response)
        if refusal:
            raise OpenAIRefusalError(refusal)
        text = extract_output_text(response)
        if not text:
            raise OpenAIEmptyOutputError("responses_output_text_empty")
        return TextGenerationResult(
            text=text,
            raw_response=response,
            api_mode="responses",
            model=model,
            latency_ms=latency_ms,
            metrics=metrics,
        )

    async def run_tool_loop(
        self,
        *,
        model: str,
        tools: list[dict[str, Any]],
        execute_tool: Any,
        messages: list[dict[str, Any]] | None = None,
        instructions: str | None = None,
        input_items: list[dict[str, Any]] | str | None = None,
        parallel_tool_calls: bool = True,
        call_type: str = "tool_loop",
        timeout_seconds: float | None = None,
        store: bool | None = None,
        max_rounds: int = 4,
        temperature: float | None = 0.3,
    ) -> ToolLoopResult:
        settings = get_settings()
        if not bool(getattr(settings, "openai_responses_tool_loop_enabled", True)):
            raise OpenAIToolLoopDisabledError("responses_tool_loop_disabled")
        resp_tools = to_responses_tools(tools)
        allowlist = _tool_allowlist(resp_tools)
        if not allowlist:
            raise OpenAIGatewayError("tool_allowlist_empty", code="openai_tool_allowlist_empty")
        resolved_instructions, resolved_input = _resolve_responses_payload(
            messages=messages,
            instructions=instructions,
            input_items=input_items,
        )
        if isinstance(resolved_input, str):
            working_input: list[dict[str, Any]] = [
                {"role": "user", "content": resolved_input}
            ]
        else:
            working_input = list(resolved_input or [])
        tool_results: list[dict[str, Any]] = []
        call_ids: list[str] = []
        last_response: Any = None
        started = time.perf_counter()
        loop_call_id = uuid.uuid4().hex
        total_input = 0
        total_output = 0
        total_cached = 0
        total_reasoning = 0
        rounds = max(1, int(max_rounds))
        resolved_timeout = _resolve_timeout(timeout_seconds)
        for round_index in range(rounds):
            kwargs: dict[str, Any] = {
                "model": model,
                "input": working_input,
                "tools": resp_tools,
                "store": _store_flag(store),
                "parallel_tool_calls": bool(parallel_tool_calls),
            }
            if resolved_instructions:
                kwargs["instructions"] = resolved_instructions
            if temperature is not None:
                kwargs["temperature"] = temperature
            _apply_responses_controls(kwargs, model=model)
            try:
                response = await execute_openai_call(
                    call_type=(
                        "decision" if round_index == 0 else call_type
                    ),
                    model=model,
                    messages=messages,
                    timeout_seconds=resolved_timeout,
                    operation=lambda: self.client.responses.create(**kwargs),
                )
            except BadRequestError:
                raise
            except Exception as exc:
                raise _map_api_error(exc) from exc
            last_response = response
            round_metrics = extract_usage_metrics(response, model=model)
            total_input += round_metrics.input_tokens
            total_output += round_metrics.output_tokens
            total_cached += round_metrics.cached_tokens
            total_reasoning += round_metrics.reasoning_tokens
            _raise_for_incomplete(response)
            calls = extract_function_calls(response)
            if not calls:
                text = extract_output_text(response) or None
                latency_ms = round((time.perf_counter() - started) * 1000, 2)
                return ToolLoopResult(
                    text=text,
                    raw_response=response,
                    api_mode="responses",
                    tool_results=tool_results,
                    call_ids=call_ids,
                    model=model,
                    latency_ms=latency_ms,
                    metrics=GatewayCallMetrics(
                        call_id=loop_call_id,
                        model=model,
                        status=getattr(response, "status", None),
                        latency_ms=latency_ms,
                        input_tokens=total_input,
                        output_tokens=total_output,
                        cached_tokens=total_cached,
                        reasoning_tokens=total_reasoning,
                    ),
                )
            # Preserve intermediate model output items (including function_call).
            for item in getattr(response, "output", None) or []:
                working_input.append(_serialize_output_item(item))
            for call in calls:
                name = str(
                    getattr(call, "name", None)
                    or (call.get("name") if isinstance(call, dict) else "")
                    or ""
                )
                tool_call_id = str(
                    getattr(call, "call_id", None)
                    or (call.get("call_id") if isinstance(call, dict) else "")
                    or ""
                )
                if name not in allowlist:
                    raise OpenAIUnknownToolError(f"unknown_tool:{name}")
                if not tool_call_id:
                    raise OpenAIInvalidToolArgumentsError("missing_call_id")
                arguments = _parse_tool_arguments(
                    getattr(call, "arguments", None)
                    if not isinstance(call, dict)
                    else call.get("arguments")
                )
                result = await execute_tool(name, arguments)
                tool_results.append(
                    {"name": name, "call_id": tool_call_id, "result": result}
                )
                call_ids.append(tool_call_id)
                working_input.append(
                    {
                        "type": "function_call_output",
                        "call_id": tool_call_id,
                        "output": json.dumps(result, ensure_ascii=False),
                    }
                )
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        return ToolLoopResult(
            text=None,
            raw_response=last_response,
            api_mode="responses",
            tool_results=tool_results,
            call_ids=call_ids,
            model=model,
            latency_ms=latency_ms,
            limit_reached=True,
            metrics=GatewayCallMetrics(
                call_id=loop_call_id,
                model=model,
                status=getattr(last_response, "status", None),
                latency_ms=latency_ms,
                input_tokens=total_input,
                output_tokens=total_output,
                cached_tokens=total_cached,
                reasoning_tokens=total_reasoning,
            ),
        )


class FallbackOpenAIGateway:
    """Responses primary with Chat Completions emergency fallback.

    Used for ``OPENAI_API_MODE=responses`` when fallback is enabled, and when
    Chat Completions primary is disabled (Phase 8 rollback-only posture).
    Tool loops never fall back (mutation safety).
    """

    def __init__(
        self,
        primary: ResponsesGateway | None = None,
        fallback: ChatCompletionsGateway | None = None,
    ) -> None:
        self._primary = primary or ResponsesGateway()
        self._fallback = fallback or ChatCompletionsGateway()

    def _mark_fallback(self, reason: str, *, call_type: str | None = None) -> None:
        from .runtime_context import get_current_turn

        runtime = get_current_turn()
        if runtime is not None:
            runtime.openai_api_fallback = True
            runtime.openai_api_route = "chat_completions"
            runtime.register_fallback("openai_responses_fallback_chat")
            runtime.register_fallback(reason)
            # Failed Responses attempt already reserved budget — refund so Chat
            # fallback counts as the same logical LLM operation (Etapa 6).
            if call_type:
                runtime.release_failed_openai_attempt(call_type)

    def _fallback_enabled(self) -> bool:
        return bool(getattr(get_settings(), "openai_responses_fallback_to_chat", True))

    async def parse_structured(
        self,
        *,
        model: str,
        text_format: type[T],
        messages: list[dict[str, Any]] | None = None,
        instructions: str | None = None,
        input_items: list[dict[str, Any]] | str | None = None,
        temperature: float | None = None,
        call_type: str = "structured",
        timeout_seconds: float | None = None,
        store: bool | None = None,
    ) -> StructuredParseResult:
        try:
            result = await self._primary.parse_structured(
                model=model,
                text_format=text_format,
                messages=messages,
                instructions=instructions,
                input_items=input_items,
                temperature=temperature,
                call_type=call_type,
                timeout_seconds=timeout_seconds,
                store=store,
            )
            result.api_mode = "responses"
            return result
        except Exception as exc:
            if not self._fallback_enabled():
                raise
            reason = f"fallback_structured:{type(exc).__name__}"
            print("[openai.responses.fallback.structured]", {
                "error_type": type(exc).__name__,
                "error": str(exc)[:200],
                "reason": reason,
            })
            self._mark_fallback(reason, call_type=call_type)
            result = await self._fallback.parse_structured(
                model=model,
                text_format=text_format,
                messages=messages,
                instructions=instructions,
                input_items=input_items,
                temperature=temperature,
                call_type=f"{call_type}_fallback_chat",
                timeout_seconds=timeout_seconds,
                store=store,
            )
            result.api_mode = "responses_fallback_chat"
            return result

    async def generate_text(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]] | None = None,
        instructions: str | None = None,
        input_items: list[dict[str, Any]] | str | None = None,
        temperature: float | None = None,
        call_type: str = "text",
        timeout_seconds: float | None = None,
        store: bool | None = None,
    ) -> TextGenerationResult:
        try:
            result = await self._primary.generate_text(
                model=model,
                messages=messages,
                instructions=instructions,
                input_items=input_items,
                temperature=temperature,
                call_type=call_type,
                timeout_seconds=timeout_seconds,
                store=store,
            )
            result.api_mode = "responses"
            return result
        except Exception as exc:
            if not self._fallback_enabled():
                raise
            reason = f"fallback_text:{type(exc).__name__}"
            print("[openai.responses.fallback.text]", {
                "error_type": type(exc).__name__,
                "error": str(exc)[:200],
                "reason": reason,
            })
            self._mark_fallback(reason, call_type=call_type)
            result = await self._fallback.generate_text(
                model=model,
                messages=messages,
                instructions=instructions,
                input_items=input_items,
                temperature=temperature,
                call_type=f"{call_type}_fallback_chat",
                timeout_seconds=timeout_seconds,
                store=store,
            )
            result.api_mode = "responses_fallback_chat"
            return result

    async def run_tool_loop(
        self,
        *,
        model: str,
        tools: list[dict[str, Any]],
        execute_tool: Any,
        messages: list[dict[str, Any]] | None = None,
        instructions: str | None = None,
        input_items: list[dict[str, Any]] | str | None = None,
        parallel_tool_calls: bool = True,
        call_type: str = "tool_loop",
        timeout_seconds: float | None = None,
        store: bool | None = None,
        max_rounds: int = 4,
        temperature: float | None = 0.3,
    ) -> ToolLoopResult:
        result = await self._primary.run_tool_loop(
            model=model,
            tools=tools,
            execute_tool=execute_tool,
            messages=messages,
            instructions=instructions,
            input_items=input_items,
            parallel_tool_calls=parallel_tool_calls,
            call_type=call_type,
            timeout_seconds=timeout_seconds,
            store=store,
            max_rounds=max_rounds,
            temperature=temperature,
        )
        result.api_mode = "responses"
        return result


class CanaryOpenAIGateway:
    """Serve a sticky percentage of traffic on Responses; rest on Chat.

    On Responses failure, optionally fall back to Chat Completions (except
    after tool mutations have already started).
    """

    def __init__(
        self,
        chat: ChatCompletionsGateway | None = None,
        responses: ResponsesGateway | None = None,
    ) -> None:
        self._chat = chat or ChatCompletionsGateway()
        self._responses = responses or ResponsesGateway()

    def _resolve_gateways(self) -> tuple[Any, str]:
        from .openai_routing import remember_route, select_api_route

        route = select_api_route()
        remember_route(route)
        if route == "responses":
            return self._responses, "canary_responses"
        return self._chat, "canary_chat"

    def _fallback_enabled(self) -> bool:
        settings = get_settings()
        return bool(getattr(settings, "openai_responses_fallback_to_chat", True))

    def _mark_fallback(self, *, call_type: str | None = None) -> None:
        from .runtime_context import get_current_turn

        runtime = get_current_turn()
        if runtime is not None:
            runtime.openai_api_fallback = True
            runtime.openai_api_route = "chat_completions"
            runtime.register_fallback("openai_responses_canary_fallback")
            if call_type:
                runtime.release_failed_openai_attempt(call_type)

    async def parse_structured(
        self,
        *,
        model: str,
        text_format: type[T],
        messages: list[dict[str, Any]] | None = None,
        instructions: str | None = None,
        input_items: list[dict[str, Any]] | str | None = None,
        temperature: float | None = None,
        call_type: str = "structured",
        timeout_seconds: float | None = None,
        store: bool | None = None,
    ) -> StructuredParseResult:
        primary, mode_label = self._resolve_gateways()
        try:
            result = await primary.parse_structured(
                model=model,
                text_format=text_format,
                messages=messages,
                instructions=instructions,
                input_items=input_items,
                temperature=temperature,
                call_type=call_type,
                timeout_seconds=timeout_seconds,
                store=store,
            )
            result.api_mode = mode_label
            print("[openai.canary.structured]", {
                "route": mode_label,
                "fallback": False,
                "latency_ms": result.latency_ms,
            })
            return result
        except Exception as exc:
            if mode_label != "canary_responses" or not self._fallback_enabled():
                raise
            print("[openai.canary.structured.fallback]", {
                "error_type": type(exc).__name__,
                "error": str(exc)[:200],
            })
            self._mark_fallback(call_type=call_type)
            result = await self._chat.parse_structured(
                model=model,
                text_format=text_format,
                messages=messages,
                instructions=instructions,
                input_items=input_items,
                temperature=temperature,
                call_type=f"{call_type}_fallback_chat",
                timeout_seconds=timeout_seconds,
                store=store,
            )
            result.api_mode = "canary_fallback_chat"
            return result

    async def generate_text(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]] | None = None,
        instructions: str | None = None,
        input_items: list[dict[str, Any]] | str | None = None,
        temperature: float | None = None,
        call_type: str = "text",
        timeout_seconds: float | None = None,
        store: bool | None = None,
    ) -> TextGenerationResult:
        primary, mode_label = self._resolve_gateways()
        try:
            result = await primary.generate_text(
                model=model,
                messages=messages,
                instructions=instructions,
                input_items=input_items,
                temperature=temperature,
                call_type=call_type,
                timeout_seconds=timeout_seconds,
                store=store,
            )
            result.api_mode = mode_label
            print("[openai.canary.text]", {
                "route": mode_label,
                "fallback": False,
                "latency_ms": result.latency_ms,
            })
            return result
        except Exception as exc:
            if mode_label != "canary_responses" or not self._fallback_enabled():
                raise
            print("[openai.canary.text.fallback]", {
                "error_type": type(exc).__name__,
                "error": str(exc)[:200],
            })
            self._mark_fallback(call_type=call_type)
            result = await self._chat.generate_text(
                model=model,
                messages=messages,
                instructions=instructions,
                input_items=input_items,
                temperature=temperature,
                call_type=f"{call_type}_fallback_chat",
                timeout_seconds=timeout_seconds,
                store=store,
            )
            result.api_mode = "canary_fallback_chat"
            return result

    async def run_tool_loop(
        self,
        *,
        model: str,
        tools: list[dict[str, Any]],
        execute_tool: Any,
        messages: list[dict[str, Any]] | None = None,
        instructions: str | None = None,
        input_items: list[dict[str, Any]] | str | None = None,
        parallel_tool_calls: bool = True,
        call_type: str = "tool_loop",
        timeout_seconds: float | None = None,
        store: bool | None = None,
        max_rounds: int = 4,
        temperature: float | None = 0.3,
    ) -> ToolLoopResult:
        # No Chat fallback here: tool loops can mutate Tray/cart/order state.
        primary, mode_label = self._resolve_gateways()
        result = await primary.run_tool_loop(
            model=model,
            tools=tools,
            execute_tool=execute_tool,
            messages=messages,
            instructions=instructions,
            input_items=input_items,
            parallel_tool_calls=parallel_tool_calls,
            call_type=call_type,
            timeout_seconds=timeout_seconds,
            store=store,
            max_rounds=max_rounds,
            temperature=temperature,
        )
        result.api_mode = mode_label
        print("[openai.canary.tool_loop]", {
            "route": mode_label,
            "fallback": False,
            "tool_count": len(result.tool_results or []),
            "latency_ms": result.latency_ms,
        })
        return result


class ShadowOpenAIGateway:
    """Primary Chat Completions reply; sample Responses for comparison only."""

    def __init__(
        self,
        primary: ChatCompletionsGateway | None = None,
        shadow: ResponsesGateway | None = None,
    ) -> None:
        self._primary = primary or ChatCompletionsGateway()
        self._shadow = shadow or ResponsesGateway()

    def _should_sample(self) -> bool:
        settings = get_settings()
        rate = float(getattr(settings, "openai_shadow_sample_rate", 0.10) or 0.0)
        if rate <= 0:
            return False
        if rate >= 1:
            return True
        return random.random() < rate

    async def parse_structured(
        self,
        *,
        model: str,
        text_format: type[T],
        messages: list[dict[str, Any]] | None = None,
        instructions: str | None = None,
        input_items: list[dict[str, Any]] | str | None = None,
        temperature: float | None = None,
        call_type: str = "structured",
        timeout_seconds: float | None = None,
        store: bool | None = None,
    ) -> StructuredParseResult:
        primary = await self._primary.parse_structured(
            model=model,
            text_format=text_format,
            messages=messages,
            instructions=instructions,
            input_items=input_items,
            temperature=temperature,
            call_type=call_type,
            timeout_seconds=timeout_seconds,
            store=store,
        )
        if self._should_sample():
            try:
                shadow = await self._shadow.parse_structured(
                    model=model,
                    text_format=text_format,
                    messages=messages,
                    instructions=instructions,
                    input_items=input_items,
                    temperature=temperature,
                    call_type=f"{call_type}_shadow",
                    timeout_seconds=timeout_seconds,
                    store=False,
                )
                structured_match = (
                    primary.parsed.model_dump(mode="json")
                    == shadow.parsed.model_dump(mode="json")
                )
                print("[openai.shadow.structured]", {
                    "structured_match": structured_match,
                    "primary_latency_ms": primary.latency_ms,
                    "shadow_latency_ms": shadow.latency_ms,
                    "latency_delta_ms": round(
                        shadow.latency_ms - primary.latency_ms,
                        2,
                    ),
                })
            except Exception as exc:
                print("[openai.shadow.structured.error]", {
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:240],
                })
        primary.api_mode = "shadow"
        return primary

    async def generate_text(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]] | None = None,
        instructions: str | None = None,
        input_items: list[dict[str, Any]] | str | None = None,
        temperature: float | None = None,
        call_type: str = "text",
        timeout_seconds: float | None = None,
        store: bool | None = None,
    ) -> TextGenerationResult:
        primary = await self._primary.generate_text(
            model=model,
            messages=messages,
            instructions=instructions,
            input_items=input_items,
            temperature=temperature,
            call_type=call_type,
            timeout_seconds=timeout_seconds,
            store=store,
        )
        if self._should_sample():
            try:
                shadow = await self._shadow.generate_text(
                    model=model,
                    messages=messages,
                    instructions=instructions,
                    input_items=input_items,
                    temperature=temperature,
                    call_type=f"{call_type}_shadow",
                    timeout_seconds=timeout_seconds,
                    store=False,
                )
                print("[openai.shadow.text]", {
                    "reply_similarity": primary.text == shadow.text,
                    "primary_latency_ms": primary.latency_ms,
                    "shadow_latency_ms": shadow.latency_ms,
                    "latency_delta_ms": round(
                        shadow.latency_ms - primary.latency_ms,
                        2,
                    ),
                })
            except Exception as exc:
                print("[openai.shadow.text.error]", {
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:240],
                })
        primary.api_mode = "shadow"
        return primary

    async def run_tool_loop(
        self,
        *,
        model: str,
        tools: list[dict[str, Any]],
        execute_tool: Any,
        messages: list[dict[str, Any]] | None = None,
        instructions: str | None = None,
        input_items: list[dict[str, Any]] | str | None = None,
        parallel_tool_calls: bool = True,
        call_type: str = "tool_loop",
        timeout_seconds: float | None = None,
        store: bool | None = None,
        max_rounds: int = 4,
        temperature: float | None = 0.3,
    ) -> ToolLoopResult:
        # Never shadow-write: tool loops stay on the primary path only.
        return await self._primary.run_tool_loop(
            model=model,
            tools=tools,
            execute_tool=execute_tool,
            messages=messages,
            instructions=instructions,
            input_items=input_items,
            parallel_tool_calls=parallel_tool_calls,
            call_type=call_type,
            timeout_seconds=timeout_seconds,
            store=store,
            max_rounds=max_rounds,
            temperature=temperature,
        )


_gateway: OpenAIGateway | None = None


def build_openai_gateway(
    mode: str | None = None,
    *,
    client: AsyncOpenAI | None = None,
) -> OpenAIGateway:
    settings = get_settings()
    from .rollout import resolve_openai_api_mode

    selected = (mode or resolve_openai_api_mode(settings) or "").strip()
    chat_primary_allowed = bool(
        getattr(settings, "openai_chat_completions_primary_allowed", False)
    )
    responses_fallback = bool(
        getattr(settings, "openai_responses_fallback_to_chat", True)
    )

    if selected == "responses":
        responses = ResponsesGateway(client=client)
        if responses_fallback:
            return FallbackOpenAIGateway(
                primary=responses,
                fallback=ChatCompletionsGateway(client=client),
            )
        return responses
    if selected == "shadow":
        return ShadowOpenAIGateway(
            primary=ChatCompletionsGateway(client=client),
            shadow=ResponsesGateway(client=client),
        )
    if selected == "canary":
        return CanaryOpenAIGateway(
            chat=ChatCompletionsGateway(client=client),
            responses=ResponsesGateway(client=client),
        )
    if selected == "chat_completions" and not chat_primary_allowed:
        print("[openai.gateway] chat_completions_primary_disabled_redirect_responses", {
            "fallback_to_chat": responses_fallback,
        })
        responses = ResponsesGateway(client=client)
        if responses_fallback:
            return FallbackOpenAIGateway(
                primary=responses,
                fallback=ChatCompletionsGateway(client=client),
            )
        return responses
    if selected == "chat_completions":
        print("[openai.gateway] chat_completions_primary_deprecated", {
            "hint": (
                "Prefer OPENAI_API_MODE=canary|responses; "
                "set OPENAI_CHAT_COMPLETIONS_PRIMARY_ALLOWED=false after canary is green"
            ),
        })
    return ChatCompletionsGateway(client=client)


def get_openai_gateway() -> OpenAIGateway:
    global _gateway
    if _gateway is None:
        _gateway = build_openai_gateway()
    return _gateway


def reset_openai_gateway() -> None:
    global _gateway
    _gateway = None


async def parse_structured_output(
    *,
    model: str,
    text_format: type[T],
    messages: list[dict[str, Any]] | None = None,
    instructions: str | None = None,
    input_items: list[dict[str, Any]] | str | None = None,
    temperature: float | None = None,
    call_type: str = "structured",
    timeout_seconds: float | None = None,
    store: bool | None = None,
) -> StructuredParseResult:
    """Domain entrypoint for Structured Outputs (Chat Completions or Responses)."""
    return await get_openai_gateway().parse_structured(
        model=model,
        text_format=text_format,
        messages=messages,
        instructions=instructions,
        input_items=input_items,
        temperature=temperature,
        call_type=call_type,
        timeout_seconds=timeout_seconds,
        store=store,
    )


async def generate_text_output(
    *,
    model: str,
    messages: list[dict[str, Any]] | None = None,
    instructions: str | None = None,
    input_items: list[dict[str, Any]] | str | None = None,
    temperature: float | None = None,
    call_type: str = "text",
    timeout_seconds: float | None = None,
    store: bool | None = None,
) -> TextGenerationResult:
    """Domain entrypoint for free-text generation."""
    return await get_openai_gateway().generate_text(
        model=model,
        messages=messages,
        instructions=instructions,
        input_items=input_items,
        temperature=temperature,
        call_type=call_type,
        timeout_seconds=timeout_seconds,
        store=store,
    )


async def run_tool_loop_output(
    *,
    model: str,
    tools: list[dict[str, Any]],
    execute_tool: Any,
    messages: list[dict[str, Any]] | None = None,
    instructions: str | None = None,
    input_items: list[dict[str, Any]] | str | None = None,
    parallel_tool_calls: bool = True,
    call_type: str = "tool_loop",
    timeout_seconds: float | None = None,
    store: bool | None = None,
    max_rounds: int = 4,
    temperature: float | None = 0.3,
) -> ToolLoopResult:
    """Domain entrypoint for function-calling tool loops."""
    return await get_openai_gateway().run_tool_loop(
        model=model,
        tools=tools,
        execute_tool=execute_tool,
        messages=messages,
        instructions=instructions,
        input_items=input_items,
        parallel_tool_calls=parallel_tool_calls,
        call_type=call_type,
        timeout_seconds=timeout_seconds,
        store=store,
        max_rounds=max_rounds,
        temperature=temperature,
    )


def generate_text_sync(
    *,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float | None = 0.3,
    call_type: str = "text",
) -> TextGenerationResult:
    """Sync text helper for legacy sync callers.

    Prefers the async gateway (Responses/canary/chat) via ``asyncio.run``.
    Falls back to direct Chat Completions only when a running event loop
    blocks ``asyncio.run`` and Chat primary is still allowed.
    """
    try:
        return asyncio.run(
            generate_text_output(
                model=model,
                messages=messages,
                temperature=temperature,
                call_type=call_type,
            )
        )
    except RuntimeError as exc:
        # Likely "asyncio.run() cannot be called from a running event loop".
        if "asyncio.run" not in str(exc) and "running event loop" not in str(exc).lower():
            raise

    settings = get_settings()
    if not bool(getattr(settings, "openai_chat_completions_primary_allowed", True)):
        raise OpenAIGatewayError(
            "sync_text_requires_async_gateway_when_chat_primary_disabled",
            code="openai_sync_chat_disabled",
        )
    from .openai_client import get_sync_openai_client
    from .openai_runtime import execute_openai_call_sync

    print("[openai.gateway] generate_text_sync_chat_emergency_path", {
        "call_type": call_type,
    })
    client = get_sync_openai_client()
    kwargs: dict[str, Any] = {"model": model, "messages": messages}
    if temperature is not None:
        kwargs["temperature"] = temperature
    started = time.perf_counter()
    try:
        response = execute_openai_call_sync(
            call_type=call_type,
            model=model,
            messages=messages,
            operation=lambda: client.chat.completions.create(**kwargs),
        )
    except BadRequestError:
        raise
    except Exception as mapped_exc:
        raise _map_api_error(mapped_exc) from mapped_exc
    message = response.choices[0].message if response.choices else None
    content = getattr(message, "content", None) if message is not None else None
    text = str(content or "").strip()
    if not text:
        raise OpenAIEmptyOutputError("empty_chat_completion_content")
    return TextGenerationResult(
        text=text,
        raw_response=response,
        api_mode="chat_completions_sync_emergency",
        model=model,
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
    )


def _coerce_chat_messages(
    *,
    messages: list[dict[str, Any]] | None,
    instructions: str | None,
    input_items: list[dict[str, Any]] | str | None,
) -> list[dict[str, Any]]:
    if messages:
        return list(messages)
    chat: list[dict[str, Any]] = []
    if instructions and instructions.strip():
        chat.append({"role": "system", "content": instructions.strip()})
    if isinstance(input_items, str):
        if input_items.strip():
            chat.append({"role": "user", "content": input_items})
        return chat
    for item in input_items or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user")
        content = item.get("content")
        chat.append({"role": role, "content": content})
    if not chat:
        raise OpenAIGatewayError("openai_messages_missing", code="openai_messages_missing")
    return chat


def _resolve_responses_payload(
    *,
    messages: list[dict[str, Any]] | None,
    instructions: str | None,
    input_items: list[dict[str, Any]] | str | None,
) -> tuple[str | None, list[dict[str, Any]] | str | None]:
    if instructions is not None or input_items is not None:
        return instructions, input_items
    return messages_to_responses_parts(messages)


def _raise_for_incomplete(response: Any) -> None:
    status = getattr(response, "status", None)
    if status in {"incomplete", "failed", "cancelled"}:
        raise OpenAIIncompleteError(f"responses_status_{status}")


def _extract_refusal(response: Any) -> str | None:
    refusal = getattr(response, "refusal", None)
    if isinstance(refusal, str) and refusal.strip():
        return refusal.strip()
    for item in getattr(response, "output", None) or []:
        content = getattr(item, "content", None)
        if content is None and isinstance(item, dict):
            content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            part_type = getattr(part, "type", None) or (
                part.get("type") if isinstance(part, dict) else None
            )
            if part_type == "refusal":
                text = getattr(part, "refusal", None)
                if text is None and isinstance(part, dict):
                    text = part.get("refusal")
                if isinstance(text, str) and text.strip():
                    return text.strip()
    return None
