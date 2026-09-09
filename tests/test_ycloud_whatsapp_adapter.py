"""YCloud WhatsApp transport adapter — signature, parsing, routing, outbound.

Transport only: no persona, no prompt, no commercial decision is exercised here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest

from app.models import AgentResult, IncomingMessage


YCLOUD_SECRET = "whsec_test_secret_value"
BUSINESS_NUMBER = "+5511900000000"
CUSTOMER_NUMBER = "+5541999998888"


def _settings(**overrides):
    values = {
        "environment": "production",
        "dry_run": False,
        "ycloud_webhook_enabled": True,
        "ycloud_api_key": "test-api-key",
        "ycloud_webhook_secret": YCLOUD_SECRET,
        "ycloud_whatsapp_from": BUSINESS_NUMBER,
        "ycloud_waba_id": "",
        "ycloud_base_url": "https://api.ycloud.com/v2",
        "agent_persona_tenant_id": "newstore",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _signed(body: bytes, *, secret: str = YCLOUD_SECRET, timestamp: str = "1700000000") -> str:
    signed_payload = f"{timestamp}.".encode("utf-8") + body
    digest = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},s={digest}"


def _inbound_payload(**overrides) -> dict:
    message = {
        "id": "ycloud-msg-1",
        "wabaId": "waba-1",
        "from": CUSTOMER_NUMBER,
        "fromUserId": "user-1",
        "fromParentUserId": "parent-1",
        "customerProfile": {"name": "Maria"},
        "to": BUSINESS_NUMBER,
        "type": "text",
        "text": {"body": "quero ver os relogios"},
    }
    message.update(overrides)
    return {
        "id": "evt-1",
        "type": "whatsapp.inbound_message.received",
        "whatsappInboundMessage": message,
    }


# --------------------------------------------------------------------------
# FASE 6 — webhook signature
# --------------------------------------------------------------------------


def test_valid_ycloud_signature_is_accepted():
    from app.channels.ycloud_whatsapp import verify_ycloud_signature

    body = json.dumps(_inbound_payload()).encode("utf-8")
    assert verify_ycloud_signature(
        secret=YCLOUD_SECRET,
        body=body,
        signature_header=_signed(body),
    )


def test_signature_from_other_secret_is_rejected():
    from app.channels.ycloud_whatsapp import verify_ycloud_signature

    body = b'{"type":"whatsapp.inbound_message.received"}'
    assert not verify_ycloud_signature(
        secret=YCLOUD_SECRET,
        body=body,
        signature_header=_signed(body, secret="another-secret"),
    )


def test_signature_binds_the_timestamp():
    """t= is part of the signed payload: replaying it under a new t must fail."""
    from app.channels.ycloud_whatsapp import verify_ycloud_signature

    body = b'{"type":"whatsapp.inbound_message.received"}'
    header = _signed(body, timestamp="1700000000")
    tampered = header.replace("t=1700000000", "t=1700009999")
    assert not verify_ycloud_signature(
        secret=YCLOUD_SECRET, body=body, signature_header=tampered
    )


def test_signature_is_verified_against_raw_bytes_not_reserialized_json():
    from app.channels.ycloud_whatsapp import verify_ycloud_signature

    body = b'{"type":"whatsapp.inbound_message.received",  "spacing":"preserved"}'
    header = _signed(body)
    assert verify_ycloud_signature(
        secret=YCLOUD_SECRET, body=body, signature_header=header
    )
    reserialized = json.dumps(json.loads(body)).encode("utf-8")
    assert reserialized != body
    assert not verify_ycloud_signature(
        secret=YCLOUD_SECRET, body=reserialized, signature_header=header
    )


@pytest.mark.parametrize(
    "header",
    ["", None, "garbage", "t=1700000000", "s=abc", "t=,s=", "t=1700000000,s="],
)
def test_malformed_signature_header_is_rejected(header):
    from app.channels.ycloud_whatsapp import verify_ycloud_signature

    assert not verify_ycloud_signature(
        secret=YCLOUD_SECRET, body=b"{}", signature_header=header
    )


def test_missing_secret_never_authenticates():
    """Fail-closed: an unset secret must not turn into an open webhook."""
    from app.channels.ycloud_whatsapp import verify_ycloud_signature

    body = b"{}"
    assert not verify_ycloud_signature(
        secret="", body=body, signature_header=_signed(body, secret="")
    )


def test_signature_accepts_reordered_header_parts():
    from app.channels.ycloud_whatsapp import verify_ycloud_signature

    body = b'{"a":1}'
    header = _signed(body)
    timestamp_part, signature_part = header.split(",", 1)
    assert verify_ycloud_signature(
        secret=YCLOUD_SECRET,
        body=body,
        signature_header=f"{signature_part}, {timestamp_part}",
    )


# --------------------------------------------------------------------------
# FASE 5 — inbound parsing
# --------------------------------------------------------------------------


def test_event_type_is_read_from_payload():
    from app.channels.ycloud_whatsapp import ycloud_event_type

    assert ycloud_event_type(_inbound_payload()) == "whatsapp.inbound_message.received"
    assert ycloud_event_type({"type": "whatsapp.message.updated"}) == (
        "whatsapp.message.updated"
    )
    assert ycloud_event_type({}) == ""
    assert ycloud_event_type(None) == ""


def test_inbound_text_message_is_normalized():
    from app.channels.ycloud_whatsapp import parse_ycloud_inbound_message

    incoming = parse_ycloud_inbound_message(_inbound_payload())

    assert incoming is not None
    assert incoming.provider == "ycloud"
    assert incoming.channel == "whatsapp"
    assert incoming.message_id == "ycloud-msg-1"
    assert incoming.text == "quero ver os relogios"
    assert incoming.sender_phone == "5541999998888"
    assert incoming.sender_name == "Maria"
    assert incoming.input_modality == "text"
    assert incoming.event_type == "whatsapp.inbound_message.received"


def test_inbound_sender_key_matches_brevo_so_history_carries_across_transports():
    """Same customer on Brevo and YCloud must resolve to one conversation key."""
    from app.channels.ycloud_whatsapp import parse_ycloud_inbound_message
    from app.webhook_parser import build_sender_key

    incoming = parse_ycloud_inbound_message(_inbound_payload())
    brevo_key = build_sender_key(
        "whatsapp", CUSTOMER_NUMBER, None, None, None
    )

    assert incoming is not None
    assert incoming.sender_key == brevo_key == "whatsapp:5541999998888"


def test_inbound_keeps_business_number_and_waba_in_channel_metadata():
    from app.channels.ycloud_whatsapp import parse_ycloud_inbound_message

    incoming = parse_ycloud_inbound_message(_inbound_payload())

    assert incoming is not None
    assert incoming.channel_metadata["ycloud_to"] == BUSINESS_NUMBER
    assert incoming.channel_metadata["ycloud_waba_id"] == "waba-1"


def test_unknown_event_type_is_ignored_without_raising():
    from app.channels.ycloud_whatsapp import parse_ycloud_inbound_message

    assert parse_ycloud_inbound_message({"type": "whatsapp.template.updated"}) is None
    assert parse_ycloud_inbound_message({"type": "contact.created"}) is None


@pytest.mark.parametrize("payload", [None, {}, [], "string", {"whatsappInboundMessage": []}])
def test_malformed_inbound_payload_is_ignored_without_raising(payload):
    from app.channels.ycloud_whatsapp import parse_ycloud_inbound_message

    assert parse_ycloud_inbound_message(payload) is None


def test_non_text_message_types_are_degraded_not_crashed():
    """Release 1 is text-only; media must be skipped, never raise."""
    from app.channels.ycloud_whatsapp import parse_ycloud_inbound_message

    for message_type in ("image", "audio", "video", "document", "sticker", "location"):
        payload = _inbound_payload(type=message_type, text=None)
        assert parse_ycloud_inbound_message(payload) is None


def test_empty_text_body_is_ignored():
    from app.channels.ycloud_whatsapp import parse_ycloud_inbound_message

    assert parse_ycloud_inbound_message(_inbound_payload(text={"body": "   "})) is None


def test_message_without_id_is_ignored_because_idempotency_needs_it():
    from app.channels.ycloud_whatsapp import parse_ycloud_inbound_message

    assert parse_ycloud_inbound_message(_inbound_payload(id="")) is None


# --------------------------------------------------------------------------
# FASE 11 — deployment / tenant routing
# --------------------------------------------------------------------------


def test_inbound_to_configured_business_number_resolves_tenant():
    from app.channels.ycloud_whatsapp import resolve_ycloud_tenant

    resolution = resolve_ycloud_tenant(
        to=BUSINESS_NUMBER, waba_id="waba-1", settings=_settings()
    )

    assert resolution.ok is True
    assert resolution.tenant_id == "newstore"
    assert resolution.source == "business_number"


def test_business_number_comparison_ignores_formatting():
    from app.channels.ycloud_whatsapp import resolve_ycloud_tenant

    resolution = resolve_ycloud_tenant(
        to="55 (11) 90000-0000",
        waba_id=None,
        settings=_settings(ycloud_whatsapp_from="+5511900000000"),
    )

    assert resolution.ok is True


def test_inbound_for_another_business_number_does_not_resolve():
    from app.channels.ycloud_whatsapp import resolve_ycloud_tenant

    resolution = resolve_ycloud_tenant(
        to="+5511911112222", waba_id="waba-1", settings=_settings()
    )

    assert resolution.ok is False
    assert resolution.tenant_id is None
    assert resolution.failure_code == "business_number_mismatch"


def test_inbound_for_another_waba_does_not_resolve_when_waba_is_configured():
    from app.channels.ycloud_whatsapp import resolve_ycloud_tenant

    resolution = resolve_ycloud_tenant(
        to=BUSINESS_NUMBER,
        waba_id="waba-999",
        settings=_settings(ycloud_waba_id="waba-1"),
    )

    assert resolution.ok is False
    assert resolution.failure_code == "waba_mismatch"


def test_unconfigured_business_number_fails_closed_in_production():
    from app.channels.ycloud_whatsapp import resolve_ycloud_tenant

    resolution = resolve_ycloud_tenant(
        to=BUSINESS_NUMBER,
        waba_id=None,
        settings=_settings(ycloud_whatsapp_from="", environment="production"),
    )

    assert resolution.ok is False
    assert resolution.failure_code == "business_number_not_configured"


def test_unconfigured_business_number_is_allowed_outside_production():
    from app.channels.ycloud_whatsapp import resolve_ycloud_tenant

    resolution = resolve_ycloud_tenant(
        to=BUSINESS_NUMBER,
        waba_id=None,
        settings=_settings(ycloud_whatsapp_from="", environment="development"),
    )

    assert resolution.ok is True
    assert resolution.source == "unverified_non_production"


def test_missing_tenant_id_setting_does_not_invent_one():
    from app.channels.ycloud_whatsapp import resolve_ycloud_tenant

    resolution = resolve_ycloud_tenant(
        to=BUSINESS_NUMBER,
        waba_id=None,
        settings=_settings(agent_persona_tenant_id=""),
    )

    assert resolution.ok is False
    assert resolution.failure_code == "tenant_not_configured"


# --------------------------------------------------------------------------
# FASE 15 — status events
# --------------------------------------------------------------------------


def test_status_event_is_summarized_for_logging():
    from app.channels.ycloud_whatsapp import parse_ycloud_status_update

    status = parse_ycloud_status_update(
        {
            "type": "whatsapp.message.updated",
            "whatsappMessage": {
                "id": "ycloud-out-1",
                "wamid": "wamid.ABC",
                "status": "delivered",
                "from": BUSINESS_NUMBER,
                "to": CUSTOMER_NUMBER,
            },
        }
    )

    assert status is not None
    assert status["message_id"] == "ycloud-out-1"
    assert status["wamid"] == "wamid.ABC"
    assert status["status"] == "delivered"


def test_status_summary_carries_no_raw_phone_numbers():
    from app.channels.ycloud_whatsapp import parse_ycloud_status_update

    status = parse_ycloud_status_update(
        {
            "type": "whatsapp.message.updated",
            "whatsappMessage": {
                "id": "ycloud-out-1",
                "status": "read",
                "from": BUSINESS_NUMBER,
                "to": CUSTOMER_NUMBER,
            },
        }
    )

    assert status is not None
    assert CUSTOMER_NUMBER not in json.dumps(status)
    assert "5541999998888" not in json.dumps(status)


def test_non_status_event_returns_none():
    from app.channels.ycloud_whatsapp import parse_ycloud_status_update

    assert parse_ycloud_status_update(_inbound_payload()) is None
    assert parse_ycloud_status_update({}) is None


# --------------------------------------------------------------------------
# FASE 14 — outbound
# --------------------------------------------------------------------------


class _Response:
    def __init__(self, status_code: int, body: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def _client_factory(response, captured: dict, raises: BaseException | None = None):
    class Client:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, json, headers):
            captured.update({"url": url, "json": json, "headers": headers})
            if raises is not None:
                raise raises
            return response

    return Client


def _outgoing():
    return (
        IncomingMessage(
            provider="ycloud",
            channel="whatsapp",
            sender_phone="5541999998888",
            sender_key="whatsapp:5541999998888",
        ),
        AgentResult(reply_text="Temos sim! Posso te mostrar as opcoes?"),
    )


@pytest.mark.asyncio
async def test_outbound_posts_to_ycloud_messages_endpoint(monkeypatch):
    import app.channels.ycloud_whatsapp as ycloud

    captured: dict = {}
    monkeypatch.setattr(ycloud, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        ycloud.httpx,
        "AsyncClient",
        _client_factory(_Response(200, {"id": "out-1", "wamid": "wamid.X"}), captured),
    )

    incoming, result = _outgoing()
    send = await ycloud.send_ycloud_reply(incoming, result)

    assert send["ok"] is True
    assert captured["url"] == "https://api.ycloud.com/v2/whatsapp/messages"


@pytest.mark.asyncio
async def test_outbound_authenticates_with_x_api_key_header(monkeypatch):
    import app.channels.ycloud_whatsapp as ycloud

    captured: dict = {}
    monkeypatch.setattr(ycloud, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        ycloud.httpx, "AsyncClient", _client_factory(_Response(200, {"id": "out-1"}), captured)
    )

    incoming, result = _outgoing()
    await ycloud.send_ycloud_reply(incoming, result)

    assert captured["headers"]["X-API-Key"] == "test-api-key"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert "Authorization" not in captured["headers"]


@pytest.mark.asyncio
async def test_outbound_body_matches_ycloud_text_contract(monkeypatch):
    import app.channels.ycloud_whatsapp as ycloud

    captured: dict = {}
    monkeypatch.setattr(ycloud, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        ycloud.httpx, "AsyncClient", _client_factory(_Response(200, {"id": "out-1"}), captured)
    )

    incoming, result = _outgoing()
    await ycloud.send_ycloud_reply(incoming, result)

    assert captured["json"] == {
        "from": BUSINESS_NUMBER,
        "to": "+5541999998888",
        "type": "text",
        "text": {"body": "Temos sim! Posso te mostrar as opcoes?"},
    }


@pytest.mark.asyncio
async def test_outbound_never_rewrites_the_agent_reply(monkeypatch):
    """Transport only: the provider must not touch the agent's words."""
    import app.channels.ycloud_whatsapp as ycloud

    captured: dict = {}
    monkeypatch.setattr(ycloud, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        ycloud.httpx, "AsyncClient", _client_factory(_Response(200, {"id": "out-1"}), captured)
    )

    reply = "Linha 1\nLinha 2 com acentuacao: coracao, R$ 1.234,56\nhttps://loja.example/p/1"
    incoming, _ = _outgoing()
    await ycloud.send_ycloud_reply(incoming, AgentResult(reply_text=reply))

    assert captured["json"]["text"]["body"] == reply


