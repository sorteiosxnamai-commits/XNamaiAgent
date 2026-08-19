from types import SimpleNamespace

import pytest

from app.cart_service import (
    CartItemRequest,
    create_cart_items_checkout,
    variant_choices,
)
from app.commerce_context import (
    CommerceConversationState,
    CommerceProductReference,
    evolve_commerce_state,
)
from app.models import AgentResult, IncomingMessage, SalesInterpretation
from app.product_retrieval import commercial_availability_facts


def _interpretation(**overrides) -> SalesInterpretation:
    payload = {
        "domain": "commerce",
        "goal": "buy",
        "subject": {"product_type": "produto"},
        "preferences": {},
        "information_needed": [],
        "references_previous_context": True,
        "needs_clarification": False,
        "confidence": 0.99,
    }
    payload.update(overrides)
    return SalesInterpretation(**payload)


def _settings():
    return SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini")


def _cart_execute(variants):
    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_product":
            return {
                "id": arguments["product_id"],
                "current_price": "100.00",
                "available": True,
                "has_variation": True,
            }
        if tool == "list_product_variants":
            return {"variants": variants}
        if tool == "create_cart":
            return {
                "cart_id": "C1",
                "session_id": "S1",
                "cart_url": "https://loja.example/checkout/S1",
            }
        if tool == "get_cart_complete":
            create = next(args for name, args in calls if name == "create_cart")
            return {
                "items": [{
                    "product_id": create["product_id"],
                    "variant_id": create.get("variant_id"),
                    "quantity": create["quantity"],
                }],
                "total": "100.00",
            }
        raise AssertionError(tool)

    return execute, calls


@pytest.mark.asyncio
async def test_zero_applicable_variants_does_not_block_cart():
    execute, calls = _cart_execute([])

    result = await create_cart_items_checkout(
        item_requests=[
            CartItemRequest(
                product_reference=CommerceProductReference(product_id="P1"),
            )
        ],
        state=CommerceConversationState(),
        execute=execute,
    )

    create = next(args for name, args in calls if name == "create_cart")
    assert "variant_id" in create
    assert create["variant_id"] is None
    assert result.safety_reason is None


@pytest.mark.asyncio
async def test_false_string_variation_flag_does_not_trigger_variant_lookup():
    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_product":
            return {
                "id": "P1",
                "current_price": "100.00",
                "available": True,
                "has_variation": "0",
            }
        if tool == "list_product_variants":
            raise AssertionError("false variation flag must not query variants")
        if tool == "create_cart":
            return {
                "cart_id": "C1",
                "session_id": "S1",
                "cart_url": "https://loja.example/checkout/S1",
            }
        if tool == "get_cart_complete":
            return {
                "items": [{"product_id": "P1", "quantity": 1}],
                "total": "100.00",
            }
        raise AssertionError(tool)

    result = await create_cart_items_checkout(
        item_requests=[
            CartItemRequest(
                product_reference=CommerceProductReference(product_id="P1"),
            )
        ],
        state=CommerceConversationState(),
        execute=execute,
    )

    assert result.safety_reason is None
    assert not any(name == "list_product_variants" for name, _ in calls)


@pytest.mark.asyncio
async def test_single_eligible_variant_is_auto_selected():
    execute, calls = _cart_execute([
        {"variant_id": "123", "available": True, "color": "Preto"},
    ])

    await create_cart_items_checkout(
        item_requests=[
            CartItemRequest(
                product_reference=CommerceProductReference(product_id="P1"),
            )
        ],
        state=CommerceConversationState(),
        execute=execute,
    )

    create = next(args for name, args in calls if name == "create_cart")
    assert create["variant_id"] == "123"


@pytest.mark.asyncio
async def test_distinct_real_variant_choices_are_presented():
    execute, calls = _cart_execute([
        {"variant_id": "123", "available": True, "properties": [{"name": "Acabamento", "value": "Fosco"}]},
        {"variant_id": "124", "available": True, "properties": [{"name": "Acabamento", "value": "Polido"}]},
    ])

    result = await create_cart_items_checkout(
        item_requests=[
            CartItemRequest(
                product_reference=CommerceProductReference(product_id="P1"),
            )
        ],
        state=CommerceConversationState(),
        execute=execute,
    )

    assert result.safety_reason == "variant_required"
    assert not any(name == "create_cart" for name, _ in calls)
    assert result.commercial_data["variants"][0]["choices"]
    assert "Fosco" in result.reply_text
    assert "Polido" in result.reply_text


