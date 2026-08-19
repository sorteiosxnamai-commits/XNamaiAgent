from types import SimpleNamespace

import pytest

from app.commerce_context import CommerceConversationState
from app.models import AgentResult, IncomingMessage, SalesInterpretation


def _interpretation(**overrides) -> SalesInterpretation:
    payload = {
        "domain": "commerce",
        "goal": "discover",
        "subject": {},
        "preferences": {},
        "information_needed": [],
        "references_previous_context": False,
        "enough_information_to_search": False,
        "ready_for_retrieval": False,
        "stop_clarification": False,
        "needs_clarification": False,
        "confidence": 0.99,
    }
    payload.update(overrides)
    return SalesInterpretation(**payload)


def _settings():
    return SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini")


async def _no_responder(*_args, **_kwargs):
    return None


def _catalog_result(*product_ids: str) -> AgentResult:
    return AgentResult(
        reply_text="Opções reais.",
        intent="commerce",
        commercial_data={
            "products": [
                {
                    "id": product_id,
                    "name": f"Produto {product_id}",
                    "current_price": "100.00",
                    "available": True,
                }
                for product_id in product_ids
            ]
        },
        response_metadata={"presented_products": True, "used_tray": True},
    )


def test_structured_generic_purchase_is_discovery_without_cart_action():
    from app.sales_agent import interpretation_to_plan

    interpretation = _interpretation(
        goal="discover",
        subject={"product_type": "relógio"},
        ready_for_retrieval=True,
        enough_information_to_search=True,
        purchase_action=None,
    )

    plan = interpretation_to_plan(interpretation, "mensagem semântica")

    assert plan["intent"] == "recommendation"
    assert plan["purchase_action"] is None
    assert interpretation.image_request is False


@pytest.mark.asyncio
async def test_broad_purchase_interest_continues_without_catalog_or_cart(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        raise AssertionError(f"unexpected factual action: {tool}")

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "_sales_response_with_openai", _no_responder)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="quero comprar um relógio"),
        {},
        {},
        _interpretation(
            goal="buy",
            subject={"product_type": "relógio"},
            needs_clarification=True,
        ),
        commerce_state=CommerceConversationState(active_domain="commerce"),
    )

    assert result is not None
    assert result.safety_reason == "commerce_clarification"
    assert calls == []
    assert result.commercial_data is None


@pytest.mark.asyncio
async def test_explicit_product_request_allows_catalog_search(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []

    async def retrieve(interpretation):
        calls.append(("search_products", interpretation.subject.model_dump()))
        return _catalog_result("TISSOT-1")

    monkeypatch.setattr(sales_agent, "_execute_compiled_product_retrieval", retrieve)
    monkeypatch.setattr(sales_agent, "_sales_response_with_openai", _no_responder)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="me mostre relógios Tissot esportivos"),
        {},
        {},
        _interpretation(
            goal="find",
            subject={"product_type": "relógio", "brand": "Tissot"},
            preferences={"style": "esportivo"},
            enough_information_to_search=True,
            ready_for_retrieval=True,
        ),
        commerce_state=CommerceConversationState(active_domain="commerce"),
    )

    assert result is not None
    assert [name for name, _ in calls] == ["search_products"]
    assert result.commercial_data["products"][0]["id"] == "TISSOT-1"


def test_active_product_requires_explicit_structured_reference():
    from app.commerce_context import resolve_commerce_reference

    state = CommerceConversationState(
        active_domain="commerce",
        active_product={"product_id": "A", "name": "Produto A"},
    )
    interpretation = _interpretation(
        goal="buy",
        references_previous_context=True,
        purchase_action="create_cart",
    )

    resolved, resolved_by = resolve_commerce_reference(interpretation, state)

    assert resolved is None
    assert resolved_by == "none"


@pytest.mark.asyncio
async def test_invalid_create_cart_without_structured_target_never_posts(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        raise AssertionError(f"unexpected factual action: {tool}")

    async def retrieve(_interpretation):
        return _catalog_result("A", "B")

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "_execute_compiled_product_retrieval", retrieve)
    monkeypatch.setattr(sales_agent, "_sales_response_with_openai", _no_responder)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="mensagem interpretada"),
        {},
        {},
        _interpretation(
            goal="buy",
            subject={"product_type": "relógio"},
            purchase_action="create_cart",
            ready_for_retrieval=True,
        ),
        commerce_state=CommerceConversationState(active_domain="commerce"),
    )

    assert result is not None
    assert not any(tool == "create_cart" for tool, _ in calls)


