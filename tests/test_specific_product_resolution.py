from types import SimpleNamespace

import pytest

from app.commerce_context import (
    CommerceConversationState,
    evolve_commerce_state,
    resolve_commerce_reference,
)
from app.models import AgentResult, IncomingMessage, SalesInterpretation
from app.product_retrieval import (
    ProductMatchError,
    ProductMatchSelection,
    match_specific_products,
    prefilter_specific_candidates,
    product_availability_state,
)
from openai_test_utils import install_fake_openai_client


def _interpretation(
    *,
    brand: str | None,
    model: str,
    product_type: str | None = None,
    reference_type: str | None = None,
    preferences: dict | None = None,
) -> SalesInterpretation:
    return SalesInterpretation(
        domain="commerce",
        goal="find",
        subject={
            "product_type": product_type,
            "brand": brand,
            "model": model,
        },
        preferences=preferences or {},
        information_needed=["catalog"],
        references_previous_context=reference_type is not None,
        needs_clarification=False,
        reference_type=reference_type,
        confidence=0.99,
    )


def _settings(*, api_key: str = "key") -> SimpleNamespace:
    return SimpleNamespace(
        openai_api_key=api_key,
        openai_model="gpt-4.1-mini",
    )


def _mock_matcher(
    monkeypatch,
    selected_ids: list[str],
    *,
    match_status: str | None = None,
    best_candidate_id: str | None = None,
):
    import app.product_retrieval as retrieval

    resolved_status = match_status or (
        "none" if not selected_ids
        else "exact" if len(selected_ids) == 1
        else "ambiguous"
    )
    resolved_best_id = (
        best_candidate_id
        if best_candidate_id is not None
        else selected_ids[0] if resolved_status == "exact" and selected_ids else None
    )

    class FakeCompletions:
        async def parse(self, **kwargs):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            parsed=ProductMatchSelection(
                                match_status=resolved_status,
                                candidate_ids=selected_ids,
                                best_candidate_id=resolved_best_id,
                                confidence=0.9,
                            )
                        )
                    )
                ]
            )

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(retrieval, "get_settings", lambda: _settings())
    install_fake_openai_client(monkeypatch, FakeClient)


