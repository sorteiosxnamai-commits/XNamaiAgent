from types import SimpleNamespace

import pytest

from app.commerce_context import (
    CommerceConversationState,
    apply_commerce_domain_context,
    evolve_commerce_state,
)
from app.models import AgentResult, IncomingMessage, SalesInterpretation
from openai_test_utils import install_fake_openai_client


def _interpretation(**overrides) -> SalesInterpretation:
    payload = {
        "domain": "commerce",
        "goal": "buy",
        "subject": {"product_type": "produto"},
        "preferences": {},
        "information_needed": ["catalog"],
        "references_previous_context": True,
        "needs_clarification": False,
        "purchase_action": "create_cart",
        "purchase_stage": "selection",
        "confidence": 0.99,
    }
    payload.update(overrides)
    return SalesInterpretation(**payload)


def _state(**overrides) -> CommerceConversationState:
    payload = {
        "active_domain": "commerce",
        "last_presented_products": [
            {"position": 1, "product_id": "A", "name": "Produto A"},
            {"position": 2, "product_id": "B", "name": "Produto B"},
            {"position": 3, "product_id": "C", "name": "Produto C"},
        ],
        "purchase_stage": "selection",
    }
    payload.update(overrides)
    return CommerceConversationState(**payload)


def _settings():
    return SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini")


def test_structured_plan_preserves_cart_action_and_quantity():
    from app.sales_agent import interpretation_to_plan

    plan = interpretation_to_plan(
        _interpretation(
            purchase_action="create_cart",
            quantity=3,
            reference_type="current_product",
        )
    )

    assert plan["purchase_action"] == "create_cart"
    assert plan["quantity"] == 3


def test_structured_plan_exposes_product_and_checkout_actions():
    from app.sales_agent import interpretation_to_plan

    plan = interpretation_to_plan(
        _interpretation(
            purchase_action=None,
            product_action="get_product_link",
            checkout_channel_preference="site",
        )
    )

    assert plan["product_action"] == "get_product_link"
    assert plan["checkout_channel_preference"] == "site"


def test_structured_plan_preserves_multiple_purchase_items():
    from app.sales_agent import interpretation_to_plan

    plan = interpretation_to_plan(
        _interpretation(
            purchase_items=[
                {
                    "reference_type": "list_position",
                    "reference_position": 1,
                    "quantity": 2,
                },
                {
                    "reference_type": "list_position",
                    "reference_position": 2,
                    "quantity": 1,
                },
            ],
        )
    )

    assert plan["purchase_items"] == [
        {
            "reference_type": "list_position",
            "reference_position": 1,
            "explicit_product_name": None,
            "quantity": 2,
        },
        {
            "reference_type": "list_position",
            "reference_position": 2,
            "explicit_product_name": None,
            "quantity": 1,
        },
    ]


@pytest.mark.asyncio
async def test_list_selection_revalidates_price_quantity_and_creates_cart(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_product":
            return {
                "id": "B",
                "name": "Produto B atualizado",
                "current_price": "125.50",
                "available": True,
                "has_variation": False,
            }
        if tool == "create_cart":
            return {
                "cart_id": "CART-1",
                "session_id": "SESSION-1",
                "cart_url": "https://loja.example/checkout/SESSION-1",
            }
        if tool == "get_cart_complete":
            return {
                "cart_id": "CART-1",
                "session_id": "SESSION-1",
                "total": "251.00",
                "items": [{"product_id": "B", "quantity": 2}],
            }
        raise AssertionError(f"unexpected tool {tool}")

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)
    interpretation = _interpretation(
        reference_type="list_position",
        reference_position=2,
        quantity=2,
    )

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="seleção de compra"),
        {"primary_intent": "commerce"},
        {},
        interpretation,
        commerce_state=_state(),
    )

    assert result is not None
    assert calls[0] == ("get_product", {"product_id": "B"})
    assert calls[1][0] == "create_cart"
    create_payload = calls[1][1]
    assert create_payload["product_id"] == "B"
    assert create_payload["variant_id"] is None
    assert create_payload["quantity"] == 2
    assert create_payload["price"] == "125.50"
    assert len(create_payload["session_id"]) == 32
    int(create_payload["session_id"], 16)
    assert calls[2] == ("get_cart_complete", {"session_id": "SESSION-1"})
    assert result.response_metadata["purchase_stage"] == "cart_created"
    assert result.response_metadata["cart_state"]["cart_product_id"] == "B"
    assert result.response_metadata["cart_state"]["cart_quantity"] == 2
    assert result.response_metadata["cart_state"]["cart_url"] == (
        "https://loja.example/checkout/SESSION-1"
    )
    assert "cart_url" not in result.commercial_data["cart"]
    assert "cart_url" not in result.commercial_data["checkout"]
    assert "https://loja.example/checkout/SESSION-1" not in result.reply_text
    assert result.commercial_data["current_price"] == "125.50"


