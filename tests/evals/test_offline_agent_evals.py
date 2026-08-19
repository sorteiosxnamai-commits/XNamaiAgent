"""Offline eval fixtures for high-signal agent behaviors.

These cases intentionally avoid live OpenAI/Tray calls and assert the
deterministic contracts that must remain stable across refactors.
"""

from __future__ import annotations

import pytest

from app.agent_contracts import build_agent_decision, evaluate_policy
from app.factual_validator import apply_factual_validation
from app.handoff_service import build_human_handoff_result
from app.models import AgentResult, IncomingMessage
from app.openai_agent import generate_agent_reply
from app.response_composer import compose_outbound_reply


EVAL_CASES = [
    {
        "id": "greeting_fast_path",
        "text": "olá",
        "expect_intent": "general",
        "expect_handoff": False,
    },
    {
        "id": "out_of_scope",
        "text": "qual a capital da frança?",
        "expect_intent": "out_of_scope",
        "expect_handoff": False,
    },
    {
        "id": "human_support",
        "text": "quero falar com um atendente humano",
        "expect_intent": "handoff",
        "expect_handoff": True,
    },
]


@pytest.mark.offline_eval
@pytest.mark.parametrize("case", EVAL_CASES, ids=[case["id"] for case in EVAL_CASES])
def test_offline_routing_eval(case, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "")
    from app.config import get_settings

    get_settings.cache_clear()
    incoming = IncomingMessage(
        channel="whatsapp",
        sender_phone="5543999999999",
        text=case["text"],
    )
    if case["id"] == "human_support":
        result = build_human_handoff_result(reason="customer_requested_human")
    else:
        result = generate_agent_reply(incoming, {"found": False})
    assert result.intent == case["expect_intent"]
    assert result.handoff_required is case["expect_handoff"]


@pytest.mark.offline_eval
def test_offline_factual_eval_blocks_unknown_money_in_enforce():
    incoming = IncomingMessage(channel="whatsapp", text="quanto custa?")
    result = AgentResult(
        reply_text="O produto custa R$ 9.999,00",
        intent="commerce",
        commercial_data={"products": [{"id": "1", "price": "100.00"}]},
        response_metadata={
            "domain": "commerce",
            "response_source": "openai",
            "factual_fallback_text": "Não confirmei o preço com segurança.",
        },
    )
    decision = build_agent_decision(incoming, result, openai_call_count=1)
    validated = apply_factual_validation(
        result,
        decision=decision,
        mode="enforce",
    )
    assert validated.safety_reason == "factual_validation_failed"
    assert "não confirmei" in validated.reply_text.lower()


@pytest.mark.offline_eval
def test_offline_policy_eval_marks_openai_urls_for_review():
    result = AgentResult(
        reply_text="Veja https://example.com/x",
        intent="commerce",
        response_metadata={"domain": "commerce", "response_source": "openai"},
    )
    decision = build_agent_decision(
        IncomingMessage(channel="instagram", text="link"),
        result,
        openai_call_count=1,
    )
    snapshot = evaluate_policy(decision, mode="shadow")
    assert snapshot.policy_action == "review"
    composed = compose_outbound_reply(
        IncomingMessage(channel="instagram", text="link"),
        result,
    )
    assert composed.response_metadata["channel_profile"]["channel"] == "instagram"
