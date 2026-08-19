"""Additional offline/online eval coverage for v6 architectural fixes."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from app.catalog_index import (
    CandidateTrace,
    build_allowed_id_sets,
    filter_products_to_allowed,
    hybrid_rank_products,
    reject_unknown_rerank_ids,
)
from app.conversation_summary_policy import (
    ConversationSummary,
    compare_summary_delta_to_facts,
)
from app.memory_models import ConversationSummaryDelta
from app.models import ProductPreferences, ProductSubject, SalesInterpretation
from app.openai_gateway import apply_responses_controls_report, model_capabilities
from app.openai_routing import sticky_routing_key
from app.rollout import resolve_openai_api_mode, resolve_rollout_profile
from app.turn_runtime import LLMCallBudget, TurnRuntimeContext
from app.turn_understanding import TurnUnderstanding
from tests.evals.fake_openai_gateway import FakeOpenAIGateway, FakeScript


def test_candidate_trace_model():
    trace = CandidateTrace(
        catalog_item_key="product:1",
        initial_score=12.0,
        exact_matches=["brand"],
        hard_constraints_passed=["brand"],
        soft_preferences_matched=["color_alias"],
        score_components={"exact_boost": 10.0, "soft_score": 2.0},
    )
    assert trace.excluded is False
    assert trace.catalog_item_key.startswith("product:")


def test_hybrid_rank_attaches_candidate_trace():
    interpretation = SalesInterpretation(
        domain="commerce",
        goal="recommend",
        subject=ProductSubject(brand="Seiko", query="Seiko"),
        preferences=ProductPreferences(),
        references_previous_context=False,
        needs_clarification=False,
        enough_information_to_search=True,
        ready_for_retrieval=True,
        confidence=0.9,
    )
    products = [
        {
            "id": "10",
            "name": "Seiko SRPD51",
            "brand": "Seiko",
            "price": 1500,
            "stock": 2,
            "available": True,
        },
        {
            "id": "11",
            "name": "Orient Bambino",
            "brand": "Orient",
            "price": 1200,
            "stock": 1,
            "available": True,
        },
    ]
    ranked = hybrid_rank_products(products, interpretation, mode="recommendation")
    assert ranked
    assert "_retrieval" in ranked[0]
    assert "candidate_trace" in ranked[0]["_retrieval"]


def test_allowed_id_sets_reject_invented():
    pool = [{"id": "1", "variant_id": "v1"}, {"id": "2"}]
    allowed = build_allowed_id_sets(pool)
    ordered, invalid = reject_unknown_rerank_ids(
        ["1", "999", "2"],
        allowed["allowed_product_ids"],
        limit=5,
    )
    assert ordered == ["1", "2"]
    assert invalid == 1
    kept, rejected = filter_products_to_allowed(
        [{"id": "1", "variant_id": "v1"}, {"id": "999"}],
        allowed,
    )
    assert len(kept) == 1
    assert rejected == 1


def test_logical_vs_transport_metrics_on_fallback_refund():
    ctx = TurnRuntimeContext(
        trace_id="t1",
        llm_budget=LLMCallBudget(max_calls=2, enforce=True),
    )
    ctx.register_openai_call("structured", transport="responses")
    assert ctx.logical_llm_calls == 1
    assert ctx.openai_transport_attempts == 1
    assert ctx.responses_attempts == 1
    ctx.release_failed_openai_attempt("structured")
    assert ctx.logical_llm_calls == 0
    assert ctx.openai_transport_attempts == 1  # never refunded
    ctx.register_openai_call("structured_fallback_chat", transport="chat_completions")
    assert ctx.logical_llm_calls == 1
    assert ctx.openai_transport_attempts == 2
    assert ctx.chat_fallback_attempts == 1


def test_promote_budget_does_not_reset_used():
    ctx = TurnRuntimeContext(
        trace_id="t2",
        llm_budget=LLMCallBudget(max_calls=2, enforce=True),
    )
    ctx.register_openai_call("decision")
    assert ctx.llm_budget.used_calls == 1
    ctx.promote_budget(4)
    assert ctx.llm_budget.max_calls == 4
    assert ctx.llm_budget.used_calls == 1
    assert ctx.execution_path == "complex"


def test_reasoning_effort_skip_reason(monkeypatch):
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "medium")
    from app.config import get_settings

    get_settings.cache_clear()
    caps = model_capabilities("gpt-3.5-turbo")
    assert caps.supports_reasoning_effort is False
    kwargs: dict = {}
    report = apply_responses_controls_report(kwargs, model="gpt-3.5-turbo")
    assert report["reasoning_effort_applied"] is False
    assert report["reasoning_effort_skip_reason"] == "model_capability_not_declared"
    assert "reasoning" not in kwargs
    get_settings.cache_clear()


def test_sticky_routing_uses_tenant_and_conversation_hash():
    a = sticky_routing_key(tenant_id="newstore", conversation_id="conv-1")
    b = sticky_routing_key(tenant_id="newstore", conversation_id="conv-1")
    c = sticky_routing_key(tenant_id="other", conversation_id="conv-1")
    assert a == b
    assert a != c
    assert "conv-1" not in a
    assert a.startswith("newstore:")


def test_rollout_shadow_profile():
    cfg = SimpleNamespace(
        agent_rollout_profile="shadow",
        agent_emergency_rollback=False,
        openai_api_mode="responses",
        openai_responses_traffic_percent=1.0,
        openai_chat_completions_primary_allowed=False,
    )
    assert resolve_rollout_profile(cfg) == "shadow"
    assert resolve_openai_api_mode(cfg) == "shadow"


def test_summary_shadow_flags_commercial_divergence():
    delta = ConversationSummaryDelta(
        current_goal="cliente quer o relógio",
        commitments=["estoque confirmado com 5 unidades"],
    )
    codes = compare_summary_delta_to_facts(delta)
    assert "summary_asserts_commercial_fact" in codes
    summary = ConversationSummary(
        confirmed_facts=[],
        pending_questions=["qual cor?"],
        expired_commercial_references=["price:1499"],
    )
    assert summary.expired_commercial_references


@pytest.mark.asyncio
async def test_fake_openai_gateway_structured_and_refusal():
    gateway = FakeOpenAIGateway(
        [
            FakeScript(
                call_type="structured",
                kind="structured",
                parsed=TurnUnderstanding(
                    primary_intent="greeting",
                    confidence=0.9,
                ),
            ),
            FakeScript(call_type="text", kind="refusal"),
        ]
    )
    parsed = await gateway.parse_structured(
        model="fake",
        text_format=TurnUnderstanding,
        call_type="structured",
    )
    assert parsed.parsed.primary_intent == "greeting"
    with pytest.raises(Exception):
        await gateway.generate_text(model="fake", call_type="text")


@pytest.mark.online_eval
def test_online_eval_gate_skips_without_flag():
    if os.getenv("RUN_ONLINE_OPENAI_EVALS", "").lower() not in {"1", "true", "yes"}:
        pytest.skip("RUN_ONLINE_OPENAI_EVALS not enabled")
    if not os.getenv("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY missing")
    # Placeholder: live evals must remain side-effect free (no checkout/payment).
    assert True