@pytest.mark.asyncio
async def test_successful_cart_post_is_not_downgraded_when_complete_lags(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_product":
            return {
                "id": "B",
                "name": "Produto B",
                "current_price": "125.50",
                "available": True,
                "has_variation": False,
            }
        if tool == "create_cart":
            return {
                "cart_id": "CART-1",
                "session_id": arguments["session_id"],
                "cart_url": "https://loja.example/checkout/CART-1",
            }
        if tool == "get_cart_complete":
            return {
                "cart_id": "CART-1",
                "session_id": arguments["session_id"],
                "items": [],
            }
        raise AssertionError(f"unexpected tool {tool}")

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="compra estruturada"),
        {},
        {},
        _interpretation(
            reference_type="list_position",
            reference_position=2,
        ),
        commerce_state=_state(),
    )

    assert [name for name, _ in calls] == [
        "get_product",
        "create_cart",
        "get_cart_complete",
    ]
    assert result.safety_reason is None
    assert result.commercial_data["cart"]["status"] == "cart_created"
    assert result.commercial_data["cart"]["verification_status"] == "pending"
    assert result.commercial_data["cart"]["items"] == [
        {"product_id": "B", "variant_id": None, "quantity": 1, "original_price": None}
    ]
    assert result.response_metadata["pending_action"] == "choose_checkout_channel"


@pytest.mark.asyncio
async def test_purchase_and_site_choice_execute_cart_before_channel_selection(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_product":
            return {
                "id": "B",
                "name": "Produto B",
                "current_price": "125.50",
                "available": True,
                "has_variation": False,
            }
        if tool == "create_cart":
            return {
                "cart_id": "CART-1",
                "session_id": arguments["session_id"],
                "cart_url": "https://loja.example/checkout/CART-1",
            }
        if tool == "get_cart_complete":
            return {
                "cart_id": "CART-1",
                "session_id": arguments["session_id"],
                "items": [{"product_id": "B", "quantity": 1}],
            }
        raise AssertionError(f"unexpected tool {tool}")

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="compra e canal estruturados"),
        {},
        {},
        _interpretation(
            reference_type="list_position",
            reference_position=2,
            checkout_channel_preference="site",
        ),
        commerce_state=_state(),
    )

    assert [name for name, _ in calls] == [
        "get_product",
        "create_cart",
        "get_cart_complete",
    ]
    assert result.safety_reason is None
    assert result.response_metadata["checkout_channel_preference"] == "site"
    assert result.response_metadata["purchase_stage"] == "checkout_ready"
    assert result.response_metadata["cart_state"]["cart_session_id"]
    assert result.commercial_data["checkout"]["selected_channel"] == "site"
    assert result.commercial_data["checkout"]["selected_channel_supported"] is True


@pytest.mark.asyncio
async def test_unavailable_product_does_not_create_cart(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        assert tool == "get_product"
        return {
            "id": "B",
            "price": "90.00",
            "available": False,
            "available_in_store": False,
        }

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)
    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="seleção"),
        {},
        {},
        _interpretation(reference_type="list_position", reference_position=2),
        commerce_state=_state(),
    )

    assert result is not None
    assert result.safety_reason == "product_unavailable"
    assert [tool for tool, _ in calls] == ["get_product"]


@pytest.mark.asyncio
async def test_single_variant_is_validated_and_used(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_product":
            return {
                "id": "B",
                "price": "100.00",
                "available": True,
                "has_variation": True,
            }
        if tool == "list_product_variants":
            return {
                "variants": [{
                    "id": "123",
                    "product_id": "B",
                    "price": "95.00",
                    "available": True,
                }]
            }
        if tool == "create_cart":
            return {
                "cart_id": "C1",
                "session_id": "S1",
                "cart_url": "https://loja.example/checkout/S1",
            }
        if tool == "get_cart_complete":
            return {
                "items": [{"product_id": "B", "variant_id": "123", "quantity": 1}],
                "total": "95.00",
            }
        raise AssertionError(tool)

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)
    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="seleção"),
        {},
        {},
        _interpretation(reference_type="list_position", reference_position=2),
        commerce_state=_state(),
    )

    assert result is not None
    create_call = next(arguments for tool, arguments in calls if tool == "create_cart")
    assert create_call["variant_id"] == "123"
    assert create_call["price"] == "95.00"
    assert result.response_metadata["active_product"]["variant_id"] == "123"


@pytest.mark.asyncio
async def test_multiple_variants_require_choice_before_cart(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_product":
            return {
                "id": "B",
                "price": "100.00",
                "available": True,
                "has_variation": True,
            }
        if tool == "list_product_variants":
            return {
                "variants": [
                    {"id": "123", "available": True, "color": "Preto"},
                    {"id": "124", "available": True, "color": "Azul"},
                ]
            }
        raise AssertionError("cart must not be created")

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)
    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="seleção"),
        {},
        {},
        _interpretation(reference_type="list_position", reference_position=2),
        commerce_state=_state(),
    )

    assert result is not None
    assert result.safety_reason == "variant_required"
    assert [tool for tool, _ in calls] == ["get_product", "list_product_variants"]


@pytest.mark.asyncio
async def test_existing_cart_link_is_reused_without_new_post(monkeypatch):
    import app.sales_agent as sales_agent

    async def never_execute(*_args, **_kwargs):
        raise AssertionError("stored checkout link must not create another cart")

    monkeypatch.setattr(sales_agent, "execute_tool", never_execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)
    state = _state(
        active_product={"product_id": "B", "name": "Produto B"},
        cart_id="C1",
        cart_session_id="S1",
        cart_url="https://loja.example/checkout/S1",
        cart_product_id="B",
        cart_quantity=1,
        purchase_stage="cart_created",
        checkout_channel_preference="site",
    )
    interpretation = _interpretation(
        goal="inspect",
        purchase_action="show_cart_link",
        reference_type=None,
        purchase_stage="cart_created",
    )

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="link atual"),
        {},
        {},
        interpretation,
        commerce_state=state,
    )

    assert result is not None
    assert "https://loja.example/checkout/S1" in result.reply_text
    assert result.response_metadata["used_tray"] is False


