from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.mercadopago_client import PixPaymentCreated
from app.pix_payment_service import (
    amount_to_cents,
    create_and_persist_pix_payment,
    extract_mp_notification_type,
    extract_mp_payment_id,
    handle_mercadopago_webhook,
)


def test_extract_mp_payment_id_from_body_and_query():
    assert extract_mp_payment_id({"data": {"id": "99"}, "type": "payment"}) == "99"
    assert extract_mp_payment_id({"id": 12}) == "12"
    assert extract_mp_payment_id({}, {"id": "77"}) == "77"
    assert extract_mp_payment_id({}, {"data.id": "55"}) == "55"
    assert extract_mp_payment_id({}) is None


def test_extract_mp_notification_type_normalizes_payment_actions():
    assert extract_mp_notification_type({"type": "payment"}) == "payment"
    assert extract_mp_notification_type({"action": "payment.updated"}) == "payment"
    assert extract_mp_notification_type({}, {"topic": "merchant_order"}) == "merchant_order"


def test_amount_to_cents():
    assert amount_to_cents(10.5) == 1050
    assert amount_to_cents(10) == 1000


def _settings(**overrides):
    values = {
        "mp_access_token": "tok",
        "mercadopago_access_token": "",
        "mp_base_url": "https://api.mercadopago.com",
        "pix_exp_min": 30,
        "public_url": "https://ns-agent-for-sorteios.vercel.app",
        "pix_direct_enabled": True,
    }
    values.update(overrides)
    ns = SimpleNamespace(**values)
    ns.resolved_mp_access_token = (
        lambda: ns.mp_access_token or ns.mercadopago_access_token or ""
    )
    ns.pix_notification_url = (
        lambda: f"{ns.public_url.rstrip('/')}/api/payments/webhook"
        if ns.public_url
        else None
    )
    return ns


@pytest.mark.asyncio
async def test_handle_webhook_ignores_non_payment_type():
    result = await handle_mercadopago_webhook(
        {"type": "merchant_order", "data": {"id": "1"}},
        settings=_settings(),
    )
    assert result["skipped"] is True
    assert result["reason"] == "ignored_type"


@pytest.mark.asyncio
async def test_handle_webhook_missing_payment_id():
    result = await handle_mercadopago_webhook({"type": "payment"}, settings=_settings())
    assert result["skipped"] is True
    assert result["reason"] == "missing_payment_id"


@pytest.mark.asyncio
async def test_handle_webhook_approved_invokes_settle(monkeypatch):
    import app.pix_payment_service as service

    async def fake_refresh(payment_id, *, settings=None):
        return {
            "payment_id": payment_id,
            "status": "approved",
            "row": {
                "mp_payment_id": payment_id,
                "status": "approved",
                "settlement_status": "pending",
            },
            "raw": {"id": payment_id, "status": "approved", "transaction_amount": 10.5},
        }

    async def fake_settle(payment_id, *, mp_payload=None, create_order=None):
        return {
            "ok": True,
            "action": "settled",
            "reason": "created_after_pix_approval",
            "settlement_status": "completed",
            "tray_order_id": "1",
        }

    monkeypatch.setattr(service, "refresh_pix_payment_status", fake_refresh)
    monkeypatch.setattr(service, "settle_approved_pix_payment", fake_settle)
    result = await handle_mercadopago_webhook(
        {"type": "payment", "data": {"id": "abc"}},
        settings=_settings(),
    )
    assert result["ok"] is True
    assert result["status"] == "approved"
    assert result["settlement"]["action"] == "settled"
    assert result["settlement_status"] == "completed"


@pytest.mark.asyncio
async def test_create_and_persist_pix_payment(monkeypatch):
    import app.pix_payment_service as service

    created = PixPaymentCreated(
        payment_id="mp-1",
        status="pending",
        qr_code="PIXCODE",
        qr_code_base64="YmFzZQ",
        copy_paste_code="PIXCODE",
        expires_in_seconds=1800,
        transaction_amount=25.0,
        date_of_expiration="2026-08-04T12:00:00.000Z",
        raw={"id": "mp-1"},
    )

    async def fake_create(**_kwargs):
        return created

    captured = {}

    def fake_upsert(**kwargs):
        captured.update(kwargs)
        return 7

    monkeypatch.setattr(service, "create_pix_payment", fake_create)
    monkeypatch.setattr(service.repo, "upsert_pix_payment_created", fake_upsert)

    result, row_id = await create_and_persist_pix_payment(
        transaction_amount=25,
        description="Pedido",
        payer_email="a@b.com",
        external_reference="cart-9",
        conversation_id="c1",
        sender_key="whatsapp:1",
        cart_session_id="sess-1",
        settings=_settings(),
    )
    assert result.payment_id == "mp-1"
    assert row_id == 7
    assert captured["mp_payment_id"] == "mp-1"
    assert captured["amount_cents"] == 2500
    assert captured["qr_code"] == "PIXCODE"
    assert captured["conversation_id"] == "c1"
    assert isinstance(captured["expires_at"], datetime)


@pytest.mark.asyncio
async def test_webhook_http_always_200(monkeypatch):
    import api.index as index
    import app.pix_webhook_api as webhook_api

    async def fake_handle(payload, query, *, settings=None):
        return {"ok": True, "skipped": True, "reason": "missing_payment_id"}

    monkeypatch.setattr(webhook_api, "handle_mercadopago_webhook", fake_handle)

    async with AsyncClient(
        transport=ASGITransport(app=index.app),
        base_url="http://test",
    ) as client:
        resp = await client.post("/api/payments/webhook", json={"type": "payment"})
    assert resp.status_code == 200
    assert resp.json()["reason"] == "missing_payment_id"


@pytest.mark.asyncio
async def test_status_endpoint_returns_mp_status(monkeypatch):
    import api.index as index
    import app.pix_webhook_api as webhook_api

    monkeypatch.setattr(
        webhook_api,
        "get_settings",
        lambda: _settings(),
    )

    async def fake_refresh(payment_id, *, settings=None):
        return {
            "payment_id": payment_id,
            "status": "pending",
            "row": {
                "settlement_status": "none",
                "paid_at": None,
            },
            "raw": {},
        }

    monkeypatch.setattr(webhook_api, "refresh_pix_payment_status", fake_refresh)
    monkeypatch.setattr(webhook_api, "get_pix_payment_by_mp_id", lambda _pid: None)

    async with AsyncClient(
        transport=ASGITransport(app=index.app),
        base_url="http://test",
    ) as client:
        resp = await client.get("/api/payments/123/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["paymentId"] == "123"
    assert body["status"] == "pending"
