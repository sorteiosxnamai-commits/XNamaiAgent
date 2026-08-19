from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.commerce_context import CommerceConversationState, evolve_commerce_state
from app.models import CheckoutDataInput, IncomingMessage, SalesInterpretation
from app.order_service import get_order_facts
from app.payment_service import inspect_order_payment
from app.tray_tools import TOOL_SCHEMAS


def _state(**overrides):
    payload = {
        "active_domain": "commerce",
        "checkout_channel_preference": "whatsapp",
        "order_id": "123",
        "order_session_id": "SESSION-1",
        "order_status": "A ENVIAR",
        "purchase_stage": "order_created",
    }
    payload.update(overrides)
    return CommerceConversationState(**payload)


def _interpretation(**overrides):
    payload = {
        "domain": "commerce",
        "goal": "after_sales",
        "subject": {},
        "preferences": {},
        "information_needed": ["payment"],
        "references_previous_context": True,
        "needs_clarification": False,
        "confidence": 0.99,
    }
    payload.update(overrides)
    return SalesInterpretation(**payload)


@pytest.mark.asyncio
async def test_pending_pix_persists_exact_official_url_and_awaiting_stage(capsys):
    official_url = "https://pay.example/order/123?token=a%2Bb&access_code=xyz"

    async def execute(tool, arguments):
        assert tool == "get_order_payment"
        assert arguments == {"order_id": "123"}
        return {
            "success": True,
            "order_id": 123,
            "payment": {
                "method_id": "10545",
                "method": "Pix - Vindi",
                "type": "pix",
                "has_payment": False,
                "payment_date": None,
                "payment_url": official_url,
            },
        }

    result = await inspect_order_payment(state=_state(), execute=execute)
    current = evolve_commerce_state(_state(), result)

    assert result.commercial_data["payment"] == {
        "status": "pending",
        "method_id": "10545",
        "method": "Pix - Vindi",
        "type": "pix",
        "has_payment": False,
        "payment_date": None,
        "payment_url": official_url,
        "payment_url_available": True,
    }
    assert current.order_payment_url == official_url
    assert current.order_payment_status == "pending"
    assert current.order_has_payment is False
    assert current.purchase_stage == "awaiting_payment"
    assert current.pending_action == "awaiting_payment"
    output = capsys.readouterr().out
    assert "payment_url_present" in output
    assert official_url not in output
    assert "access_code=xyz" not in output


@pytest.mark.asyncio
async def test_confirmed_payment_keeps_order_status_independent():
    async def execute(_tool, _arguments):
        return {
            "success": True,
            "order_id": "123",
            "payment": {
                "method_id": "10545",
                "method": "Pix - Vindi",
                "type": "pix",
                "has_payment": True,
                "payment_date": "2026-07-26T18:00:00Z",
                "payment_url": None,
            },
        }

    result = await inspect_order_payment(state=_state(), execute=execute)
    current = evolve_commerce_state(_state(), result)

    assert current.order_payment_status == "confirmed"
    assert current.order_has_payment is True
    assert current.purchase_stage == "payment_confirmed"
    assert current.pending_action is None
    assert current.order_status == "A ENVIAR"

    async def get_order(_tool, _arguments):
        return {
            "order_id": "123",
            "status": "A ENVIAR VINDI",
            "status_group": "awaiting_shipment",
        }

    order_result = await get_order_facts(state=current, execute=get_order)
    refreshed = evolve_commerce_state(current, order_result)

    assert refreshed.order_status == "A ENVIAR VINDI"
    assert refreshed.order_payment_status == "confirmed"
    assert refreshed.purchase_stage == "payment_confirmed"


@pytest.mark.asyncio
async def test_pending_without_url_does_not_invent_checkout():
    async def execute(_tool, _arguments):
        return {
            "success": True,
            "order_id": "123",
            "payment": {
                "method": "Boleto",
                "type": "boleto",
                "has_payment": False,
                "payment_url": None,
            },
        }

    result = await inspect_order_payment(state=_state(), execute=execute)
    current = evolve_commerce_state(_state(), result)

    assert result.commercial_data["payment"]["status"] == "pending"
    assert result.commercial_data["payment"]["payment_url"] is None
    assert result.commercial_data["payment"]["payment_url_available"] is False
    assert current.order_id == "123"
    assert current.order_payment_url is None


@pytest.mark.asyncio
async def test_lookup_error_preserves_order_and_marks_payment_unknown():
    async def execute(_tool, _arguments):
        return {
            "error": "commerce_upstream_error",
            "status_code": 500,
            "tray_error_code": "payment_lookup_failed",
            "tray_error_message": "temporary failure",
        }

    result = await inspect_order_payment(state=_state(), execute=execute)
    current = evolve_commerce_state(_state(), result)

    assert result.safety_reason == "order_payment_technical_failure"
    assert result.commercial_data["payment"]["status"] == "unknown"
    assert current.order_id == "123"
    assert current.order_payment_status == "unknown"
    assert current.order_has_payment is None
    assert result.response_metadata["payment_failure_status"] == 500
    assert result.response_metadata["payment_failure_code"] == "payment_lookup_failed"


@pytest.mark.asyncio
async def test_null_has_payment_is_unknown_and_empty_contract_is_not_available():
    responses = [
        {
            "success": True,
            "order_id": "123",
            "payment": {
                "method": "Pix - Vindi",
                "type": "pix",
                "has_payment": None,
                "payment_url": None,
            },
        },
        {"success": True, "order_id": "123", "payment": None},
    ]

    async def execute(_tool, _arguments):
        return responses.pop(0)

    unknown = await inspect_order_payment(state=_state(), execute=execute)
    unavailable = await inspect_order_payment(state=_state(), execute=execute)

    assert unknown.commercial_data["payment"]["status"] == "unknown"
    assert unavailable.commercial_data["payment"]["status"] == "not_available"


@pytest.mark.asyncio
async def test_refresh_after_customer_paid_transitions_pending_to_confirmed(monkeypatch):
    import app.sales_agent as sales_agent

    responses = [False, True]

    async def execute(tool, arguments):
        assert tool == "get_order_payment"
        assert arguments == {"order_id": "123"}
        paid = responses.pop(0)
        return {
            "success": True,
            "order_id": "123",
            "payment": {
                "method": "Pix - Vindi",
                "type": "pix",
                "has_payment": paid,
                "payment_date": "2026-07-26T18:00:00Z" if paid else None,
                "payment_url": None if paid else "https://pay.example/official",
            },
        }

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(
        sales_agent,
        "get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )
    state = _state()

    async def turn():
        nonlocal state
        result = await sales_agent.handle_sales_message(
            IncomingMessage(text="ja paguei"),
            {},
            {},
            _interpretation(payment_action="order_payment"),
            commerce_state=state,
        )
        state = evolve_commerce_state(state, result)
        return result

    first = await turn()
    second = await turn()

    assert first.commercial_data["payment"]["status"] == "pending"
    assert second.commercial_data["payment"]["status"] == "confirmed"
    assert state.order_payment_status == "confirmed"
    assert state.purchase_stage == "payment_confirmed"
    assert state.order_status == "A ENVIAR"
    assert responses == []


def test_no_model_or_openai_tool_accepts_native_card_secrets():
    forbidden = {"card_number", "pan", "cvv", "cvc"}

    assert forbidden.isdisjoint(CheckoutDataInput.model_fields)
    assert forbidden.isdisjoint(CommerceConversationState.model_fields)
    exposed_schema = repr(TOOL_SCHEMAS).casefold()
    assert not any(field in exposed_schema for field in forbidden)
