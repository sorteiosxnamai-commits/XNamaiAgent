"""`execute_tool` instrumenta toda chamada de tool, com ou sem provider.

A telemetria de tool-call é genérica (vale para qualquer provider) e é área
protegida desta migração: não pode degradar. Estes testes asseram sobre o estado
realmente acumulado em ``TurnRuntimeContext``, não sobre um mock da função de
observabilidade.
"""

from typing import Any

import pytest

from app.commerce.errors import COMMERCE_UNAVAILABLE_CODE
from app.commerce.provider import (
    reset_commerce_provider,
    set_commerce_provider,
)
from app.commerce.tools import execute_tool
from app.runtime_context import reset_current_turn, set_current_turn
from app.turn_runtime import TurnRuntimeContext


@pytest.fixture
def runtime():
    context = TurnRuntimeContext(trace_id="commerce-obs", conversation_key="c1")
    token = set_current_turn(context)
    try:
        yield context
    finally:
        reset_current_turn(token)


@pytest.mark.asyncio
async def test_unavailable_call_is_still_observed(runtime):
    assert runtime.commerce_calls == []

    result = await execute_tool("search_products", {"query": "relogio"})

    assert result["error"] == COMMERCE_UNAVAILABLE_CODE
    assert len(runtime.commerce_calls) == 1, "chamada indisponível também precisa ser observada"
    observation = runtime.commerce_calls[0]
    assert observation["tool"] == "search_products"
    assert observation["ok"] is False
    assert isinstance(observation["elapsed_ms"], float)
    assert observation["elapsed_ms"] >= 0
    assert observation["result"]["error"] == COMMERCE_UNAVAILABLE_CODE
    assert "arguments" in observation


@pytest.mark.asyncio
async def test_successful_call_is_observed_as_ok(runtime):
    class FakeProvider:
        name = "fake"

        async def execute(self, capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
            return {"products": [{"id": "1"}], "count": 1}

    try:
        set_commerce_provider(FakeProvider())
        result = await execute_tool("search_products", {"query": "relogio"})
    finally:
        reset_commerce_provider()

    assert result["count"] == 1
    assert len(runtime.commerce_calls) == 1
    observation = runtime.commerce_calls[0]
    assert observation["tool"] == "search_products"
    assert observation["ok"] is True
    assert observation["result"]["ok"] is True
    assert observation["result"]["products_count"] == 1


@pytest.mark.asyncio
async def test_provider_exception_is_observed_and_does_not_leak(runtime):
    class BoomProvider:
        name = "boom"

        async def execute(self, capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
            raise ValueError("provider exploded")

    try:
        set_commerce_provider(BoomProvider())
        result = await execute_tool("get_product", {"product_id": "123"})
    finally:
        reset_commerce_provider()

    assert result["error_type"] == "ValueError"
    assert len(runtime.commerce_calls) == 1
    assert runtime.commerce_calls[0]["tool"] == "get_product"
    assert runtime.commerce_calls[0]["ok"] is False


@pytest.mark.asyncio
async def test_every_call_accumulates_one_observation(runtime):
    await execute_tool("search_products", {"query": "a"})
    await execute_tool("get_product", {"product_id": "1"})
    await execute_tool("create_order", {"customer_id": "9"})

    assert [item["tool"] for item in runtime.commerce_calls] == [
        "search_products",
        "get_product",
        "create_order",
    ]


@pytest.mark.asyncio
async def test_turn_metrics_see_the_tool_calls(runtime):
    """A telemetria precisa chegar ao evento turn.quality, não parar no runtime."""
    from app.turn_metrics import build_turn_quality_event

    await execute_tool("search_products", {"query": "a"})
    await execute_tool("get_product", {"product_id": "1"})

    event = build_turn_quality_event(runtime)
    assert event["tool_calls"] == 2
    assert event["commerce_latency_ms"] >= 0


@pytest.mark.asyncio
async def test_observation_works_without_an_active_turn():
    """Sem turno ativo a instrumentação não pode quebrar a chamada."""
    result = await execute_tool("search_products", {"query": "a"})
    assert result["error"] == COMMERCE_UNAVAILABLE_CODE
