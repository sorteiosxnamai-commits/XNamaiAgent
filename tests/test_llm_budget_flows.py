"""Fluxos reais de orcamento: quais chamadas LLM sao obrigatorias e quais sao redundantes.

Turno de esclarecimento = interpretar (1) + perguntar com persona (2). O
budget normal e 2. Um esclarecimento nao afirma fato comercial: gastar uma 3a
chamada para o critico julga-lo e redundante — e, com o budget cheio, so
produz ``llm_budget_exceeded``, que conta como fallback nas metricas de
rollout. A protecao contra runaway (budget) continua intacta.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models import AgentResult, IncomingMessage
from app.runtime_context import reset_current_turn, set_current_turn
from app.turn_runtime import LLMCallBudget, LLMCallBudgetExceeded, TurnRuntimeContext


def _clarification(confidence: float = 0.3) -> AgentResult:
    return AgentResult(
        reply_text="Para qual modelo de celular você precisa da capa?",
        intent="commerce",
        safety_reason="commerce_clarification",
        response_metadata={"response_source": "openai", "interpretation_confidence": confidence},
    )


def _factual(confidence: float = 0.3) -> AgentResult:
    return AgentResult(
        reply_text="A capa X custa R$ 29,90.",
        intent="commerce",
        commercial_data={"products": [{"id": "1", "current_price": "29.90"}]},
        response_metadata={"response_source": "openai", "interpretation_confidence": confidence},
    )


def _turn_after(calls: list[str]) -> TurnRuntimeContext:
    runtime = TurnRuntimeContext(trace_id="t", llm_budget=LLMCallBudget(max_calls=2, enforce=True))
    for call_type in calls:
        runtime.register_openai_call(call_type, transport="responses")
    return runtime


@pytest.fixture
def critique_spy(monkeypatch):
    import app.response_critique as critique

    spy = AsyncMock(side_effect=AssertionError("critic LLM must not run"))
    monkeypatch.setattr(critique, "_run_response_critique_loop", spy)
    return spy


@pytest.mark.asyncio
async def test_clarification_turn_spends_no_third_call_on_the_critic(critique_spy):
    from app.response_critique import apply_response_critique_loop

    runtime = _turn_after(["decision", "clarification"])
    token = set_current_turn(runtime)
    try:
        result, report = await apply_response_critique_loop(
            incoming=IncomingMessage(text="quero uma capa"),
            result=_clarification(),
            mode="shadow",
            openai_call_count=runtime.openai_call_count,
        )
    finally:
        reset_current_turn(token)

    critique_spy.assert_not_awaited()
    assert result.response_metadata["response_critique"]["skip_reason"] == "clarification_without_facts"
    assert runtime.openai_call_count == 2
    assert "llm_budget_exceeded" not in runtime.fallback_reasons


@pytest.mark.asyncio
async def test_factual_reply_with_low_confidence_is_still_critiqued(monkeypatch):
    """A reducao nao desliga a protecao: resposta com preco continua passando pelo critico."""
    import app.response_critique as critique

    spy = AsyncMock(side_effect=lambda **kwargs: (kwargs["result"], kwargs["report"]))
    monkeypatch.setattr(critique, "_run_response_critique_loop", spy)
    await critique.apply_response_critique_loop(
        incoming=IncomingMessage(text="quanto custa a capa X?"),
        result=_factual(),
        mode="shadow",
        openai_call_count=2,
    )
    spy.assert_awaited_once()


@pytest.mark.asyncio
async def test_clarification_mentioning_a_price_is_not_skipped(critique_spy, monkeypatch):
    """Se o esclarecimento afirmar fato comercial, ele deixa de ser 'sem fatos'."""
    import app.response_critique as critique

    passthrough = AsyncMock(side_effect=lambda **kwargs: (kwargs["result"], kwargs["report"]))
    monkeypatch.setattr(critique, "_run_response_critique_loop", passthrough)
    result = _clarification()
    result.reply_text = "Temos capas a partir de R$ 19,90. Qual o modelo?"
    await critique.apply_response_critique_loop(
        incoming=IncomingMessage(text="quero uma capa"), result=result, mode="shadow", openai_call_count=2,
    )
    passthrough.assert_awaited_once()


def test_mandatory_vs_optional_calls_on_a_clarification_turn():
    """Obrigatorias: decision + clarification (cabem no budget 2).
    Opcional: critico — so com sinal de risco. Runaway continua bloqueado."""
    runtime = _turn_after(["decision", "clarification"])
    with pytest.raises(LLMCallBudgetExceeded):
        runtime.register_openai_call("judge", transport="responses")