def test_nested_sku_choice_is_kept_without_technical_fields():
    choices = variant_choices({
        "variant_id": "123",
        "Sku": {
            "name": "Acabamento",
            "value": "Fosco",
            "price": "100.00",
            "stock": 3,
        },
    })

    assert choices == {"Acabamento": "Fosco"}


@pytest.mark.asyncio
async def test_duplicate_technical_variants_do_not_create_impossible_question():
    execute, calls = _cart_execute([
        {"variant_id": "123", "available": True, "properties": [{"name": "Acabamento", "value": "Fosco"}]},
        {"variant_id": "124", "available": True, "properties": [{"name": "Acabamento", "value": "Fosco"}]},
    ])

    result = await create_cart_items_checkout(
        item_requests=[
            CartItemRequest(
                product_reference=CommerceProductReference(product_id="P1"),
            )
        ],
        state=CommerceConversationState(),
        execute=execute,
    )

    assert result.safety_reason is None
    assert any(name == "create_cart" for name, _ in calls)


@pytest.mark.asyncio
async def test_structured_preference_selects_real_variant_and_continues_cart():
    execute, calls = _cart_execute([
        {"variant_id": "123", "available": True, "properties": [{"name": "Acabamento", "value": "Fosco"}]},
        {"variant_id": "124", "available": True, "properties": [{"name": "Acabamento", "value": "Polido"}]},
    ])

    result = await create_cart_items_checkout(
        item_requests=[
            CartItemRequest(
                product_reference=CommerceProductReference(product_id="P1"),
                variant_preferences={"attributes": ["Polido"]},
            )
        ],
        state=CommerceConversationState(),
        execute=execute,
    )

    create = next(args for name, args in calls if name == "create_cart")
    assert create["variant_id"] == "124"
    assert result.safety_reason is None


@pytest.mark.asyncio
async def test_pending_create_cart_confirmation_executes_without_retrieval(monkeypatch):
    import app.sales_agent as sales_agent

    execute, calls = _cart_execute([])
    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)
    state = CommerceConversationState(
        active_domain="commerce",
        active_product={"product_id": "P1", "name": "Produto"},
        pending_action="create_cart",
        pending_action_product_ids=["P1"],
        purchase_stage="selection",
    )

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="confirmação semântica"),
        {},
        {},
        _interpretation(confirmation="confirm"),
        commerce_state=state,
    )

    assert any(name == "create_cart" for name, _ in calls)
    assert not any(name == "search_products" for name, _ in calls)
    assert result.response_metadata["clear_pending_action"] is True


@pytest.mark.asyncio
async def test_pending_product_link_confirmation_uses_real_product_url(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        assert tool == "get_product_link"
        return {
            "product_id": "P1",
            "product_name": "Produto",
            "product_url": "https://loja.example/produto/P1",
        }

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)
    state = CommerceConversationState(
        active_domain="commerce",
        active_product={"product_id": "P1", "name": "Produto"},
        pending_action="send_product_link",
        pending_action_product_ids=["P1"],
    )

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="confirmação semântica"),
        {},
        {},
        _interpretation(confirmation="confirm", goal="inspect"),
        commerce_state=state,
    )

    assert calls == [("get_product_link", {"product_id": "P1"})]
    assert "https://loja.example/produto/P1" in result.reply_text


@pytest.mark.asyncio
async def test_pending_action_rejection_clears_without_execution(monkeypatch):
    import app.sales_agent as sales_agent

    async def never_execute(*_args, **_kwargs):
        raise AssertionError("rejected action must not execute")

    monkeypatch.setattr(sales_agent, "execute_tool", never_execute)
    state = CommerceConversationState(
        active_domain="commerce",
        active_product={"product_id": "P1"},
        pending_action="create_cart",
        pending_action_product_ids=["P1"],
    )

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="rejeição semântica"),
        {},
        {},
        _interpretation(confirmation="reject"),
        commerce_state=state,
    )
    evolved = evolve_commerce_state(state, result)

    assert evolved.pending_action is None
    assert evolved.pending_action_product_ids == []


