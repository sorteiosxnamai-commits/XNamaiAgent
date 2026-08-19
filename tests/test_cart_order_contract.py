from decimal import Decimal

import pytest

from app.cart_service import CartItemRequest, create_cart_items_checkout
from app.commerce_context import (
    CommerceConversationState,
    CommerceProductReference,
    normalize_variant_identity,
    evolve_commerce_state,
)
from app.order_service import (
    cart_order_products,
    confirm_prepared_order,
    create_order,
    prepare_order,
)
from app.shipping_service import cart_shipping_products, quote_shipping


@pytest.mark.parametrize("value", [None, "", 0, "0"])
def test_variant_sentinels_normalize_to_none(value):
    assert normalize_variant_identity(value) is None


def test_shipping_contract_normalizes_legacy_zero_variant():
    products = cart_shipping_products({
        "items": [{
            "product_id": "803",
            "variant_id": "0",
            "quantity": 1,
            "price": "3999.99",
        }],
    })

    assert products == [{
        "product_id": "803",
        "variant_id": None,
        "price": "3999.99",
        "quantity": 1,
    }]


@pytest.mark.asyncio
async def test_cart_persists_effective_and_original_promotional_prices():
    async def execute(tool, arguments):
        if tool == "get_product":
            return {
                "id": "803",
                "price": "3999.99",
                "promotional_price": "3399.99",
                "available": True,
            }
        if tool == "create_cart":
            return {
                "cart_id": "C1",
                "session_id": arguments["session_id"],
                "cart_url": "https://loja.example/checkout/C1",
            }
        if tool == "get_cart_complete":
            return {
                "cart_id": "C1",
                "items": [{
                    "product_id": "803",
                    "variant_id": "0",
                    "quantity": 1,
                    "price": "3399.99",
                }],
            }
        raise AssertionError(tool)

    result = await create_cart_items_checkout(
        item_requests=[CartItemRequest(
            CommerceProductReference(product_id="803"),
        )],
        state=CommerceConversationState(),
        execute=execute,
    )

    item = result.response_metadata["cart_state"]["cart_items"][0]
    assert item["variant_id"] is None
    assert item["unit_price"] == "3399.99"
    assert item["original_price"] == "3999.99"


def _ready_state(*, original_price="3999.99"):
    return CommerceConversationState(
        checkout_channel_preference="whatsapp",
        cart_session_id="SESSION-1",
        cart_items=[{
            "product_id": "803",
            "variant_id": "0",
            "quantity": 1,
            "unit_price": "3399.99",
            "original_price": original_price,
        }],
        shipping_quote_zipcode="86480000",
        shipping_quotes=[{
            "shipping_id": "1",
            "quotation_id": "Q1",
            "name": "PAC Tray",
            "price": "35.10",
        }],
        selected_shipping={
            "shipping_id": "1",
            "quotation_id": "Q1",
            "name": "PAC Tray",
            "price": "35.10",
        },
        selected_payment_method="pix",
        selected_payment_option_id="10545",
        selected_payment_option={
            "id": "10545",
            "name": "Pix - Vindi",
            "method": "pix",
        },
        checkout_draft={
            "customer": {
                "type": "0",
                "name": "Joao",
                "cpf": "52998224725",
                "email": "joao@example.com",
                "phone": "11999999999",
            },
            "address": {
                "address": "Rua Um",
                "zip_code": "86480000",
                "number": "10",
                "neighborhood": "Centro",
                "city": "Cornelio Procopio",
                "state": "PR",
                "country": "BRA",
                "type": "1",
            },
        },
    )


@pytest.mark.asyncio
async def test_order_contract_includes_factual_original_price_and_null_variant():
    captured = {}

    async def execute(tool, arguments):
        if tool == "get_cart_complete":
            return {
                "items": [{
                    "product_id": "803",
                    "variant_id": "0",
                    "quantity": 1,
                    "price": "3399.99",
                }],
                "subtotal": "3399.99",
            }
        if tool == "create_order":
            captured.update(arguments)
            return {"success": True, "order_id": "900"}
        raise AssertionError(tool)

    state = _ready_state()
    prepared = evolve_commerce_state(
        state,
        await prepare_order(state=state, execute=execute),
    )
    confirmed = evolve_commerce_state(
        prepared,
        confirm_prepared_order(prepared),
    )
    created = await create_order(state=confirmed, execute=execute)

    assert created.commercial_data["order_id"] == "900"
    assert captured["products"] == [{
        "product_id": "803",
        "variant_id": None,
        "price": "3399.99",
        "original_price": "3999.99",
        "quantity": 1,
    }]
    product = captured["products"][0]
    assert int(product["product_id"]) > 0
    assert product["variant_id"] is None
    assert Decimal(product["price"]) > 0
    assert Decimal(product["original_price"]) > 0
    assert isinstance(product["quantity"], int) and product["quantity"] > 0


