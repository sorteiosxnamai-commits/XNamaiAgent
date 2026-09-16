from types import SimpleNamespace
from unittest.mock import MagicMock

from app import db, attendance_learning
from app.ingress import outbox


def fake_connection(monkeypatch, module):
    connection = MagicMock()
    cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
    monkeypatch.setattr(module, "get_conn", lambda: connection)
    return cursor


def test_response_audit_preserves_retry_context_and_server_selected_tenant(monkeypatch):
    cursor = fake_connection(monkeypatch, db)
    cursor.fetchone.return_value = {"id": 7}
    monkeypatch.setattr(db, "get_settings", lambda: SimpleNamespace(database_url="test-only", agent_persona_tenant_id="xnamai"))
    monkeypatch.setattr(db, "ensure_tables", lambda: None)
    metadata = {"commerce_state": {"last_catalog_query": "cabo usb c"}, "turn_trace": {"trace_id": "inbox-1"}}
    assert db.insert_agent_response({"reply_text": "Cabo USB C", "response_metadata": metadata,
        "provider_response": {"provider_message_id": "out-1", "_agent_tenant_id": "wrong-tenant"}}) == 7
    saved = cursor.execute.call_args.args[1]["provider_response"].obj
    assert saved["_agent_tenant_id"] == "xnamai"
    assert saved["_agent_metadata"] == metadata
    assert db._commerce_state_from_provider_response(saved) == metadata["commerce_state"]
    assert saved["provider_message_id"] == "out-1"


def test_learning_query_requires_delivered_scoped_unreviewed_evidence(monkeypatch):
    cursor = fake_connection(monkeypatch, attendance_learning)
    cursor.fetchall.return_value = []
    assert attendance_learning.fetch_recent_attendances(tenant_id="xnamai", limit=9999) == []
    query, params = cursor.execute.call_args.args
    assert "DISTINCT ON (response.inbound_id)" in query
    assert "response.provider_send_ok = true" in query
    assert "response.provider_response->>'_agent_tenant_id' = %s" in query
    assert "reviewed.inbound_id = response.inbound_id" in query
    assert "{provider_response,dry_run}" in query
    assert "{provider_response,skipped}" in query
    assert "TRIM(response.reply_text)" in query
    assert params[1:] == ("xnamai", "xnamai", 1000)


def test_stale_delivery_owner_cannot_record_a_sent_receipt(monkeypatch):
    cursor = fake_connection(monkeypatch, outbox)
    monkeypatch.setattr(outbox, "get_settings", lambda: SimpleNamespace(database_url="test-only"))
    cursor.rowcount = 0
    assert outbox.mark_outbox_sent(7, owner="old-worker") is False
    query, params = cursor.execute.call_args.args
    assert params["owner"] == "old-worker"
    assert "lease_owner = %(owner)s" in query
    assert "lease_expires_at > now()" in query
    cursor.rowcount = 1
    assert outbox.mark_outbox_sent(7, owner="current-worker") is True
