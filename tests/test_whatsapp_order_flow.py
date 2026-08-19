from __future__ import annotations

import pytest
from types import SimpleNamespace

from app.checkout_data_service import update_checkout_data
from app.commerce_context import CommerceConversationState, evolve_commerce_state
from app.payment_service import checkout_payment_blockers, inspect_payment_options
from app.order_service import (
    confirm_prepared_order,
    create_order,
    get_order_facts,
    prepare_order,
)
from app.shipping_service import quote_shipping, select_shipping
from app.models import AgentResult, IncomingMessage, SalesInterpretation


def _cart(*, variant=True, multiple=False):
    items = [{
        "product_id": "803",
        "variant_id": "123" if variant else None,
        "quantity": 1,
        "unit_price": "4699.99",
        "name": "Relogio",
    }]
    if multiple:
        items.append({
            "product_id": "804",
            "variant_id": None,
            "quantity": 2,
            "price": "100.00",
            "name": "Acessorio",
        })
    return {"items": items, "subtotal": "4899.99" if multiple else "4699.99"}


def _whatsapp_state(**overrides):
    payload = {
        "active_domain": "commerce",
        "checkout_channel_preference": "whatsapp",
        "cart_session_id": "SESSION-1",
        "cart_url": "https://loja.example/checkout/SESSION-1",
        "cart_items": [{
            "product_id": "803", "variant_id": "123", "quantity": 1,
            "unit_price": "4699.99", "original_price": "4699.99",
        }],
    }
    payload.update(overrides)
    return CommerceConversationState(**payload)


def _ready_state(**overrides):
    payload = {
        **_whatsapp_state().model_dump(mode="json"),
        "shipping_quote_zipcode": "19900000",
        "shipping_quotes": [{
            "shipping_id": "1",
            "quotation_id": "Q1",
            "name": "PAC Tray",
            "price": "35.10",
            "min_period": 3,
            "max_period": 8,
        }],
        "selected_shipping": {
            "shipping_id": "1",
            "quotation_id": "Q1",
            "name": "PAC Tray",
            "price": "35.10",
            "min_period": 3,
            "max_period": 8,
        },
        "selected_payment_method": "pix",
        "selected_payment_option_id": "10545",
        "selected_payment_option": {
            "id": "10545", "name": "Pix - Vindi", "method": "pix",
        },
        "checkout_draft": {
            "customer": {
                "type": "0", "name": "Joao Pedro", "cpf": "52998224725",
                "email": "joao@example.com", "phone": "11999999999",
            },
            "address": {
                "address": "Rua Um", "zip_code": "19900000", "number": "10",
                "neighborhood": "Centro", "city": "Ourinhos", "state": "SP",
                "country": "BRA", "type": "1",
            },
        },
    }
    payload.update(overrides)
    return CommerceConversationState(**payload)


