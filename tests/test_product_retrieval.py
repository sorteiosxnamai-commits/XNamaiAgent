from types import SimpleNamespace

import pytest

from app.models import IncomingMessage, SalesInterpretation
from app.product_retrieval import (
    ProductRerankSelection,
    ProductRetrievalCompiler,
    hard_filter_products,
    product_availability_state,
    rerank_products,
)
from openai_test_utils import install_fake_openai_client


def _interpretation(
    *,
    goal: str = "recommend",
    product_type: str | None = "relógio",
    brand: str | None = None,
    model: str | None = None,
    reference: str | None = None,
    preferences: dict | None = None,
    ready: bool = True,
) -> SalesInterpretation:
    return SalesInterpretation(
        domain="commerce",
        goal=goal,
        subject={
            "product_type": product_type,
            "brand": brand,
            "model": model,
            "reference": reference,
        },
        preferences=preferences or {},
        information_needed=["catalog"],
        references_previous_context=False,
        enough_information_to_search=True,
        ready_for_retrieval=ready,
        stop_clarification=False,
        needs_clarification=False,
        clarification_question=None,
        confidence=0.98,
    )


def test_compiler_uses_product_type_as_name_and_never_as_brand():
    plan = ProductRetrievalCompiler.compile(
        _interpretation(preferences={"style": "social"})
    )

    assert plan.mode == "recommendation"
    assert plan.requests[0].name == "relógio"
    assert plan.requests[0].brand is None


def test_compiler_preserves_only_explicit_brand():
    plan = ProductRetrievalCompiler.compile(_interpretation(brand="Tissot"))

    assert plan.requests[0].name == "relógio"
    assert plan.requests[0].brand == "Tissot"
    assert all(request.brand != "relógio" for request in plan.requests)


def test_semantic_style_never_becomes_name_or_brand():
    plan = ProductRetrievalCompiler.compile(
        _interpretation(preferences={"style": "esportivo"})
    )

    arguments = plan.requests[0].tool_arguments()
    assert arguments["name"] == "relógio"
    assert "brand" not in arguments
    assert "esportivo" not in arguments.values()


def test_specific_query_keeps_brand_and_model_separate_without_combined_name():
    plan = ProductRetrievalCompiler.compile(
        _interpretation(
            goal="find",
            product_type="relógio",
            brand="Hamilton",
            model="Murph",
        )
    )

    assert plan.mode == "exact"
    strategies = [request.strategy for request in plan.requests]
    assert strategies[0] == "token_and_search"
    assert "exact_model_with_brand" in strategies
    assert "exact_query_full" in strategies
    assert "brand_candidates" in strategies
    model_probe = next(
        request
        for request in plan.requests
        if request.strategy == "exact_model_with_brand"
    )
    assert model_probe.name == "Murph"
    assert model_probe.brand == "Hamilton"
    assert all(request.name != "Hamilton Murph" for request in plan.requests)


def test_long_model_title_matches_short_tray_model_field():
    from app.product_retrieval import (
        exact_specific_product_matches,
        required_model_tokens,
        significant_model_tokens,
    )

    interpretation = _interpretation(
        goal="find",
        brand="Christopher Ward",
        model="C63 Sealander Automático Rosa",
    )
    products = [
        {
            "id": "9991",
            "brand": "Christopher Ward",
            "model": "Sealander",
            "name": (
                "Relógio Christopher Ward C63 Sealander Automático Rosa "
                "C63-36ADA4-S00P0-B0 36 mm"
            ),
            "reference": "C63-36ADA4-S00P0-B0",
        },
        {
            "id": "9992",
            "brand": "Christopher Ward",
            "model": "C60",
            "name": "Relógio Christopher Ward C60 Trident Pro 600",
        },
    ]

    assert significant_model_tokens(interpretation.subject.model) == (
        "c63",
        "sealander",
        "rosa",
    )
    assert required_model_tokens(interpretation.subject.model) == (
        "c63",
        "sealander",
    )
    matches = exact_specific_product_matches(products, interpretation)
    assert [product["id"] for product in matches] == ["9991"]

    plan = ProductRetrievalCompiler.compile(interpretation)
    strategies = [request.strategy for request in plan.requests]
    assert "exact_query_full" in strategies
    assert "exact_model_code" in strategies
    assert any(
        request.name
        and request.name.startswith("Relógio Christopher Ward")
        and "Rosa" in request.name
        for request in plan.requests
        if request.name
    )


