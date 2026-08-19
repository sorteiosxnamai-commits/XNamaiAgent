"""Etapa 4 — canonical catalog + hybrid ranking tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.catalog_index import (
    evaluate_hard_constraints,
    hybrid_rank_products,
    reject_unknown_rerank_ids,
    to_canonical_item,
    trigram_similarity,
)
from app.models import SalesInterpretation
from app.product_retrieval import hard_filter_products
from app.turn_understanding import (
    ExtractedEntities,
    ProductHardConstraints,
    ProductSoftPreferences,
    TurnUnderstanding,
    turn_understanding_to_sales,
)


def _product(**overrides):
    base = {
        "id": "1",
        "name": "Relógio Casio MTP Azul",
        "brand": "Casio",
        "model": "MTP",
        "reference": "MTP-1374",
        "ean": "7891234567890",
        "color": "Blue Dial",
        "current_price": 450.0,
        "stock": 3,
        "available": True,
        "available_in_store": True,
        "url": "https://example.com/p/1",
    }
    base.update(overrides)
    return base


def test_trigram_similarity_basic():
    assert trigram_similarity("casio", "casio") == 1.0
    assert trigram_similarity("azul", "azull") > 0.3
    assert trigram_similarity("seiko", "tissot") < 0.3


def test_to_canonical_item_shape():
    item = to_canonical_item(_product(), factual_source="tray_search")
    assert item is not None
    assert item.product_id == "1"
    assert item.brand == "Casio"
    assert item.price == 450.0
    assert "blue" in item.colors_normalized or "azul" in item.colors_normalized


def test_hard_constraints_exclude_over_budget():
    item = to_canonical_item(_product(current_price=900))
    assert item is not None
    ok, reason, _ = evaluate_hard_constraints(
        item,
        {"budget_max": 500, "brand": "Casio"},
        mode="recommendation",
    )
    assert ok is False
    assert reason == "over_budget"


def test_brand_exclusive_excludes_other_brands():
    interpretation = turn_understanding_to_sales(
        TurnUnderstanding(
            primary_intent="commerce_recommend",
            confidence=0.9,
            entities=ExtractedEntities(brand="Casio", category="relógio"),
            hard_constraints=ProductHardConstraints(
                brand="Casio",
                brand_exclusive=True,
                exact_only=True,
                budget_max=500,
            ),
            soft_preferences=ProductSoftPreferences(),
            answer_strategy="search_catalog",
        )
    )
    products = [
        _product(id="1", brand="Casio", current_price=400),
        _product(id="2", brand="Seiko", name="Seiko 5 Azul", current_price=400),
    ]
    filtered = hard_filter_products(products, interpretation, mode="recommendation")
    assert [p["id"] for p in filtered] == ["1"]


def test_hybrid_rank_prefers_color_and_budget_fit():
    interpretation = SalesInterpretation(
        domain="commerce",
        goal="recommend",
        subject={"brand": "Casio", "product_type": "relógio"},
        preferences={"color": "azul", "budget_max": 500, "attributes": ["azul"]},
        references_previous_context=False,
        needs_clarification=False,
        confidence=0.9,
        enough_information_to_search=True,
        ready_for_retrieval=True,
    )
    products = [
        _product(id="a", name="Casio Preto", color="Black", current_price=400),
        _product(id="b", name="Casio Azul", color="Blue Dial", current_price=420),
        _product(id="c", name="Casio Azul Premium", color="Blue", current_price=800),
    ]
    ranked = hybrid_rank_products(products, interpretation, mode="recommendation")
    ids = [p["id"] for p in ranked]
    assert "c" not in ids  # over budget hard-excluded
    assert ids[0] == "b"
    assert ranked[0]["_retrieval"]["score"] >= ranked[-1]["_retrieval"]["score"]
    assert "score_explanation" in ranked[0]["_retrieval"]


def test_reject_unknown_rerank_ids():
    ordered, invalid = reject_unknown_rerank_ids(
        ["1", "999", "2", "1", "invented"],
        {"1", "2", "3"},
        limit=15,
    )
    assert ordered == ["1", "2"]
    assert invalid == 2


@pytest.mark.asyncio
async def test_rerank_rejects_invented_ids(monkeypatch):
    from app import product_retrieval as pr

    products = [
        _product(id="10", name="Casio Azul"),
        _product(id="11", name="Casio Preto", color="Black"),
    ]
    interpretation = SalesInterpretation(
        domain="commerce",
        goal="recommend",
        subject={"brand": "Casio"},
        preferences={"color": "azul"},
        references_previous_context=False,
        needs_clarification=False,
        confidence=0.9,
    )

    class _Parsed:
        selected_product_ids = ["10", "invented-id", "11"]

    async def _fake_parse(**kwargs):
        return SimpleNamespace(parsed=_Parsed(), api_mode="responses")

    monkeypatch.setattr(
        pr,
        "get_settings",
        lambda: SimpleNamespace(
            openai_api_key="k",
            openai_model="gpt-4.1-mini",
            agent_rerank_selection_limit=15,
            agent_candidate_pool_limit=20,
        ),
    )
    monkeypatch.setattr(
        "app.openai_gateway.parse_structured_output",
        _fake_parse,
    )
    ranked = await pr.rerank_products(products, interpretation)
    assert [p["id"] for p in ranked] == ["10", "11"]
