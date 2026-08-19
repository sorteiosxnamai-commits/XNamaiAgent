"""Etapa 5 — factual authority layers."""

from __future__ import annotations

from app.fact_authority import (
    CommerceDataAuthority,
    PersonaAuthority,
    PolicyAuthority,
    claim_from_product_field,
    filter_commerce_safe_evidence,
)
from app.fact_sources import (
    FACT_SOURCE_RANK,
    FactSource,
    StructuredFact,
    infer_source_for_payload_key,
    preferred_fact,
)
from app.factual_validator import build_fact_pack, validate_factual_response
from app.agent_contracts import build_agent_decision
from app.models import AgentResult, IncomingMessage


def test_commerce_rank_tray_live_beats_local_db_and_persona():
    assert FACT_SOURCE_RANK[FactSource.TRAY_LIVE] > FACT_SOURCE_RANK[FactSource.TRAY_ADAPTER]
    assert FACT_SOURCE_RANK[FactSource.TRAY_ADAPTER] > FACT_SOURCE_RANK[FactSource.LOCAL_DATABASE]
    assert FACT_SOURCE_RANK[FactSource.LOCAL_DATABASE] > FACT_SOURCE_RANK[FactSource.COMMERCE_STATE]
    assert FACT_SOURCE_RANK[FactSource.SECURITY_RULE] > FACT_SOURCE_RANK[FactSource.TRAY_LIVE]
    assert PolicyAuthority.allows_persona_to_state_commercial_facts() is False
    assert PersonaAuthority.may_assert_commercial_fact() is False


def test_preferred_fact_ignores_persona_price():
    facts = [
        StructuredFact(
            source=FactSource.APPROVED_PERSONA,
            key="price",
            value="1.00",
            entity_type="price",
        ),
        StructuredFact(
            source=FactSource.CUSTOMER_MEMORY,
            key="price",
            value="2.00",
            entity_type="price",
        ),
        StructuredFact(
            source=FactSource.TRAY_LIVE,
            key="price",
            value="199.90",
            entity_type="price",
            entity_id="p1",
            revalidation_status="revalidated",
            metadata={"revalidated": True},
        ),
    ]
    chosen = preferred_fact(facts, key="price", entity_type="price")
    assert chosen is not None
    assert chosen.source == FactSource.TRAY_LIVE
    assert chosen.value == "199.90"
    assert filter_commerce_safe_evidence(facts) == [facts[2]]


def test_infer_source_tray_live_from_revalidated_flag():
    assert (
        infer_source_for_payload_key("current_price", used_tray=True, revalidated=True)
        == FactSource.TRAY_LIVE
    )
    assert (
        infer_source_for_payload_key(
            "current_price", factual_source="catalog_cache"
        )
        == FactSource.CATALOG_SNAPSHOT
    )


def test_claim_from_product_field_marks_revalidated():
    claim = claim_from_product_field(
        {
            "id": "42",
            "current_price": 199.9,
            "_revalidated": True,
            "_factual_source": "tray_live",
            "tenant_id": "newstore",
        },
        kind="price",
        key="current_price",
        value=199.9,
        tenant_id="newstore",
    )
    assert claim.source == FactSource.TRAY_LIVE
    assert claim.product_id == "42"
    assert claim.revalidation_status.value == "revalidated"
    assert claim.is_commerce_safe() is True


def test_build_fact_pack_tags_revalidated_products():
    result = AgentResult(
        reply_text="R$ 199,90",
        intent="commerce",
        commercial_data={
            "products": [
                {
                    "id": "1",
                    "current_price": "199.90",
                    "_revalidated": True,
                    "_factual_source": "tray_live",
                }
            ]
        },
        response_metadata={"domain": "commerce", "used_tray": True},
    )
    pack = build_fact_pack(result)
    price_facts = [e for e in pack.evidence if e.entity_type == "price"]
    assert price_facts
    assert price_facts[0].source == FactSource.TRAY_LIVE
    assert price_facts[0].revalidation_status == "revalidated"
    assert price_facts[0].entity_id == "1"


def test_commerce_authority_prefers_revalidated():
    facts = [
        StructuredFact(
            source=FactSource.TRAY_ADAPTER,
            key="price",
            value="199.90",
            entity_type="price",
            entity_id="1",
        ),
        StructuredFact(
            source=FactSource.TRAY_LIVE,
            key="price",
            value="189.90",
            entity_type="price",
            entity_id="1",
            metadata={"revalidated": True},
        ),
    ]
    best = CommerceDataAuthority.prefer(facts, entity_type="price")
    assert best is not None
    assert best.value == "189.90"
    assert best.source == FactSource.TRAY_LIVE


def test_stock_violation_is_high_risk_for_enforce():
    result = AgentResult(
        reply_text="O modelo está em estoque agora.",
        intent="commerce",
        commercial_data={
            "products": [{"id": "1", "stock": 0, "available": False}],
        },
        response_metadata={"domain": "commerce", "used_tray": True},
    )
    decision = build_agent_decision(
        IncomingMessage(channel="whatsapp", sender_key="wa:1", text="estoque"),
        result,
        openai_call_count=1,
    )
    report = validate_factual_response(result, decision=decision, mode="enforce")
    assert report.valid is False
    assert report.risk_level == "high"
    assert report.fallback_required is True
