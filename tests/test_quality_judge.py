import pytest

from app.models import AgentResult, IncomingMessage
from app.quality_judge import (
    JudgeVerdict,
    attach_judge_report,
    collect_judge_risk_signals,
    is_low_risk_judge_skip,
    run_quality_judge,
    should_trigger_judge,
)
from app.runtime_context import reset_current_turn, set_current_turn
from app.turn_runtime import TurnRuntimeContext
from openai_test_utils import install_fake_openai_client


def test_judge_triggers_on_high_risk_only():
    assert should_trigger_judge(
        risk_score=40,
        factual_valid=True,
        handoff_required=False,
        openai_call_count=1,
    ) == (False, None)
    assert should_trigger_judge(
        risk_score=80,
        factual_valid=True,
        handoff_required=False,
        openai_call_count=1,
    )[0] is True


def test_low_risk_paths_skip_judge():
    greeting = AgentResult(
        reply_text="Olá!",
        intent="general",
        response_metadata={"response_source": "local_greeting"},
    )
    assert is_low_risk_judge_skip(
        IncomingMessage(text="oi"),
        greeting,
    )[0] is True

    thanks = AgentResult(
        reply_text="Por nada!",
        intent="general",
        response_metadata={"response_source": "deterministic_fallback"},
    )
    assert is_low_risk_judge_skip(
        IncomingMessage(text="obrigado"),
        thanks,
    )[0] is True

    handoff = AgentResult(
        reply_text="Vou transferir",
        intent="handoff",
        handoff_required=True,
        response_metadata={"response_source": "handoff"},
    )
    assert should_trigger_judge(
        risk_score=90,
        factual_valid=True,
        handoff_required=True,
        openai_call_count=0,
        incoming=IncomingMessage(text="quero atendente"),
        result=handoff,
    )[0] is False


def test_commercial_signals_trigger_judge():
    priced = AgentResult(
        reply_text="O modelo custa R$ 199,90.",
        intent="commerce",
        commercial_data={"products": [{"id": "1", "current_price": "199.90"}]},
        response_metadata={"response_source": "openai", "used_tray": True},
    )
    signals = collect_judge_risk_signals(
        result=priced,
        risk_score=20,
        factual_valid=True,
        openai_call_count=1,
    )
    assert "reply_contains_price" in signals
    assert should_trigger_judge(
        risk_score=20,
        factual_valid=True,
        handoff_required=False,
        openai_call_count=1,
        incoming=IncomingMessage(text="quanto custa?"),
        result=priced,
    )[0] is True


@pytest.mark.asyncio
async def test_shadow_judge_does_not_rewrite_reply(monkeypatch):
    incoming = IncomingMessage(channel="whatsapp", text="quero pagar")
    result = AgentResult(
        reply_text="Segue o link oficial",
        intent="order",
        handoff_required=False,
        commercial_data={"payment_url": "https://checkout.example/1"},
        response_metadata={"response_source": "openai", "used_tray": True},
    )
    context = TurnRuntimeContext(trace_id="judge-shadow")
    token = set_current_turn(context)

    class FakeMessage:
        def __init__(self):
            self.parsed = JudgeVerdict(
                score=40,
                pass_check=False,
                issues=["possible_invention"],
                summary="risk",
            )

    class FakeCompletions:
        async def parse(self, **kwargs):
            return type(
                "Response",
                (),
                {"choices": [type("Choice", (), {"message": FakeMessage()})()]},
            )()

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = type(
                "Chat",
                (),
                {"completions": FakeCompletions()},
            )()

    install_fake_openai_client(monkeypatch, FakeClient)
    monkeypatch.setattr(
        "app.quality_judge.get_settings",
        lambda: type(
            "S",
            (),
            {
                "openai_api_key": "sk-test",
                "openai_model": "gpt",
                "agent_quality_judge_risk_threshold": 70,
            },
        )(),
    )
    try:
        report = await run_quality_judge(
            incoming,
            result,
            mode="shadow",
            risk_score=80,
            factual_valid=True,
            openai_call_count=1,
        )
        attach_judge_report(result, report)
    finally:
        reset_current_turn(token)

    assert report.triggered is True
    assert report.applied is False
    assert result.reply_text == "Segue o link oficial"
    assert result.response_metadata["quality_judge"]["mode"] == "shadow"
    assert report.signals


@pytest.mark.asyncio
async def test_greeting_does_not_call_judge_llm(monkeypatch):
    incoming = IncomingMessage(channel="whatsapp", text="oi")
    result = AgentResult(
        reply_text="Olá! Como posso ajudar?",
        intent="general",
        response_metadata={"response_source": "local_greeting"},
    )

    async def boom(*_a, **_k):
        raise AssertionError("judge LLM must not run for greetings")

    monkeypatch.setattr("app.openai_gateway.parse_structured_output", boom)
    monkeypatch.setattr(
        "app.quality_judge.get_settings",
        lambda: type(
            "S",
            (),
            {
                "openai_api_key": "sk-test",
                "openai_model": "gpt",
                "agent_quality_judge_risk_threshold": 70,
            },
        )(),
    )
    report = await run_quality_judge(
        incoming,
        result,
        mode="shadow",
        risk_score=90,
        factual_valid=True,
        openai_call_count=0,
    )
    assert report.triggered is False
    assert report.skipped_reason == "deterministic:local_greeting"