def _interpretation(**overrides):
    payload = {
        "domain": "commerce",
        "goal": "buy",
        "subject": {},
        "preferences": {},
        "information_needed": [],
        "references_previous_context": True,
        "needs_clarification": False,
        "confidence": 0.99,
    }
    payload.update(overrides)
    return SalesInterpretation(**payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("variant,multiple", [(True, False), (False, False), (True, True)])
async def test_shipping_quote_uses_only_complete_cart_products(variant, multiple):
    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_cart_complete":
            return _cart(variant=variant, multiple=multiple)
        if tool == "quote_shipping":
            return {
                "success": True,
                "zipcode": "19900000",
                "options": [{
                    "shipping_id": 1, "quotation_id": "Q1", "name": "PAC Tray",
                    "price": "35.10", "min_period": 3, "max_period": 8,
                }],
            }
        raise AssertionError(tool)

    result = await quote_shipping(
        state=_whatsapp_state(), zipcode="19900-000", execute=execute,
    )

    assert result.safety_reason is None
    posted = calls[1][1]
    assert posted["zipcode"] == "19900000"
    assert len(posted["products"]) == (2 if multiple else 1)
    assert posted["products"][0] == {
        "product_id": "803",
        "variant_id": "123" if variant else None,
        "price": "4699.99",
        "quantity": 1,
    }
    assert result.commercial_data["options"][0]["price"] == "35.10"


@pytest.mark.asyncio
async def test_shipping_uses_reconciled_cart_price_when_complete_read_omits_it():
    calls = []
    state = _whatsapp_state(cart_items=[{
        "product_id": "803",
        "variant_id": "123",
        "quantity": 1,
        "unit_price": "4699.99",
    }])

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_cart_complete":
            return {
                "items": [{
                    "product_id": "803",
                    "variant_id": "123",
                    "quantity": 1,
                }]
            }
        if tool == "quote_shipping":
            return {
                "success": True,
                "options": [{
                    "shipping_id": 1,
                    "quotation_id": "Q1",
                    "name": "PAC Tray",
                    "price": "35.10",
                }],
            }
        raise AssertionError(tool)

    result = await quote_shipping(
        state=state,
        zipcode="86480000",
        execute=execute,
    )

    assert result.safety_reason is None
    assert [name for name, _ in calls] == [
        "get_cart_complete",
        "quote_shipping",
    ]
    assert calls[1][1]["products"] == [{
        "product_id": "803",
        "variant_id": "123",
        "price": "4699.99",
        "quantity": 1,
    }]


@pytest.mark.asyncio
async def test_shipping_rejects_invalid_zipcode_adapter_error_and_empty_options():
    async def never(*_args):
        raise AssertionError("invalid zipcode must not call adapter")

    invalid = await quote_shipping(state=_whatsapp_state(), zipcode="123", execute=never)
    assert invalid.safety_reason == "shipping_zipcode_invalid"

    async def failing(tool, _arguments):
        if tool == "get_cart_complete":
            return _cart()
        return {"error": "commerce_upstream_error", "status_code": 400}

    failed = await quote_shipping(state=_whatsapp_state(), zipcode="19900000", execute=failing)
    assert failed.commercial_data == {
        "success": False, "stage": "shipping_quote", "recoverable": False,
    }

    async def empty(tool, _arguments):
        return _cart() if tool == "get_cart_complete" else {"success": True, "options": []}

    no_options = await quote_shipping(state=_whatsapp_state(), zipcode="19900000", execute=empty)
    assert no_options.safety_reason == "shipping_options_empty"
    assert no_options.commercial_data["options"] == []


def test_shipping_selection_accepts_only_active_quote_and_never_free_price():
    state = _ready_state(selected_shipping=None)
    selected = select_shipping(state, selection_position=1)
    updated = evolve_commerce_state(state, selected)

    assert selected.safety_reason is None
    assert updated.selected_shipping is not None
    assert updated.selected_shipping.price == "35.10"
    rejected = select_shipping(state, selection_id="999")
    assert rejected.safety_reason == "shipping_selection_invalid"


def test_checkout_draft_partial_updates_and_missing_fields_are_persistent():
    state = _whatsapp_state()
    first = update_checkout_data(state, {"name": "Joao", "cpf": "529.982.247-25"})
    state = evolve_commerce_state(state, first)
    assert "name" not in first.commercial_data["missing_fields"]
    assert "email" in first.commercial_data["missing_fields"]

    second = update_checkout_data(state, {"email": "JOAO@example.com"})
    state = evolve_commerce_state(state, second)
    assert state.checkout_draft.customer.name == "Joao"
    assert state.checkout_draft.customer.cpf == "52998224725"
    assert state.checkout_draft.customer.email == "joao@example.com"

    address = update_checkout_data(state, {
        "phone": "(55) 11 99999-9999", "address": "Rua Um",
        "zipcode": "19900-000", "number": "10", "neighborhood": "Centro",
        "city": "Ourinhos", "state": "sp",
    })
    assert address.commercial_data["missing_fields"] == []


def test_checkout_block_merges_valid_fields_when_cpf_is_absent():
    state = _ready_state(
        shipping_quote_zipcode="86480000",
        checkout_draft={
            "customer": {"type": "0"},
            "address": {"zip_code": "86480000", "country": "BRA", "type": "1"},
        },
    )
    result = update_checkout_data(state, {
        "name": "joao pedro",
        "email": "jpfmatosfm@gmail.com",
        "phone": "43998640480",
        "city": "Conselheiro Mairinck",
        "state": "pr",
        "address": "Rua Prefeito Paulo de Oliveira",
        "number": "345",
        "neighborhood": "Centro",
    })
    updated = evolve_commerce_state(state, result)

    assert result.commercial_data["missing_fields"] == ["cpf"]
    assert updated.checkout_draft.customer.name == "joao pedro"
    assert updated.checkout_draft.customer.email == "jpfmatosfm@gmail.com"
    assert updated.checkout_draft.customer.phone == "43998640480"
    assert updated.checkout_draft.address.city == "Conselheiro Mairinck"
    assert updated.checkout_draft.address.state == "PR"
    assert updated.checkout_draft.address.zip_code == "86480000"


def test_full_state_name_is_normalized_without_discarding_checkout_data():
    state = _whatsapp_state()
    first = update_checkout_data(state, {
        "name": "Joao Pedro", "email": "jpfmatosfm@gmail.com",
        "phone": "43998640480", "address": "Rua Prefeito Paulo de Oliveira",
        "number": "345", "neighborhood": "Centro", "city": "Conselheiro Mairinck",
        "state": "Parana", "zipcode": "86480000",
    })
    state = evolve_commerce_state(state, first)
    assert first.commercial_data["field_errors"] == {}
    assert state.checkout_draft.customer.email == "jpfmatosfm@gmail.com"
    assert state.checkout_draft.address.city == "Conselheiro Mairinck"
    assert state.checkout_draft.address.state == "PR"

    second = update_checkout_data(state, {"state": "PR"})
    state = evolve_commerce_state(state, second)
    assert state.checkout_draft.address.state == "PR"
    assert state.checkout_draft.customer.email == "jpfmatosfm@gmail.com"


@pytest.mark.asyncio
async def test_checkout_data_and_pix_are_processed_in_the_same_turn(monkeypatch):
    import app.sales_agent as sales_agent

    async def execute(tool, _arguments):
        if tool == "get_cart_complete":
            return _cart()
        if tool == "get_payment_options":
            return {"payment_options": {
                "pix": {"id": "10545", "name": "Pix - Gateway XPTO"},
                "options": [{"id": "10545", "name": "Pix - Gateway XPTO"}],
            }}
        raise AssertionError(tool)

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(
        sales_agent, "get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )
    state = _ready_state(
        selected_payment_method=None,
        selected_payment_option=None,
        checkout_draft={
            **_ready_state().checkout_draft.model_dump(mode="json"),
            "customer": {
                **_ready_state().checkout_draft.customer.model_dump(mode="json"),
                "cpf": None,
            },
        },
    )
    first = await sales_agent.handle_sales_message(
        IncomingMessage(text="dados e pix"), {}, {},
        _interpretation(
            checkout_data={"name": "Joao Pedro", "email": "jpfmatosfm@gmail.com", "phone": "43998640480",
                           "address": "Rua Prefeito Paulo de Oliveira", "number": "345", "neighborhood": "Centro",
                           "city": "Conselheiro Mairinck", "state": "PR"},
            payment_action="payment_options",
            payment_method_preference="pix",
            payment_request_kind="checkout",
        ),
        commerce_state=state,
    )
    state = evolve_commerce_state(state, first)

    assert first.commercial_data["missing_fields"] == ["cpf"]
    assert state.checkout_draft.customer.email == "jpfmatosfm@gmail.com"
    assert state.payment_method_preference == "pix"

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="10425415902"), {}, {},
        _interpretation(checkout_data={"cpf": "10425415902"}),
        commerce_state=state,
    )
    updated = evolve_commerce_state(state, result)

    assert updated.checkout_draft.customer.email == "jpfmatosfm@gmail.com"
    assert updated.payment_method_preference == "pix"
    assert updated.selected_payment_option is not None
    assert updated.selected_payment_option.name == "Pix - Gateway XPTO"


