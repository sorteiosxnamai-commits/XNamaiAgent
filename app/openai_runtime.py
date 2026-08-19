from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from .observability import record_openai_observation
from .runtime_context import get_current_turn


T = TypeVar("T")


def _usage_tokens(response: Any) -> tuple[int, int, int, int]:
    """Return input, output, cached, reasoning token counts."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0, 0, 0
    input_tokens = (
        getattr(usage, "prompt_tokens", None)
        or getattr(usage, "input_tokens", None)
        or 0
    )
    output_tokens = (
        getattr(usage, "completion_tokens", None)
        or getattr(usage, "output_tokens", None)
        or 0
    )
    cached_tokens = 0
    reasoning_tokens = 0
    details_in = getattr(usage, "input_tokens_details", None)
    if details_in is not None:
        if isinstance(details_in, dict):
            cached_tokens = int(details_in.get("cached_tokens") or 0)
        else:
            cached_tokens = int(getattr(details_in, "cached_tokens", 0) or 0)
    details_out = getattr(usage, "output_tokens_details", None)
    if details_out is not None:
        if isinstance(details_out, dict):
            reasoning_tokens = int(details_out.get("reasoning_tokens") or 0)
        else:
            reasoning_tokens = int(getattr(details_out, "reasoning_tokens", 0) or 0)
    return (
        int(input_tokens or 0),
        int(output_tokens or 0),
        cached_tokens,
        reasoning_tokens,
    )


def _response_preview(response: Any) -> str | None:
    try:
        # Responses API
        output_parsed = getattr(response, "output_parsed", None)
        if output_parsed is not None:
            if hasattr(output_parsed, "model_dump"):
                return str(output_parsed.model_dump(mode="json"))
            return str(output_parsed)
        output_text = getattr(response, "output_text", None)
        if isinstance(output_text, str) and output_text.strip():
            return output_text
        # Chat Completions
        choice = response.choices[0] if getattr(response, "choices", None) else None
        message = getattr(choice, "message", None) if choice is not None else None
        content = getattr(message, "content", None) if message is not None else None
        if content:
            return str(content)
        parsed = getattr(message, "parsed", None) if message is not None else None
        if parsed is not None:
            if hasattr(parsed, "model_dump"):
                return str(parsed.model_dump(mode="json"))
            return str(parsed)
    except Exception:
        return None
    return None


def _start_call(
    call_type: str,
    *,
    reason: str | None = None,
) -> tuple[Any, float]:
    context = get_current_turn()
    if context is not None:
        context.register_openai_call(call_type, reason=reason)
    return context, time.perf_counter()


def _finish_call(
    context: Any,
    started_at: float,
    response: Any,
    *,
    call_type: str,
    model: str | None,
    messages: list[dict[str, Any]] | None,
    ok: bool = True,
    error_type: str | None = None,
) -> None:
    input_tokens, output_tokens, cached_tokens, reasoning_tokens = (
        _usage_tokens(response) if ok else (0, 0, 0, 0)
    )
    if context is not None and ok:
        context.register_openai_usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            reasoning_tokens=reasoning_tokens,
        )
        stage_name = f"openai_{context.openai_call_count}"
        context.stage_durations_ms[stage_name] = round(
            (time.perf_counter() - started_at) * 1000,
            2,
        )
    record_openai_observation(
        call_type=call_type,
        model=model,
        messages=messages,
        response_preview=_response_preview(response) if ok else None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        elapsed_ms=round((time.perf_counter() - started_at) * 1000, 2),
        ok=ok,
        error_type=error_type,
        cached_tokens=cached_tokens,
        reasoning_tokens=reasoning_tokens,
    )


async def execute_openai_call(
    *,
    call_type: str,
    operation: Callable[[], Awaitable[T]],
    timeout_seconds: float | None = None,
    model: str | None = None,
    messages: list[dict[str, Any]] | None = None,
    reason: str | None = None,
) -> T:
    context, started_at = _start_call(call_type, reason=reason)
    try:
        awaitable = operation()
        response = (
            await asyncio.wait_for(awaitable, timeout=timeout_seconds)
            if timeout_seconds and timeout_seconds > 0
            else await awaitable
        )
    except Exception as exc:
        if context is not None:
            context.register_integration_failure("openai")
        _finish_call(
            context,
            started_at,
            None,
            call_type=call_type,
            model=model,
            messages=messages,
            ok=False,
            error_type=type(exc).__name__,
        )
        raise
    _finish_call(
        context,
        started_at,
        response,
        call_type=call_type,
        model=model,
        messages=messages,
    )
    return response


def execute_openai_call_sync(
    *,
    call_type: str,
    operation: Callable[[], T],
    model: str | None = None,
    messages: list[dict[str, Any]] | None = None,
    reason: str | None = None,
) -> T:
    context, started_at = _start_call(call_type, reason=reason)
    try:
        response = operation()
    except Exception as exc:
        if context is not None:
            context.register_integration_failure("openai")
        _finish_call(
            context,
            started_at,
            None,
            call_type=call_type,
            model=model,
            messages=messages,
            ok=False,
            error_type=type(exc).__name__,
        )
        raise
    _finish_call(
        context,
        started_at,
        response,
        call_type=call_type,
        model=model,
        messages=messages,
    )
    return response
