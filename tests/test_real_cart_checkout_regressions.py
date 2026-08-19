from types import SimpleNamespace

import pytest

from app.cart_service import (
    CartItemRequest,
    create_cart_items_checkout,
    current_cart_reply,
    set_cart_item_quantity,
)
from app.checkout_service import select_checkout_channel
from app.commerce_context import (
    CommerceConversationState,
    CommerceProductReference,
    evolve_commerce_state,
)
from app.models import IncomingMessage, SalesInterpretation
from app.payment_service import inspect_payment_options
from app.sales_agent import SALES_INTERPRETER_INSTRUCTIONS
from app.tray_adapter_client import TrayAdapterClient
from app.tray_tools import execute_tool


def _reference():
    return CommerceProductReference(product_id="803", name="Relogio")


def _cart_state(*, quantity=1, channel="whatsapp"):
    return CommerceConversationState(
        active_domain="commerce",
        active_product={"product_id": "803", "name": "Relogio"},
        cart_id="C1",
        cart_session_id="S1",
        cart_url="https://loja.example/redirect_cart_service.php?session=S1",
        cart_product_id="803",
        cart_quantity=quantity,
        cart_items=[{
            "product_id": "803",
            "variant_id": None,
            "quantity": quantity,
            "unit_price": "3799.99",
        }],
        checkout_channel_preference=channel,
        purchase_stage="cart_created",
    )


def _contains_key(value, target):
    if isinstance(value, dict):
        return target in value or any(_contains_key(item, target) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, target) for item in value)
    return False


def test_interpreter_examples_distinguish_interest_context_and_explicit_retrieval():
    assert '"quero comprar um relogio"' in SALES_INTERPRETER_INSTRUCTIONS
    assert "enough_information_to_search=false" in SALES_INTERPRETER_INSTRUCTIONS
    assert "ready_for_retrieval=false" in SALES_INTERPRETER_INSTRUCTIONS
    assert '"quero um relogio casual ate uns R$ 5.000"' in SALES_INTERPRETER_INSTRUCTIONS
    assert '"me mostre os relogios disponiveis"' in SALES_INTERPRETER_INSTRUCTIONS


def test_quantity_action_is_semantic_and_does_not_accept_session_id():
    interpretation = SalesInterpretation(
        domain="commerce",
        goal="buy",
        subject={"product_type": "relogio"},
        preferences={},
        information_needed=[],
        references_previous_context=True,
        enough_information_to_search=False,
        ready_for_retrieval=False,
        needs_clarification=False,
        purchase_action="set_cart_item_quantity",
        quantity=1,
        reference_type="current_product",
        confidence=0.99,
    )
    assert interpretation.purchase_action == "set_cart_item_quantity"
    assert "session_id" not in interpretation.model_dump()


@pytest.mark.asyncio
async def test_repeated_confirmations_reconcile_but_never_post_or_increment():
    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        assert tool == "get_cart_complete"
        return {
            "session_id": "S1",
            "cart_url": "https://loja.example/redirect_cart_service.php?session=S1",
            "items": [{
                "product_id": "803", "variant_id": None,
                "quantity": 1, "unit_price": "3799.99",
            }],
            "subtotal": "3799.99",
            "total": "3799.99",
        }

    state = _cart_state(quantity=1)
    for _ in range(3):
        result = await create_cart_items_checkout(
            item_requests=[CartItemRequest(_reference(), quantity=1)],
            state=state,
            execute=execute,
        )
        state = evolve_commerce_state(state, result)
        assert result.commercial_data["cart"]["already_satisfied"] is True
        assert state.cart_quantity == 1
        assert state.pending_action == "awaiting_shipping_zipcode"
    assert [name for name, _ in calls] == ["get_cart_complete"] * 3