@pytest.mark.asyncio
async def test_remove_request_overrides_pending_zipcode_without_local_cart_mutation(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []
    sessions = {
        "SESSION-1": [
            {"product_id": "803", "variant_id": "123", "quantity": 1, "unit_price": "4699.99"},
            {"product_id": "804", "variant_id": None, "quantity": 1, "unit_price": "2000.00"},
        ]
    }

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        if tool == "get_cart_complete":
            session_id = arguments.get("session_id")
            return {"items": sessions.get(session_id, [])}
        if tool == "delete_cart":
            session_id = arguments.get("session_id")
            if session_id in sessions:
                del sessions[session_id]
            return {"success": True}
        if tool == "create_cart":
            # When called with product_id, it adds item to cart
            if "product_id" in arguments:
                session_id = arguments["session_id"]
                if session_id not in sessions:
                    sessions[session_id] = []
                sessions[session_id].append({
                    "product_id": arguments["product_id"],
                    "variant_id": arguments.get("variant_id"),
                    "quantity": arguments.get("quantity", 1),
                    "unit_price": arguments.get("unit_price", "0.00"),
                })
                return {"success": True, "quantity": arguments.get("quantity", 1)}
            else:
                # When called without product_id, it creates a new session
                session_id = arguments["session_id"]
                sessions[session_id] = []
                return {"session_id": session_id}
        if tool == "add_cart_item":
            session_id = arguments.get("session_id")
            if session_id not in sessions:
                sessions[session_id] = []
            sessions[session_id].append({
                "product_id": arguments["product_id"],
                "variant_id": arguments.get("variant_id"),
                "quantity": arguments.get("quantity", 1),
                "unit_price": arguments.get("unit_price", "0.00"),
            })
            return {"success": True, "quantity": arguments.get("quantity", 1)}
        return {}

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(
        sales_agent, "get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )
    state = _whatsapp_state(
        pending_action="awaiting_shipping_zipcode",
        cart_items=[
            {"product_id": "803", "variant_id": "123", "quantity": 1, "unit_price": "4699.99", "name": "Citizen"},
            {"product_id": "804", "variant_id": None, "quantity": 1, "unit_price": "2000.00", "name": "Tissot"},
        ],
    )
    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="tire o primeiro relogio"), {}, {},
        _interpretation(purchase_action="remove_cart_item", reference_type="list_position", reference_position=1),
        commerce_state=state,
    )

    assert result.safety_reason is None
    assert result.commercial_data["removal_requested"] is True
    assert result.commercial_data["removal_supported"] is True
    assert result.commercial_data["mutation_success"] is True
    assert len(result.commercial_data["cart_items"]) == 1
    assert result.commercial_data["cart_items"][0]["product_id"] == "804"
    assert result.response_metadata["clear_pending_action"] is True


@pytest.mark.asyncio
async def test_quantity_request_overrides_pending_zipcode(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []
    quantity_updated = False

    async def execute(tool, arguments):
        nonlocal quantity_updated
        calls.append((tool, arguments))
        if tool == "get_cart_complete":
            return {"cart_url": "https://loja.example/checkout/SESSION-1", "items": [{
                "product_id": "803", "variant_id": "123",
                "quantity": 1 if quantity_updated else 2,
                "unit_price": "4699.99", "name": "Citizen",
            }]}
        if tool == "set_cart_item_quantity":
            quantity_updated = True
            return {"success": True}
        raise AssertionError(tool)

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(
        sales_agent, "get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )
    state = _whatsapp_state(
        active_product={"product_id": "803", "variant_id": "123", "name": "Citizen"},
        cart_items=[{"product_id": "803", "variant_id": "123", "quantity": 2}],
        pending_action="awaiting_shipping_zipcode",
    )
    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="quero alterar a quantidade para 1"), {}, {},
        _interpretation(
            purchase_action="set_cart_item_quantity", quantity=1,
            reference_type="current_product", confirmation="confirm",
        ),
        commerce_state=state,
    )

    assert "set_cart_item_quantity" in [tool for tool, _ in calls]
    assert result.safety_reason != "checkout_requirements_missing"
    assert result.response_metadata["cart_state"]["cart_quantity"] == 1


@pytest.mark.asyncio
async def test_quantity_change_requotes_with_saved_zipcode(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []
    quantity_updated = False

    async def execute(tool, arguments):
        nonlocal quantity_updated
        calls.append((tool, arguments))
        if tool == "get_cart_complete":
            return {**_cart(), "items": [{
                "product_id": "803", "variant_id": "123",
                "quantity": 1 if quantity_updated else 2,
                "unit_price": "4699.99", "name": "Citizen",
            }]}
        if tool == "set_cart_item_quantity":
            quantity_updated = True
            return {**_cart(), "items": [{
                "product_id": "803", "variant_id": "123", "quantity": 1,
                "unit_price": "4699.99", "name": "Citizen",
            }]}
        if tool == "quote_shipping":
            return {"success": True, "options": [{
                "shipping_id": "SEDEX", "quotation_id": "Q2", "name": "Sedex",
                "price": "74.97", "min_period": 40, "max_period": 40,
            }]}
        raise AssertionError(tool)

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(
        sales_agent, "get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )
    state = _ready_state(
        active_product={"product_id": "803", "variant_id": "123", "name": "Citizen"},
        cart_items=[{"product_id": "803", "variant_id": "123", "quantity": 2}],
        shipping_quote_zipcode="86480000",
        checkout_draft={
            **_ready_state().checkout_draft.model_dump(mode="json"),
            "address": {
                **_ready_state().checkout_draft.address.model_dump(mode="json"),
                "zip_code": "86480000",
            },
        },
    )
    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="somente uma unidade"), {}, {},
        _interpretation(
            purchase_action="set_cart_item_quantity", quantity=1,
            reference_type="current_product",
        ),
        commerce_state=state,
    )
    updated = evolve_commerce_state(state, result)

    assert any(tool == "quote_shipping" and args["zipcode"] == "86480000" for tool, args in calls)
    assert updated.checkout_draft.address.zip_code == "86480000"
    assert updated.shipping_quotes and updated.shipping_quotes[0].name == "Sedex"
    assert updated.selected_shipping is None