@pytest.mark.asyncio
async def test_outbound_returns_provider_message_ids(monkeypatch):
    import app.channels.ycloud_whatsapp as ycloud

    captured: dict = {}
    monkeypatch.setattr(ycloud, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        ycloud.httpx,
        "AsyncClient",
        _client_factory(
            _Response(200, {"id": "out-1", "wamid": "wamid.X", "status": "accepted"}),
            captured,
        ),
    )

    incoming, result = _outgoing()
    send = await ycloud.send_ycloud_reply(incoming, result)

    assert send["provider_message_id"] == "out-1"
    assert send["wamid"] == "wamid.X"
    assert send["status"] == "accepted"


@pytest.mark.asyncio
async def test_dry_run_does_not_call_ycloud(monkeypatch):
    import app.channels.ycloud_whatsapp as ycloud

    def _explode(**_kwargs):
        raise AssertionError("DRY_RUN must not reach the network")

    monkeypatch.setattr(ycloud, "get_settings", lambda: _settings(dry_run=True))
    monkeypatch.setattr(ycloud.httpx, "AsyncClient", _explode)

    incoming, result = _outgoing()
    send = await ycloud.send_ycloud_reply(incoming, result)

    assert send["ok"] is True
    assert send["dry_run"] is True


@pytest.mark.asyncio
async def test_outbound_without_api_key_fails_without_network_call(monkeypatch):
    import app.channels.ycloud_whatsapp as ycloud

    def _explode(**_kwargs):
        raise AssertionError("must not call YCloud without an API key")

    monkeypatch.setattr(ycloud, "get_settings", lambda: _settings(ycloud_api_key=""))
    monkeypatch.setattr(ycloud.httpx, "AsyncClient", _explode)

    incoming, result = _outgoing()
    send = await ycloud.send_ycloud_reply(incoming, result)

    assert send["ok"] is False
    assert send["error"] == "ycloud_api_key_missing"