@pytest.mark.asyncio
async def test_absolute_quantity_reconciles_four_to_one_with_put_result():
    calls = []
    quantity_updated = False

    async def execute(tool, arguments):
        nonlocal quantity_updated
        calls.append((tool, arguments))
        if tool == "get_cart_complete":
            return {
                "cart_url": "https://loja.example/redirect_cart_service.php?session=S1",
                "items": [{
                    "product_id": "803", "variant_id": None,
                    "quantity": 1 if quantity_updated else 4, "unit_price": "3799.99",
                }],
                "subtotal": "3799.99" if quantity_updated else "15199.96",
                "total": "3799.99" if quantity_updated else "15199.96",
            }
        assert tool == "set_cart_item_quantity"
        assert arguments == {
            "session_id": "S1",
            "product_id": "803",
            "variant_id": None,
            "quantity": 1,
        }
        quantity_updated = True
        return {
            "success": True,
            "changed": True,
            "already_satisfied": False,
            "cart_url": "https://loja.example/redirect_cart_service.php?session=S1",
            "items": [{
                "product_id": "803", "variant_id": None,
                "quantity": 1, "unit_price": "3799.99",
            }],
            "subtotal": "3799.99",
            "total": "3799.99",
        }

    result = await set_cart_item_quantity(
        product_reference=_reference(),
        quantity=1,
        state=_cart_state(quantity=4),
        execute=execute,
    )
    assert [name for name, _ in calls] == [
        "get_cart_complete", "set_cart_item_quantity", "get_cart_complete",
    ]
    assert result.commercial_data["cart"]["mutation_success"] is True
    assert result.commercial_data["cart"]["total"] == "3799.99"
    assert result.response_metadata["cart_state"]["cart_quantity"] == 1


@pytest.mark.asyncio
async def test_failed_quantity_mutation_never_returns_success_fact():
    async def execute(tool, _arguments):
        if tool == "get_cart_complete":
            return {"items": [{"product_id": "803", "quantity": 4}]}
        return {"error": "adapter_failure", "status_code": 500}

    result = await set_cart_item_quantity(
        product_reference=_reference(), quantity=1,
        state=_cart_state(quantity=4), execute=execute,
    )
    assert result.safety_reason == "cart_technical_failure"
    assert not (result.commercial_data or {}).get("cart", {}).get("mutation_success")


@pytest.mark.asyncio
async def test_repeated_yes_while_waiting_zipcode_does_not_readd_or_clear_requirement(monkeypatch):
    import app.sales_agent as sales_agent

    async def never_execute(*_args, **_kwargs):
        raise AssertionError("short confirmation cannot mutate the cart again")

    monkeypatch.setattr(sales_agent, "execute_tool", never_execute)
    monkeypatch.setattr(
        sales_agent,
        "get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )
    state = _cart_state()
    state.pending_action = "awaiting_shipping_zipcode"
    state.purchase_stage = "shipping"
    interpretation = SalesInterpretation(
        domain="commerce",
        goal="buy",
        subject={"product_type": "relogio"},
        preferences={},
        information_needed=[],
        references_previous_context=True,
        enough_information_to_search=False,
        ready_for_retrieval=False,
        needs_clarification=False,
        confirmation="confirm",
        confidence=0.99,
    )

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="sim"), {}, {}, interpretation,
        commerce_state=state,
    )
    updated = evolve_commerce_state(state, result)
    assert updated.pending_action == "awaiting_shipping_zipcode"
    assert updated.cart_quantity == 1
    assert result.commercial_data["checkout_blockers"] == [
        "shipping_zipcode_missing"
    ]

@pytest.mark.asyncio
async def test_payment_advance_before_zipcode_is_blocked_without_financial_tool():
    async def never_execute(*_args, **_kwargs):
        raise AssertionError("payment checkout must not advance before shipping")

    result = await inspect_payment_options(
        state=_cart_state(),
        installment_count=None,
        payment_method_preference="pix",
        execute=never_execute,
        advance_checkout=True,
    )
    assert result.safety_reason == "checkout_requirements_missing"
    assert "shipping_zipcode_missing" in result.commercial_data["checkout_blockers"]
    assert result.response_metadata["pending_action"] == "awaiting_shipping_zipcode"


