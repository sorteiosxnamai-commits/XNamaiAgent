"""delivery_unknown -> reconciliacao por callback de status (YCloud externalId).

Nunca vira sucesso por suposicao, nunca reenvia para "ter certeza", e callback
repetido/forjado/antigo nao move estado.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.ingress import delivery_reconciliation as rec

PHONE = "+5541999998888"


def _row(**overrides):
    row = {
        "id": 42, "provider": "ycloud", "status": "dead", "last_error": "delivery_unknown:ReadTimeout",
        "reply_payload": {"incoming": {"sender_phone": "5541999998888"}}, "provider_response": {},
    }
    row.update(overrides)
    return row


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(rec, "get_settings", lambda: SimpleNamespace(database_url="test-only"))
    connection = MagicMock()
    cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
    monkeypatch.setattr(rec, "get_conn", lambda: connection)
    cursor.rowcount = 1
    return cursor


def _reconcile(**overrides):
    kwargs = dict(provider="ycloud", external_id="outbox:42", provider_status="delivered",
                  event_id="evt_1", recipient=PHONE)
    kwargs.update(overrides)
    return rec.reconcile_provider_status(**kwargs)


@pytest.mark.parametrize("external_id", ["outbox:abc", "outbox:", "outbox:0", "order:42", "outbox:42;drop", " outbox:-1", None])
def test_foreign_or_malformed_external_id_is_ignored(db, external_id):
    assert _reconcile(external_id=external_id)["outcome"] == "ignored_foreign_external_id"
    db.execute.assert_not_called()


@pytest.mark.parametrize("status", ["delivered", "read", "sent", "accepted"])
def test_provider_evidence_of_delivery_closes_unknown_as_sent(db, status):
    db.fetchone.return_value = _row()
    assert _reconcile(provider_status=status)["outcome"] == "confirmed_sent"
    sql, params = db.execute.call_args.args
    assert "SET status = 'sent'" in sql
    assert "status = 'dead' AND last_error LIKE %(unknown)s" in sql
    assert params["unknown"] == "delivery_unknown%"


def test_provider_failure_is_terminal_and_never_success(db):
    db.fetchone.return_value = _row()
    result = _reconcile(provider_status="failed", error_code="131047")
    assert result["outcome"] == "confirmed_failed"
    sql, params = db.execute.call_args.args
    assert "status = 'sent'" not in sql
    assert params["error"] == "delivery_failed_confirmed:131047"


def test_unknown_or_intermediate_status_changes_nothing(db):
    assert _reconcile(provider_status="pending")["outcome"] == "ignored_status"
    db.execute.assert_not_called()


def test_missing_row_provider_or_recipient_mismatch_are_rejected(db):
    db.fetchone.return_value = None
    assert _reconcile()["outcome"] == "unknown_outbox"
    db.fetchone.return_value = _row(provider="meta")
    assert _reconcile()["outcome"] == "provider_mismatch"
    db.fetchone.return_value = _row()
    assert _reconcile(recipient="+5511911112222")["outcome"] == "recipient_mismatch"


def test_repeated_event_is_idempotent(db):
    db.fetchone.return_value = _row(provider_response={"reconciliation": {"event_ids": ["evt_1"]}})
    assert _reconcile(event_id="evt_1")["outcome"] == "duplicate_event"
    assert db.execute.call_count == 1  # only the SELECT ... FOR UPDATE


def test_terminal_state_does_not_move_backwards(db):
    """Falha reportada para linha ja enviada (ou ainda em retry) nao muda nada."""
    db.fetchone.return_value = _row(status="sent", last_error=None)
    db.rowcount = 0
    assert _reconcile(provider_status="failed")["outcome"] == "no_transition"


def test_reconciliation_never_triggers_a_send(db, monkeypatch):
    import app.ingress.worker as worker

    def boom(*_a, **_k):
        raise AssertionError("reconciliation must never send")

    monkeypatch.setattr(worker, "_send_reply", boom)
    db.fetchone.return_value = _row()
    _reconcile()


# --- rota: autenticacao, replay, dispatch --------------------------------

SECRET = "whsec_reconcile_secret"


@pytest.fixture
def route(monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("YCLOUD_WEBHOOK_ENABLED", "true")
    monkeypatch.setenv("YCLOUD_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("DATABASE_URL", "")
    calls: list[dict] = []
    monkeypatch.setattr(rec, "reconcile_provider_status",
                        lambda **kwargs: calls.append(kwargs) or {"outcome": "confirmed_sent"})
    from api.index import app

    yield TestClient(app), calls
    get_settings.cache_clear()


def _status_body(external_id="outbox:42", status="delivered") -> bytes:
    return json.dumps({
        "id": "evt_9", "type": "whatsapp.message.updated", "apiVersion": "v2",
        "whatsappMessage": {"id": "m1", "wamid": "wamid.x", "status": status, "to": PHONE,
                            "externalId": external_id},
    }).encode()


def _sign(body: bytes, timestamp: float, secret: str = SECRET) -> str:
    ts = str(int(timestamp))
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},s={digest}"


def test_signed_fresh_status_callback_reconciles(route):
    client, calls = route
    body = _status_body()
    response = client.post("/api/webhooks/ycloud/whatsapp", content=body,
                           headers={"YCloud-Signature": _sign(body, time.time())})
    assert response.status_code == 200 and response.json()["reconciliation"] == "confirmed_sent"
    assert calls[0]["external_id"] == "outbox:42" and calls[0]["event_id"] == "evt_9"
    assert calls[0]["recipient"] == PHONE


def test_unsigned_or_forged_callback_is_rejected(route):
    client, calls = route
    body = _status_body()
    assert client.post("/api/webhooks/ycloud/whatsapp", content=body).status_code == 401
    forged = _sign(body, time.time(), secret="not-the-secret")
    assert client.post("/api/webhooks/ycloud/whatsapp", content=body,
                       headers={"YCloud-Signature": forged}).status_code == 401
    assert calls == []


@pytest.mark.parametrize("offset", [-(86400 + 60), 3600])
def test_replayed_old_or_future_signature_does_not_move_state(route, offset):
    client, calls = route
    body = _status_body()
    response = client.post("/api/webhooks/ycloud/whatsapp", content=body,
                           headers={"YCloud-Signature": _sign(body, time.time() + offset)})
    assert response.status_code == 200 and response.json()["skipped"] == "stale_signature"
    assert calls == []


def test_status_without_our_correlation_is_only_logged(route):
    client, calls = route
    body = json.dumps({"id": "e", "type": "whatsapp.message.updated",
                       "whatsappMessage": {"status": "delivered"}}).encode()
    response = client.post("/api/webhooks/ycloud/whatsapp", content=body,
                           headers={"YCloud-Signature": _sign(body, time.time())})
    assert response.json()["skipped"] == "status_only"
    assert calls == []