@pytest.mark.asyncio
async def test_outbound_without_recipient_fails_without_network_call(monkeypatch):
    import app.channels.ycloud_whatsapp as ycloud

    def _explode(**_kwargs):
        raise AssertionError("must not call YCloud without a recipient")

    monkeypatch.setattr(ycloud, "get_settings", lambda: _settings())
    monkeypatch.setattr(ycloud.httpx, "AsyncClient", _explode)

    send = await ycloud.send_ycloud_reply(
        IncomingMessage(provider="ycloud", channel="whatsapp"),
        AgentResult(reply_text="oi"),
    )

    assert send["ok"] is False
    assert send["error"] == "ycloud_recipient_missing"


@pytest.mark.asyncio
async def test_outbound_without_business_number_fails_without_network_call(monkeypatch):
    """Non-production may skip the inbound guard; outbound still needs a sender."""
    import app.channels.ycloud_whatsapp as ycloud

    def _explode(**_kwargs):
        raise AssertionError("must not call YCloud without a sender number")

    monkeypatch.setattr(ycloud, "get_settings", lambda: _settings(ycloud_whatsapp_from=""))
    monkeypatch.setattr(ycloud.httpx, "AsyncClient", _explode)

    incoming, result = _outgoing()
    send = await ycloud.send_ycloud_reply(incoming, result)

    assert send["ok"] is False
    assert send["error"] == "ycloud_sender_missing"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_error"),
    [
        (400, "ycloud_bad_request"),
        (401, "ycloud_unauthorized"),
        (403, "ycloud_unauthorized"),
        (429, "ycloud_rate_limited"),
        (500, "ycloud_server_error"),
        (503, "ycloud_server_error"),
    ],
)
async def test_outbound_http_errors_are_translated(monkeypatch, status_code, expected_error):
    import app.channels.ycloud_whatsapp as ycloud

    captured: dict = {}
    monkeypatch.setattr(ycloud, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        ycloud.httpx,
        "AsyncClient",
        _client_factory(_Response(status_code, {"error": {"message": "nope"}}), captured),
    )

    incoming, result = _outgoing()
    send = await ycloud.send_ycloud_reply(incoming, result)

    assert send["ok"] is False
    assert send["error"] == expected_error
    assert send["status_code"] == status_code