def test_vision_color_phrase_is_not_treated_as_product_reference():
    from app.product_retrieval import (
        ProductRetrievalCompiler,
        exact_specific_product_matches,
        effective_product_reference,
        hard_filter_products,
        is_plausible_product_reference,
        required_model_tokens,
    )

    assert is_plausible_product_reference("rosa claro (mostrador)") is False
    assert effective_product_reference("rosa claro (mostrador)") is None
    assert is_plausible_product_reference("C63-36ADA4-S00P0-B0") is True

    interpretation = _interpretation(
        goal="find",
        brand="Christopher Ward",
        model="Sealander Automatic rosa claro (mostrador)",
        reference="rosa claro (mostrador)",
        preferences={"color": "rosa claro (mostrador)"},
    )
    products = [
        {
            "id": "8975",
            "brand": "Christopher Ward",
            "model": "Sealander",
            "name": (
                "Relógio Christopher Ward C63 Sealander Automático Rosa "
                "C63-36ADA4-S00P0-B0 36 mm"
            ),
            "reference": "C63-36ADA4-S00P0-B0",
        },
        {
            "id": "8977",
            "brand": "Christopher Ward",
            "model": "Sealander",
            "name": "Relógio Christopher Ward C63 Sealander Automático Azul",
            "reference": "C63-36ADA4-S00B0-B0",
        },
    ]

    assert "mostrador" not in required_model_tokens(interpretation.subject.model)
    assert "claro" not in required_model_tokens(interpretation.subject.model)
    matches = exact_specific_product_matches(products, interpretation)
    assert "8975" in [product["id"] for product in matches]
    filtered = hard_filter_products(products, interpretation, mode="exact")
    assert [product["id"] for product in filtered] == ["8975", "8977"] or "8975" in [
        product["id"] for product in filtered
    ]
    plan = ProductRetrievalCompiler.compile(interpretation)
    assert all(request.reference is None for request in plan.requests)


@pytest.mark.asyncio
async def test_color_preference_narrows_ambiguous_sealander_matches():
    from app.product_retrieval import match_specific_products

    interpretation = _interpretation(
        goal="find",
        brand="Christopher Ward",
        model="Sealander Automatic",
        preferences={"color": "rosa"},
    )
    products = [
        {
            "id": "8975",
            "brand": "Christopher Ward",
            "model": "Sealander",
            "name": "Relógio Christopher Ward C63 Sealander Automático Rosa",
            "reference": "C63-36ADA4-S00P0-B0",
        },
        {
            "id": "8977",
            "brand": "Christopher Ward",
            "model": "Sealander",
            "name": "Relógio Christopher Ward C63 Sealander GMT Automático Azul 39 mm",
            "reference": "C63-39AGM3-S00B4-B1",
        },
    ]

    resolution = await match_specific_products(products, interpretation)
    assert resolution.status == "exact"
    assert [product["id"] for product in resolution.products] == ["8975"]


def test_catalog_match_tokens_drops_strap_accessories_for_beaubleu():
    from app.product_retrieval import catalog_match_tokens, preference_color_tokens

    interpretation = _interpretation(
        goal="find",
        brand="Beaubleu",
        model="Branco Prata Pulseira Bege",
        preferences={"color": "branco prata pulseira bege"},
    )
    colors = preference_color_tokens(interpretation)
    tokens = catalog_match_tokens(interpretation)
    assert colors == ("branco",)
    assert "beaubleu" in tokens
    assert "branco" in tokens
    assert "pulseira" not in tokens
    assert "bege" not in tokens
    assert "prata" not in tokens


def test_catalog_match_tokens_keeps_model_line_without_strap():
    from app.product_retrieval import catalog_match_tokens, preference_color_tokens

    interpretation = _interpretation(
        goal="find",
        brand="Beaubleu",
        model="Ecce Lys Automático Branco Prata Pulseira Bege",
        preferences={"color": "branco"},
    )
    assert preference_color_tokens(interpretation) == ("branco",)
    tokens = catalog_match_tokens(interpretation)
    assert "ecce" in tokens
    assert "lys" in tokens
    assert "bege" not in tokens
    assert "pulseira" not in tokens


