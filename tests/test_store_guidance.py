from __future__ import annotations

import pytest

from app.models import AgentResult, IncomingMessage, SalesInterpretation
from app.response_critique import apply_fast_deterministic_critique
from app.site_knowledge import STORE_URL
from app.store_guidance import build_store_guidance, is_broad_store_question


def _interpretation(**overrides) -> SalesInterpretation:
    payload = {
        "domain": "commerce",
        "goal": "discover",
        "subject": {"product_type": "acessórios"},
        "preferences": {},
        "information_needed": ["catalog", "price"],
        "references_previous_context": False,
        "needs_clarification": True,
        "confidence": 0.96,
    }
    payload.update(overrides)
    return SalesInterpretation(**payload)


def test_multi_topic_product_question_gets_consultative_guidance():
    text = (
        "Oi boa tarde, gostaria de saber sobre os seus produtos, fone de ouvido, "
        "mouse e preço de compra no atacado e varejo"
    )

    guidance = build_store_guidance(text, _interpretation())

    assert guidance is not None
    reply = guidance.reply_text
    assert reply.startswith("Boa tarde! 😊")
    assert "principalmente com atacado" in reply
    assert "fones de ouvido" in reply
    assert "mouses" in reply
    assert "R$ 800,00" not in reply
    assert "CPF ou CNPJ" in reply
    assert STORE_URL in reply
    assert reply.count("?") == 1


def test_specific_product_request_stays_on_catalog_path():
    interpretation = _interpretation(
        goal="find",
        subject={"product_type": "fone", "model": "AD-190"},
        needs_clarification=False,
    )

    assert not is_broad_store_question(
        "Tem o fone AD-190 Bluetooth e qual o preço?",
        interpretation,
    )


def test_fast_critique_recovers_broad_question_from_empty_catalog_reply():
    incoming = IncomingMessage(
        text="Gostaria de saber sobre seus produtos, fones, mouses, atacado, varejo e preços"
    )
    original = AgentResult(
        reply_text="Não encontrei opções disponíveis para esses critérios agora.",
        intent="commerce",
        safety_reason="recommendation_no_match",
    )

    fixed, verdict, skip_reason = apply_fast_deterministic_critique(
        incoming=incoming,
        result=original,
    )

    assert skip_reason == "fast_store_guidance"
    assert verdict is not None and verdict.pass_check is False
    assert fixed.safety_reason is None
    assert "R$ 800,00" not in fixed.reply_text
    assert "CPF ou CNPJ" in fixed.reply_text
    assert "Não encontrei opções" not in fixed.reply_text


def test_short_specific_question_is_not_rewritten_as_store_overview():
    incoming = IncomingMessage(text="Tem fone Bluetooth?")
    original = AgentResult(
        reply_text="Não encontrei esse produto no catálogo agora.",
        intent="commerce",
        safety_reason="product_not_found",
    )

    fixed, verdict, skip_reason = apply_fast_deterministic_critique(
        incoming=incoming,
        result=original,
    )

    assert fixed is original
    assert verdict is None
    assert skip_reason is None


@pytest.mark.asyncio
async def test_sales_flow_answers_overview_without_querying_catalog(monkeypatch):
    import app.sales_agent as sales_agent

    async def forbid_catalog(*args, **kwargs):
        raise AssertionError("store overview must not be sent as a catalog query")

    monkeypatch.setattr(sales_agent, "execute_tool", forbid_catalog)
    result = await sales_agent.handle_sales_message(
        IncomingMessage(
            text=(
                "Boa tarde, gostaria de saber sobre os produtos, fones, mouses, "
                "preços no atacado e varejo"
            )
        ),
        {"primary_intent": "commerce"},
        {},
        _interpretation(),
        recent_turns=[],
    )

    assert result is not None
    assert result.response_metadata["response_source"] == "official_store_guidance"
    assert result.response_metadata["used_commerce_provider"] is False
    assert "CPF ou CNPJ" in result.reply_text