@pytest.mark.asyncio
async def test_informational_pix_query_uses_current_cart_and_separates_hosted_url():
    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_cart_complete":
            return {
                "items": [{
                    "product_id": "803", "quantity": 1,
                    "unit_price": "3799.99",
                }],
                "subtotal": "3799.99",
                "total": "3799.99",
            }
        assert tool == "get_payment_options"
        return {"payment_options": {
            "pix": {"id": "PIX", "name": "Pix - Vindi", "value": "3799.99"},
            "options": [{"id": "PIX", "name": "Pix - Vindi"}],
        }}

    result = await inspect_payment_options(
        state=_cart_state(),
        installment_count=None,
        payment_method_preference="pix",
        execute=execute,
        advance_checkout=False,
    )
    assert [name for name, _ in calls] == [
        "get_cart_complete", "get_payment_options",
    ]
    assert result.commercial_data["payment_method"]["available"] is True
    assert result.commercial_data["hosted_payment"] == {
        "order_created": False,
        "payment_url_available": False,
    }
    assert result.commercial_data["cart"]["total"] == "3799.99"
    assert "15199.96" not in repr(result.commercial_data)
    assert not _contains_key(result.commercial_data, "cart_url")
    assert not _contains_key(result.commercial_data, "payment_url")


def test_whatsapp_hides_cart_url_everywhere_and_site_exposes_it():
    whatsapp = _cart_state(channel="whatsapp")
    selected = select_checkout_channel(whatsapp, "whatsapp")
    current = current_cart_reply(whatsapp, checkout_question=False)
    assert not _contains_key(selected.commercial_data, "cart_url")
    assert not _contains_key(current.commercial_data, "cart_url")
    assert "redirect_cart_service.php" not in selected.reply_text
    assert "redirect_cart_service.php" not in current.reply_text

    site = _cart_state(channel="site")
    site_result = current_cart_reply(site, checkout_question=False)
    assert site_result.commercial_data["cart"]["cart_url"].startswith("https://")
    assert "redirect_cart_service.php" in site_result.reply_text


class _FakeResponse:
    status_code = 200

    def json(self):
        return {"success": True}


class _FakeHttp:
    def __init__(self):
        self.calls = []

    async def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return _FakeResponse()


@pytest.mark.asyncio
async def test_client_uses_absolute_quantity_internal_put_contract():
    http = _FakeHttp()
    client = TrayAdapterClient("https://adapter.example", "secret", http)
    await client.set_cart_item_quantity(
        session_id="S1", product_id=803, variant_id=None, quantity=1,
    )
    args, kwargs = http.calls[0]
    assert args == ("PUT", "https://adapter.example/internal/carts/S1/items")
    assert kwargs["json"] == {
        "product_id": "803", "quantity": 1,
    }


@pytest.mark.asyncio
async def test_internal_quantity_tool_preserves_reconciled_facts():
    class Adapter:
        async def set_cart_item_quantity(self, **arguments):
            assert arguments["session_id"] == "S1"
            return {
                "success": True,
                "changed": True,
                "already_satisfied": False,
                "cart": {
                    "session_id": "S1",
                    "subtotal": "3799.99",
                    "total": "3799.99",
                    "items": [{
                        "product_id": 803, "variant_id": None,
                        "quantity": 1, "unit_price": "3799.99",
                    }],
                },
            }

    result = await execute_tool(
        "set_cart_item_quantity",
        {"session_id": "S1", "product_id": "803", "variant_id": None, "quantity": 1},
        Adapter(),
    )
    assert result["changed"] is True
    assert result["already_satisfied"] is False
    assert result["items"][0]["quantity"] == 1
    assert result["total"] == "3799.99"