@pytest.mark.asyncio
async def test_color_mismatch_does_not_substitute_other_automatic_colors(monkeypatch):
    from app.product_retrieval import (
        ProductRetrievalCompiler,
        catalog_match_tokens,
        exact_specific_product_matches,
        infer_family_codes_from_candidates,
        match_specific_products,
        normalize_pt_catalog_query,
    )

    assert "Automático" in normalize_pt_catalog_query("Sealander Automatic")
    monkeypatch.setattr(
        "app.product_retrieval.get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )

    interpretation = _interpretation(
        goal="find",
        brand="Christopher Ward",
        model="Sealander Automatic rosa claro",
        preferences={"color": "rosa claro"},
    )
    products = [
        {
            "id": "8975",
            "brand": "Christopher Ward",
            "model": "Sealander",
            "name": "Relógio Christopher Ward C63 Sealander GMT Automático Azul 39 mm",
        },
        {
            "id": "8977",
            "brand": "Christopher Ward",
            "model": "Sealander",
            "name": "Relógio Christopher Ward C63 Sealander GMT Automático Verde 39 mm",
        },
        {
            "id": "auto-blue",
            "brand": "Christopher Ward",
            "model": "Sealander",
            "name": "Relógio Christopher Ward C63 Sealander Automático Azul 36 mm",
        },
    ]
    assert exact_specific_product_matches(products, interpretation) == []
    resolution = await match_specific_products(products, interpretation)
    assert resolution.status == "none"
    assert resolution.products == ()
    assert infer_family_codes_from_candidates(products, interpretation) == ("C63",)
    tokens = catalog_match_tokens(interpretation)
    assert "sealander" in tokens
    assert "rosa" in tokens
    assert "claro" not in tokens

    plan = ProductRetrievalCompiler.compile(interpretation)
    strategies = [request.strategy for request in plan.requests]
    assert "token_and_search" in strategies
    assert "exact_color_core" in strategies or "exact_color_automatic" in strategies
    assert "category_candidates" not in strategies
    assert "brand_candidates" in strategies
    assert plan.discovery_max_pages >= 5
    token_request = next(
        request for request in plan.requests if request.strategy == "token_and_search"
    )
    assert "rosa" in token_request.tokens
    assert "claro" not in token_request.tokens
    assert any(
        request.name and "rosa" in request.name.casefold()
        for request in plan.requests
        if request.name
    )
    assert any(
        request.name and "Automático" in request.name
        for request in plan.requests
        if request.name
    )
    assert not any(
        request.name and "claro" in request.name.casefold()
        for request in plan.requests
        if request.name
    )
    assert not any(
        request.name and request.name.casefold().count("rosa") > 1
        for request in plan.requests
        if request.name
    )
    probe_count = sum(
        1
        for request in plan.requests
        if request.strategy not in {"brand_candidates", "category_candidates"}
    )
    assert probe_count <= 8


@pytest.mark.asyncio
async def test_local_color_aliases_match_pink_dial_for_rosa(monkeypatch):
    """'rosa' matches catalog 'Pink Dial' without needing GPT."""
    from app.product_retrieval import match_specific_products

    interpretation = _interpretation(
        goal="find",
        brand="Christopher Ward",
        model="Sealander Automatic",
        preferences={"color": "rosa"},
    )
    products = [
        {
            "id": "12295",
            "brand": "Christopher Ward",
            "name": "Relógio Christopher Ward C63 Sealander Automático Kingfisher",
        },
        {
            "id": "pink-sku",
            "brand": "Christopher Ward",
            "name": "Relógio Christopher Ward C63 Sealander Automático Pink Dial 36 mm",
        },
    ]

    monkeypatch.setattr(
        "app.product_retrieval.get_settings",
        lambda: SimpleNamespace(openai_api_key="sk-test", openai_model="gpt-4.1-mini"),
    )

    resolution = await match_specific_products(products, interpretation)
    assert resolution.status == "exact"
    assert resolution.match_source == "exact"
    assert resolution.products[0]["id"] == "pink-sku"

@pytest.mark.asyncio
async def test_color_mismatch_soft_confirm_does_not_list_wrong_colors():
    from app.product_retrieval import soft_confirm_candidates

    interpretation = _interpretation(
        goal="find",
        brand="Christopher Ward",
        model="Sealander Automatic",
        preferences={"color": "rosa"},
    )
    products = [
        {
            "id": "auto-blue",
            "brand": "Christopher Ward",
            "model": "Sealander",
            "name": "Relógio Christopher Ward C63 Sealander Automático Azul 36 mm",
        },
        {
            "id": "kingfisher",
            "brand": "Christopher Ward",
            "model": "Sealander",
            "name": (
                "Relógio Christopher Ward C63 Sealander Automático Kingfisher "
                "C63-39ADA3S00B10-B0"
            ),
        },
        {
            "id": "8975",
            "brand": "Christopher Ward",
            "model": "Sealander",
            "name": "Relógio Christopher Ward C63 Sealander GMT Automático Azul 39 mm",
        },
    ]
    soft = soft_confirm_candidates(products, interpretation)
    assert soft == []


def test_infer_family_codes_prefers_c63_not_reference_fragments():
    from app.product_retrieval import infer_family_codes_from_candidates

    interpretation = _interpretation(
        goal="find",
        brand="Christopher Ward",
        model="Sealander Automatic",
        preferences={"color": "rosa"},
    )
    products = [
        {
            "id": "1",
            "brand": "Christopher Ward",
            "name": (
                "Relógio Christopher Ward C63 Sealander Automático Kingfisher "
                "C63-39ADA3S00B10-B0"
            ),
        },
        {
            "id": "2",
            "brand": "Christopher Ward",
            "name": "Relógio Christopher Ward C63 Sealander GMT Automático Azul 39AGM3",
        },
    ]
    assert infer_family_codes_from_candidates(products, interpretation) == ("C63",)


def test_compact_product_lines_omit_long_payment_dump():
    from app.commerce_router import _product_lines

    products = [
        {
            "id": "1",
            "name": "Relógio Christopher Ward C63 Sealander Automático Rosa",
            "reference": "C63-36ADA4-S00P0-B0",
            "current_price": 13004.99,
            "payment_option_details": {
                "pix": {"value": 13004.99},
                "installments": [
                    {"count": 12, "value": 1275, "interest": False},
                    {"count": 21, "value": 829.84, "interest": True},
                ],
            },
        }
    ]
    compact = _product_lines(products, compact=True)[0]
    full = _product_lines(products, compact=False)[0]
    assert "Preço:" in compact
    assert "Condições comerciais" not in compact
    assert "Condições comerciais" in full


@pytest.mark.asyncio
async def test_keyword_match_finds_pink_sealander_beyond_first_twenty():
    from app.product_retrieval import (
        score_catalog_candidates,
        match_specific_products,
        ProductRetrievalCompiler,
    )

    interpretation = _interpretation(
        goal="find",
        brand="Christopher Ward",
        model="Sealander Automatic",
        preferences={"color": "rosa claro"},
    )
    filler = [
        {
            "id": str(index),
            "brand": "Christopher Ward",
            "model": "Sealander",
            "name": f"Relógio Christopher Ward C63 Sealander GMT Automático Azul {index}",
        }
        for index in range(30)
    ]
    pink = {
        "id": "pink",
        "brand": "Christopher Ward",
        "model": "Sealander",
        "name": (
            "Relógio Christopher Ward C63 Sealander Automático Rosa "
            "C63-36ADA4-S00P0-B0 36 mm"
        ),
        "reference": "C63-36ADA4-S00P0-B0",
    }
    products = [*filler, pink]
    hits = score_catalog_candidates(
        products,
        interpretation,
        require_color=True,
    )
    assert [product["id"] for product in hits] == ["pink"]

    resolution = await match_specific_products(products, interpretation)
    assert resolution.status == "exact"
    assert [product["id"] for product in resolution.products] == ["pink"]

    plan = ProductRetrievalCompiler.compile(interpretation)
    probe_count = sum(
        1
        for request in plan.requests
        if request.strategy not in {"brand_candidates", "category_candidates"}
    )
    assert probe_count <= 8
    assert "token_and_search" in [request.strategy for request in plan.requests]
    # Without C63 in the Vision model, probes stay generic; matching is local.
    assert any(
        request.name and "Sealander" in request.name and "Rosa" in request.name
        for request in plan.requests
        if request.name
    )


@pytest.mark.asyncio
async def test_score_rejects_single_token_accessory_match():
    from app.product_retrieval import score_catalog_candidates

    interpretation = _interpretation(
        goal="find",
        brand="Rolex",
        model="Explorer",
    )
    products = [
        {
            "id": "strap",
            "brand": "Rolex",
            "model": "",
            "name": "Pulseira Explorer Strap Couro",
        },
        {
            "id": "watch",
            "brand": "Rolex",
            "model": "Explorer",
            "name": "Relógio Rolex Explorer 36 mm",
        },
    ]
    hits = score_catalog_candidates(products, interpretation, require_color=False)
    assert [product["id"] for product in hits] == ["watch"]


def test_exact_progress_matches_requires_requested_color():
    from app.product_retrieval import exact_progress_matches

    interpretation = _interpretation(
        goal="find",
        brand="Christopher Ward",
        model="Sealander Automatic",
        preferences={"color": "rosa claro"},
    )
    pink = {
        "id": "pink",
        "brand": "Christopher Ward",
        "model": "Sealander",
        "name": "Relógio Christopher Ward C63 Sealander Automático Rosa 36 mm",
    }
    blue_gmt = {
        "id": "8975",
        "brand": "Christopher Ward",
        "model": "Sealander",
        "name": "Relógio Christopher Ward C63 Sealander GMT Automático Azul 39 mm",
    }
    assert [product["id"] for product in exact_progress_matches([pink, blue_gmt], interpretation)] == [
        "pink"
    ]
    assert exact_progress_matches([blue_gmt], interpretation) == []


def test_certina_title_without_relogio_prefix_still_matches():
    from app.product_retrieval import (
        exact_specific_product_matches,
        extract_model_codes,
        required_model_tokens,
    )

    interpretation = _interpretation(
        goal="find",
        brand="Certina",
        model="DS Super PH2000M Automático Branco Titânio",
    )
    products = [
        {
            "id": "certina-1",
            "brand": "Certina",
            "model": "DS Super PH2000M",
            "name": (
                "Relógio Certina DS Super PH2000M Automático Branco Titânio "
                "C050.607.44.011.02"
            ),
            "reference": "C050.607.44.011.02",
        }
    ]

    assert "PH2000M" in extract_model_codes(interpretation.subject.model)
    assert required_model_tokens(interpretation.subject.model) == (
        "ds",
        "super",
        "ph2000m",
    )
    matches = exact_specific_product_matches(products, interpretation)
    assert [product["id"] for product in matches] == ["certina-1"]

    plan = ProductRetrievalCompiler.compile(interpretation)
    names = [request.name for request in plan.requests if request.name]
    assert any(
        name and name.startswith("Relógio Certina") for name in names
    )
    assert "PH2000M" in names


def test_budget_is_applied_after_retrieval_using_effective_price():
    products = [
        {"id": "A", "name": "A", "current_price": 3000},
        {"id": "B", "name": "B", "current_price": 5500},
        {"id": "C", "name": "C", "price": 4800, "promotional_price": 4500},
    ]

    selected = hard_filter_products(
        products,
        _interpretation(preferences={"budget_max": 5000}),
        mode="recommendation",
    )

    assert [product["id"] for product in selected] == ["A", "C"]


@pytest.mark.asyncio
async def test_candidate_pool_is_twenty_and_customer_result_is_three(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []

    async def fake_execute(name, arguments):
        calls.append((name, arguments))
        if name == "list_categories":
            return {"categories": []}
        if name == "get_product":
            product_id = arguments["product_id"]
            return {"id": product_id, "name": f"Relógio {product_id}", "current_price": 1000 + int(product_id)}
        return {
            "products": [
                {"id": str(index), "name": f"Relógio {index}", "current_price": 1000 + index}
                for index in range(20)
            ]
        }

    monkeypatch.setattr(sales_agent, "execute_tool", fake_execute)
    monkeypatch.setattr(
        "app.product_retrieval.get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )
    result = await sales_agent._execute_compiled_product_retrieval(_interpretation())

    search_calls = [call for call in calls if call[0] == "search_products"]
    assert search_calls == [("search_products", {"name": "relógio", "available": True, "available_in_store": True, "limit": 20, "page": 1})]
    assert len(result.commercial_data["products"]) == 3


@pytest.mark.asyncio
async def test_reranker_discards_ids_outside_candidate_set(monkeypatch):
    import app.product_retrieval as retrieval

    class FakeCompletions:
        async def parse(self, **kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(
                        parsed=ProductRerankSelection(
                            selected_product_ids=["invented", "2"]
                        )
                    )
                )]
            )

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(
        retrieval,
        "get_settings",
        lambda: SimpleNamespace(openai_api_key="key", openai_model="gpt-4.1-mini"),
    )
    install_fake_openai_client(monkeypatch, FakeClient)

    selected = await rerank_products(
        [{"id": "1", "name": "A"}, {"id": "2", "name": "B"}],
        _interpretation(preferences={"style": "social"}),
    )

    assert [product["id"] for product in selected] == ["2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("preference", [{}, {"style": "social"}])
async def test_ready_broad_request_retrieves_without_new_clarification(
    monkeypatch,
    preference,
):
    import app.sales_agent as sales_agent

    calls = []

    async def fake_execute(name, arguments):
        calls.append((name, arguments))
        if name == "list_categories":
            return {"categories": []}
        if name == "get_product":
            return {"id": arguments["product_id"], "name": "Modelo atualizado", "current_price": 3000}
        return {
            "products": [
                {"id": "1", "name": "Modelo Classic", "current_price": 3000},
                {"id": "2", "name": "Modelo Urban", "current_price": 4000},
            ]
        }

    settings = SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini")
    monkeypatch.setattr(sales_agent, "get_settings", lambda: settings)
    monkeypatch.setattr("app.product_retrieval.get_settings", lambda: settings)
    monkeypatch.setattr(sales_agent, "execute_tool", fake_execute)
    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="continuação comercial"),
        {"primary_intent": "commerce"},
        {},
        _interpretation(preferences=preference, ready=True),
        recent_turns=[],
    )

    search_call = next(call for call in calls if call[0] == "search_products")
    assert search_call[1]["name"] == "relógio"
    assert "brand" not in search_call[1]
    assert result.safety_reason != "commerce_clarification"
    assert result.safety_reason != "product_not_found"
    assert len(result.commercial_data["products"]) == 2