@pytest.mark.asyncio
async def test_shipping_quote_preserves_factual_names_per_cart_item():
    state = _whatsapp_state(
        cart_items=[
            {"product_id": "803", "variant_id": "123", "quantity": 1, "name": "Citizen"},
            {"product_id": "804", "variant_id": None, "quantity": 1, "name": "Tissot"},
        ],
    )

    async def execute(tool, _arguments):
        if tool == "get_cart_complete":
            return {"items": [
                {"product_id": "803", "variant_id": "123", "quantity": 1, "price": "100.00"},
                {"product_id": "804", "variant_id": None, "quantity": 1, "price": "200.00"},
            ]}
        assert tool == "quote_shipping"
        return {"success": True, "options": [{
            "shipping_id": "1", "name": "Sedex", "price": "20.00",
        }]}

    result = await quote_shipping(state=state, zipcode="13010000", execute=execute)
    updated = evolve_commerce_state(state, result)

    assert [(item.product_id, item.variant_id, item.quantity, item.name) for item in updated.cart_items] == [
        ("803", "123", 1, "Citizen"),
        ("804", None, 1, "Tissot"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("preference", "option_name"),
    [
        ("pix", "Pix - Gateway XPTO"),
        ("card", "Cartao - Gateway XPTO"),
        ("boleto", "Boleto - Gateway XPTO"),
    ],
)
async def test_isolated_payment_preference_is_persisted_until_cpf_arrives(
    monkeypatch, preference, option_name,
):
    import app.sales_agent as sales_agent

    calls = []

    async def execute(tool, _arguments):
        calls.append(tool)
        if tool == "get_cart_complete":
            return _cart()
        if tool == "get_payment_options":
            return {"payment_options": {
                preference: {"id": preference.upper(), "name": option_name},
                "options": [{"id": preference.upper(), "name": option_name}],
            }}
        raise AssertionError(tool)

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(
        sales_agent, "get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )
    state = _ready_state(
        selected_payment_method=None,
        selected_payment_option=None,
        checkout_draft={
            **_ready_state().checkout_draft.model_dump(mode="json"),
            "customer": {
                **_ready_state().checkout_draft.customer.model_dump(mode="json"),
                "cpf": None,
            },
        },
    )
    first = await sales_agent.handle_sales_message(
        IncomingMessage(text=preference), {}, {},
        _interpretation(
            payment_action="payment_options",
            payment_method_preference=preference,
            payment_request_kind="checkout",
        ),
        commerce_state=state,
    )
    state = evolve_commerce_state(state, first)

    assert state.payment_method_preference == preference
    assert state.checkout_draft.customer.cpf is None
    assert calls == []

    second = await sales_agent.handle_sales_message(
        IncomingMessage(text="10425415902"), {}, {},
        _interpretation(checkout_data={"cpf": "10425415902"}),
        commerce_state=state,
    )
    updated = evolve_commerce_state(state, second)

    assert updated.selected_payment_method == preference
    assert updated.selected_payment_option.name == option_name
    assert calls == ["get_cart_complete", "get_payment_options", "get_cart_complete"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("previous", "replacement", "previous_name"),
    [
        ("pix", "card", "Pix - Gateway XPTO"),
        ("card", "pix", "Cartao - Gateway XPTO"),
    ],
)
async def test_isolated_payment_preference_replaces_an_incompatible_selection(
    monkeypatch, previous, replacement, previous_name,
):
    import app.sales_agent as sales_agent

    async def never_execute(*_args, **_kwargs):
        raise AssertionError("incomplete checkout must not query payment options")

    monkeypatch.setattr(sales_agent, "execute_tool", never_execute)
    monkeypatch.setattr(
        sales_agent, "get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )
    state = _ready_state(
        payment_method_preference=previous,
        selected_payment_method=previous,
        selected_payment_option_id=f"{previous}-1",
        selected_payment_option={
            "id": f"{previous}-1", "name": previous_name, "method": previous,
        },
        checkout_draft={
            **_ready_state().checkout_draft.model_dump(mode="json"),
            "customer": {
                **_ready_state().checkout_draft.customer.model_dump(mode="json"),
                "cpf": None,
            },
        },
    )
    result = await sales_agent.handle_sales_message(
        IncomingMessage(text=f"prefiro {replacement}"), {}, {},
        _interpretation(
            payment_action="payment_options",
            payment_method_preference=replacement,
            payment_request_kind="checkout",
        ),
        commerce_state=state,
    )

    updated = evolve_commerce_state(state, result)
    assert updated.payment_method_preference == replacement
    assert updated.selected_payment_method is None
    assert updated.selected_payment_option_id is None
    assert updated.selected_payment_option is None
    assert updated.checkout_draft.customer.cpf is None
    assert updated.checkout_draft.customer.email == state.checkout_draft.customer.email
    assert updated.shipping_quotes == state.shipping_quotes
    assert updated.selected_shipping == state.selected_shipping
    assert updated.cart_items == state.cart_items


@pytest.mark.asyncio
async def test_repeated_payment_preference_keeps_a_compatible_factual_selection(monkeypatch):
    import app.sales_agent as sales_agent

    async def never_execute(*_args, **_kwargs):
        raise AssertionError("incomplete checkout must not query payment options")

    monkeypatch.setattr(sales_agent, "execute_tool", never_execute)
    monkeypatch.setattr(
        sales_agent, "get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )
    state = _ready_state(
        payment_method_preference="pix",
        checkout_draft={
            **_ready_state().checkout_draft.model_dump(mode="json"),
            "customer": {
                **_ready_state().checkout_draft.customer.model_dump(mode="json"),
                "cpf": None,
            },
        },
    )
    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="pix"), {}, {},
        _interpretation(
            payment_action="payment_options", payment_method_preference="pix",
            payment_request_kind="checkout",
        ),
        commerce_state=state,
    )
    updated = evolve_commerce_state(state, result)

    assert updated.selected_payment_method == "pix"
    assert updated.selected_payment_option_id == "10545"
    assert updated.selected_payment_option.name == "Pix - Vindi"


@pytest.mark.asyncio
async def test_cpf_after_payment_change_selects_only_the_new_factual_method(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []

    async def execute(tool, _arguments):
        calls.append(tool)
        if tool == "get_cart_complete":
            return _cart()
        if tool == "get_payment_options":
            return {"payment_options": {
                "card": {"id": "CARD-1", "name": "Cartao - Gateway XPTO"},
                "options": [{"id": "CARD-1", "name": "Cartao - Gateway XPTO"}],
            }}
        raise AssertionError(tool)

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(
        sales_agent, "get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )
    state = _ready_state(
        payment_method_preference="pix",
        checkout_draft={
            **_ready_state().checkout_draft.model_dump(mode="json"),
            "customer": {
                **_ready_state().checkout_draft.customer.model_dump(mode="json"),
                "cpf": None,
            },
        },
    )
    changed = await sales_agent.handle_sales_message(
        IncomingMessage(text="prefiro cartao"), {}, {},
        _interpretation(
            payment_action="payment_options", payment_method_preference="card",
            payment_request_kind="checkout",
        ),
        commerce_state=state,
    )
    state = evolve_commerce_state(state, changed)
    completed = await sales_agent.handle_sales_message(
        IncomingMessage(text="10425415902"), {}, {},
        _interpretation(checkout_data={"cpf": "10425415902"}),
        commerce_state=state,
    )
    updated = evolve_commerce_state(state, completed)

    assert updated.payment_method_preference == "card"
    assert updated.selected_payment_method == "card"
    assert updated.selected_payment_option.name == "Cartao - Gateway XPTO"
    assert calls == ["get_cart_complete", "get_payment_options", "get_cart_complete"]


@pytest.mark.asyncio
async def test_payment_change_during_review_reprepares_the_only_confirmable_order(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []

    async def execute(tool, _arguments):
        calls.append(tool)
        if tool == "get_cart_complete":
            return _cart()
        if tool == "get_payment_options":
            return {"payment_options": {
                "card": {"id": "CARD-1", "name": "Cartao factual", "card": 1},
                "options": [{"id": "CARD-1", "name": "Cartao factual", "card": 1}],
            }}
        raise AssertionError(tool)

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(
        sales_agent, "get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )
    initial = _ready_state(payment_method_preference="pix")
    prepared = await prepare_order(state=initial, execute=execute)
    review_state = evolve_commerce_state(initial, prepared)
    old_review = review_state.order_review_version
    calls.clear()

    changed = await sales_agent.handle_sales_message(
        IncomingMessage(text="prefiro cartao"), {}, {},
        _interpretation(
            payment_action="payment_options", payment_method_preference="card",
            payment_request_kind="checkout",
        ),
        commerce_state=review_state,
    )
    updated = evolve_commerce_state(review_state, changed)

    assert updated.order_id is None
    assert updated.payment_method_preference == "card"
    assert updated.selected_payment_method == "card"
    assert updated.selected_payment_option.name == "Cartao factual"
    assert updated.pending_action == "awaiting_order_confirmation"
    assert updated.order_review_version != old_review
    assert calls == ["get_cart_complete", "get_payment_options", "get_cart_complete"]


@pytest.mark.asyncio
async def test_automatic_checkout_reuses_one_cart_snapshot_before_order_review(monkeypatch):
    import app.sales_agent as sales_agent

    calls = []

    async def execute(tool, _arguments):
        calls.append(tool)
        if tool == "get_cart_complete":
            return _cart()
        if tool == "quote_shipping":
            return {"success": True, "options": [{
                "shipping_id": "1", "quotation_id": "Q1", "name": "Sedex",
                "price": "35.10", "min_period": 3, "max_period": 8,
            }]}
        if tool == "get_payment_options":
            return {"payment_options": {
                "pix": {"id": "PIX-1", "name": "Pix factual"},
                "options": [{"id": "PIX-1", "name": "Pix factual"}],
            }}
        raise AssertionError(tool)

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    state = _ready_state(
        shipping_quote_zipcode=None, shipping_quotes=[], selected_shipping=None,
        payment_method_preference="pix", selected_payment_method=None,
        selected_payment_option=None, selected_payment_option_id=None,
    )
    result = await sales_agent._advance_whatsapp_checkout(
        state,
        AgentResult(
            reply_text="Dados persistidos.", intent="commerce",
            response_metadata={"domain": "commerce"},
        ),
        "pix",
        None,
    )
    updated = evolve_commerce_state(state, result)

    assert calls == [
        "get_cart_complete", "quote_shipping", "get_cart_complete",
        "get_payment_options",
    ]
    assert calls.count("get_cart_complete") <= 2
    assert updated.pending_action == "awaiting_order_confirmation"


@pytest.mark.asyncio
async def test_prepare_requires_shipping_and_email_then_sets_pending_when_ready():
    async def execute(_tool, _arguments):
        return _cart()

    no_shipping = await prepare_order(
        state=_ready_state(selected_shipping=None), execute=execute,
    )
    assert no_shipping.commercial_data["order_ready"] is False
    assert "selected_shipping" in no_shipping.commercial_data["missing_fields"]

    missing_email_state = _ready_state()
    missing_email_state.checkout_draft.customer.email = None
    missing_email = await prepare_order(state=missing_email_state, execute=execute)
    assert "email" in missing_email.commercial_data["missing_fields"]

    ready = await prepare_order(state=_ready_state(), execute=execute)
    assert ready.commercial_data["order_ready"] is True
    assert ready.commercial_data["display_total"] == "4735.09"
    assert ready.response_metadata["order_state"]["order_confirmation_status"] == "pending"
    assert ready.response_metadata["pending_action"] == "awaiting_order_confirmation"


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["sim", "confirmo", "pode finalizar"])
async def test_textual_order_confirmation_bypasses_interpreter_and_creates_once(monkeypatch, text):
    import app.sales_agent as sales_agent

    calls = []

    async def execute(tool, _arguments):
        calls.append(tool)
        if tool == "get_cart_complete":
            return _cart()
        if tool == "create_order":
            return {"order_id": "123", "status": "AGUARDANDO PAGAMENTO"}
        if tool == "get_payment_options":
            return {"payment_options": {"options": [{"id": "10545", "name": "Pix factual"}]}}
        if tool == "get_order_payment":
            return {"order_id": "123", "payment": {"has_payment": False}}
        raise AssertionError(tool)

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(
        sales_agent, "get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )
    prepared = await prepare_order(state=_ready_state(), execute=execute)
    review_state = evolve_commerce_state(_ready_state(), prepared)
    calls.clear()

    async def must_not_prepare(**_kwargs):
        raise AssertionError("confirmation must not prepare a new review")

    monkeypatch.setattr(sales_agent, "prepare_order", must_not_prepare)
    result = await sales_agent.handle_sales_message(
        IncomingMessage(text=text), {}, {}, _interpretation(confirmation="none"),
        commerce_state=review_state,
    )
    updated = evolve_commerce_state(review_state, result)

    assert calls.count("create_order") == 1
    assert updated.order_id == "123"
    assert "get_product" not in calls


@pytest.mark.asyncio
async def test_confirmation_text_with_payment_change_does_not_create_order(monkeypatch):
    import app.sales_agent as sales_agent

    state = _ready_state(payment_method_preference="pix")
    async def prepare_execute(_tool, _arguments):
        return _cart()

    prepared = await prepare_order(state=state, execute=prepare_execute)
    review_state = evolve_commerce_state(state, prepared)
    calls = []

    async def execute(tool, _arguments):
        calls.append(tool)
        if tool == "get_cart_complete":
            return _cart()
        if tool == "get_payment_options":
            return {"payment_options": {
                "card": {"id": "C1", "name": "Cartao factual", "card": 1},
                "options": [{"id": "C1", "name": "Cartao factual", "card": 1}],
            }}
        raise AssertionError(tool)

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"))
    changed = await sales_agent.handle_sales_message(
        IncomingMessage(text="sim, mas quero cartao"), {}, {},
        _interpretation(payment_action="payment_options", payment_method_preference="card"),
        commerce_state=review_state,
    )
    updated = evolve_commerce_state(review_state, changed)

    assert "create_order" not in calls
    assert updated.selected_payment_method == "card"
    assert updated.pending_action == "awaiting_order_confirmation"


@pytest.mark.asyncio
async def test_create_is_blocked_without_confirmation_and_stale_confirmation():
    calls = []

    async def execute(tool, arguments):
        calls.append((tool, arguments))
        return _cart()

    blocked = await create_order(state=_ready_state(), execute=execute)
    assert blocked.safety_reason == "order_confirmation_required"
    assert calls == []

    prepared = await prepare_order(state=_ready_state(), execute=execute)
    state = evolve_commerce_state(_ready_state(), prepared)
    confirmed = evolve_commerce_state(state, confirm_prepared_order(state))
    confirmed.selected_shipping.price = "40.00"
    stale = await create_order(state=confirmed, execute=execute)
    assert stale.safety_reason == "order_confirmation_stale"
    stale_state = evolve_commerce_state(confirmed, stale)
    assert stale_state.order_confirmation_status == "not_ready"
    assert stale_state.order_review_version is None
    assert stale_state.confirmed_order_review_version is None
    assert "selected_shipping_not_in_active_quote" in stale.commercial_data["missing_fields"]
    assert not any(tool == "create_order" for tool, _ in calls)


def test_changing_checkout_zipcode_invalidates_shipping_quote():
    state = _ready_state()

    result = update_checkout_data(state, {"zipcode": "01001000"})
    updated = evolve_commerce_state(state, result)

    assert result.commercial_data["shipping_quote_required"] is True
    assert updated.shipping_quote_zipcode is None
    assert updated.shipping_quotes == []
    assert updated.selected_shipping is None


def test_new_cart_session_starts_new_order_scope():
    previous = _ready_state(
        order_id="123",
        order_session_id="SESSION-1",
        order_status="AGUARDANDO PAGAMENTO",
    )
    cart_result = AgentResult(
        reply_text="Carrinho novo.",
        intent="commerce",
        response_metadata={
            "domain": "commerce",
            "cart_state": {
                "cart_session_id": "SESSION-2",
                "cart_url": "https://loja.example/checkout/SESSION-2",
                "cart_items": [{
                    "product_id": "804", "variant_id": None, "quantity": 1,
                }],
            },
        },
    )

    current = evolve_commerce_state(previous, cart_result)

    assert current.cart_session_id == "SESSION-2"
    assert current.order_id is None
    assert current.order_session_id is None
    assert current.selected_shipping is None
    assert current.selected_payment_option is None


def test_cart_reconciliation_keeps_whatsapp_shipping_and_checkout_state():
    previous = _ready_state(
        shipping_quote_zipcode="86480000",
        checkout_channel_preference="whatsapp",
    )
    reconciled = AgentResult(
        reply_text="Estado factual do carrinho confirmado.",
        intent="commerce",
        response_metadata={
            "domain": "commerce",
            "cart_state": {
                "cart_id": previous.cart_id,
                "cart_session_id": previous.cart_session_id,
                "cart_url": previous.cart_url,
                "cart_items": [item.model_dump(mode="json") for item in previous.cart_items],
            },
        },
    )

    current = evolve_commerce_state(previous, reconciled)

    assert current.shipping_quote_zipcode == "86480000"
    assert current.shipping_quotes
    assert current.selected_shipping is not None
    assert current.checkout_channel_preference == "whatsapp"
    assert current.selected_payment_method == "pix"
    assert current.selected_payment_option_id == "10545"


def test_payment_reconciliation_does_not_return_whatsapp_checkout_to_zipcode():
    previous = _ready_state(
        shipping_quote_zipcode="86480000",
        pending_action="awaiting_payment",
    )
    payment_reconciliation = AgentResult(
        reply_text="Consultei as formas de pagamento reais deste carrinho.",
        intent="commerce",
        response_metadata={
            "domain": "commerce",
            "purchase_stage": "payment_discussion",
            "clear_pending_action": True,
            "selected_payment_method": "pix",
            "selected_payment_option_id": "10545",
            "cart_state": {
                "cart_session_id": previous.cart_session_id,
                "cart_url": previous.cart_url,
                "cart_items": [item.model_dump(mode="json") for item in previous.cart_items],
            },
        },
    )

    current = evolve_commerce_state(previous, payment_reconciliation)

    assert "shipping_zipcode_missing" not in checkout_payment_blockers(current)
    assert current.pending_action != "awaiting_shipping_zipcode"
    assert current.shipping_quote_zipcode == "86480000"
    assert current.selected_shipping is not None


def test_material_cart_change_invalidates_whatsapp_shipping_and_payment_state():
    previous = _ready_state(shipping_quote_zipcode="86480000")
    changed_cart = AgentResult(
        reply_text="Carrinho atualizado.",
        intent="commerce",
        response_metadata={
            "domain": "commerce",
            "cart_materially_changed": True,
            "cart_state": {
                "cart_session_id": previous.cart_session_id,
                "cart_url": previous.cart_url,
                "cart_items": [{
                    "product_id": "803", "variant_id": "123", "quantity": 2,
                    "unit_price": "4699.99", "original_price": "4699.99",
                }],
            },
        },
    )

    current = evolve_commerce_state(previous, changed_cart)

    assert current.cart_items[0].quantity == 2
    assert current.shipping_quote_zipcode is None
    assert current.shipping_quotes == []
    assert current.selected_shipping is None
    assert current.selected_payment_method is None
    assert current.selected_payment_option is None


def test_legacy_commerce_state_roundtrip_remains_compatible():
    legacy_payload = {
        "active_domain": "commerce",
        "cart_session_id": "SESSION-1",
        "cart_items": [{"product_id": "803", "quantity": 1}],
        "checkout_channel_preference": "whatsapp",
    }

    restored = CommerceConversationState.from_payload(legacy_payload)

    assert restored.cart_session_id == "SESSION-1"
    assert restored.cart_items[0].product_id == "803"
    assert restored.shipping_quotes == []
    assert CommerceConversationState.from_payload(
        restored.model_dump(mode="json")
    ) == restored


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "option", "expected_installments"),
    [
        ("pix", {"id": "PIX", "name": "Pix - Gateway XPTO"}, []),
        ("card", {
            "id": "CARD", "name": "Cartao - Gateway XPTO",
            "plots": [{"count": 3, "value": 40.0}],
            "increase_value": 3.5,
        }, [{"count": 3, "value": 40.0}]),
        ("boleto", {"id": "BOLETO", "name": "Boleto - Gateway XPTO"}, []),
    ],
)
async def test_payment_lookup_keeps_whatsapp_checkout_after_price_reconciliation(
    method,
    option,
    expected_installments,
):
    previous = _ready_state(shipping_quote_zipcode="86480000")
    previous.checkout_draft.address.zip_code = "86480000"

    async def execute(tool, arguments):
        if tool == "get_cart_complete":
            assert arguments == {"session_id": "SESSION-1"}
            return {"items": [{
                "product_id": "803", "variant_id": "123", "quantity": 1,
                "unit_price": "4699.990", "original_price": "4699.990",
            }]}
        assert tool == "get_payment_options"
        return {"payment_options": {
            method: option,
            "options": [option],
            "installments": option.get("plots", []),
        }}

    result = await inspect_payment_options(
        state=previous,
        installment_count=None,
        payment_method_preference=method,
        execute=execute,
        advance_checkout=True,
    )
    current = evolve_commerce_state(previous, result)

    assert current.shipping_quote_zipcode == "86480000"
    assert current.shipping_quotes
    assert current.selected_shipping is not None
    assert current.checkout_channel_preference == "whatsapp"
    assert current.checkout_draft.address.zip_code == "86480000"
    assert current.selected_payment_option.name == option["name"]
    assert current.selected_payment_option.installments == expected_installments
    assert "shipping_zipcode_missing" not in checkout_payment_blockers(current)
    assert current.pending_action != "awaiting_shipping_zipcode"


@pytest.mark.asyncio
async def test_create_persists_order_and_prevents_duplicate_post():
    posts = 0

    async def execute(tool, arguments):
        nonlocal posts
        if tool == "get_cart_complete":
            return _cart()
        if tool == "create_order":
            posts += 1
            assert arguments["products"][0]["product_id"] == "803"
            assert arguments["payment"] == {"method_id": "10545", "name": "Pix - Vindi"}
            return {"success": True, "order_id": 123, "code": 201, "status": "AGUARDANDO VINDI"}
        raise AssertionError(tool)

    base = _ready_state()
    prepared = evolve_commerce_state(base, await prepare_order(state=base, execute=execute))
    confirmed = evolve_commerce_state(prepared, confirm_prepared_order(prepared))
    created = await create_order(state=confirmed, execute=execute)
    state = evolve_commerce_state(confirmed, created)

    assert state.order_id == "123"
    assert state.purchase_stage == "order_created"
    assert state.order_confirmation_status == "not_ready"
    duplicate = await create_order(state=state, execute=execute)
    assert duplicate.commercial_data["existing"] is True
    assert posts == 1


@pytest.mark.asyncio
async def test_ambiguous_post_reconciles_by_session_id():
    async def execute(tool, arguments):
        if tool == "get_cart_complete":
            return _cart()
        if tool == "create_order":
            return {"error": "commerce_upstream_error", "status_code": 503}
        if tool == "list_orders":
            assert arguments == {"session_id": "SESSION-1"}
            return {"orders": [{"order_id": "321", "status": "AGUARDANDO PAGAMENTO"}]}
        raise AssertionError(tool)

    base = _ready_state()
    prepared = evolve_commerce_state(base, await prepare_order(state=base, execute=execute))
    confirmed = evolve_commerce_state(prepared, confirm_prepared_order(prepared))
    created = await create_order(state=confirmed, execute=execute)
    assert created.commercial_data["order_id"] == "321"


@pytest.mark.asyncio
async def test_created_order_survives_payment_lookup_failure(monkeypatch):
    import app.sales_agent as sales_agent

    async def execute(tool, _arguments):
        if tool == "get_cart_complete":
            return _cart()
        if tool == "create_order":
            return {
                "success": True,
                "order_id": "777",
                "status": "AGUARDANDO VINDI",
            }
        if tool == "get_order_payment":
            return {
                "error": "commerce_upstream_error",
                "status_code": 500,
            }
        raise AssertionError(tool)

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    base = _ready_state()
    prepared = evolve_commerce_state(
        base, await prepare_order(state=base, execute=execute),
    )
    confirmed = evolve_commerce_state(
        prepared, confirm_prepared_order(prepared),
    )

    result = await sales_agent._create_order_with_payment_lookup(confirmed)
    current = evolve_commerce_state(confirmed, result)

    assert result.commercial_data["success"] is True
    assert result.commercial_data["order_id"] == "777"
    assert result.commercial_data["payment"]["status"] == "unknown"
    assert result.safety_reason == "order_payment_technical_failure"
    assert current.order_id == "777"
    assert current.order_payment_status == "unknown"
    assert current.purchase_stage == "order_created"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,status_group",
    [
        ("AGUARDANDO VINDI", "awaiting_payment"),
        ("A ENVIAR VINDI", "awaiting_shipment"),
        ("ENVIADO", "shipped"),
        ("FINALIZADO", "completed"),
    ],
)
async def test_status_and_tracking_preserve_adapter_facts(status, status_group):
    async def execute(tool, arguments):
        assert tool == "get_order_complete"
        assert arguments == {"order_id": "123"}
        return {
            "order_id": "123", "status": status, "status_group": status_group,
            "sending_code": "TRACK123", "tracking_url": "https://track.example/123",
            "estimated_delivery_date": "2026-08-10",
        }

    result = await get_order_facts(
        state=_ready_state(order_id="123"), execute=execute,
    )
    assert result.commercial_data["status"] == status
    assert result.commercial_data["status_group"] == status_group
    assert result.commercial_data["tracking"] == {
        "sending_code": "TRACK123",
        "tracking_url": "https://track.example/123",
        "estimated_delivery_date": "2026-08-10",
    }


@pytest.mark.asyncio
async def test_tracking_absence_is_not_filled():
    async def execute(_tool, _arguments):
        return {"order_id": "123", "status": "A ENVIAR"}

    result = await get_order_facts(
        state=_ready_state(order_id="123"), execute=execute,
    )
    assert result.commercial_data["tracking"] == {}


@pytest.mark.asyncio
async def test_site_channel_cannot_start_whatsapp_shipping():
    async def never(*_args):
        raise AssertionError("site flow must not call shipping quote")

    state = _whatsapp_state(checkout_channel_preference="site")
    result = await quote_shipping(state=state, zipcode="19900000", execute=never)
    assert result.safety_reason == "whatsapp_order_channel_required"


@pytest.mark.asyncio
async def test_sales_agent_orchestrates_whatsapp_flow_without_real_openai(monkeypatch):
    import app.sales_agent as sales_agent

    posts = 0

    async def execute(tool, arguments):
        nonlocal posts
        if tool == "get_cart_complete":
            return _cart()
        if tool == "quote_shipping":
            return {"success": True, "options": [{
                "shipping_id": "1", "quotation_id": "Q1", "name": "PAC Tray",
                "price": "35.10", "min_period": 3, "max_period": 8,
            }]}
        if tool == "get_payment_options":
            return {"payment_options": {
                "pix": {"id": "10545", "name": "Pix - Vindi"},
                "options": [{"id": "10545", "name": "Pix - Vindi"}],
            }}
        if tool == "create_order":
            posts += 1
            return {"success": True, "order_id": "900", "status": "AGUARDANDO VINDI"}
        if tool == "get_order_payment":
            assert arguments == {"order_id": "900"}
            return {
                "success": True,
                "order_id": "900",
                "payment": {
                    "method_id": "10545",
                    "method": "Pix - Vindi",
                    "type": "pix",
                    "has_payment": False,
                    "payment_date": None,
                    "payment_url": "https://pay.example/order/900?token=official",
                },
            }
        raise AssertionError(tool)

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(
        sales_agent,
        "get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )
    state = _whatsapp_state()

    async def turn(interpretation):
        nonlocal state
        result = await sales_agent.handle_sales_message(
            IncomingMessage(text="mensagem interpretada"), {}, {}, interpretation,
            commerce_state=state,
        )
        state = evolve_commerce_state(state, result)
        return result

    await turn(_interpretation(shipping_action="quote", shipping_zipcode="19900-000"))
    await turn(_interpretation(shipping_action="select", shipping_selection_position=1))
    await turn(_interpretation(
        checkout_data={
            "name": "Joao", "cpf": "52998224725", "email": "joao@example.com",
            "phone": "11999999999", "address": "Rua Um", "zipcode": "19900000",
            "number": "10", "neighborhood": "Centro", "city": "Ourinhos", "state": "SP",
        }
    ))
    await turn(_interpretation(
        payment_action="payment_options", payment_method_preference="pix",
        information_needed=["payment"],
    ))
    prepared = await turn(_interpretation(checkout_action="prepare_order"))
    assert prepared.commercial_data["order_ready"] is True
    created = await turn(_interpretation(confirmation="confirm"))

    assert created.commercial_data["order_id"] == "900"
    assert created.commercial_data["payment"]["status"] == "pending"
    assert created.commercial_data["payment"]["payment_url"] == (
        "https://pay.example/order/900?token=official"
    )
    assert state.order_id == "900"
    assert state.purchase_stage == "awaiting_payment"
    assert state.order_payment_status == "pending"
    assert state.pending_action == "awaiting_payment"
    assert posts == 1


@pytest.mark.asyncio
async def test_order_failure_propagates_adapter_validation_error_field():
    async def execute(tool, arguments):
        if tool == "get_cart_complete":
            return _cart()
        if tool == "create_order":
            return {
                "error": "validation_error",
                "status_code": 422,
                "tray_error_field": "body.customer.phone",
                "tray_error_fields": ["body.customer.phone"],
                "tray_error_message": "Value error, must contain 10 or 11 digits",
            }
        raise AssertionError(tool)

    base = _ready_state()
    prepared = evolve_commerce_state(base, await prepare_order(state=base, execute=execute))
    confirmed = evolve_commerce_state(prepared, confirm_prepared_order(prepared))
    created = await create_order(state=confirmed, execute=execute)

    assert created.commercial_data["success"] is False
    assert created.response_metadata["order_failure_status"] == 422
    assert created.response_metadata["order_failure_field"] == "body.customer.phone"
    assert created.response_metadata["order_failure_fields"] == ["body.customer.phone"]
    assert "must contain 10 or 11 digits" in created.response_metadata["order_failure_message"]


@pytest.mark.asyncio
async def test_order_failure_preserves_adapter_error_causes():
    async def execute(tool, arguments):
        if tool == "get_cart_complete":
            return _cart()
        if tool == "create_order":
            return {
                "success": False,
                "error": "tray_api_error",
                "status_code": 400,
                "tray_error_code": "INVALID_PAYMENT",
                "tray_error_causes": [
                    {"field": "payment_form", "message": "nao encontrado"},
                ],
            }
        raise AssertionError(tool)

    base = _ready_state()
    prepared = evolve_commerce_state(base, await prepare_order(state=base, execute=execute))
    confirmed = evolve_commerce_state(prepared, confirm_prepared_order(prepared))
    created = await create_order(state=confirmed, execute=execute)

    assert created.commercial_data["success"] is False
    assert created.response_metadata.get("order_failure_causes") == [
        {"field": "payment_form", "message": "nao encontrado"},
    ]


@pytest.mark.asyncio
async def test_removal_invalidates_state_after_removal(monkeypatch):
    import app.sales_agent as sales_agent
    from app.cart_service import resolve_cart_item_reference
    from app.models import SalesInterpretation

    monkeypatch.setattr(
        sales_agent, "get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )

    state = _ready_state(
        cart_items=[
            {"product_id": "803", "variant_id": "123", "quantity": 1, "unit_price": "4699.99", "name": "Citizen"},
            {"product_id": "804", "variant_id": None, "quantity": 1, "unit_price": "2000.00", "name": "Tissot"},
        ],
    )

    interpretation = _interpretation(purchase_action="remove_cart_item", reference_type="list_position", reference_position=1)

    targets, reason = resolve_cart_item_reference(interpretation, state)

    assert len(targets) == 1
    assert targets[0][0] == "803"
    assert reason == "list_position"
    assert state.selected_shipping is not None
    assert state.selected_payment_option is not None