@pytest.mark.asyncio
async def test_new_explicit_product_does_not_execute_old_pending_action(monkeypatch):
    import app.sales_agent as sales_agent

    async def never_execute(tool, _arguments):
        if tool == "create_cart":
            raise AssertionError("old pending product must not be added")
        raise AssertionError(tool)

    async def retrieve(_interpretation):
        return AgentResult(
            reply_text="novo produto",
            intent="commerce",
            commercial_data={"products": [{"id": "P2", "name": "Produto novo"}]},
            response_metadata={"presented_products": True},
        )

    monkeypatch.setattr(sales_agent, "execute_tool", never_execute)
    monkeypatch.setattr(sales_agent, "_execute_compiled_product_retrieval", retrieve)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)
    state = CommerceConversationState(
        active_domain="commerce",
        active_product={"product_id": "P1"},
        pending_action="create_cart",
        pending_action_product_ids=["P1"],
    )

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="mudança semântica de produto"),
        {},
        {},
        _interpretation(
            goal="find",
            subject={"product_type": "produto", "model": "Novo Modelo"},
            references_previous_context=False,
            ready_for_retrieval=True,
            confirmation="none",
        ),
        commerce_state=state,
    )
    evolved = evolve_commerce_state(state, result)

    assert evolved.pending_action is None
    assert result.response_metadata["clear_pending_action"] is True


@pytest.mark.asyncio
async def test_product_detail_does_not_invent_pending_cart_action(monkeypatch):
    import app.sales_agent as sales_agent

    async def execute(tool, arguments):
        assert tool == "get_product"
        return {
            "id": arguments["product_id"],
            "name": "Produto",
            "current_price": "100.00",
            "available": True,
            "has_variation": False,
        }

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)
    state = CommerceConversationState(
        active_domain="commerce",
        active_product={"product_id": "P1", "name": "Produto"},
    )

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="detalhes contextuais"),
        {},
        {},
        _interpretation(
            goal="inspect",
            subject={"product_type": "item", "model": "Modelo"},
            reference_type="current_product",
            enough_information_to_search=True,
            ready_for_retrieval=True,
        ),
        commerce_state=state,
    )
    evolved = evolve_commerce_state(state, result)

    assert evolved.pending_action is None
    assert evolved.pending_action_product_ids == []


@pytest.mark.asyncio
async def test_pix_commitment_with_zero_variants_reaches_cart_then_payment(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_product":
            return {
                "id": "P1",
                "current_price": "100.00",
                "available": True,
                "has_variation": True,
            }
        if tool == "list_product_variants":
            return {"variants": []}
        if tool == "create_cart":
            return {
                "cart_id": "C1",
                "session_id": "S1",
                "cart_url": "https://loja.example/checkout/S1",
            }
        if tool == "get_cart_complete":
            return {
                "items": [{"product_id": "P1", "quantity": 1}],
                "total": "100.00",
            }
        if tool == "get_payment_options":
            return {"payment_options": {"pix": {"value": 95.00}}}
        raise AssertionError(tool)

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)
    state = CommerceConversationState(
        active_domain="commerce",
        active_product={"product_id": "P1"},
        purchase_stage="selection",
    )

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="compromisso semântico com Pix"),
        {},
        {},
        _interpretation(
            purchase_action="create_cart",
            reference_type="current_product",
            payment_action="payment_options",
            payment_method_preference="pix",
        ),
        commerce_state=state,
    )

    assert [name for name, _ in calls] == [
        "get_product",
        "list_product_variants",
        "create_cart",
        "get_cart_complete",
        "get_cart_complete",
        "get_payment_options",
    ]
    assert result.commercial_data["requested_method_available"] is True


def test_pending_action_is_persisted_in_existing_commerce_json_state():
    previous = CommerceConversationState(
        active_domain="commerce",
        active_product={"product_id": "P1"},
    )
    result = AgentResult(
        reply_text="ação oferecida",
        response_metadata={
            "domain": "commerce",
            "pending_action": "create_cart",
            "pending_action_product_ids": ["P1"],
        },
    )

    evolved = evolve_commerce_state(previous, result)

    assert evolved.pending_action == "create_cart"
    assert evolved.pending_action_product_ids == ["P1"]
    assert evolved.interpreter_payload()["pending_action"] == "create_cart"


def test_stock_with_thirty_day_lead_time_is_not_immediate_delivery():
    facts = commercial_availability_facts({
        "stock": 38,
        "available": True,
        "order_days_availability": 30,
    })

    assert facts["has_stock"] is True
    assert facts["has_lead_time"] is True
    assert facts["lead_time_days"] == 30
    assert facts["immediate_delivery_supported"] is False


def test_explicit_zero_day_lead_time_supports_immediate_delivery():
    facts = commercial_availability_facts({
        "stock": 2,
        "available": True,
        "order_days_availability": 0,
    })

    assert facts["immediate_delivery_supported"] is True
