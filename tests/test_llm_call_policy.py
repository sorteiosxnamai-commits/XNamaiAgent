from app.config import get_settings
from app.llm_call_policy import (
    should_run_llm_critique,
    should_run_quality_judge,
)
from app.models import AgentResult, IncomingMessage


def test_should_run_llm_critique_skips_low_risk(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("AGENT_CRITIQUE_LLM_ON_RISK_ONLY", "true")
    monkeypatch.setenv("AGENT_CRITIQUE_SHADOW_SAMPLE_RATE", "0")
    get_settings.cache_clear()
    run, reason, signals = should_run_llm_critique(
        incoming=IncomingMessage(channel="whatsapp", text="tem relógio?"),
        result=AgentResult(
            reply_text="Tenho opções",
            intent="commerce",
            commercial_data={"products": [{"id": "1"}]},
        ),
        critique_mode="shadow",
        risk_score=10,
        factual_valid=True,
    )
    assert run is False
    assert reason == "risk_gate_skip"
    assert "factual_validation_failed" not in signals


def test_should_run_llm_critique_on_factual_fail(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("AGENT_CRITIQUE_LLM_ON_RISK_ONLY", "true")
    get_settings.cache_clear()
    result = AgentResult(
        reply_text="Esse relógio custa R$ 10",
        intent="commerce",
        response_metadata={"factual_validation": {"valid": False}},
    )
    run, reason, signals = should_run_llm_critique(
        incoming=IncomingMessage(channel="whatsapp", text="quanto custa?"),
        result=result,
        critique_mode="shadow",
        risk_score=10,
        factual_valid=False,
    )
    assert run is True
    assert reason.startswith("risk:")
    assert "factual_validation_failed" in signals


def test_quality_judge_off_by_default():
    run, reason, _ = should_run_quality_judge(
        incoming=IncomingMessage(channel="whatsapp", text="x"),
        result=AgentResult(reply_text="y", intent="commerce"),
        judge_mode="off",
        factual_valid=False,
        risk_score=99,
    )
    assert run is False
    assert reason == "judge_mode_off"