@pytest.mark.asyncio
async def test_brand_candidates_resolve_partial_specific_model(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []
    candidates = [
        {"id": "1", "name": "Longines HydroConquest", "brand": "Longines"},
        {"id": "2", "name": "Longines Spirit Zulu Time", "brand": "Longines"},
        {"id": "3", "name": "Longines Conquest", "brand": "Longines"},
    ]

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "search_products":
            if arguments.get("tokens"):
                return {"products": candidates}
            if (
                arguments.get("brand") == "Longines"
                and "name" not in arguments
                and "query" not in arguments
                and "tokens" not in arguments
            ):
                return {"products": candidates}
            return {"products": []}
        if tool == "get_product":
            return {
                **candidates[1],
                "available": True,
                "available_in_store": True,
            }
        raise AssertionError(tool)

    _mock_matcher(monkeypatch, ["2"])
    monkeypatch.setattr(sales_agent, "execute_tool", execute)

    result = await sales_agent._execute_compiled_product_retrieval(
        _interpretation(brand="Longines", model="Zulu")
    )

    search_calls = [arguments for tool, arguments in calls if tool == "search_products"]
    assert any(arguments.get("tokens") for arguments in search_calls)
    assert {
        "name": "Zulu",
        "brand": "Longines",
        "limit": 20,
        "page": 1,
    } in search_calls
    assert {"query": "Longines Zulu", "limit": 20, "page": 1} in search_calls
    assert result.safety_reason != "product_not_found"
    assert [product["id"] for product in result.commercial_data["products"]] == ["2"]


@pytest.mark.asyncio
async def test_exact_structured_model_does_not_need_brand_fallback(monkeypatch):
    import app.sales_agent as sales_agent
    import app.product_retrieval as retrieval

    calls = []
    product = {
        "id": "10",
        "name": "Longines Spirit Zulu Time",
        "brand": "Longines",
        "model": "Spirit Zulu Time",
        "available": True,
    }

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "search_products":
            return {"products": [product]}
        if tool == "get_product":
            return product
        raise AssertionError(tool)

    async def forbid_parse(**kwargs):
        raise AssertionError("exact model match must not need OpenAI matcher")

    monkeypatch.setattr(
        "app.openai_gateway.parse_structured_output",
        forbid_parse,
    )
    monkeypatch.setattr(retrieval, "get_settings", lambda: _settings())
    monkeypatch.setattr(sales_agent, "execute_tool", execute)

    result = await sales_agent._execute_compiled_product_retrieval(
        _interpretation(brand="Longines", model="Spirit Zulu Time")
    )

    search_calls = [call for call in calls if call[0] == "search_products"]
    assert any(
        call[1].get("name") == "Spirit Zulu Time"
        and call[1].get("brand") == "Longines"
        for call in search_calls
    )
    # Exact structured model must resolve from Tier-1 probes, not brand paging.
    assert not any(
        call[1].get("brand")
        and not call[1].get("name")
        and not call[1].get("tokens")
        and not call[1].get("query")
        for call in search_calls
    )
    assert result.safety_reason != "product_not_found"
    assert result.commercial_data["products"][0]["id"] == "10"


@pytest.mark.asyncio
async def test_exact_model_filters_unrelated_same_brand_candidates(monkeypatch):
    import app.product_retrieval as retrieval

    products = [
        {"id": "1", "name": "Tissot Seastar", "brand": "Tissot", "model": "Seastar"},
        {"id": "2", "name": "Tissot Tradition", "brand": "Tissot", "model": "Tradition"},
        {"id": "3", "name": "Tissot PRX", "brand": "Tissot", "model": "PRX"},
    ]
    monkeypatch.setattr(retrieval, "get_settings", lambda: _settings(api_key=""))

    selected = await match_specific_products(
        products,
        _interpretation(brand="Tissot", model="Seastar"),
    )

    assert selected.status == "exact"
    assert [product["id"] for product in selected.products] == ["1"]


@pytest.mark.asyncio
async def test_brand_candidates_without_semantic_match_return_not_found(monkeypatch):
    import app.sales_agent as sales_agent

    async def execute(tool, arguments):
        if tool == "search_products":
            if (
                arguments.get("brand")
                and "name" not in arguments
                and "query" not in arguments
            ):
                return {
                    "products": [
                        {"id": "1", "name": "Longines Conquest", "brand": "Longines"},
                        {"id": "2", "name": "Longines Master Collection", "brand": "Longines"},
                    ]
                }
            return {"products": []}
        raise AssertionError(tool)

    _mock_matcher(monkeypatch, [])
    monkeypatch.setattr(sales_agent, "execute_tool", execute)

    result = await sales_agent._execute_compiled_product_retrieval(
        _interpretation(brand="Longines", model="ProdutoQueNaoExiste")
    )

    assert result.safety_reason == "exact_product_ambiguous_brand"
    assert "Longines" in result.reply_text


@pytest.mark.asyncio
async def test_found_but_unavailable_is_not_product_not_found(monkeypatch):
    import app.sales_agent as sales_agent

    product = {
        "id": "20",
        "name": "Tissot Seastar",
        "brand": "Tissot",
        "model": "Seastar",
        "available": False,
        "available_in_store": False,
        "available_for_purchase": False,
    }

    async def execute(tool, arguments):
        if tool == "search_products":
            return {"products": [product]}
        if tool == "get_product":
            return product
        raise AssertionError(tool)

    monkeypatch.setattr(sales_agent, "execute_tool", execute)

    result = await sales_agent._execute_compiled_product_retrieval(
        _interpretation(brand="Tissot", model="Seastar")
    )

    assert result.safety_reason == "product_unavailable"
    assert result.response_metadata["product_resolution_state"] == "found_unavailable"
    assert result.commercial_data["availability_state"] == "unavailable"


@pytest.mark.asyncio
async def test_contextual_product_id_avoids_text_search(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_product":
            return {
                "id": "303",
                "name": "Modelo contextual",
                "available": True,
            }
        raise AssertionError(f"text search must not run: {tool}")

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", lambda: _settings(api_key=""))
    state = CommerceConversationState(
        active_domain="commerce",
        active_product={
            "product_id": "303",
            "name": "Modelo contextual",
            "reference": "REF-303",
        },
    )

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="continuação contextual"),
        {"primary_intent": "commerce"},
        {},
        _interpretation(
            brand=None,
            model="Modelo contextual",
            reference_type="current_product",
        ),
        commerce_state=state,
    )

    assert calls == [("get_product", {"product_id": "303"})]
    assert result.safety_reason != "product_not_found"


