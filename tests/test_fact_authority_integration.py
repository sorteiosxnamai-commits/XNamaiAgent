"""Additional fact-authority integration tests (v6 fixes)."""

from __future__ import annotations

from app.fact_authority import (
    CommerceDataAuthority,
    PersonaAuthority,
    authorize_products_for_responder,
    catalog_item_key_for,
    grounded_evidence_from_product,
)
from app.fact_sources import FactSource
from app.llm_call_policy import build_llm_call_budget, should_promote_to_complex


def test_catalog_item_key_variant_vs_product():
    assert catalog_item_key_for("10", None) == "product:10"
    assert catalog_item_key_for("10", "v1") == "variant:v1"


def test_persona_cannot_supply_price_via_authority():
    assert PersonaAuthority.may_assert_commercial_fact() is False
    assert CommerceDataAuthority.may_supply("price", FactSource.APPROVED_PERSONA) is False
    assert CommerceDataAuthority.may_supply("price", FactSource.TRAY_LIVE) is True


def test_cross_tenant_product_rejected():
    product = {
        "id": "1",
        "tenant_id": "other",
        "price": 100,
        "_factual_source": "tray_live",
        "_revalidated": True,
    }
    assert grounded_evidence_from_product(product, expected_tenant_id="newstore") == []


def test_stale_index_price_omitted_from_grounded():
    product = {
        "id": "1",
        "tenant_id": "newstore",
        "price": 99,
        "name": "Watch",
        "_factual_source": "catalog_index",
        "_revalidated": False,
    }
    rows = grounded_evidence_from_product(product, tenant_id="newstore")
    fields = {r.field for r in rows}
    assert "price" not in fields
    assert "name" in fields or not rows  # name may map to product kind


def test_authorize_strips_unauthorized_price():
    products = [
        {
            "id": "1",
            "tenant_id": "newstore",
            "price": 50,
            "name": "A",
            "_factual_source": "catalog_cache",
            "_revalidated": False,
        }
    ]
    authorized, evidence = authorize_products_for_responder(
        products, tenant_id="newstore"
    )
    assert authorized
    assert "price" not in authorized[0]
    assert all(e.field != "price" for e in evidence)


def test_build_llm_budget_promotes_on_image():
    promote, signals = should_promote_to_complex(has_image=True)
    assert promote is True
    assert "image" in signals
    budget = build_llm_call_budget(execution_path="normal", risk_signals=signals)
    assert budget["complex_turn"] is True
    assert budget["max_calls"] >= 2


def test_build_llm_budget_normal_defaults():
    budget = build_llm_call_budget(execution_path="normal")
    assert budget["max_calls"] == 2
    assert budget["enforce"] is True