@pytest.mark.asyncio
async def test_outbound_network_timeout_is_translated(monkeypatch):
    import app.channels.ycloud_whatsapp as ycloud

    captured: dict = {}
    monkeypatch.setattr(ycloud, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        ycloud.httpx,
        "AsyncClient",
        _client_factory(
            None, captured, raises=ycloud.httpx.TimeoutException("timeout")
        ),
    )

    incoming, result = _outgoing()
    send = await ycloud.send_ycloud_reply(incoming, result)

    assert send["ok"] is False
    assert send["error"] == "ycloud_network_error"


@pytest.mark.asyncio
async def test_outbound_non_json_error_body_does_not_raise(monkeypatch):
    import app.channels.ycloud_whatsapp as ycloud

    captured: dict = {}
    monkeypatch.setattr(ycloud, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        ycloud.httpx,
        "AsyncClient",
        _client_factory(_Response(502, None, text="<html>bad gateway</html>"), captured),
    )

    incoming, result = _outgoing()
    send = await ycloud.send_ycloud_reply(incoming, result)

    assert send["ok"] is False
    assert send["error"] == "ycloud_server_error"


@pytest.mark.asyncio
async def test_outbound_respects_configured_base_url(monkeypatch):
    import app.channels.ycloud_whatsapp as ycloud

    captured: dict = {}
    monkeypatch.setattr(
        ycloud,
        "get_settings",
        lambda: _settings(ycloud_base_url="https://api.ycloud.com/v2/"),
    )
    monkeypatch.setattr(
        ycloud.httpx, "AsyncClient", _client_factory(_Response(200, {"id": "o"}), captured)
    )

    incoming, result = _outgoing()
    await ycloud.send_ycloud_reply(incoming, result)

    assert captured["url"] == "https://api.ycloud.com/v2/whatsapp/messages"