@pytest.mark.asyncio
async def test_exact_missing_product_keeps_product_not_found(monkeypatch):
    import app.sales_agent as sales_agent

    async def fake_execute(name, arguments):
        return {"products": []}

    monkeypatch.setattr(sales_agent, "execute_tool", fake_execute)
    result = await sales_agent._execute_compiled_product_retrieval(
        _interpretation(
            goal="find",
            product_type=None,
            brand="Tissot",
            model="Seastar XYZ",
            ready=False,
        )
    )

    assert result.safety_reason == "product_not_found"
    assert "esse produto" in result.reply_text


@pytest.mark.parametrize("upon_request_value", ["1", 1, True])
def test_product_upon_request_is_unavailable_regardless_of_available_flags(upon_request_value):
    product = {
        "id": "123",
        "name": "Relógio sob consulta",
        "available": 1,
        "available_in_store": 1,
        "upon_request": upon_request_value,
    }

    state = product_availability_state(product)

    assert state == "unavailable"


def test_product_upon_request_in_settings_is_unavailable():
    product = {
        "id": "123",
        "name": "Relógio sob consulta",
        "available": 1,
        "available_in_store": 1,
        "ProductSettings": {
            "upon_request": True,
        },
    }

    state = product_availability_state(product)

    assert state == "unavailable"


