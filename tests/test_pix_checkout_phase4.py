from types import SimpleNamespace

import pytest

from app.commerce_context import CommerceConversationState, evolve_commerce_state
from app.mercadopago_client import PixPaymentCreated
from app.pix_checkout_service import (
    generate_direct_pix_checkout,
    should_use_direct_pix,
)


def _settings(**overrides):
    values = {
        "pix_direct_enabled": True,
        "mp_access_token": "tok",
        "mercadopago_access_token": "",
        "pix_exp_min": 30,
        "public_url": "https://ns-agent-for-sorteios.vercel.app",
        "mp_base_url": "https://api.mercadopago.com",
    }
    values.update(overrides)
    ns = SimpleNamespace(**values)
    ns.resolved_mp_access_token = (
        lambda: ns.mp_access_token or ns.mercadopago_access_token or ""
    )
    ns.pix_notification_url = lambda: (
        f"{ns.public_url}/api/payments/webhook" if ns.public_url else None
    )
    return ns


def _confirmed_state(**overrides) -> CommerceConversationState:
    base = {
        "active_domain": "commerce",
        "cart_session_id": "SESSION-1",
        "cart_url": "https://loja.example/c/SESSION-1",
        "checkout_channel_preference": "whatsapp",
        "selected_payment_method": "pix",
        "payment_method_preference": "pix",
        "selected_payment_option": {
            "id": "10545",
            "name": "Pix - Vindi",
            "method": "pix",
        },
        "selected_shipping": {
            "shipping_id": "1",
            "quotation_id": "Q1",
            "name": "PAC",
            "price": "10.00",
        },
        "shipping_quotes": [{
            "shipping_id": "1",
            "quotation_id": "Q1",
            "name": "PAC",
            "price": "10.00",
        }],
        "shipping_quote_zipcode": "86480000",
        "checkout_draft": {
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
        "order_confirmation_status": "confirmed",
        "order_review_version": "review-v1",
        "confirmed_order_review_version": "review-v1",
        "cart_items": [{
            "product_id": "803",
            "variant_id": None,
            "quantity": 1,
            "unit_price": "90.00",
            "original_price": "100.00",
            "name": "Produto",
        }],
    }
    base.update(overrides)
    return CommerceConversationState(**base)


def test_should_use_direct_pix_requires_flag_whatsapp_and_pix(monkeypatch):
    import app.pix_checkout_service as service

    monkeypatch.setattr(service, "get_settings", lambda: _settings())
    state = _confirmed_state()
    assert should_use_direct_pix(state) is True

    monkeypatch.setattr(
        service, "get_settings", lambda: _settings(pix_direct_enabled=False)
    )
    assert should_use_direct_pix(state) is False

    monkeypatch.setattr(service, "get_settings", lambda: _settings())
    assert should_use_direct_pix(_confirmed_state(selected_payment_method="card")) is False
    assert should_use_direct_pix(
        _confirmed_state(checkout_channel_preference="site")
    ) is False


@pytest.mark.asyncio
async def test_generate_direct_pix_builds_snapshot_and_reply(monkeypatch):
    import app.pix_checkout_service as service

    monkeypatch.setattr(service, "get_settings", lambda: _settings())

    async def fake_facts(state, execute, cart_snapshot=None):
        payload = {
            "session_id": state.cart_session_id,
            "shipping": {"shipping_id": 1, "name": "PAC", "value": "10.00"},
            "payment": {"method_id": "10545", "name": "Pix"},
            "customer": {"name": "Joao", "email": "joao@example.com"},
            "address": {"zip_code": "86480000"},
            "products": [{
                "product_id": "803",
                "variant_id": None,
                "price": "90.00",
                "original_price": "100.00",
                "quantity": 1,
            }],
        }
        summary = {
            "display_total": "100.00",
            "products": [{"name": "Produto", "quantity": 1}],
        }
        return {
            "payload": payload,
            "summary": summary,
            "version": state.confirmed_order_review_version,
        }, []

    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return (
            PixPaymentCreated(
                payment_id="mp-99",
                status="pending",
                qr_code="PIXCODE99",
                qr_code_base64="",
                copy_paste_code="PIXCODE99",
                expires_in_seconds=1800,
                transaction_amount=100.0,
                date_of_expiration=None,
                raw={"id": "mp-99"},
            ),
            1,
        )

    monkeypatch.setattr(service, "_current_order_facts", fake_facts)
    monkeypatch.setattr(service, "create_and_persist_pix_payment", fake_create)

    async def execute(_tool, _args):
        raise AssertionError("should not call tray for PIX create")

    result = await generate_direct_pix_checkout(
        state=_confirmed_state(),
        execute=execute,
        conversation_id="c1",
        sender_key="whatsapp:1",
    )
    assert result.safety_reason is None
    assert "PIXCODE99" in result.reply_text
    assert result.commercial_data["pix"]["payment_id"] == "mp-99"
    assert result.response_metadata["pending_action"] == "awaiting_payment"
    assert captured["checkout_snapshot"]["expected_amount_cents"] == 10000
    assert captured["checkout_snapshot"]["order_payload"]["session_id"] == "SESSION-1"
    assert captured["transaction_amount"] == 100.0

    updated = evolve_commerce_state(_confirmed_state(), result)
    assert updated.pix_payment_id == "mp-99"
    assert updated.pix_copy_paste_code == "PIXCODE99"
    assert updated.pending_action == "awaiting_payment"
    assert updated.order_id is None


@pytest.mark.asyncio
async def test_generate_direct_pix_rejects_amount_mismatch(monkeypatch):
    import app.pix_checkout_service as service

    monkeypatch.setattr(service, "get_settings", lambda: _settings())

    async def fake_facts(state, execute, cart_snapshot=None):
        return {
            "payload": {"session_id": "SESSION-1", "products": [{"product_id": "1"}]},
            "summary": {"display_total": "100.00"},
            "version": state.confirmed_order_review_version,
        }, []

    async def fake_create(**_kwargs):
        return (
            PixPaymentCreated(
                payment_id="mp-1",
                status="pending",
                qr_code="X",
                qr_code_base64="",
                copy_paste_code="X",
                expires_in_seconds=1800,
                transaction_amount=50.0,
                date_of_expiration=None,
                raw={},
            ),
            1,
        )

    monkeypatch.setattr(service, "_current_order_facts", fake_facts)
    monkeypatch.setattr(service, "create_and_persist_pix_payment", fake_create)

    result = await generate_direct_pix_checkout(
        state=_confirmed_state(),
        execute=lambda *_a, **_k: None,
    )
    assert result.safety_reason == "pix_amount_mismatch"
    assert result.commercial_data["success"] is False


@pytest.mark.asyncio
async def test_fulfill_uses_pix_when_enabled(monkeypatch):
    import app.sales_agent as sales

    calls = []

    async def fake_pix(**kwargs):
        calls.append("pix")
        from app.models import AgentResult
        return AgentResult(
            reply_text="pix",
            intent="commerce",
            commercial_data={"success": True},
            response_metadata={"domain": "commerce"},
        )

    async def fake_order(state):
        calls.append("order")
        from app.models import AgentResult
        return AgentResult(reply_text="order", intent="commerce")

    monkeypatch.setattr(sales, "should_use_direct_pix", lambda _s: True)
    monkeypatch.setattr(sales, "generate_direct_pix_checkout", fake_pix)
    monkeypatch.setattr(sales, "_create_order_with_payment_lookup", fake_order)

    result = await sales._fulfill_confirmed_order(_confirmed_state())
    assert calls == ["pix"]
    assert result.reply_text == "pix"


@pytest.mark.asyncio
async def test_fulfill_uses_tray_order_when_pix_off(monkeypatch):
    import app.sales_agent as sales

    calls = []

    async def fake_order(state):
        calls.append("order")
        from app.models import AgentResult
        return AgentResult(reply_text="order", intent="commerce")

    monkeypatch.setattr(sales, "should_use_direct_pix", lambda _s: False)
    monkeypatch.setattr(sales, "_create_order_with_payment_lookup", fake_order)

    result = await sales._fulfill_confirmed_order(_confirmed_state())
    assert calls == ["order"]
    assert result.reply_text == "order"


def test_checkout_capabilities_native_on_when_flag(monkeypatch):
    import app.checkout_service as checkout

    monkeypatch.setattr(checkout, "get_settings", lambda: _settings())
    facts = checkout.checkout_capabilities(_confirmed_state())
    assert facts["pix_direct_enabled"] is True
    assert facts["whatsapp_native_payment_supported"] is True