@pytest.mark.asyncio
async def test_repeated_cart_creation_reconciles_without_post(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        assert tool == "get_cart_complete"
        return {
            "session_id": "S1",
            "cart_url": "https://loja.example/checkout/S1",
            "items": [{"product_id": "B", "variant_id": None, "quantity": 1}],
            "total": "10.00",
        }

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)
    state = _state(
        active_product={"product_id": "B", "name": "Produto B"},
        cart_id="C1",
        cart_session_id="S1",
        cart_url="https://loja.example/checkout/S1",
        cart_product_id="B",
        cart_quantity=1,
        cart_items=[{"product_id": "B", "quantity": 1}],
        purchase_stage="cart_created",
    )

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="seleção repetida"),
        {},
        {},
        _interpretation(reference_type="current_product"),
        commerce_state=state,
    )

    assert result is not None
    assert [name for name, _ in calls] == ["get_cart_complete"]
    assert result.commercial_data["cart"]["already_satisfied"] is True
    assert "cart_url" not in result.commercial_data["cart"]
    assert result.response_metadata["used_tray"] is True

@pytest.mark.asyncio
async def test_persistent_cart_state_is_loaded_by_evolution():
    from app.cart_service import create_cart_checkout

    async def execute(tool, arguments):
        if tool == "get_product":
            return {"id": "B", "price": "10.00", "available": True}
        if tool == "create_cart":
            return {
                "cart_id": "C1",
                "session_id": "S1",
                "cart_url": "https://loja.example/checkout/S1",
            }
        if tool == "get_cart_complete":
            return {
                "items": [{"product_id": "B", "quantity": 1}],
                "total": "10.00",
            }
        raise AssertionError(tool)

    previous = _state(active_product={"product_id": "B", "name": "Produto B"})
    result = await create_cart_checkout(
        interpretation=_interpretation(reference_type="current_product"),
        product_reference=previous.active_product,
        state=previous,
        execute=execute,
    )
    updated = evolve_commerce_state(previous, result)

    assert updated.cart_id == "C1"
    assert updated.cart_session_id == "S1"
    assert updated.cart_url == "https://loja.example/checkout/S1"
    assert updated.purchase_stage == "cart_created"
    assert updated.active_product.product_id == "B"


@pytest.mark.asyncio
async def test_cart_adapter_failure_is_technical_not_product_not_found(monkeypatch):
    import app.sales_agent as sales_agent

    async def execute(tool, arguments):
        if tool == "get_product":
            return {"id": "B", "price": "10.00", "available": True}
        if tool == "create_cart":
            return {"error": "unavailable", "status_code": 503}
        raise AssertionError(tool)

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)
    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="seleção"),
        {},
        {},
        _interpretation(reference_type="list_position", reference_position=2),
        commerce_state=_state(),
    )

    assert result is not None
    assert result.safety_reason == "cart_technical_failure"
    assert result.response_metadata["response_source"] == "technical_fallback"


@pytest.mark.asyncio
async def test_product_803_cart_http_400_keeps_diagnostics_and_selected_product(capsys):
    from app.cart_service import create_cart_checkout
    from app.commerce_context import CommerceProductReference

    async def execute(tool, arguments):
        if tool == "get_product":
            return {
                "id": "803",
                "name": "Produto 803",
                "price": "10.00",
                "available": True,
                "has_variation": False,
            }
        if tool == "create_cart":
            assert arguments["product_id"] == "803"
            assert arguments["variant_id"] is None
            assert arguments["quantity"] == 1
            return {
                "error": "commerce_upstream_error",
                "status_code": 400,
                "error_type": "TrayAdapterError",
                "tray_error_code": "invalid_cart",
                "tray_error_type": "validation_error",
                "tray_error_field": "Cart.variant_id",
                "tray_error_fields": ["Cart.variant_id"],
                "tray_error_message": "Campo invÃ¡lido",
            }
        raise AssertionError(tool)

    selected = CommerceProductReference(
        product_id="803", name="Produto 803", product_url="https://loja.example/produto/803"
    )
    result = await create_cart_checkout(
        interpretation=_interpretation(reference_type="current_product"),
        product_reference=selected,
        state=_state(active_product=selected.model_dump(mode="json")),
        execute=execute,
    )
    persisted = evolve_commerce_state(_state(), result)

    assert result.reply_text == ""
    assert result.safety_reason == "cart_technical_failure"
    assert result.commercial_data["cart"] == {
        "cart_created": False,
        "failure_stage": "cart_http",
        "recoverable": False,
        "status_code": 400,
    }
    assert result.response_metadata["cart_failure_status"] == 400
    assert result.response_metadata["cart_failure_code"] == "invalid_cart"
    assert result.response_metadata["cart_failure_type"] == "validation_error"
    assert result.response_metadata["cart_failure_field"] == "Cart.variant_id"
    assert result.response_metadata["cart_failure_fields"] == ["Cart.variant_id"]
    assert persisted.active_product.product_id == "803"
    assert persisted.active_product.product_url == "https://loja.example/produto/803"
    output = capsys.readouterr().out
    assert "tray_error_code" in output
    assert "Campo invÃ¡lido" in output
    assert "Bearer" not in output


