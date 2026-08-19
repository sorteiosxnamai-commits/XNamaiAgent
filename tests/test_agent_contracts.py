from app.agent_contracts import build_agent_decision, evaluate_policy
from app.models import AgentResult, IncomingMessage


def _incoming() -> IncomingMessage:
    return IncomingMessage(
        channel="whatsapp",
        sender_key="whatsapp:test",
        text="Olá",
    )


def test_deterministic_turn_uses_fast_execution_path():
    result = AgentResult(
        reply_text="Olá! Como posso ajudar?",
        intent="general",
        response_metadata={
            "domain": "greeting",
            "response_source": "deterministic",
        },
    )

    decision = build_agent_decision(
        _incoming(),
        result,
        openai_call_count=0,
    )

    assert decision.domain == "greeting"
    assert decision.source == "deterministic"
    assert decision.fast_path is True
    assert decision.execution_path == "fast"
    assert decision.risk.level == "low"


def test_multiple_llm_calls_classify_complex_turn():
    result = AgentResult(
        reply_text="Encontrei estas opções.",
        intent="commerce",
        commercial_data={"products": [{"id": "1", "name": "Produto"}]},
        response_metadata={
            "domain": "commerce",
            "response_source": "openai",
            "used_tray": True,
        },
    )

    decision = build_agent_decision(
        _incoming(),
        result,
        openai_call_count=2,
    )

    assert decision.execution_path == "complex"
    assert decision.fast_path is False
    assert decision.risk.required_validations == ["catalog_facts"]


def test_transactional_facts_force_critical_execution_path():
    result = AgentResult(
        reply_text="Seu pedido 123 foi criado: https://checkout.example/123",
        intent="order",
        commercial_data={
            "order_id": "123",
            "payment_url": "https://checkout.example/123",
        },
        response_metadata={
            "domain": "commerce",
            "response_source": "tool",
            "used_tray": True,
        },
    )

    decision = build_agent_decision(
        _incoming(),
        result,
        openai_call_count=1,
    )
    snapshot = evaluate_policy(decision, mode="shadow")

    assert decision.execution_path == "critical"
    assert decision.risk.level == "high"
    assert set(decision.risk.required_validations) == {
        "urls",
        "transactional_facts",
    }
    assert snapshot.policy_action == "allow"
    assert snapshot.factual_validation_required is True


def test_openai_factual_claim_is_marked_for_shadow_review():
    result = AgentResult(
        reply_text="Veja: https://example.com/produto",
        intent="commerce",
        response_metadata={
            "domain": "commerce",
            "response_source": "openai",
        },
    )

    decision = build_agent_decision(
        _incoming(),
        result,
        openai_call_count=1,
    )
    snapshot = evaluate_policy(decision, mode="shadow")

    assert snapshot.policy_action == "review"
    assert snapshot.policy_reasons == ["facts_require_validation"]
