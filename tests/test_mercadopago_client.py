from types import SimpleNamespace

import pytest

from app.config import Settings
from app.mercadopago_client import (
    MercadoPagoError,
    create_pix_payment,
    get_payment,
    normalize_pix_payment,
)


def test_settings_resolve_mp_token_prefers_mp_access_token():
    settings = Settings(
        MP_ACCESS_TOKEN=" tok-a ",
        MERCADOPAGO_ACCESS_TOKEN="tok-b",
        PUBLIC_URL="https://ns-agent-for-sorteios.vercel.app/",
    )
    assert settings.resolved_mp_access_token() == "tok-a"
    assert settings.public_url == "https://ns-agent-for-sorteios.vercel.app"
    assert (
        settings.pix_notification_url()
        == "https://ns-agent-for-sorteios.vercel.app/api/payments/webhook"
    )


def test_settings_resolve_mp_token_fallback_mercadopago():
    settings = Settings(MERCADOPAGO_ACCESS_TOKEN="tok-only")
    assert settings.resolved_mp_access_token() == "tok-only"
    assert settings.pix_notification_url() is None


def test_normalize_pix_payment_extracts_qr():
    payload = {
        "id": 12345,
        "status": "pending",
        "transaction_amount": 99.9,
        "date_of_expiration": "2026-08-04T01:00:00.000-03:00",
        "point_of_interaction": {
            "transaction_data": {
                "qr_code": " 00020126EXEMPLO ",
                "qr_code_base64": "YW\nJj ",
            }
        },
    }
    result = normalize_pix_payment(payload, expires_in_seconds=1800)
    assert result.payment_id == "12345"
    assert result.status == "pending"
    assert result.qr_code == "00020126EXEMPLO"
    assert result.copy_paste_code == "00020126EXEMPLO"
    assert result.qr_code_base64 == "YWJj"
    assert result.expires_in_seconds == 1800
    assert result.transaction_amount == 99.9


def _settings(**overrides):
    values = {
        "mp_access_token": "test-token",
        "mercadopago_access_token": "",
        "mp_base_url": "https://api.mercadopago.com",
        "pix_exp_min": 30,
        "public_url": "https://ns-agent-for-sorteios.vercel.app",
        "pix_direct_enabled": True,
    }
    values.update(overrides)
    ns = SimpleNamespace(**values)
    ns.resolved_mp_access_token = lambda: ns.mp_access_token or ns.mercadopago_access_token or ""
    ns.pix_notification_url = (
        lambda: f"{ns.public_url.rstrip('/')}/api/payments/webhook"
        if ns.public_url
        else None
    )
    return ns


@pytest.mark.asyncio
async def test_create_pix_payment_posts_expected_body(monkeypatch):
    import app.mercadopago_client as mp

    captured = {}

    class Response:
        status_code = 201
        content = b"{}"
        text = "{}"

        def json(self):
            return {
                "id": 987,
                "status": "pending",
                "transaction_amount": 10.5,
                "date_of_expiration": "2026-08-04T12:00:00.000Z",
                "point_of_interaction": {
                    "transaction_data": {
                        "qr_code": "PIXCODE",
                        "qr_code_base64": "YmFzZTY0",
                    }
                },
            }

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, method, url, *, json, headers):
            captured.update(
                {"method": method, "url": url, "json": json, "headers": headers}
            )
            return Response()

    monkeypatch.setattr(mp.httpx, "AsyncClient", Client)

    result = await create_pix_payment(
        transaction_amount=10.5,
        description="Pedido teste",
        payer_email="cliente@example.com",
        external_reference="cart-1",
        metadata={"conversation_id": "c1"},
        idempotency_key="idem-1",
        settings=_settings(),
    )

    assert result.payment_id == "987"
    assert result.qr_code == "PIXCODE"
    assert result.copy_paste_code == "PIXCODE"
    assert result.expires_in_seconds == 1800
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.mercadopago.com/v1/payments"
    assert captured["headers"]["Authorization"] == "Bearer test-token"
    assert captured["headers"]["X-Idempotency-Key"] == "idem-1"
    body = captured["json"]
    assert body["payment_method_id"] == "pix"
    assert body["transaction_amount"] == 10.5
    assert body["payer"] == {"email": "cliente@example.com"}
    assert body["external_reference"] == "cart-1"
    assert body["metadata"] == {"conversation_id": "c1"}
    assert (
        body["notification_url"]
        == "https://ns-agent-for-sorteios.vercel.app/api/payments/webhook"
    )
    assert "date_of_expiration" in body


@pytest.mark.asyncio
async def test_create_pix_payment_missing_token():
    with pytest.raises(MercadoPagoError) as exc:
        await create_pix_payment(
            transaction_amount=1,
            description="x",
            payer_email="a@b.com",
            settings=_settings(mp_access_token="", mercadopago_access_token=""),
        )
    assert exc.value.code == "mp_token_missing"


@pytest.mark.asyncio
async def test_get_payment_success(monkeypatch):
    import app.mercadopago_client as mp

    class Response:
        status_code = 200
        content = b"{}"
        text = "{}"

        def json(self):
            return {"id": 42, "status": "approved"}

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, method, url, *, json, headers):
            assert method == "GET"
            assert url.endswith("/v1/payments/42")
            assert json is None
            return Response()

    monkeypatch.setattr(mp.httpx, "AsyncClient", Client)
    payload = await get_payment("42", settings=_settings())
    assert payload["status"] == "approved"


@pytest.mark.asyncio
async def test_mp_http_error_maps_message(monkeypatch):
    import app.mercadopago_client as mp

    class Response:
        status_code = 400
        content = b"{}"
        text = "{}"

        def json(self):
            return {
                "message": "Invalid email",
                "cause": [{"description": "payer.email invalid"}],
            }

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, method, url, *, json, headers):
            return Response()

    monkeypatch.setattr(mp.httpx, "AsyncClient", Client)
    with pytest.raises(MercadoPagoError) as exc:
        await get_payment("1", settings=_settings())
    assert exc.value.status_code == 400
    assert "Invalid email" in str(exc.value)
