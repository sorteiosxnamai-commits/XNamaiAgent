import asyncio
from types import SimpleNamespace

import pytest

from app.openai_runtime import execute_openai_call, execute_openai_call_sync
from app.runtime_context import (
    get_current_turn,
    register_database_call,
    register_tray_call,
    reset_current_turn,
    runtime_stage,
    set_current_turn,
)
from app.turn_runtime import (
    LLMCallBudget,
    LLMCallBudgetExceeded,
    TurnRuntimeContext,
)


def _response(input_tokens: int = 3, output_tokens: int = 5):
    return SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
        )
    )


def test_runtime_collects_safe_metrics_without_exposing_identity():
    context = TurnRuntimeContext(
        trace_id="trace-1",
        conversation_key="whatsapp:5511999999999",
    )
    token = set_current_turn(context)
    try:
        with runtime_stage("load_context"):
            register_database_call()
        register_tray_call()
        result = execute_openai_call_sync(
            call_type="decision",
            operation=lambda: _response(),
        )
    finally:
        reset_current_turn(token)

    assert result.usage.prompt_tokens == 3
    summary = context.safe_summary()
    assert summary["openai_call_count"] == 1
    assert summary["tray_call_count"] == 1
    assert summary["database_call_count"] == 1
    assert summary["openai_input_tokens"] == 3
    assert summary["openai_output_tokens"] == 5
    assert summary["execution_path"] == "normal"
    assert summary["conversation_key_present"] is True
    assert "whatsapp:5511999999999" not in str(summary)
    assert summary["stage_durations_ms"]["load_context"] >= 0


def test_llm_budget_observes_without_blocking_when_enforcement_is_disabled():
    context = TurnRuntimeContext(
        trace_id="trace-observe",
        llm_budget=LLMCallBudget(max_calls=1, enforce=False),
    )
    token = set_current_turn(context)
    try:
        execute_openai_call_sync(
            call_type="decision",
            operation=lambda: _response(),
        )
        execute_openai_call_sync(
            call_type="response_composition",
            operation=lambda: _response(),
        )
    finally:
        reset_current_turn(token)

    assert context.openai_call_count == 2
    assert context.llm_budget.used_calls == 2
    assert context.execution_path == "complex"


def test_llm_budget_blocks_before_external_call_when_enforced():
    context = TurnRuntimeContext(
        trace_id="trace-enforced",
        llm_budget=LLMCallBudget(max_calls=1, enforce=True),
    )
    external_calls = 0

    def operation():
        nonlocal external_calls
        external_calls += 1
        return _response()

    token = set_current_turn(context)
    try:
        execute_openai_call_sync(call_type="decision", operation=operation)
        with pytest.raises(LLMCallBudgetExceeded):
            execute_openai_call_sync(
                call_type="response_composition",
                operation=operation,
            )
    finally:
        reset_current_turn(token)

    assert external_calls == 1
    assert context.openai_call_count == 1
    assert context.integration_failures == {}


@pytest.mark.asyncio
async def test_runtime_context_is_isolated_between_concurrent_turns():
    async def run_turn(trace_id: str, input_tokens: int):
        context = TurnRuntimeContext(trace_id=trace_id)
        token = set_current_turn(context)
        try:
            async def operation():
                await asyncio.sleep(0)
                assert get_current_turn() is context
                return _response(input_tokens=input_tokens, output_tokens=1)

            await execute_openai_call(
                call_type="decision",
                operation=operation,
            )
            return context.safe_summary()
        finally:
            reset_current_turn(token)

    first, second = await asyncio.gather(
        run_turn("trace-a", 2),
        run_turn("trace-b", 7),
    )

    assert first["trace_id"] == "trace-a"
    assert first["openai_input_tokens"] == 2
    assert second["trace_id"] == "trace-b"
    assert second["openai_input_tokens"] == 7
    assert get_current_turn() is None


@pytest.mark.asyncio
async def test_openai_failure_is_recorded_and_propagated():
    context = TurnRuntimeContext(trace_id="trace-failure")
    token = set_current_turn(context)
    try:
        async def operation():
            raise RuntimeError("provider_down")

        with pytest.raises(RuntimeError, match="provider_down"):
            await execute_openai_call(
                call_type="decision",
                operation=operation,
            )
    finally:
        reset_current_turn(token)

    assert context.openai_call_count == 1
    assert context.integration_failures == {"openai": 1}
