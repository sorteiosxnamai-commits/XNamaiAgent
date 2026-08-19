"""Color synonym matching (azul ↔ blue) and soft recommendation filters."""

from app.models import SalesInterpretation
from app.product_retrieval import (
    ProductRetrievalCompiler,
    expand_color_aliases,
    hard_filter_products,
    preference_color_search_labels,
    preference_color_tokens,
    product_matches_color_tokens,
)


def _interp(**kwargs) -> SalesInterpretation:
    data = {
        "domain": "commerce",
        "goal": "recommend",
        "subject": {"product_type": "relógio", "brand": "Seiko"},
        "preferences": {"color": "azul"},
        "information_needed": ["catalog"],
        "references_previous_context": False,
        "enough_information_to_search": True,
        "ready_for_retrieval": True,
        "stop_clarification": False,
        "needs_clarification": False,
        "clarification_question": None,
        "confidence": 0.95,
    }
    data.update(kwargs)
    return SalesInterpretation.model_validate(data)


def test_azul_expands_to_blue_aliases():
    aliases = expand_color_aliases("azul")
    assert "azul" in aliases
    assert "blue" in aliases


def test_product_with_blue_matches_azul_preference():
    interpretation = _interp()
    tokens = preference_color_tokens(interpretation)
    assert tokens == ("azul",)
    product = {
        "id": "1",
        "name": "Relógio Seiko Presage Blue Dial",
        "brand": "Seiko",
        "color": "Blue",
        "price": 2500,
        "available": True,
    }
    assert product_matches_color_tokens(product, tokens) is True


def test_recommendation_keeps_brand_pool_for_llm_even_without_literal_azul():
    interpretation = _interp()
    products = [
        {
            "id": "blue-1",
            "name": "Seiko 5 Sports Blue",
            "brand": "Seiko",
            "price": 1800,
            "available": True,
            "available_in_store": True,
        },
        {
            "id": "black-1",
            "name": "Seiko 5 Sports Black",
            "brand": "Seiko",
            "price": 1800,
            "available": True,
            "available_in_store": True,
        },
    ]
    filtered = hard_filter_products(products, interpretation, mode="recommendation")
    ids = {product["id"] for product in filtered}
    assert "blue-1" in ids
    assert "black-1" in ids  # pool kept; LLM/reranker picks blue


def test_exact_mode_still_requires_color_match_with_aliases():
    interpretation = _interp(goal="find", subject={"product_type": "relógio", "brand": "Seiko", "model": "Presage"})
    products = [
        {
            "id": "blue-1",
            "name": "Seiko Presage Blue",
            "brand": "Seiko",
            "model": "Presage",
            "price": 3000,
            "available": True,
        },
        {
            "id": "black-1",
            "name": "Seiko Presage Black",
            "brand": "Seiko",
            "model": "Presage",
            "price": 3000,
            "available": True,
        },
    ]
    filtered = hard_filter_products(products, interpretation, mode="exact")
    assert [product["id"] for product in filtered] == ["blue-1"]


def test_compiler_adds_color_brand_probes_for_azul():
    plan = ProductRetrievalCompiler.compile(_interp())
    strategies = {request.strategy for request in plan.requests}
    assert "color_brand_probe" in strategies
    names = {str(request.name or "").lower() for request in plan.requests}
    assert "azul" in names or "blue" in names
    labels = preference_color_search_labels(_interp())
    assert "azul" in labels and "blue" in labels
