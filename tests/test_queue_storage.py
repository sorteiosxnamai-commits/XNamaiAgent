"""Storage contracts for queue ownership, terminal states and bounded retries."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.ingress import inbox, outbox


@pytest.fixture(params=[inbox, outbox], ids=["inbox", "outbox"])
def storage(request, monkeypatch):
    module = request.param
    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(database_url="test-only"))
    monkeypatch.setattr(module, "ensure_tables", lambda: None)
    connection = MagicMock()
    cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
    monkeypatch.setattr(module, "get_conn", lambda: connection)
    return module, cursor


def test_claim_recovers_expired_leases_with_backoff_and_locking(storage):
    module, cursor = storage
    cursor.fetchall.return_value = [{"id": 3, "lease_owner": "test", "attempts": 2, "max_attempts": 8}]
    claim = module.claim_pending_inbox if module is inbox else module.claim_pending_outbox
    result = claim(limit=999, owner="test")
    assert result[0]["lease_owner"] == "test"
    query, parameters = cursor.execute.call_args.args
    assert parameters["limit"] == 25
    assert parameters["owner"] == "test"
    assert "FOR UPDATE SKIP LOCKED" in query
    assert "lease_expires_at < now()" in query
    assert "attempts < max_attempts" in query
    assert "make_interval" in query
    cleanup = cursor.execute.call_args_list[0].args[0]
    assert "attempts >=" in cleanup
    assert "'dead'" in cleanup


def test_failure_receipts_require_live_owner_and_preserve_terminal_states(storage):
    module, cursor = storage
    mark = module.mark_inbox_failed if module is inbox else module.mark_outbox_failed
    mark(3, error="provider timeout", owner="test")
    query, parameters = cursor.execute.call_args.args
    assert parameters["owner"] == "test"
    assert "lease_owner = %(owner)s" in query
    assert "lease_expires_at > now()" in query
    assert "status NOT IN" in query


@pytest.mark.parametrize("status", ["pending", "leased", "failed", "sent", "dead", "skipped"])
def test_accepted_envelope_is_reused_for_all_states(monkeypatch, status):
    monkeypatch.setattr(outbox, "get_settings", lambda: SimpleNamespace(database_url="test-only"))
    monkeypatch.setattr(outbox, "ensure_tables", lambda: None)
    connection = MagicMock()
    cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = {"id": 7, "status": status}
    monkeypatch.setattr(outbox, "get_conn", lambda: connection)
    assert outbox.enqueue_outbound(provider="ycloud", channel="whatsapp", inbound_id=91,
                                  reply_text="Do not replace accepted reply") == 7
    queries = [call.args[0] for call in cursor.execute.call_args_list]
    assert "pg_advisory_xact_lock" in queries[0]
    assert not any("INSERT" in query for query in queries)


def test_inline_claim_does_not_bypass_failed_delivery_backoff(monkeypatch):
    monkeypatch.setattr(outbox, "get_settings", lambda: SimpleNamespace(database_url="test-only"))
    connection = MagicMock()
    cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = None
    monkeypatch.setattr(outbox, "get_conn", lambda: connection)
    assert outbox.claim_outbox_for_send(7) is None
    assert "status = 'pending'" in cursor.execute.call_args.args[0]