@pytest.mark.asyncio
async def test_product_803_cart_success_advances_purchase_stage():
    from app.cart_service import create_cart_checkout
    from app.commerce_context import CommerceProductReference

    async def execute(tool, arguments):
        if tool == "get_product":
            return {"id": "803", "name": "Produto 803", "price": "10.00", "available": True}
        if tool == "create_cart":
            return {"session_id": arguments["session_id"], "cart_url": "https://loja.example/checkout/S803"}
        if tool == "get_cart_complete":
            return {"items": [{"product_id": "803", "quantity": 1}], "total": "10.00"}
        raise AssertionError(tool)

    selected = CommerceProductReference(product_id="803", name="Produto 803")
    result = await create_cart_checkout(
        interpretation=_interpretation(reference_type="current_product"),
        product_reference=selected,
        state=_state(active_product=selected.model_dump(mode="json")),
        execute=execute,
    )
    persisted = evolve_commerce_state(_state(), result)

    assert result.safety_reason is None
    assert result.commercial_data["cart"]["status"] == "cart_created"
    assert result.response_metadata["purchase_stage"] == "cart_created"
    assert persisted.cart_session_id
    assert persisted.cart_url == "https://loja.example/checkout/S803"
    assert persisted.purchase_stage == "cart_created"


@pytest.mark.asyncio
async def test_failed_cart_for_new_selection_keeps_new_active_product(monkeypatch):
    import app.sales_agent as sales_agent

    async def execute(tool, arguments):
        assert tool == "get_product"
        return {
            "id": "B",
            "price": "10.00",
            "available": False,
            "available_in_store": False,
        }

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)
    previous = _state(
        active_product={"product_id": "A", "name": "Produto A"},
        cart_session_id="OLD",
        cart_url="https://loja.example/checkout/OLD",
        cart_product_id="A",
        cart_quantity=1,
    )
    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="nova seleção"),
        {},
        {},
        _interpretation(reference_type="list_position", reference_position=2),
        commerce_state=previous,
    )
    updated = evolve_commerce_state(previous, result)

    assert updated.active_product.product_id == "B"
    assert updated.cart_product_id == "A"
    assert updated.purchase_stage == "selection"


def test_checkout_question_uses_structured_commerce_domain():
    state = _state(
        active_product={"product_id": "B"},
        cart_session_id="S1",
        cart_url="https://loja.example/checkout/S1",
        purchase_stage="cart_created",
    )
    interpretation = _interpretation(
        domain="commerce",
        purchase_action="checkout_question",
        purchase_stage="cart_created",
        domain_change_explicit=False,
    )

    contextual, changed = apply_commerce_domain_context(interpretation, state)

    assert changed is False
    assert contextual.domain == "commerce"
    assert contextual.purchase_action == "checkout_question"


@pytest.mark.asyncio
async def test_active_product_purchase_does_not_search_by_name(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_product":
            return {"id": "B", "price": "10.00", "available": True}
        if tool == "create_cart":
            return {
                "session_id": "S1",
                "cart_url": "https://loja.example/checkout/S1",
            }
        if tool == "get_cart_complete":
            return {
                "items": [{"product_id": "B", "quantity": 1}],
                "total": "10.00",
            }
        raise AssertionError(tool)

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)
    state = _state(active_product={"product_id": "B", "name": "Produto B"})
    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="produto atual"),
        {},
        {},
        _interpretation(reference_type="current_product"),
        commerce_state=state,
    )

    assert result is not None
    assert [tool for tool, _ in calls] == [
        "get_product",
        "create_cart",
        "get_cart_complete",
    ]
    assert all(tool != "search_products" for tool, _ in calls)


@pytest.mark.asyncio
async def test_cart_success_uses_openai_sales_responder(monkeypatch):
    import app.sales_agent as sales_agent

    captured = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=(
                                "Pronto, seu produto está no carrinho. "
                                "Finalize pelo checkout oficial informado."
                            )
                        )
                    )
                ]
            )

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(
        sales_agent,
        "get_settings",
        lambda: SimpleNamespace(
            openai_api_key="test-key",
            openai_model="gpt-4.1-mini",
        ),
    )
    install_fake_openai_client(monkeypatch, FakeOpenAI)
    tray_result = AgentResult(
        reply_text="fallback",
        intent="commerce",
        commercial_data={
            "cart": {
                "status": "cart_created",
                "cart_url": "https://loja.example/checkout/S1",
            }
        },
        response_metadata={"purchase_stage": "cart_created", "used_tray": True},
    )
    interpretation = _interpretation()

    result = await sales_agent._sales_response_with_openai(
        IncomingMessage(text="compra confirmada"),
        {"goal": "buy"},
        tray_result,
        interpretation,
    )

    assert result is not None
    assert result.response_metadata["response_source"] == "openai"
    assert result.response_metadata["purchase_stage"] == "cart_created"
    assert "cartão, CVV" in captured["messages"][0]["content"]
    assert "requires_channel_choice" in captured["messages"][0]["content"]


@pytest.mark.asyncio
async def test_cart_failure_gives_openai_only_safe_semantic_facts(monkeypatch):
    import app.sales_agent as sales_agent

    captured = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="Houve uma falha interna temporária no carrinho.",
                        )
                    )
                ]
            )

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(
        sales_agent,
        "get_settings",
        lambda: SimpleNamespace(
            openai_api_key="test-key",
            openai_model="gpt-4.1-mini",
        ),
    )
    install_fake_openai_client(monkeypatch, FakeOpenAI)
    technical = AgentResult(
        reply_text="Não consegui preparar o carrinho neste momento.",
        intent="commerce",
        safety_reason="cart_technical_failure",
        commercial_data={
            "cart": {
                "status": "technical_failure",
                "failure_stage": "cart_http",
            },
            "technical_failure": {
                "operation": "cart",
                "category": "integration_failure",
                "retryable": False,
            },
        },
        response_metadata={"used_tray": True},
    )

    result = await sales_agent._sales_response_with_openai(
        IncomingMessage(text="compra estruturada"),
        {"goal": "buy"},
        technical,
        _interpretation(),
    )

    assert result is not None
    facts_message = captured["messages"][1]["content"]
    assert '"category": "integration_failure"' in facts_message
    assert "tray_error_message" not in facts_message