# --------------------------------------------------------------------------
# FASE 10 / 18 — idempotency namespace
# --------------------------------------------------------------------------


def test_idempotency_key_is_namespaced_per_provider():
    """The same provider message id must not collide with Brevo's."""
    from app.ingress.inbox import build_idempotency_key

    ycloud_key = build_idempotency_key(
        provider="ycloud", message_id="shared-id", payload={}
    )
    brevo_key = build_idempotency_key(
        provider="brevo", message_id="shared-id", payload={}
    )

    assert ycloud_key == "ycloud:msg:shared-id"
    assert ycloud_key != brevo_key


def test_repeated_ycloud_event_produces_the_same_idempotency_key():
    from app.channels.ycloud_whatsapp import parse_ycloud_inbound_message
    from app.ingress.inbox import build_idempotency_key

    first = parse_ycloud_inbound_message(_inbound_payload())
    second = parse_ycloud_inbound_message(_inbound_payload())

    assert first is not None and second is not None
    assert build_idempotency_key(
        provider="ycloud", message_id=first.message_id, payload={}
    ) == build_idempotency_key(
        provider="ycloud", message_id=second.message_id, payload={}
    )


# --------------------------------------------------------------------------
# FASE 8 — the adapter carries no business logic
# --------------------------------------------------------------------------


def test_adapter_imports_only_transport_level_modules():
    """The adapter must not reach into the agent, prompt or commerce subsystems.

    Checked on the import graph rather than on raw text: reading a config value
    named agent_persona_tenant_id is configuration, not a dependency.
    """
    import ast
    from pathlib import Path

    allowed = {
        "app.config",
        "app.http_resilience",
        "app.models",
        "app.observability",
        "app.repository",
        "app.webhook_parser",
    }

    tree = ast.parse(
        Path("app/channels/ycloud_whatsapp.py").read_text(encoding="utf-8")
    )
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    app_imports = {name for name in imported if name.split(".")[0] == "app"}
    assert app_imports <= allowed, (
        f"transport adapter reaches outside the transport layer: {app_imports - allowed}"
    )