def test_order_contract_preserves_positive_numeric_variant():
    products = cart_order_products(
        {"items": [{
            "product_id": 803,
            "variant_id": 123,
            "price": "3399.99",
            "original_price": "3999.99",
            "quantity": 1,
        }]},
        factual_items=[],
    )

    assert normalize_variant_identity(123) == "123"
    assert products[0]["variant_id"] == "123"


@pytest.mark.asyncio
async def test_missing_original_price_blocks_prepare_and_order_post():
    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_cart_complete":
            return {
                "items": [{
                    "product_id": "803",
                    "variant_id": "0",
                    "quantity": 1,
                    "price": "3399.99",
                }],
            }
        raise AssertionError("order POST must remain blocked")

    state = _ready_state(original_price=None)
    prepared = await prepare_order(state=state, execute=execute)
    assert prepared.commercial_data["order_ready"] is False
    assert "cart_original_price_missing" in prepared.commercial_data["missing_fields"]

    state.order_confirmation_status = "confirmed"
    state.order_review_version = "legacy"
    state.confirmed_order_review_version = "legacy"
    created = await create_order(state=state, execute=execute)
    assert created.safety_reason == "order_confirmation_stale"
    assert "cart_original_price_missing" in created.commercial_data["missing_fields"]
    assert not any(tool == "create_order" for tool, _ in calls)
@pytest.mark.asyncio
async def test_zipcode_quote_uses_complete_cart_and_nulls_legacy_zero_variant():
    calls = []
    state = CommerceConversationState(
        checkout_channel_preference="whatsapp",
        cart_session_id="SESSION-1",
        cart_items=[{
            "product_id": "803",
            "variant_id": "0",
            "quantity": 1,
            "unit_price": "3999.99",
            "original_price": "3999.99",
        }],
    )

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_cart_complete":
            return {"items": [{
                "product_id": "803",
                "variant_id": "0",
                "quantity": 1,
                "price": "3999.99",
            }]}
        if tool == "quote_shipping":
            return {"success": True, "options": [{
                "shipping_id": 1,
                "quotation_id": "Q1",
                "name": "PAC Tray",
                "price": "35.10",
            }]}
        raise AssertionError(tool)

    result = await quote_shipping(
        state=state,
        zipcode="86480000",
        execute=execute,
    )

    assert result.safety_reason is None
    assert [name for name, _ in calls] == ["get_cart_complete", "quote_shipping"]
    assert calls[1][1]["products"] == [{
        "product_id": "803",
        "variant_id": None,
        "price": "3999.99",
        "quantity": 1,
    }]


@pytest.mark.asyncio
async def test_shipping_422_preserves_structured_validation_diagnostics():
    state = CommerceConversationState(
        checkout_channel_preference="whatsapp",
        cart_session_id="SESSION-1",
        cart_items=[{
            "product_id": "803",
            "quantity": 1,
            "unit_price": "3999.99",
            "original_price": "3999.99",
        }],
    )

    async def execute(tool, _arguments):
        if tool == "get_cart_complete":
            return {"items": [{
                "product_id": "803",
                "variant_id": "0",
                "quantity": 1,
                "price": "3999.99",
            }]}
        return {
            "error": "commerce_upstream_error",
            "status_code": 422,
            "tray_error_field": "products.0.variant_id",
            "tray_error_fields": ["products.0.variant_id"],
            "tray_error_message": "invalid shipping product",
        }

    result = await quote_shipping(
        state=state,
        zipcode="86480000",
        execute=execute,
    )

    assert result.safety_reason == "shipping_quote_upstream_validation_failed"
    assert result.response_metadata["shipping_failure_status"] == 422
    assert result.response_metadata["shipping_failure_field"] == "products.0.variant_id"
    assert result.response_metadata["shipping_failure_fields"] == ["products.0.variant_id"]
    assert result.response_metadata["shipping_failure_message"] == "invalid shipping product"