@pytest.mark.asyncio
async def test_cart_removal_of_one_of_two_items():
    from app.cart_service import rebuild_cart_without

    calls = []
    new_session_id = None

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_cart_complete":
            session = arguments.get("session_id")
            if session == "old_session":
                return {
                    "items": [
                        {"product_id": "A", "variant_id": None, "quantity": 1, "unit_price": "100.00"},
                        {"product_id": "B", "variant_id": None, "quantity": 1, "unit_price": "200.00"},
                    ]
                }
            else:
                return {"items": [{"product_id": "B", "variant_id": None, "quantity": 1, "unit_price": "200.00"}]}
        if tool == "delete_cart":
            return {"success": True}
        if tool == "create_cart":
            nonlocal new_session_id
            new_session_id = arguments["session_id"]
            return {"session_id": arguments["session_id"]}
        return {}

    state = _state(
        cart_session_id="old_session",
        cart_items=[
            {"product_id": "A", "variant_id": None, "quantity": 1},
            {"product_id": "B", "variant_id": None, "quantity": 1},
        ],
    )

    new_state, result = await rebuild_cart_without(state, [("A", None)], execute)

    assert result["success"] is True
    assert len(new_state.cart_items) == 1
    assert new_state.cart_items[0].product_id == "B"
    assert new_state.cart_session_id != "old_session"
    assert any(call[0] == "delete_cart" for call in calls)
    assert sum(1 for call in calls if call[0] == "delete_cart") == 1
    assert sum(1 for call in calls if call[0] == "create_cart") == 1


@pytest.mark.asyncio
async def test_cart_removal_of_only_item():
    from app.cart_service import rebuild_cart_without

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_cart_complete":
            session = arguments.get("session_id")
            if session == "old_session":
                return {"items": [{"product_id": "A", "variant_id": None, "quantity": 1, "unit_price": "100"}]}
            return {"items": []}
        if tool == "delete_cart":
            return {"success": True}
        return {}

    state = _state(cart_session_id="old_session", cart_items=[{"product_id": "A", "variant_id": None, "quantity": 1}])

    new_state, result = await rebuild_cart_without(state, [("A", None)], execute)

    assert result["success"] is True
    assert result["empty_cart"] is True
    assert new_state.cart_session_id is None
    assert len(new_state.cart_items) == 0
    assert any(call[0] == "delete_cart" for call in calls)
    assert not any(call[0] == "create_cart" for call in calls)


@pytest.mark.asyncio
async def test_cart_removal_item_not_found():
    from app.cart_service import rebuild_cart_without

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_cart_complete":
            return {"items": [{"product_id": "A", "variant_id": None, "quantity": 1, "unit_price": "100"}]}
        return {}

    state = _state(cart_session_id="session", cart_items=[{"product_id": "A", "variant_id": None, "quantity": 1}])

    new_state, result = await rebuild_cart_without(state, [("B", None)], execute)

    assert result["success"] is False
    assert result["reason"] == "item_not_found"
    assert not any(call[0] == "delete_cart" for call in calls)
    assert new_state == state


@pytest.mark.asyncio
async def test_cart_removal_get_cart_fails():
    from app.cart_service import rebuild_cart_without

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_cart_complete":
            return {"error": "upstream_error"}
        return {}

    state = _state(cart_session_id="session", cart_items=[{"product_id": "A", "variant_id": None, "quantity": 1}])

    new_state, result = await rebuild_cart_without(state, [("A", None)], execute)

    assert result["success"] is False
    assert result["reason"] == "cart_read_failed"
    assert not any(call[0] == "delete_cart" for call in calls)
    assert new_state == state


@pytest.mark.asyncio
async def test_cart_removal_partial_rebuild_failure():
    """Quando create_cart falha, aborta imediatamente (no fails-fast)"""
    from app.cart_service import rebuild_cart_without

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_cart_complete":
            if arguments.get("session_id") == "old":
                return {
                    "items": [
                        {"product_id": "A", "variant_id": None, "quantity": 1, "unit_price": "100"},
                        {"product_id": "B", "variant_id": None, "quantity": 1, "unit_price": "200"},
                    ]
                }
            return {"items": []}
        if tool == "delete_cart":
            return {"success": True}
        if tool == "create_cart":
            # create_cart falha para B (o item que será recriado)
            if arguments["product_id"] == "B":
                return {"error": "api_error"}
            return {"session_id": arguments["session_id"]}
        return {}

    state = _state(
        cart_session_id="old",
        cart_items=[
            {"product_id": "A", "variant_id": None, "quantity": 1},
            {"product_id": "B", "variant_id": None, "quantity": 1},
        ],
    )

    new_state, result = await rebuild_cart_without(state, [("A", None)], execute)

    # Quando criar B falha, operação é abortada imediatamente
    assert result["success"] is False
    assert result["reason"] == "rebuild_failed"
    assert new_state.cart_session_id == state.cart_session_id

    # delete_cart da sessão antiga NUNCA deve ser chamado
    old_session_deletes = [call for call in calls if call[0] == "delete_cart" and call[1].get("session_id") == "old"]
    assert len(old_session_deletes) == 0