@pytest.mark.asyncio
async def test_product_matcher_discards_invented_id(monkeypatch):
    products = [
        {"id": "1", "name": "Modelo Alpha", "brand": "Marca"},
        {"id": "2", "name": "Modelo Beta", "brand": "Marca"},
    ]
    _mock_matcher(
        monkeypatch,
        ["invented", "2"],
        match_status="exact",
        best_candidate_id="2",
    )

    selected = await match_specific_products(
        products,
        _interpretation(brand="Marca", model="Beta aproximado"),
    )

    assert selected.status == "exact"
    assert [product["id"] for product in selected.products] == ["2"]


def test_availability_uses_commercial_flags_not_stock_alone():
    assert product_availability_state({
        "stock": 10,
        "available": False,
        "available_in_store": False,
        "available_for_purchase": False,
    }) == "unavailable"
    assert product_availability_state({
        "stock": 0,
        "ProductSettings": {"upon_request": True},
    }) == "unavailable"


@pytest.mark.asyncio
async def test_matcher_technical_failure_is_not_reported_as_not_found(monkeypatch):
    import app.sales_agent as sales_agent

    async def execute(tool, arguments):
        if tool == "search_products":
            return {
                "products": [
                    {"id": "1", "name": "Candidato real", "brand": "Marca"}
                ]
            }
        raise AssertionError(tool)

    async def failed_matcher(products, interpretation):
        raise ProductMatchError("failed")

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "match_specific_products", failed_matcher)

    result = await sales_agent._execute_compiled_product_retrieval(
        _interpretation(brand="Marca", model="Modelo específico")
    )

    assert result.safety_reason == "product_match_failed"
    assert result.safety_reason != "product_not_found"


@pytest.mark.asyncio
async def test_color_harvest_finds_rosa_outside_brand_first_pages(monkeypatch):
    """Brand pages miss Rosa; name=Rosa&brand paging must still surface it."""
    import app.sales_agent as sales_agent

    rosa = {
        "id": "rosa",
        "name": (
            "Relógio Christopher Ward C63 Sealander Automático Rosa "
            "C63-36ADA4-S00P0-B0 36 mm"
        ),
        "brand": "Christopher Ward",
        "reference": "C63-36ADA4-S00P0-B0",
        "available": True,
    }
    brand_page = [
        {
            "id": f"cw-{index}",
            "name": f"Relógio Christopher Ward C63 Sealander Automático Kingfisher {index}",
            "brand": "Christopher Ward",
        }
        for index in range(20)
    ]

    async def execute(tool, arguments):
        if tool == "search_products":
            name = (arguments.get("name") or "")
            if name.casefold() == "rosa" and arguments.get("brand"):
                page = int(arguments.get("page") or 1)
                if page == 1:
                    return {
                        "products": [
                            {
                                "id": "other-pink",
                                "name": "Relógio Christopher Ward pulseira Rosa",
                                "brand": "Christopher Ward",
                            }
                        ],
                        "paging": {"total": 21, "page": 1, "limit": 20},
                    }
                if page == 2:
                    return {
                        "products": [rosa],
                        "paging": {"total": 21, "page": 2, "limit": 20},
                    }
                return {"products": [], "paging": {"total": 21, "page": page, "limit": 20}}
            if (
                arguments.get("brand") == "Christopher Ward"
                and not arguments.get("name")
                and not arguments.get("tokens")
                and not arguments.get("query")
            ):
                return {
                    "products": brand_page,
                    "paging": {"total": 20, "page": 1, "limit": 20},
                }
            return {"products": []}
        if tool == "get_product":
            return rosa
        if tool == "list_product_variants":
            return {"variants": []}
        raise AssertionError((tool, arguments))

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(
        "app.product_retrieval.get_settings",
        lambda: _settings(api_key=""),
    )

    result = await sales_agent._execute_compiled_product_retrieval(
        _interpretation(
            brand="Christopher Ward",
            model="Sealander Automatic",
            preferences={"color": "rosa claro"},
        )
    )

    assert result is not None
    assert result.safety_reason != "product_not_found"
    assert result.commercial_data["products"][0]["id"] == "rosa"


