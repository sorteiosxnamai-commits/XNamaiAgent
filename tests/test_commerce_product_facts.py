from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from app.commerce.mercos.normalizer import CommerceProduct
from app.commerce.mercos.provider import MercosCommerceProvider
from app.commerce.product_facts import normalize_product_detail
from app.consultative_agent import consult
from app.fact_authority import authorize_products_for_responder
from app.factual_validator import validate_factual_response
from app.models import AgentResult
from app.product_retrieval import revalidate_products
from tests.test_consultative_agent import interpretation, message, settings
from tests.test_factual_validator import _decision


@pytest.mark.asyncio
@pytest.mark.parametrize("age_hours", [0, 2, 24])
async def test_real_mercos_detail_survives_consultation_without_expired_values(monkeypatch, age_hours):
    import app.persona_repository as repository
    import app.prompt_compiler as compiler
    import app.commerce.tools as commerce
    monkeypatch.setattr(repository, "get_active_persona", lambda *a: NS(metadata={}))
    monkeypatch.setattr(compiler, "resolve_system_instructions", lambda **k: "safe")
    monkeypatch.setattr(commerce, "tool_schemas_for_model", lambda: commerce.TOOL_SCHEMAS)
    product = CommerceProduct(external_id="1", name="Fone Teste", price=25, stock=4,
        active=True, excluded=False, available=True,
        changed_at=(datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat())
    provider = MercosCommerceProvider(NS(), tenant_id="one", index=NS(
        search_products=lambda **k: [product], get_product=lambda **k: product))

    async def gateway(**kwargs):
        response = await kwargs["execute_tool"]("search_products", {"query": "fone"})
        assert len(response["products"]) == 1
        detail = response["products"][0]
        assert detail["id"] == "1" and detail["name"] == "Fone Teste"
        assert ("price" in detail) == (age_hours < 12)
        assert ("stock" in detail) == (age_hours < 1)
        assert ("available" in detail) == (age_hours < 1)
        assert detail["_factual_source"] == "local_database"
        return NS(text="Encontrei o Fone Teste.", limit_reached=False)

    result = await consult(message(), {}, interpretation(), settings=settings(),
                           execute=provider.execute, gateway=gateway)
    assert not result.safety_reason
    assert len(result.commercial_data["products"]) == 1


def test_unknown_detail_identity_is_not_replaced_with_search_identity():
    assert normalize_product_detail({"product": {"id": "2", "price": 3}}, "1") is None
    assert normalize_product_detail({"ok": False, "id": "1"}, "1") is None


@pytest.mark.asyncio
async def test_revalidation_drops_old_search_price_when_detail_omits_price():
    async def execute(name, args):
        assert name == "get_product"
        return {"ok": True, "source": "local_index", "product": {"id": "1", "name": "Fone"}}
    result, failed = await revalidate_products([{"id": "1", "name": "Fone", "price": 99}],
                                              interpretation(), execute)
    assert not failed and result[0]["id"] == "1"
    assert "price" not in result[0]


@pytest.mark.parametrize("freshness", [
    {"price_confirmed": False, "stock_confirmed": False},
    {"price": {"freshness": "unconfirmed"}, "stock": {"freshness": "unknown"}},
])
def test_expired_facts_cannot_be_authorized_by_a_revalidated_flag(freshness):
    product = {"id": "1", "name": "Fone", "price": 25, "promotional_price": 25,
        "stock": 4, "available": True, "freshness": freshness,
        "variants": [{"id": "v1", "price": 25, "stock": 4}],
        "commercial_availability": {"immediate_delivery_supported": True},
        "_revalidated": True, "_factual_source": "commerce_live"}
    result = AgentResult(reply_text="Fone custa R$ 25,00 e está em estoque.", intent="commerce",
        commercial_data={"products": [product]}, response_metadata={"domain": "commerce",
        "response_source": "consultative_openai", "used_commerce_provider": True})
    report = validate_factual_response(result, decision=_decision(result), mode="enforce")
    assert not report.valid
    assert {v.kind for v in report.violations} >= {"money", "stock"}
    authorized, evidence = authorize_products_for_responder([product])
    assert "price" not in authorized[0] and "stock" not in authorized[0]
    assert not any(e.field in {"price", "stock", "available"} for e in evidence)


def test_freshness_metadata_is_not_evidence_of_available_stock():
    result = AgentResult(reply_text="Fone está em estoque.", intent="commerce",
        commercial_data={"products": [{"id": "1", "name": "Fone", "freshness": {"stock_confirmed": True}}]},
        response_metadata={"domain": "commerce", "response_source": "consultative_openai", "used_commerce_provider": True})
    assert not validate_factual_response(result, decision=_decision(result), mode="enforce").valid