@pytest.mark.asyncio
async def test_cart_removal_invalidates_shipping_and_payment():
    from app.cart_service import rebuild_cart_without

    async def execute(tool, arguments):
        if tool == "get_cart_complete":
            session = arguments.get("session_id")
            if session == "old":
                return {
                    "items": [
                        {"product_id": "A", "variant_id": None, "quantity": 1, "unit_price": "100"},
                        {"product_id": "B", "variant_id": None, "quantity": 1, "unit_price": "200"},
                    ]
                }
            return {"items": [{"product_id": "B", "variant_id": None, "quantity": 1, "unit_price": "200"}]}
        if tool == "delete_cart":
            return {"success": True}
        if tool == "create_cart":
            return {"session_id": arguments["session_id"]}
        return {}

    state = _state(
        cart_session_id="old",
        cart_items=[
            {"product_id": "A", "variant_id": None, "quantity": 1},
            {"product_id": "B", "variant_id": None, "quantity": 1},
        ],
        selected_shipping={"shipping_id": "1", "quotation_id": "Q1", "name": "PAC", "price": "35.10"},
        selected_payment_option={"id": "10545", "name": "Pix", "method": "pix"},
        shipping_quote_zipcode="86480000",
    )

    new_state, result = await rebuild_cart_without(state, [("A", None)], execute)

    assert new_state.selected_shipping is None
    assert new_state.selected_payment_option is None
    assert new_state.shipping_quote_zipcode is None
    assert new_state.shipping_quotes == []


@pytest.mark.asyncio
async def test_cart_removal_by_name():
    from app.cart_service import resolve_cart_item_reference

    state = _state(
        cart_session_id="session",
        cart_items=[
            {"product_id": "A", "variant_id": None, "quantity": 1, "name": "Citizen Watch"},
            {"product_id": "B", "variant_id": None, "quantity": 1, "name": "Tissot Watch"},
        ],
    )
    interpretation = _interpretation(purchase_action="remove_cart_item")
    interpretation.subject.model = "Tissot"

    targets, reason = resolve_cart_item_reference(interpretation, state)

    assert len(targets) == 1
    assert targets[0][0] == "B"
    assert reason == "name_match"


@pytest.mark.asyncio
async def test_cart_removal_ambiguous_name():
    from app.cart_service import resolve_cart_item_reference

    state = _state(
        cart_session_id="session",
        cart_items=[
            {"product_id": "A", "variant_id": None, "quantity": 1, "name": "Watch"},
            {"product_id": "B", "variant_id": None, "quantity": 1, "name": "Watch"},
        ],
    )
    interpretation = _interpretation(purchase_action="remove_cart_item")
    interpretation.subject.model = "Watch"

    targets, reason = resolve_cart_item_reference(interpretation, state)

    assert len(targets) == 0
    assert reason == "ambiguous"


@pytest.mark.asyncio
async def test_cart_removal_name_not_found():
    from app.cart_service import resolve_cart_item_reference

    state = _state(
        cart_session_id="session",
        cart_items=[
            {"product_id": "A", "variant_id": None, "quantity": 1, "name": "Citizen"},
            {"product_id": "B", "variant_id": None, "quantity": 1, "name": "Tissot"},
        ],
    )
    interpretation = _interpretation(purchase_action="remove_cart_item")
    interpretation.subject.model = "Seiko"

    targets, reason = resolve_cart_item_reference(interpretation, state)

    assert len(targets) == 0
    assert reason == "not_found"


def test_cart_removal_single_item():
    from app.cart_service import resolve_cart_item_reference

    state = _state(
        cart_session_id="session",
        cart_items=[{"product_id": "A", "variant_id": None, "quantity": 1}],
    )
    interpretation = _interpretation(purchase_action="remove_cart_item")

    targets, reason = resolve_cart_item_reference(interpretation, state)

    assert len(targets) == 1
    assert targets[0][0] == "A"
    assert reason == "single_item"


def test_cart_removal_position_out_of_range():
    from app.cart_service import resolve_cart_item_reference

    state = _state(
        cart_session_id="session",
        cart_items=[
            {"product_id": "A", "variant_id": None, "quantity": 1},
            {"product_id": "B", "variant_id": None, "quantity": 1},
        ],
    )
    interpretation = _interpretation(purchase_action="remove_cart_item", reference_position=3)
    interpretation.reference_type = "list_position"

    targets, reason = resolve_cart_item_reference(interpretation, state)

    assert len(targets) == 0
    assert reason == "position_out_of_range"


@pytest.mark.asyncio
async def test_cart_removal_order_create_before_delete():
    """A. create_cart é chamado ANTES de delete_cart"""
    from app.cart_service import rebuild_cart_without

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_cart_complete":
            if arguments.get("session_id") == "old_session":
                return {
                    "items": [
                        {"product_id": "X", "variant_id": None, "quantity": 1, "unit_price": "100.00"},
                        {"product_id": "Y", "variant_id": None, "quantity": 1, "unit_price": "200.00"},
                    ]
                }
            return {"items": [{"product_id": "Y", "variant_id": None, "quantity": 1, "unit_price": "200.00"}]}
        if tool == "delete_cart":
            return {"success": True}
        if tool == "create_cart":
            return {"session_id": "new_session"}
        return {}

    state = _state(
        cart_session_id="old_session",
        cart_items=[
            {"product_id": "X", "variant_id": None, "quantity": 1},
            {"product_id": "Y", "variant_id": None, "quantity": 1},
        ],
    )

    new_state, result = await rebuild_cart_without(state, [("X", None)], execute)

    assert result["success"] is True

    create_calls = [i for i, call in enumerate(calls) if call[0] == "create_cart"]
    delete_calls = [i for i, call in enumerate(calls) if call[0] == "delete_cart"]

    assert len(create_calls) > 0, "create_cart deve ser chamado"
    assert len(delete_calls) > 0, "delete_cart deve ser chamado"
    assert create_calls[0] < delete_calls[-1], "create_cart deve ser chamado ANTES do delete_cart final"


