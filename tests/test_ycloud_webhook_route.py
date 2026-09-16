"""YCloud webhook route + inbox worker routing.

Covers the boundary only: signature -> tenant -> SAME durable inbox -> SAME worker.
No agent, no persona, no OpenAI is executed here.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.models import AgentResult, IncomingMessage


SECRET = "whsec_route_test_secret"
BUSINESS_NUMBER = "+5511900000000"
CUSTOMER_NUMBER = "+5541999998888"
ROUTE = "/api/webhooks/ycloud/whatsapp"


@pytest.fixture
def ycloud_env(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("YCLOUD_WEBHOOK_ENABLED", "true")
    monkeypatch.setenv("YCLOUD_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("YCLOUD_API_KEY", "route-test-key")
    monkeypatch.setenv("YCLOUD_WHATSAPP_FROM", BUSINESS_NUMBER)
    monkeypatch.setenv("YCLOUD_WABA_ID", "")
    monkeypatch.setenv("AGENT_PERSONA_TENANT_ID", "newstore")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("DRY_RUN", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client():
    from api.index import app

    return TestClient(app)


def _sign(body: bytes, *, secret: str = SECRET, timestamp: str = "1700000000") -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.".encode("utf-8") + body,
        hashlib.sha256,
    ).hexdigest()
    return f"t={timestamp},s={digest}"


def _body(**message_overrides) -> bytes:
    message = {
        "id": "ycloud-route-1",
        "wabaId": "waba-1",
        "from": CUSTOMER_NUMBER,
        "customerProfile": {"name": "Maria"},
        "to": BUSINESS_NUMBER,
        "type": "text",
        "text": {"body": "bom dia"},
    }
    message.update(message_overrides)
    payload = {
        "id": "evt-route-1",
        "type": "whatsapp.inbound_message.received",
        "whatsappInboundMessage": message,
    }
    return json.dumps(payload).encode("utf-8")


@pytest.fixture
def spy(monkeypatch):
    """Replace the durable-ingress boundary so no DB or agent is touched."""
    import app.ingress.inbox as inbox
    import app.ingress.worker as worker

    calls: dict = {"enqueued": [], "batches": []}

    def _enqueue(**kwargs):
        calls["enqueued"].append(kwargs)
        return True, 4242

    async def _batch(**kwargs):
        calls["batches"].append(kwargs)
        return {"ok": True, "claimed": 1, "processed": 1, "failed": 0, "results": []}

    monkeypatch.setattr(inbox, "enqueue_inbound", _enqueue)
    monkeypatch.setattr(worker, "process_inbox_batch", _batch)
    return calls


# --------------------------------------------------------------------------
# route existence / feature flag
# --------------------------------------------------------------------------


def test_route_is_registered_under_the_api_webhooks_prefix():
    """/api/webhooks/* is what turn_runtime_middleware instruments."""
    from api.index import app

    paths = {getattr(route, "path", "") for route in app.routes}
    assert ROUTE in paths


def test_webhook_returns_404_when_provider_is_disabled(monkeypatch, client):
    get_settings.cache_clear()
    monkeypatch.setenv("YCLOUD_WEBHOOK_ENABLED", "false")
    get_settings.cache_clear()
    try:
        body = _body()
        response = client.post(
            ROUTE, content=body, headers={"YCloud-Signature": _sign(body)}
        )
        assert response.status_code == 404
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------
# FASE 6 — signature
# --------------------------------------------------------------------------


def test_valid_signature_is_accepted(ycloud_env, client, spy):
    body = _body()
    response = client.post(
        ROUTE, content=body, headers={"YCloud-Signature": _sign(body)}
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_unpersisted_inbound_returns_retryable_error(ycloud_env, client, spy, monkeypatch):
    monkeypatch.setattr("app.ingress.inbox.enqueue_inbound", lambda **kwargs: (False, None))
    body = _body()
    response = client.post(ROUTE, content=body, headers={"YCloud-Signature": _sign(body)})
    assert response.status_code == 503
    assert spy["batches"] == []


def test_large_webhook_is_rejected_before_queue(ycloud_env, client, spy):
    body = b"x" * (1024 * 1024 + 1)
    response = client.post(ROUTE, content=body, headers={"YCloud-Signature": _sign(body)})
    assert response.status_code == 413
    assert spy["enqueued"] == []


def test_invalid_signature_is_rejected_with_401(ycloud_env, client, spy):
    body = _body()
    response = client.post(
        ROUTE,
        content=body,
        headers={"YCloud-Signature": _sign(body, secret="wrong-secret")},
    )

    assert response.status_code == 401
    assert spy["enqueued"] == []


def test_missing_signature_header_is_rejected_with_401(ycloud_env, client, spy):
    response = client.post(ROUTE, content=_body())

    assert response.status_code == 401
    assert spy["enqueued"] == []


def test_tampered_body_is_rejected_with_401(ycloud_env, client, spy):
    body = _body()
    signature = _sign(body)
    tampered = _body(text={"body": "transferir para outra conta"})

    response = client.post(
        ROUTE, content=tampered, headers={"YCloud-Signature": signature}
    )

    assert response.status_code == 401
    assert spy["enqueued"] == []


def test_webhook_fails_closed_when_secret_is_not_configured(monkeypatch, client, spy):
    get_settings.cache_clear()
    monkeypatch.setenv("YCLOUD_WEBHOOK_ENABLED", "true")
    monkeypatch.setenv("YCLOUD_WEBHOOK_SECRET", "")
    get_settings.cache_clear()
    try:
        body = _body()
        response = client.post(
            ROUTE, content=body, headers={"YCloud-Signature": _sign(body)}
        )
        assert response.status_code == 401
        assert spy["enqueued"] == []
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------
# FASE 9 / 10 — same durable inbox, same worker
# --------------------------------------------------------------------------


def test_inbound_is_enqueued_into_the_shared_inbox(ycloud_env, client, spy):
    body = _body()
    client.post(ROUTE, content=body, headers={"YCloud-Signature": _sign(body)})

    assert len(spy["enqueued"]) == 1
    enqueued = spy["enqueued"][0]
    assert enqueued["provider"] == "ycloud"
    assert enqueued["channel"] == "whatsapp"
    assert enqueued["message_id"] == "ycloud-route-1"
    assert enqueued["sender_key"] == "whatsapp:5541999998888"


def test_enqueued_payload_uses_the_shared_normalized_envelope(ycloud_env, client, spy):
    """ingress.reconstruct must be able to rebuild the IncomingMessage."""
    from app.ingress.reconstruct import incoming_from_inbox_payload

    body = _body()
    client.post(ROUTE, content=body, headers={"YCloud-Signature": _sign(body)})

    payload = spy["enqueued"][0]["payload"]
    rebuilt = incoming_from_inbox_payload(payload)

    assert rebuilt is not None
    assert rebuilt.provider == "ycloud"
    assert rebuilt.channel == "whatsapp"
    assert rebuilt.text == "bom dia"
    assert rebuilt.sender_phone == "5541999998888"


def test_webhook_drives_the_existing_worker(ycloud_env, client, spy):
    body = _body()
    client.post(ROUTE, content=body, headers={"YCloud-Signature": _sign(body)})

    assert len(spy["batches"]) == 1


def test_duplicate_event_does_not_enqueue_twice(ycloud_env, client, monkeypatch):
    """Idempotency is the inbox's job: a duplicate returns created=False."""
    import app.ingress.inbox as inbox
    import app.ingress.worker as worker

    seen: set[str] = set()
    enqueued: list[dict] = []
    batches: list[dict] = []

    def _enqueue(**kwargs):
        enqueued.append(kwargs)
        key = f"{kwargs['provider']}:{kwargs['message_id']}"
        created = key not in seen
        seen.add(key)
        return created, 99

    async def _batch(**kwargs):
        batches.append(kwargs)
        return {"ok": True, "claimed": 0, "processed": 0, "failed": 0, "results": []}

    monkeypatch.setattr(inbox, "enqueue_inbound", _enqueue)
    monkeypatch.setattr(worker, "process_inbox_batch", _batch)

    body = _body()
    headers = {"YCloud-Signature": _sign(body)}
    first = client.post(ROUTE, content=body, headers=headers)
    second = client.post(ROUTE, content=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["queued"][0]["created"] is True
    assert second.json()["queued"][0]["created"] is False


# --------------------------------------------------------------------------
# FASE 11 — unknown deployment must not run the agent
# --------------------------------------------------------------------------


def test_message_for_another_business_number_does_not_reach_the_agent(
    ycloud_env, client, spy
):
    body = _body(to="+5511911112222")
    response = client.post(
        ROUTE, content=body, headers={"YCloud-Signature": _sign(body)}
    )

    assert response.status_code == 200
    assert response.json()["skipped"] == "tenant_unresolved"
    assert spy["enqueued"] == []
    assert spy["batches"] == []


# --------------------------------------------------------------------------
# FASE 16 — unsupported events degrade, never crash
# --------------------------------------------------------------------------


def test_unknown_event_type_is_acknowledged_without_enqueueing(ycloud_env, client, spy):
    body = json.dumps({"type": "whatsapp.template.updated", "id": "e"}).encode("utf-8")
    response = client.post(
        ROUTE, content=body, headers={"YCloud-Signature": _sign(body)}
    )

    assert response.status_code == 200
    assert spy["enqueued"] == []


def test_media_message_is_acknowledged_without_enqueueing(ycloud_env, client, spy):
    body = _body(type="image", text=None)
    response = client.post(
        ROUTE, content=body, headers={"YCloud-Signature": _sign(body)}
    )

    assert response.status_code == 200
    assert spy["enqueued"] == []


def test_invalid_json_with_valid_signature_is_acknowledged(ycloud_env, client, spy):
    body = b"not-json"
    response = client.post(
        ROUTE, content=body, headers={"YCloud-Signature": _sign(body)}
    )

    assert response.status_code == 200
    assert spy["enqueued"] == []


def test_status_event_is_acknowledged_without_enqueueing(ycloud_env, client, spy):
    body = json.dumps(
        {
            "type": "whatsapp.message.updated",
            "whatsappMessage": {
                "id": "out-1",
                "wamid": "wamid.X",
                "status": "delivered",
                "from": BUSINESS_NUMBER,
                "to": CUSTOMER_NUMBER,
            },
        }
    ).encode("utf-8")

    response = client.post(
        ROUTE, content=body, headers={"YCloud-Signature": _sign(body)}
    )

    assert response.status_code == 200
    assert response.json()["event"] == "whatsapp.message.updated"
    assert spy["enqueued"] == []


# --------------------------------------------------------------------------
# FASE 14 / 19 — worker outbound routing, with Brevo and Meta intact
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_sends_ycloud_replies_through_ycloud(monkeypatch):
    import app.channels.ycloud_whatsapp as ycloud
    import app.ingress.worker as worker

    sent: dict = {}

    async def _ycloud_send(incoming, result):
        sent["provider"] = "ycloud"
        return {"ok": True, "provider_message_id": "out-1"}

    async def _brevo_send(incoming, result):
        raise AssertionError("YCloud replies must not go through Brevo")

    monkeypatch.setattr(ycloud, "send_ycloud_reply", _ycloud_send)
    monkeypatch.setattr("app.brevo_client.send_brevo_reply", _brevo_send)

    info = await worker._send_reply(
        IncomingMessage(
            provider="ycloud",
            channel="whatsapp",
            sender_phone="5541999998888",
        ),
        AgentResult(reply_text="oi"),
    )

    assert sent["provider"] == "ycloud"
    assert info["ok"] is True


@pytest.mark.asyncio
async def test_worker_still_sends_brevo_replies_through_brevo(monkeypatch):
    """Regression: the existing WhatsApp provider must keep working."""
    import app.brevo_client as brevo
    import app.ingress.worker as worker
    from app.models import BrevoSendResult

    sent: dict = {}

    async def _brevo_send(incoming, result):
        sent["provider"] = "brevo"
        return BrevoSendResult(ok=True, dry_run=True)

    monkeypatch.setattr(brevo, "send_brevo_reply", _brevo_send)

    info = await worker._send_reply(
        IncomingMessage(
            provider="brevo",
            channel="whatsapp",
            sender_phone="5541999998888",
        ),
        AgentResult(reply_text="oi"),
    )

    assert sent["provider"] == "brevo"
    assert info["ok"] is True


@pytest.mark.asyncio
async def test_worker_still_sends_meta_replies_through_meta(monkeypatch):
    """Regression: Instagram Meta routing must be untouched."""
    import app.channels.meta_instagram as meta
    import app.ingress.worker as worker

    sent: dict = {}

    async def _meta_send(incoming, result):
        sent["provider"] = "meta"
        return {"ok": True}

    async def _brevo_send(incoming, result):
        raise AssertionError("Meta replies must not go through Brevo")

    monkeypatch.setattr(meta, "send_meta_instagram_reply", _meta_send)
    monkeypatch.setattr("app.brevo_client.send_brevo_reply", _brevo_send)

    info = await worker._send_reply(
        IncomingMessage(provider="meta", channel="instagram", sender_external_id="u1"),
        AgentResult(reply_text="oi"),
    )

    assert sent["provider"] == "meta"
    assert info["ok"] is True


# --------------------------------------------------------------------------
# regression: existing webhooks keep their routes
# --------------------------------------------------------------------------


def test_existing_provider_routes_are_untouched():
    from api.index import app

    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/api/webhooks/brevo/whatsapp" in paths
    assert "/api/webhooks/brevo/conversations" in paths
    assert "/api/webhooks/meta" in paths