@pytest.mark.asyncio
async def test_pending_action_without_persisted_target_does_not_use_active_product(
    monkeypatch,
):
    import app.sales_agent as sales_agent

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        raise AssertionError(f"unexpected factual action: {tool}")

    async def retrieve(_interpretation):
        return _catalog_result("B")

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "_execute_compiled_product_retrieval", retrieve)
    monkeypatch.setattr(sales_agent, "_sales_response_with_openai", _no_responder)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)
    state = CommerceConversationState(
        active_domain="commerce",
        active_product={"product_id": "A"},
        pending_action="create_cart",
        pending_action_product_ids=[],
    )

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="confirmação interpretada"),
        {},
        {},
        _interpretation(
            goal="find",
            subject={"product_type": "relógio", "model": "Novo"},
            confirmation="confirm",
            ready_for_retrieval=True,
        ),
        commerce_state=state,
    )

    assert result is not None
    assert not any(tool == "create_cart" for tool, _ in calls)
    assert result.response_metadata["clear_pending_action"] is True


@pytest.mark.asyncio
async def test_complete_pipeline_keeps_current_semantics_above_old_state(monkeypatch):
    import app.message_pipeline as message_pipeline
    import app.openai_agent as openai_agent
    import app.sales_agent as sales_agent

    interpretations = iter([
        _interpretation(domain="greeting", goal=None),
        _interpretation(
            goal="discover",
            subject={"product_type": "relógio"},
            ready_for_retrieval=True,
            enough_information_to_search=True,
        ),
        _interpretation(
            goal="find",
            subject={"product_type": "relógio", "brand": "Marca Alfa"},
            ready_for_retrieval=True,
            enough_information_to_search=True,
        ),
        _interpretation(
            goal="inspect",
            reference_type="list_position",
            reference_position=1,
            references_previous_context=True,
        ),
        _interpretation(
            goal="inspect",
            reference_type="current_product",
            references_previous_context=True,
            image_request=True,
        ),
        _interpretation(
            goal="find",
            subject={"product_type": "relógio", "model": "Coleção Nova"},
            ready_for_retrieval=True,
            enough_information_to_search=True,
            confirmation="none",
        ),
        _interpretation(
            goal="buy",
            reference_type="list_position",
            reference_position=2,
            references_previous_context=True,
            purchase_action="create_cart",
        ),
    ])
    holder = {"state": {}}
    calls = []

    async def interpret(*_args, **_kwargs):
        return next(interpretations)

    async def retrieve(interpretation):
        if interpretation.subject.model == "Coleção Nova":
            return _catalog_result("C", "D")
        return _catalog_result("A", "B")

    async def execute(tool, arguments):
        calls.append((tool, dict(arguments)))
        if tool == "get_product":
            return {
                "id": arguments["product_id"],
                "name": f"Produto {arguments['product_id']}",
                "current_price": "100.00",
                "available": True,
                "has_variation": False,
                "primary_image_url": (
                    f"https://images.example/{arguments['product_id']}.jpg"
                ),
            }
        if tool == "create_cart":
            return {
                "cart_id": "CART",
                "session_id": "SESSION",
                "cart_url": "https://loja.example/checkout/SESSION",
            }
        if tool == "get_cart_complete":
            return {
                "items": [{
                    "product_id": "D",
                    "quantity": 1,
                }],
                "total": "100.00",
            }
        raise AssertionError(tool)

    monkeypatch.setattr(
        message_pipeline,
        "get_settings",
        lambda: SimpleNamespace(
            audio_inbound_enabled=False,
            audio_outbound_enabled=False,
        ),
    )
    monkeypatch.setattr(
        message_pipeline,
        "load_commerce_conversation_state",
        lambda **_kwargs: holder["state"],
    )
    monkeypatch.setattr(openai_agent, "load_recent_conversation_turns", lambda **_kwargs: [])
    monkeypatch.setattr(openai_agent, "interpret_message", interpret)
    monkeypatch.setattr(sales_agent, "_execute_compiled_product_retrieval", retrieve)
    monkeypatch.setattr(sales_agent, "_sales_response_with_openai", _no_responder)
    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)

    async def turn(text: str) -> AgentResult:
        result = await message_pipeline.process_incoming_message(
            IncomingMessage(text=text),
            {},
        )
        holder["state"] = result.response_metadata["commerce_state"]
        return result

    await turn("saudação")
    await turn("interesse genérico")
    await turn("pesquisa por marca")
    await turn("seleção da primeira opção")
    await turn("pedido visual do produto ativo")

    state_after_image = CommerceConversationState.from_payload(holder["state"])
    state_after_image.pending_action = "show_images"
    state_after_image.pending_action_product_ids = ["A"]
    holder["state"] = state_after_image.model_dump(mode="json")

    await turn("nova pesquisa semântica")
    state_after_search = CommerceConversationState.from_payload(holder["state"])

    assert state_after_search.pending_action is None
    assert [item.product_id for item in state_after_search.last_presented_products] == [
        "C",
        "D",
    ]
    assert not any(
        tool == "get_product" and arguments["product_id"] == "A"
        for tool, arguments in calls[2:]
    )

    await turn("compra semântica da segunda opção")

    create = next(arguments for tool, arguments in calls if tool == "create_cart")
    assert create["product_id"] == "D"
