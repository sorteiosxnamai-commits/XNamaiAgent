"""Gate 5 — telemetria de tool-call é neutra (sem nome de fornecedor)."""

from __future__ import annotations

import inspect

import pytest


def test_observability_exports_neutral_names():
    from app import observability

    assert hasattr(observability, "record_commerce_observation")
    assert hasattr(observability, "summarize_commerce_result")
    assert not hasattr(observability, "record_tray_observation")
    assert not hasattr(observability, "summarize_tray_result")


def test_runtime_context_exports_neutral_register():
    from app import runtime_context

    assert hasattr(runtime_context, "register_commerce_call")
    assert not hasattr(runtime_context, "register_tray_call")


def test_turn_runtime_fields_are_neutral():
    from app.turn_runtime import TurnRuntimeContext

    fields = set(TurnRuntimeContext.model_fields)
    assert "commerce_call_count" in fields
    assert "commerce_calls" in fields
    assert "tray_call_count" not in fields
    assert "tray_calls" not in fields


def test_turn_runtime_summary_uses_neutral_keys():
    from app.runtime_context import (
        register_commerce_call,
        reset_current_turn,
        set_current_turn,
    )
    from app.turn_runtime import TurnRuntimeContext

    runtime = TurnRuntimeContext(trace_id="t", conversation_key="k")
    token = set_current_turn(runtime)
    try:
        register_commerce_call()
    finally:
        reset_current_turn(token)
    summary = runtime.safe_summary()
    assert summary["commerce_call_count"] == 1
    assert "commerce_tools" in summary
    assert "tray_call_count" not in summary
    assert "tray_tools" not in summary


def test_turn_metrics_uses_neutral_latency_key():
    from app.turn_metrics import build_turn_quality_event
    from app.turn_runtime import TurnRuntimeContext

    event = build_turn_quality_event(TurnRuntimeContext(trace_id="t", conversation_key="k"))
    assert "commerce_latency_ms" in event
    assert "tray_latency_ms" not in event


@pytest.mark.asyncio
async def test_commerce_tool_call_emits_neutral_event(monkeypatch):
    from app import observability
    from app.commerce.tools import execute_tool
    from app.runtime_context import reset_current_turn, set_current_turn
    from app.turn_runtime import TurnRuntimeContext

    events: list[str] = []
    monkeypatch.setattr(
        observability,
        "log_event",
        lambda event, payload=None: events.append(event),
    )

    runtime = TurnRuntimeContext(trace_id="t", conversation_key="k")
    token = set_current_turn(runtime)
    try:
        await execute_tool("search_products", {"query": "x"})
    finally:
        reset_current_turn(token)

    assert "commerce.call" in events
    assert "tray.call" not in events
    assert len(runtime.commerce_calls) == 1
    assert runtime.commerce_call_count == 1


def test_no_vendor_named_telemetry_symbols_in_runtime_modules():
    import app.commerce.tools as tools
    import app.message_pipeline as message_pipeline
    import app.observability as observability
    import app.runtime_context as runtime_context
    import app.turn_metrics as turn_metrics
    import app.turn_runtime as turn_runtime

    forbidden = (
        "record_tray_observation",
        "summarize_tray_result",
        "register_tray_call",
        "tray_calls",
        "tray_call_count",
        "tray_latency_ms",
        '"tray_tools"',
        '"tray.call"',
    )
    for module in (
        tools,
        message_pipeline,
        observability,
        runtime_context,
        turn_metrics,
        turn_runtime,
    ):
        source = inspect.getsource(module)
        for token in forbidden:
            assert token not in source, f"{module.__name__} ainda usa {token}"