@pytest.mark.asyncio
async def test_cart_removal_create_fails_no_delete():
    """B. create_cart falha -> delete_cart da sessão antiga NUNCA chamado, estado inalterado"""
    from app.cart_service import rebuild_cart_without

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_cart_complete":
            return {
                "items": [
                    {"product_id": "X", "variant_id": None, "quantity": 1, "unit_price": "100.00"},
                    {"product_id": "Y", "variant_id": None, "quantity": 1, "unit_price": "200.00"},
                ]
            }
        if tool == "delete_cart":
            return {"success": True}
        if tool == "create_cart":
            return {"error": "Falha ao criar carrinho"}
        return {}

    state = _state(
        cart_session_id="old_session",
        cart_items=[
            {"product_id": "X", "variant_id": None, "quantity": 1},
            {"product_id": "Y", "variant_id": None, "quantity": 1},
        ],
    )

    new_state, result = await rebuild_cart_without(state, [("X", None)], execute)

    assert result["success"] is False
    assert result["reason"] == "rebuild_failed"
    assert new_state.cart_session_id == state.cart_session_id
    assert len(new_state.cart_items) == len(state.cart_items)

    # delete_cart da sessão ANTIGA nunca deve ser chamado
    old_session_deletes = [call for call in calls if call[0] == "delete_cart" and call[1].get("session_id") == "old_session"]
    assert len(old_session_deletes) == 0, "delete_cart da sessão antiga não deve ser chamado quando create_cart falha"


@pytest.mark.asyncio
async def test_cart_removal_includes_price_in_create():
    """C. create_cart recebe 'price' com o valor factual vindo do snapshot"""
    from app.cart_service import rebuild_cart_without

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_cart_complete":
            if arguments.get("session_id") == "old_session":
                return {
                    "items": [
                        {"product_id": "X", "variant_id": None, "quantity": 1, "unit_price": "123.45"},
                        {"product_id": "Y", "variant_id": None, "quantity": 2, "unit_price": "678.90"},
                    ]
                }
            return {"items": [{"product_id": "Y", "variant_id": None, "quantity": 2, "unit_price": "678.90"}]}
        if tool == "delete_cart":
            return {"success": True}
        if tool == "create_cart":
            return {"session_id": "new_session"}
        return {}

    state = _state(
        cart_session_id="old_session",
        cart_items=[
            {"product_id": "X", "variant_id": None, "quantity": 1},
            {"product_id": "Y", "variant_id": None, "quantity": 2},
        ],
    )

    new_state, result = await rebuild_cart_without(state, [("X", None)], execute)

    assert result["success"] is True

    create_calls = [call for call in calls if call[0] == "create_cart"]
    assert len(create_calls) == 1

    create_args = create_calls[0][1]
    assert "price" in create_args, "price deve estar presente em create_cart"
    assert create_args["price"] == "678.90", "price deve ser o valor factual do item Y"


@pytest.mark.asyncio
async def test_cart_removal_verification_failure_no_delete():
    """D. Verificação do novo carrinho retorna erro -> antigo não apagado"""
    from app.cart_service import rebuild_cart_without

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_cart_complete":
            if arguments.get("session_id") == "old_session":
                return {
                    "items": [
                        {"product_id": "X", "variant_id": None, "quantity": 1, "unit_price": "100.00"},
                        {"product_id": "Y", "variant_id": None, "quantity": 1, "unit_price": "200.00"},
                    ]
                }
            # Verificação do novo carrinho falha
            return {"error": "Carrinho não encontrado"}
        if tool == "delete_cart":
            return {"success": True}
        if tool == "create_cart":
            return {"session_id": "new_session"}
        return {}

    state = _state(
        cart_session_id="old_session",
        cart_items=[
            {"product_id": "X", "variant_id": None, "quantity": 1},
            {"product_id": "Y", "variant_id": None, "quantity": 1},
        ],
    )

    new_state, result = await rebuild_cart_without(state, [("X", None)], execute)

    assert result["success"] is False
    assert result["reason"] == "verification_failed"
    assert new_state.cart_session_id == state.cart_session_id

    # delete_cart da sessão ANTIGA nunca deve ser chamado
    old_session_deletes = [call for call in calls if call[0] == "delete_cart" and call[1].get("session_id") == "old_session"]
    assert len(old_session_deletes) == 0


