"""Contrato genérico de formas de pagamento do cérebro comercial.

Provider-agnóstico: todos os testes injetam ``execute`` e exercitam
``inspect_payment_options`` / ``_order_payment_revalidation`` / ``_responder_contract``,
que não conhecem provider algum. Os dois testes que validavam o *normalizador de
payload do TrayAdapter* foram removidos junto com o módulo (`app/tray_tools.py`);
o payload já normalizado permanece aqui como literal para preservar a cobertura
genérica de seleção exata de parcela e de restrições de pagamento por produto.
"""

import pytest

from app.commerce_context import CommerceConversationState, evolve_commerce_state
from app.payment_service import inspect_payment_options
from app.sales_agent import _order_payment_revalidation, _responder_contract

#: Payload já normalizado (forma neutra que qualquer CommerceProvider deve devolver).
NORMALIZED_PAYMENT_OPTIONS = {
    "payment_options": {
        "pix": {
            "id": "P1",
            "name": "Pagamento instantâneo",
            "discount_value": 100.0,
            "plots": [{"count": 1, "value": 1100.0}],
        },
        "card": {
            "id": "C1",
            "name": "Crédito",
            "plots": [
                {"count": 1, "value": 1100.0},
                {"count": 2, "value": 600.0},
                {"count": 10, "value": 125.0},
                {"count": 12, "value": 108.33},
            ],
        },
        "installments": [
            {
                "count": 1, "value": 1100.0, "interest": False, "interest_value": 0.0,
                "discount_value": 0.0, "base_value": 1200.0, "order_total": 1100.0,
            },
            {
                "count": 2, "value": 600.0, "interest": False, "interest_value": 0.0,
                "discount_value": 0.0, "base_value": 1200.0, "order_total": 1200.0,
            },
            {
                "count": 10, "value": 125.0, "interest": True, "interest_value": 4.5,
                "discount_value": 0.0, "base_value": 1200.0, "order_total": 1250.0,
            },
            {
                "count": 12, "value": 108.33, "interest": True, "interest_value": 4.5,
                "discount_value": 0.0, "base_value": 1200.0, "order_total": 1299.96,
            },
        ],
        "options": [
            {"id": "P1", "name": "Pagamento instantâneo"},
            {"id": "C1", "name": "Crédito", "card": 1},
        ],
    }
}


@pytest.mark.asyncio
async def test_payment_service_uses_exact_ten_installment_plot():
    async def execute(tool, arguments):
        if tool == "get_cart_complete":
            assert arguments == {"session_id": "SESSION"}
            return {"items": [], "total": "1250.00"}
        assert tool == "get_payment_options"
        assert arguments == {"cart_session_id": "SESSION"}
        return NORMALIZED_PAYMENT_OPTIONS

    result = await inspect_payment_options(
        state=CommerceConversationState(
            cart_session_id="SESSION",
            cart_url="https://loja.example/checkout/SESSION",
        ),
        installment_count=10,
        payment_method_preference="card",
        execute=execute,
    )

    assert result.commercial_data["requested_method_available"] is True
    assert result.commercial_data["requested_installment"]["count"] == 10
    assert result.commercial_data["requested_installment"]["value"] == 125.0
    assert result.commercial_data["requested_installment"]["order_total"] == 1250.0
    assert result.response_metadata["selected_payment_method"] == "card"
    assert result.response_metadata["selected_payment_option_id"] == "C1"
    assert result.response_metadata["pending_action"] == "choose_checkout_channel"


@pytest.mark.asyncio
async def test_pix_selection_uses_factual_gateway_name_and_persists_option_details():
    async def execute(tool, _arguments):
        if tool == "get_cart_complete":
            return {"items": [], "total": "10.00"}
        assert tool == "get_payment_options"
        return {"payment_options": {
            "pix": {
                "id": "PIX-XPTO",
                "name": "Pix - Gateway XPTO",
                "discount_value": 2.5,
                "plots": [{"count": 1, "value": 97.5}],
            },
            "options": [{"id": "PIX-XPTO", "name": "Pix - Gateway XPTO"}],
        }}

    previous = CommerceConversationState(cart_session_id="SESSION")
    result = await inspect_payment_options(
        state=previous,
        installment_count=None,
        payment_method_preference="pix",
        execute=execute,
    )
    current = evolve_commerce_state(previous, result)

    assert result.commercial_data["payment_method"]["name"] == "Pix - Gateway XPTO"
    assert current.selected_payment_option.name == "Pix - Gateway XPTO"
    assert current.selected_payment_option.installments == [{"count": 1, "value": 97.5}]
    assert current.selected_payment_option.discount_value == 2.5