@pytest.mark.asyncio
async def test_partial_model_returns_plausible_matches_from_brand_candidates(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []
    candidates = [
        {"id": "1", "name": "Longines HydroConquest", "brand": "Longines"},
        {"id": "2", "name": "Longines Spirit Zulu Time 39", "brand": "Longines"},
        {"id": "3", "name": "Longines Spirit Zulu Time 42", "brand": "Longines"},
        {"id": "4", "name": "Longines Conquest", "brand": "Longines"},
    ]

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "search_products":
            if arguments == {"brand": "Longines", "limit": 20, "page": 1}:
                return {"products": candidates}
            return {"products": []}
        if tool == "get_product":
            product = next(item for item in candidates if item["id"] == arguments["product_id"])
            return {**product, "available": True}
        raise AssertionError(tool)

    _mock_matcher(monkeypatch, ["2", "3"], match_status="ambiguous")
    monkeypatch.setattr(sales_agent, "execute_tool", execute)

    result = await sales_agent._execute_compiled_product_retrieval(
        _interpretation(brand="Longines", model="Zulu")
    )

    assert result.safety_reason != "product_not_found"
    assert result.commercial_data["match_status"] == "ambiguous"
    assert [item["id"] for item in result.commercial_data["products"]] == ["2", "3"]
    assert result.response_metadata["product_resolution_state"] == "plausible_matches"
    assert result.response_metadata["presented_products"] is True
    assert "active_product" not in result.response_metadata
    assert any(
        tool == "search_products"
        and arguments == {"brand": "Longines", "limit": 20, "page": 1}
        for tool, arguments in calls
    )


@pytest.mark.asyncio
async def test_informal_product_name_can_return_ambiguous_real_candidates(monkeypatch):
    import app.sales_agent as sales_agent

    candidates = [
        {"id": "11", "name": "Citizen Aviation Alpha", "brand": "Citizen"},
        {"id": "12", "name": "Citizen Aviation Bravo", "brand": "Citizen"},
        {"id": "13", "name": "Citizen Classic", "brand": "Citizen"},
    ]

    async def execute(tool, arguments):
        if tool == "search_products":
            if arguments.get("brand") == "Citizen" and "name" not in arguments:
                return {"products": candidates}
            return {"products": []}
        if tool == "get_product":
            product = next(item for item in candidates if item["id"] == arguments["product_id"])
            return {**product, "available": True}
        raise AssertionError(tool)

    _mock_matcher(monkeypatch, ["11", "12"], match_status="ambiguous")
    monkeypatch.setattr(sales_agent, "execute_tool", execute)

    result = await sales_agent._execute_compiled_product_retrieval(
        _interpretation(brand="Citizen", model="Pilot")
    )

    assert result.commercial_data["match_status"] == "ambiguous"
    assert [item["id"] for item in result.commercial_data["products"]] == ["11", "12"]


@pytest.mark.asyncio
async def test_unavailable_product_remains_in_ambiguous_identification(monkeypatch):
    import app.sales_agent as sales_agent

    candidates = [
        {"id": "21", "name": "Modelo parcial 39", "brand": "Marca"},
        {"id": "22", "name": "Modelo parcial 42", "brand": "Marca"},
    ]

    async def execute(tool, arguments):
        if tool == "search_products":
            if arguments.get("brand") == "Marca" and "name" not in arguments:
                return {"products": candidates}
            return {"products": []}
        if tool == "get_product":
            product = next(item for item in candidates if item["id"] == arguments["product_id"])
            if product["id"] == "21":
                return {
                    **product,
                    "available": False,
                    "available_in_store": False,
                    "available_for_purchase": False,
                }
            return {**product, "available": True}
        raise AssertionError(tool)

    _mock_matcher(monkeypatch, ["21", "22"], match_status="ambiguous")
    monkeypatch.setattr(sales_agent, "execute_tool", execute)

    result = await sales_agent._execute_compiled_product_retrieval(
        _interpretation(brand="Marca", model="Modelo parcial")
    )

    assert [item["id"] for item in result.commercial_data["products"]] == ["21", "22"]
    assert result.commercial_data["products"][0]["availability_state"] == "unavailable"
    assert result.commercial_data["products"][1]["availability_state"] == "available"


def test_disambiguation_list_replaces_previous_list_and_resolves_choice():
    previous = CommerceConversationState(
        active_domain="commerce",
        active_product={"product_id": "old", "name": "Produto anterior"},
        last_presented_products=[
            {"position": 1, "product_id": "old", "name": "Lista anterior"},
        ],
    )
    result = AgentResult(
        reply_text="possibilidades",
        intent="commerce",
        commercial_data={
            "products": [
                {"id": "101", "name": "Possibilidade A"},
                {"id": "202", "name": "Possibilidade B"},
            ],
            "match_status": "ambiguous",
        },
        response_metadata={
            "domain": "commerce",
            "presented_products": True,
            "product_resolution_state": "plausible_matches",
            "clear_active_product": True,
        },
    )

    updated = evolve_commerce_state(previous, result)
    choice = _interpretation(
        brand=None,
        model="",
        reference_type="list_position",
    ).model_copy(update={"reference_position": 2})
    resolved, resolved_by = resolve_commerce_reference(choice, updated)

    assert [item.product_id for item in updated.last_presented_products] == ["101", "202"]
    assert updated.active_product is None
    assert updated.product_resolution_state == "plausible_matches"
    assert resolved.product_id == "202"
    assert resolved_by == "product_id"


@pytest.mark.asyncio
async def test_brand_discovery_finds_exact_product_on_second_page(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []
    page_one = [
        {"id": str(index), "name": f"Hamilton catálogo {index}", "brand": "Hamilton"}
        for index in range(1, 21)
    ]
    murph = {
        "id": "murph",
        "name": "Hamilton Khaki Field Murph",
        "brand": "Hamilton",
        "model": "Murph",
        "available": True,
    }

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "search_products":
            if (
                arguments.get("name")
                or arguments.get("query")
                or arguments.get("tokens")
            ):
                return {"products": []}
            if arguments.get("brand") == "Hamilton":
                if arguments["page"] == 1:
                    return {
                        "products": page_one,
                        "paging": {"total": 21, "page": 1, "limit": 20},
                    }
                return {
                    "products": [murph],
                    "paging": {"total": 21, "page": 2, "limit": 20},
                }
        if tool == "get_product":
            return murph
        raise AssertionError((tool, arguments))

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    result = await sales_agent._execute_compiled_product_retrieval(
        _interpretation(brand="Hamilton", model="Murph")
    )

    brand_pages = [
        arguments["page"]
        for tool, arguments in calls
        if tool == "search_products"
        and arguments.get("brand") == "Hamilton"
        and "name" not in arguments
        and "query" not in arguments
        and "tokens" not in arguments
    ]
    assert brand_pages == [1, 2]
    assert result.safety_reason != "product_not_found"
    assert result.commercial_data["products"][0]["id"] == "murph"


@pytest.mark.asyncio
async def test_brand_discovery_reaches_third_page_and_disambiguates(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []
    pages = {
        1: [
            {"id": f"l1-{index}", "name": f"Longines catálogo A {index}", "brand": "Longines"}
            for index in range(20)
        ],
        2: [
            {"id": f"l2-{index}", "name": f"Longines catálogo B {index}", "brand": "Longines"}
            for index in range(20)
        ],
        3: [
            {"id": "z39", "name": "Longines Spirit Zulu Time 39", "brand": "Longines"},
            {"id": "z42", "name": "Longines Spirit Zulu Time 42", "brand": "Longines"},
            {"id": "zg", "name": "Longines Spirit Zulu Time GMT", "brand": "Longines"},
        ],
    }

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "search_products":
            if (
                arguments.get("name")
                or arguments.get("query")
                or arguments.get("tokens")
            ):
                return {"products": []}
            if arguments.get("brand") == "Longines":
                page = arguments["page"]
                return {
                    "products": pages[page],
                    "paging": {"total": 43, "page": page, "limit": 20},
                }
        if tool == "get_product":
            product_id = arguments["product_id"]
            product = next(item for item in pages[3] if item["id"] == product_id)
            return {**product, "available": True}
        raise AssertionError((tool, arguments))

    _mock_matcher(
        monkeypatch,
        ["z39", "z42", "zg"],
        match_status="ambiguous",
    )
    monkeypatch.setattr(sales_agent, "execute_tool", execute)

    result = await sales_agent._execute_compiled_product_retrieval(
        _interpretation(brand="Longines", model="Zulu")
    )

    brand_pages = [
        arguments["page"]
        for tool, arguments in calls
        if tool == "search_products"
        and arguments.get("brand") == "Longines"
        and "name" not in arguments
        and "query" not in arguments
        and "tokens" not in arguments
    ]
    assert brand_pages == [1, 2, 3]
    assert result.commercial_data["match_status"] == "ambiguous"
    assert [item["id"] for item in result.commercial_data["products"]] == [
        "z39",
        "z42",
        "zg",
    ]


def test_generic_prefilter_uses_real_properties_and_limits_matcher_payload():
    interpretation = _interpretation(brand="Citizen", model="Pilot")
    unrelated = [
        {
            "id": str(index),
            "name": f"Citizen catálogo {index}",
            "brand": "Citizen",
            "properties": {"collection": "Classic"},
        }
        for index in range(30)
    ]
    related = {
        "id": "pilot",
        "name": "Citizen Promaster",
        "brand": "Citizen",
        "properties": {"collection": "Pilot", "use": "aviação"},
    }

    shortlisted = prefilter_specific_candidates(
        [*unrelated, related],
        interpretation,
    )

    assert len(shortlisted) == 20
    assert "pilot" in {item["id"] for item in shortlisted}


@pytest.mark.asyncio
async def test_property_evidence_from_later_brand_page_reaches_matcher(monkeypatch):
    import app.sales_agent as sales_agent

    page_one = [
        {
            "id": f"c1-{index}",
            "name": f"Citizen catálogo {index}",
            "brand": "Citizen",
            "properties": {"collection": "Classic"},
        }
        for index in range(20)
    ]
    related = {
        "id": "pilot",
        "name": "Citizen Promaster",
        "brand": "Citizen",
        "properties": {"collection": "Pilot", "use": "aviação"},
    }

    async def execute(tool, arguments):
        if tool == "search_products":
            if arguments.get("name"):
                return {"products": []}
            if arguments["page"] == 1:
                return {
                    "products": page_one,
                    "paging": {"total": 21, "page": 1, "limit": 20},
                }
            return {
                "products": [related],
                "paging": {"total": 21, "page": 2, "limit": 20},
            }
        if tool == "get_product":
            return {**related, "available": True}
        raise AssertionError((tool, arguments))

    _mock_matcher(
        monkeypatch,
        ["pilot"],
        match_status="exact",
        best_candidate_id="pilot",
    )
    monkeypatch.setattr(sales_agent, "execute_tool", execute)

    result = await sales_agent._execute_compiled_product_retrieval(
        _interpretation(brand="Citizen", model="Pilot")
    )

    assert result.safety_reason != "product_not_found"
    assert result.commercial_data["products"][0]["id"] == "pilot"


@pytest.mark.asyncio
async def test_first_brand_page_exact_match_stops_pagination(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []
    product = {
        "id": "khaki",
        "name": "Hamilton Khaki Field",
        "brand": "Hamilton",
        "model": "Khaki Field",
        "available": True,
    }

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "search_products":
            if arguments.get("name") or arguments.get("query"):
                return {"products": []}
            # Token search (and brand page 1) can both surface the exact hit.
            return {
                "products": [product, *[
                    {
                        "id": f"other-{index}",
                        "name": f"Hamilton catálogo {index}",
                        "brand": "Hamilton",
                    }
                    for index in range(19)
                ]],
                "paging": {"total": 60, "page": 1, "limit": 20},
            }
        if tool == "get_product":
            return product
        raise AssertionError((tool, arguments))

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    result = await sales_agent._execute_compiled_product_retrieval(
        _interpretation(brand="Hamilton", model="Khaki Field")
    )

    brand_pages = [
        arguments["page"]
        for tool, arguments in calls
        if tool == "search_products"
        and "name" not in arguments
        and "query" not in arguments
        and "tokens" not in arguments
    ]
    # Exact hit may come from token_and_search alone — brand paging optional.
    assert brand_pages in ([1], [])
    assert result.safety_reason != "product_not_found"


@pytest.mark.asyncio
async def test_hamilton_khaki_field_multiple_real_matches_stay_ambiguous(monkeypatch):
    import app.sales_agent as sales_agent

    products = [
        {
            "id": "k1",
            "name": "Hamilton Khaki Field Auto",
            "brand": "Hamilton",
            "model": "Khaki Field",
        },
        {
            "id": "k2",
            "name": "Hamilton Khaki Field Mechanical",
            "brand": "Hamilton",
            "model": "Khaki Field",
        },
    ]

    async def execute(tool, arguments):
        if tool == "search_products":
            return {"products": products}
        if tool == "get_product":
            product = next(item for item in products if item["id"] == arguments["product_id"])
            return {**product, "available": True}
        raise AssertionError(tool)

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    result = await sales_agent._execute_compiled_product_retrieval(
        _interpretation(brand="Hamilton", model="Khaki Field")
    )

    assert result.commercial_data["match_status"] == "ambiguous"
    assert [item["id"] for item in result.commercial_data["products"]] == ["k1", "k2"]


@pytest.mark.asyncio
async def test_persistent_catalog_failure_is_technical_not_product_not_found(monkeypatch):
    import app.sales_agent as sales_agent

    async def execute(tool, arguments):
        assert tool == "search_products"
        return {"error": "temporary failure"}

    monkeypatch.setattr(sales_agent, "execute_tool", execute)

    result = await sales_agent._execute_compiled_product_retrieval(
        _interpretation(brand="Doxa", model="SUB 300")
    )

    assert result.safety_reason == "tray_adapter_unavailable"
    assert result.safety_reason != "product_not_found"


@pytest.mark.asyncio
async def test_neutral_brand_product_on_second_page_reaches_matcher(monkeypatch):
    import app.sales_agent as sales_agent

    page_one = [
        {
            "id": f"alpha-{index}",
            "name": f"Marca Alfa catálogo {index}",
            "brand": "Marca Alfa",
        }
        for index in range(20)
    ]
    expected = {
        "id": "aero",
        "name": "Relógio Marca Alfa Aero Commander",
        "brand": "Marca Alfa",
        "model": "Aero",
        "available": True,
    }

    async def execute(tool, arguments):
        if tool == "search_products":
            if arguments.get("name"):
                return {"products": []}
            if arguments["page"] == 1:
                return {
                    "products": page_one,
                    "paging": {"total": 21, "page": 1, "limit": 20},
                }
            return {
                "products": [expected],
                "paging": {"total": 21, "page": 2, "limit": 20},
            }
        if tool == "get_product":
            return expected
        raise AssertionError((tool, arguments))

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    result = await sales_agent._execute_compiled_product_retrieval(
        _interpretation(brand="Marca Alfa", model="Aero")
    )

    assert result.safety_reason != "product_not_found"
    assert result.commercial_data["products"][0]["id"] == "aero"


def test_neutral_properties_evidence_prioritizes_candidate_generically():
    interpretation = _interpretation(brand="Marca Beta", model="Aero")
    products = [
        {
            "id": f"filler-{index}",
            "name": f"Marca Beta catálogo {index}",
            "brand": "Marca Beta",
            "properties": {"family": "Classic"},
        }
        for index in range(25)
    ]
    products.extend([
        {
            "id": "ocean",
            "name": "Beta Ocean Master",
            "brand": "Marca Beta",
            "properties": {"family": "Ocean"},
        },
        {
            "id": "explorer",
            "name": "Beta Explorer Chronograph",
            "brand": "Marca Beta",
            "properties": {"family": "Aero", "movement": "automatic"},
        },
        {
            "id": "classic",
            "name": "Beta Classic",
            "brand": "Marca Beta",
            "properties": {"family": "Classic"},
        },
    ])

    shortlisted = prefilter_specific_candidates(products, interpretation)

    assert len(shortlisted) == 20
    assert "explorer" in {product["id"] for product in shortlisted}


@pytest.mark.asyncio
async def test_neutral_same_brand_without_semantic_relation_returns_none(monkeypatch):
    products = [
        {"id": "g1", "name": "Marca Gamma Ocean", "brand": "Marca Gamma"},
        {"id": "g2", "name": "Marca Gamma Classic", "brand": "Marca Gamma"},
        {"id": "g3", "name": "Marca Gamma Field", "brand": "Marca Gamma"},
    ]
    _mock_matcher(monkeypatch, [], match_status="none")

    resolution = await match_specific_products(
        products,
        _interpretation(brand="Marca Gamma", model="Nebula"),
    )

    assert resolution.status == "none"
    assert resolution.products == ()


@pytest.mark.asyncio
async def test_partial_literal_name_does_not_stop_before_objective_match(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []
    weak = {
        "id": "strap",
        "name": "Explorer Strap",
        "brand": "Marca Delta",
    }
    strong = {
        "id": "automatic",
        "name": "Marca Delta Explorer Automatic",
        "brand": "Marca Delta",
        "model": "Explorer",
        "available": True,
    }

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "search_products":
            if (
                arguments.get("name")
                or arguments.get("query")
                or arguments.get("tokens")
            ):
                return {"products": []}
            if arguments["page"] == 1:
                return {
                    "products": [
                        weak,
                        *[
                            {
                                "id": f"delta-{index}",
                                "name": f"Marca Delta catálogo {index}",
                                "brand": "Marca Delta",
                            }
                            for index in range(19)
                        ],
                    ],
                    "paging": {"total": 21, "page": 1, "limit": 20},
                }
            return {
                "products": [strong],
                "paging": {"total": 21, "page": 2, "limit": 20},
            }
        if tool == "get_product":
            return strong
        raise AssertionError((tool, arguments))

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    result = await sales_agent._execute_compiled_product_retrieval(
        _interpretation(brand="Marca Delta", model="Explorer")
    )

    brand_pages = [
        arguments["page"]
        for tool, arguments in calls
        if tool == "search_products"
        and "name" not in arguments
        and "query" not in arguments
        and "tokens" not in arguments
    ]
    assert brand_pages == [1, 2]
    assert result.commercial_data["products"][0]["id"] == "automatic"