@pytest.mark.asyncio
async def test_cart_removal_content_mismatch_no_delete():
    """E. Verificação retorna conteúdo divergente ([X, Z] em vez de [Y]) -> antigo não apagado"""
    from app.cart_service import rebuild_cart_without

    calls = []
    cart_state = {"old_session": "old", "new_session": "new"}

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_cart_complete":
            session = arguments.get("session_id")
            if session == "old_session":
                return {
                    "items": [
                        {"product_id": "X", "variant_id": None, "quantity": 1, "unit_price": "100.00"},
                        {"product_id": "Y", "variant_id": None, "quantity": 1, "unit_price": "200.00"},
                    ]
                }
            # Qualquer outra sessão retorna conteúdo divergente (Z em vez de Y)
            return {"items": [{"product_id": "Z", "variant_id": None, "quantity": 1, "unit_price": "200.00"}]}
        if tool == "delete_cart":
            return {"success": True}
        if tool == "create_cart":
            return {"session_id": arguments["session_id"]}
        return {}

    state = _state(
        cart_session_id="old_session",
        cart_items=[
            {"product_id": "X", "variant_id": None, "quantity": 1},
            {"product_id": "Y", "variant_id": None, "quantity": 1},
        ],
    )

    new_state, result = await rebuild_cart_without(state, [("X", None)], execute)

    assert result["success"] is False
    assert result["reason"] == "verification_failed"
    assert new_state.cart_session_id == state.cart_session_id

    old_session_deletes = [call for call in calls if call[0] == "delete_cart" and call[1].get("session_id") == "old_session"]
    assert len(old_session_deletes) == 0, "delete_cart da sessão antiga não deve ser chamado quando conteúdo diverge"


@pytest.mark.asyncio
async def test_cart_removal_price_missing():
    """F. Item do snapshot sem preço -> aborta com 'price_missing', nada apagado"""
    from app.cart_service import rebuild_cart_without

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_cart_complete":
            return {
                "items": [
                    {"product_id": "X", "variant_id": None, "quantity": 1, "unit_price": "100.00"},
                    {"product_id": "Y", "variant_id": None, "quantity": 1},  # Sem price!
                ]
            }
        if tool == "delete_cart":
            return {"success": True}
        if tool == "create_cart":
            return {"session_id": "new_session"}
        return {}

    state = _state(
        cart_session_id="old_session",
        cart_items=[
            {"product_id": "X", "variant_id": None, "quantity": 1},
            {"product_id": "Y", "variant_id": None, "quantity": 1},
        ],
    )

    new_state, result = await rebuild_cart_without(state, [("X", None)], execute)

    assert result["success"] is False
    assert result["reason"] == "price_missing"
    assert new_state.cart_session_id == state.cart_session_id
    assert len(new_state.cart_items) == len(state.cart_items)

    # Nada deve ser chamado
    assert len([call for call in calls if call[0] == "delete_cart"]) == 0
    assert len([call for call in calls if call[0] == "create_cart"]) == 0


@pytest.mark.asyncio
async def test_cart_removal_success_full():
    """G. Remoção bem-sucedida -> cart_session_id muda, cart_items atualizado, frete e pagamento invalidados"""
    from app.cart_service import rebuild_cart_without

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_cart_complete":
            if arguments.get("session_id") == "old_session":
                return {
                    "items": [
                        {"product_id": "X", "variant_id": None, "quantity": 1, "unit_price": "100.00", "name": "Produto X"},
                        {"product_id": "Y", "variant_id": None, "quantity": 1, "unit_price": "200.00", "name": "Produto Y"},
                    ]
                }
            return {"items": [{"product_id": "Y", "variant_id": None, "quantity": 1, "unit_price": "200.00", "name": "Produto Y"}]}
        if tool == "delete_cart":
            return {"success": True}
        if tool == "create_cart":
            return {"session_id": arguments["session_id"]}
        return {}

    state = _state(
        cart_session_id="old_session",
        cart_items=[
            {"product_id": "X", "variant_id": None, "quantity": 1},
            {"product_id": "Y", "variant_id": None, "quantity": 1},
        ],
        selected_shipping={"shipping_id": "1", "name": "PAC", "price": "35.10"},
        selected_payment_option={"id": "1", "name": "Pix", "method": "pix"},
        shipping_quote_zipcode="12345",
    )

    new_state, result = await rebuild_cart_without(state, [("X", None)], execute)

    assert result["success"] is True
    assert new_state.cart_session_id != "old_session"
    assert len(new_state.cart_items) == 1
    assert new_state.cart_items[0].product_id == "Y"
    assert new_state.selected_shipping is None, "Frete deve ser invalidado"
    assert new_state.selected_payment_option is None, "Pagamento deve ser invalidado"
    assert new_state.shipping_quote_zipcode is None, "CEP deve ser invalidado"


@pytest.mark.asyncio
async def test_cart_removal_delete_old_fails_success():
    """H. delete_cart do antigo falha DEPOIS da verificação OK -> operação ainda é sucesso"""
    from app.cart_service import rebuild_cart_without

    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_cart_complete":
            if arguments.get("session_id") == "old_session":
                return {
                    "items": [
                        {"product_id": "X", "variant_id": None, "quantity": 1, "unit_price": "100.00"},
                        {"product_id": "Y", "variant_id": None, "quantity": 1, "unit_price": "200.00"},
                    ]
                }
            return {"items": [{"product_id": "Y", "variant_id": None, "quantity": 1, "unit_price": "200.00"}]}
        if tool == "delete_cart":
            if arguments.get("session_id") == "old_session":
                return {"error": "Falha ao deletar"}  # Falha no delete do antigo
            return {"success": True}
        if tool == "create_cart":
            return {"session_id": arguments["session_id"]}
        return {}

    state = _state(
        cart_session_id="old_session",
        cart_items=[
            {"product_id": "X", "variant_id": None, "quantity": 1},
            {"product_id": "Y", "variant_id": None, "quantity": 1},
        ],
    )

    new_state, result = await rebuild_cart_without(state, [("X", None)], execute)

    # Mesmo com delete failure, operação é sucesso (best-effort)
    assert result["success"] is True
    assert new_state.cart_session_id != "old_session"
    assert len(new_state.cart_items) == 1
    assert new_state.cart_items[0].product_id == "Y"