@pytest.mark.asyncio
async def test_unavailable_pix_does_not_select_another_factual_option():
    async def execute(tool, _arguments):
        if tool == "get_cart_complete":
            return {"items": [], "total": "10.00"}
        assert tool == "get_payment_options"
        return {"payment_options": {
            "boleto": {"id": "B1", "name": "Boleto - Gateway XPTO"},
            "options": [{"id": "B1", "name": "Boleto - Gateway XPTO"}],
        }}

    result = await inspect_payment_options(
        state=CommerceConversationState(cart_session_id="SESSION"),
        installment_count=None,
        payment_method_preference="pix",
        execute=execute,
    )

    assert result.safety_reason == "payment_method_unavailable"
    assert result.commercial_data["payment_method"] == {
        "type": "pix", "name": None, "available": False,
    }
    assert "selected_payment_option" not in result.response_metadata


@pytest.mark.asyncio
async def test_product_payment_rule_blocks_an_otherwise_factual_pix_option():
    async def execute(tool, _arguments):
        if tool == "get_cart_complete":
            return {"items": [{
                "product_id": "803", "quantity": 1,
                "payment_methods": [{"payment_method_id": "PIX-1", "blocked": "1"}],
            }]}
        return {"payment_options": {
            "pix": {"id": "PIX-1", "name": "Pix - Gateway XPTO"},
            "options": [{"id": "PIX-1", "name": "Pix - Gateway XPTO"}],
        }}

    result = await inspect_payment_options(
        state=CommerceConversationState(cart_session_id="SESSION"),
        installment_count=None,
        payment_method_preference="pix",
        execute=execute,
    )

    assert result.safety_reason == "payment_method_unavailable"
    assert result.commercial_data["cart_payment_method_available"] is False
    assert result.commercial_data["product_payment_restrictions"] == {
        "blocked": True, "max_plots": None,
    }


@pytest.mark.asyncio
async def test_product_payment_max_plots_caps_only_positive_factual_limits():
    async def execute(tool, _arguments):
        if tool == "get_cart_complete":
            return {"items": [
                {"product_id": "1", "quantity": 1, "payment_methods": [
                    {"payment_method_id": "CARD-1", "blocked": "0", "max_plots": "6"},
                ]},
                {"product_id": "2", "quantity": 1, "payment_methods": [
                    {"payment_method_id": "CARD-1", "blocked": "0", "max_plots": "10"},
                ]},
                {"product_id": "3", "quantity": 1, "payment_methods": [
                    {"payment_method_id": "CARD-1", "blocked": "0", "max_plots": "0"},
                ]},
            ]}
        return {"payment_options": {
            "card": {"id": "CARD-1", "name": "Cartao - Gateway XPTO", "plots": [
                {"count": 1, "value": 100}, {"count": 6, "value": 20},
                {"count": 10, "value": 12},
            ]},
            "options": [{"id": "CARD-1", "name": "Cartao - Gateway XPTO"}],
        }}

    result = await inspect_payment_options(
        state=CommerceConversationState(cart_session_id="SESSION"),
        installment_count=None,
        payment_method_preference="card",
        execute=execute,
    )

    assert result.commercial_data["product_payment_restrictions"] == {
        "blocked": False, "max_plots": 6,
    }
    assert [plot["count"] for plot in result.commercial_data[
        "selected_payment_option"]["installments"]
    ] == [1, 6]


def test_order_payment_revalidation_requires_a_unique_factual_match():
    state = CommerceConversationState(
        cart_session_id="SESSION",
        selected_payment_method="pix",
        selected_payment_option_id="7",
        selected_payment_option={"id": "7", "name": "Pix factual", "method": "pix"},
    )
    assert _order_payment_revalidation(state, {"payment_options": {
        "options": [{"id": "7", "name": "Pix factual"}],
    }}) == "confirmed"
    assert _order_payment_revalidation(state, {"payment_options": {
        "options": [{"id": "10", "name": "Cartao factual", "card": 1}],
    }}) == "unavailable"

    state.selected_payment_method = "card"
    state.payment_method_preference = "card"
    state.selected_payment_option_id = None
    state.selected_payment_option = state.selected_payment_option.model_validate(
        {"name": "Cartao", "method": "card"}
    )
    assert _order_payment_revalidation(state, {"payment_options": {
        "options": [
            {"id": "10", "name": "Cartao A", "card": 1},
            {"id": "11", "name": "Cartao B", "card": 1},
        ],
    }}) == "ambiguous"


def test_responder_contract_keeps_pix_available_when_link_is_pending():
    state = CommerceConversationState(
        cart_items=[{"product_id": "803", "quantity": 1, "name": "Produto"}],
        order_id="100",
        selected_payment_method="pix",
        selected_payment_option={"id": "7", "name": "Pix factual", "method": "pix"},
        order_payment_revalidation_status="confirmed",
    )
    assert _responder_contract(state) == {
        "product_state": "in_cart",
        "payment_method_state": "available",
        "payment_link_state": "pending",
        "order_state": "created",
        "order_payment_revalidation_status": "confirmed",
        "hosted_payment_supported": False,
        "customer_confirmation_required": False,
        "payment_payable_total": None,
    }