@pytest.mark.parametrize("upon_request_value", ["0", 0, False, None])
def test_product_with_upon_request_false_or_absent_respects_availability_flags(upon_request_value):
    product = {
        "id": "123",
        "name": "Relógio disponível",
        "available": 1,
        "upon_request": upon_request_value,
    }

    state = product_availability_state(product)

    assert state == "available"


def test_product_with_zero_stock_and_available_flag_is_still_available():
    product = {
        "id": "123",
        "name": "Relógio por encomenda",
        "available": 1,
        "available_in_store": 0,
        "stock": 0,
        "upon_request": 0,
    }

    state = product_availability_state(product)

    assert state == "available"


@pytest.mark.asyncio
async def test_rerank_products_filters_out_upon_request_items(monkeypatch):
    import app.product_retrieval as retrieval

    class FakeCompletions:
        async def parse(self, **kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(
                        parsed=ProductRerankSelection(
                            selected_product_ids=["1", "2", "3"]
                        )
                    )
                )]
            )

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(
        retrieval,
        "get_settings",
        lambda: SimpleNamespace(openai_api_key="key", openai_model="gpt-4.1-mini"),
    )
    install_fake_openai_client(monkeypatch, FakeClient)

    products = [
        {"id": "1", "name": "Disponível A", "available": 1},
        {"id": "2", "name": "Sob consulta", "upon_request": 1, "available": 1},
        {"id": "3", "name": "Disponível B", "available": 1},
    ]

    selected = await rerank_products(
        products,
        _interpretation(preferences={"style": "social"}),
    )

    selected_ids = [product["id"] for product in selected]
    assert "2" not in selected_ids
    assert all(pid in ["1", "3"] for pid in selected_ids)
