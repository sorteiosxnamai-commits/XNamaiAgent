"""Outbox: sucesso, falha temporaria, retry com backoff, limite, dead-letter,
lease, ambiguidade de entrega (idempotencia) e entrypoint.

Garantia declarada (ver ``app/ingress/delivery_policy.py``): nenhum provider
usado oferece chave de idempotencia; falha definitiva e reenviada
(at-least-once), falha AMBIGUA para por padrao (at-most-once).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import httpx
import pytest

from app.ingress import outbox, outbox_worker, worker
from app.ingress.delivery_policy import (
    DELIVERY_UNKNOWN_ERROR,
    OutboxRetryPolicy,
    outbox_correlation_id,
    resolve_failure,
)
from app.models import AgentResult, IncomingMessage


# --- politica de backoff -----------------------------------------------------


def test_backoff_is_exponential_and_capped():
    policy = OutboxRetryPolicy(base_seconds=30, max_seconds=300)
    assert [policy.backoff_seconds(n) for n in range(1, 7)] == [30, 60, 120, 240, 300, 300]


def test_attempt_schedule_never_retries_immediately():
    schedule = OutboxRetryPolicy().attempt_schedule(4)
    assert schedule == [0, 30, 90, 210]
    assert all(later > earlier for earlier, later in zip(schedule, schedule[1:]))


def test_effective_attempts_are_bounded_by_the_retry_window():
    """Com os defaults, max_attempts=8 do schema nao cabe na janela de 15 min."""
    policy = OutboxRetryPolicy(base_seconds=30, max_seconds=300, window_seconds=900)
    assert policy.effective_max_attempts(8) == 6
    wider = OutboxRetryPolicy(base_seconds=30, max_seconds=300, window_seconds=3600)
    assert wider.effective_max_attempts(8) == 8


def test_policy_is_configurable_through_settings():
    from app.config import Settings

    settings = Settings(
        _env_file=None,
        AGENT_QUEUE_RETRY_BASE_SECONDS=5,
        AGENT_QUEUE_RETRY_MAX_SECONDS=60,
        AGENT_OUTBOX_RETRY_WINDOW_SECONDS=1800,
        AGENT_OUTBOX_LEASE_SECONDS=90,
        AGENT_OUTBOX_RETRY_UNKNOWN_DELIVERY=True,
    )
    policy = OutboxRetryPolicy.from_settings(settings)
    assert policy == OutboxRetryPolicy(5, 60, 1800, 90, True)
    defaults = OutboxRetryPolicy.from_settings(Settings(_env_file=None))
    assert defaults == OutboxRetryPolicy(30, 300, 900, 180, False)


# --- decisao apos falha ------------------------------------------------------


def test_temporary_failure_is_retried():
    assert resolve_failure({"error": "ycloud_rate_limited"}, attempts=1, max_attempts=8,
                           policy=OutboxRetryPolicy()) == (False, "ycloud_rate_limited")


def test_permanent_failure_goes_to_dead_letter():
    dead, _ = resolve_failure({"error": "ycloud_bad_request", "permanent": True}, attempts=1,
                              max_attempts=8, policy=OutboxRetryPolicy())
    assert dead is True


def test_max_attempts_goes_to_dead_letter():
    dead, _ = resolve_failure({"error": "down"}, attempts=8, max_attempts=8, policy=OutboxRetryPolicy())
    assert dead is True


def test_ambiguous_delivery_is_not_retried_by_default():
    dead, error = resolve_failure({"error": "ycloud_delivery_unknown", "delivery_unknown": True},
                                  attempts=1, max_attempts=8, policy=OutboxRetryPolicy())
    assert dead is True
    assert error.startswith(DELIVERY_UNKNOWN_ERROR)


def test_ambiguous_delivery_is_retried_only_when_opted_in():
    dead, _ = resolve_failure({"error": "x", "delivery_unknown": True}, attempts=1, max_attempts=8,
                              policy=OutboxRetryPolicy(retry_unknown_delivery=True))
    assert dead is False


def test_correlation_id_is_stable():
    assert outbox_correlation_id(42) == "outbox:42"
    assert outbox_correlation_id(None) is None


# --- SQL de claim: lease, SKIP LOCKED, janela e backoff configuraveis ---------


@pytest.fixture
def outbox_cursor(monkeypatch):
    settings = SimpleNamespace(
        database_url="test-only",
        agent_queue_retry_base_seconds=7,
        agent_queue_retry_max_seconds=70,
        agent_outbox_retry_window_seconds=1234,
        agent_outbox_lease_seconds=45,
    )
    monkeypatch.setattr(outbox, "get_settings", lambda: settings)
    monkeypatch.setattr(outbox, "ensure_tables", lambda: None)
    connection = MagicMock()
    cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
    monkeypatch.setattr(outbox, "get_conn", lambda: connection)
    return cursor


def test_claim_uses_configured_window_backoff_and_skip_locked(outbox_cursor):
    outbox_cursor.fetchall.return_value = []
    outbox.claim_pending_outbox(limit=5, owner="w1")

    cleanup_sql, cleanup_params = outbox_cursor.execute.call_args_list[0].args
    assert "interval '15 minutes'" not in cleanup_sql
    assert "make_interval(secs => %(window)s)" in cleanup_sql
    assert cleanup_params == {"window": 1234}

    claim_sql, claim_params = outbox_cursor.execute.call_args_list[1].args
    assert "FOR UPDATE SKIP LOCKED" in claim_sql
    assert "lease_expires_at < now()" in claim_sql  # lease expirado e recuperavel
    assert (claim_params["retry_base"], claim_params["retry_max"]) == (7, 70)
    assert claim_params["owner"] == "w1"


def test_claim_lease_duration_comes_from_policy(outbox_cursor):
    from datetime import datetime, timezone

    outbox_cursor.fetchall.return_value = []
    before = datetime.now(timezone.utc)
    outbox.claim_pending_outbox(owner="w1")
    expires = outbox_cursor.execute.call_args_list[1].args[1]["expires"]
    assert 40 <= (expires - before).total_seconds() <= 50


def test_stale_owner_cannot_record_sent(outbox_cursor):
    """Lease perdido: o worker antigo nao confirma a entrega de outro."""
    outbox_cursor.rowcount = 0
    assert outbox.mark_outbox_sent(4, owner="stale-worker") is False
    sql, params = outbox_cursor.execute.call_args.args
    assert "lease_owner = %(owner)s" in sql and "lease_expires_at > now()" in sql
    assert params["owner"] == "stale-worker"


# --- fluxo do worker ---------------------------------------------------------


def _row(attempts: int = 1, max_attempts: int = 8) -> dict:
    incoming = IncomingMessage(provider="ycloud", channel="whatsapp", sender_phone="5511999999999",
                               conversation_id="wa:c1")
    result = AgentResult(reply_text="Seu pedido está em revisão.")
    return {"id": 4, "inbound_id": 91, "inbox_id": None, "lease_owner": "owner", "attempts": attempts,
            "max_attempts": max_attempts, "reply_text": result.reply_text,
            "reply_payload": outbox.build_outbound_envelope(incoming, result)}


@pytest.fixture
def worker_env(monkeypatch):
    monkeypatch.setattr(outbox_worker, "outbox_lease_current", lambda _: True)
    monkeypatch.setattr(outbox_worker, "has_later_queued_inbound", lambda _: False)
    monkeypatch.setattr("app.db.has_successful_agent_response", lambda _: False)
    monkeypatch.setattr("app.db.is_latest_inbound_message", lambda *args: True)
    monkeypatch.setattr("app.db.insert_agent_response", lambda *_: None)
    monkeypatch.setattr("app.human_takeover.human_takeover_active", lambda _: False)
    monkeypatch.setattr(outbox_worker, "get_settings", lambda: SimpleNamespace(agent_inbox_batch_size=5))
    marks = {"sent": Mock(return_value=True), "failed": Mock()}
    monkeypatch.setattr(outbox_worker, "mark_outbox_sent", marks["sent"])
    monkeypatch.setattr(outbox_worker, "mark_outbox_failed", marks["failed"])
    return marks


def _install(monkeypatch, rows, send):
    monkeypatch.setattr(outbox_worker, "claim_pending_outbox", lambda **kwargs: rows)
    monkeypatch.setattr(worker, "_send_reply", send)


@pytest.mark.asyncio
async def test_outbound_success_is_marked_sent_with_live_owner(monkeypatch, worker_env):
    send = AsyncMock(return_value={"ok": True, "provider_response": {"id": "y1"}})
    _install(monkeypatch, [_row()], send)
    report = await outbox_worker.process_outbox_batch()
    assert report["sent"] == 1
    assert worker_env["sent"].call_args.kwargs["owner"] == "owner"
    # correlacao chega ao provider para reconciliacao
    assert send.await_args.args[1].response_metadata["outbox_correlation_id"] == "outbox:4"


@pytest.mark.asyncio
async def test_temporary_failure_is_left_for_retry(monkeypatch, worker_env):
    _install(monkeypatch, [_row(attempts=1)], AsyncMock(return_value={"ok": False, "error": "ycloud_rate_limited"}))
    report = await outbox_worker.process_outbox_batch()
    assert report["failed"] == 1 and report["dead"] == 0
    assert worker_env["failed"].call_args.kwargs["dead"] is False


@pytest.mark.asyncio
async def test_connect_error_is_retryable(monkeypatch, worker_env):
    _install(monkeypatch, [_row()], AsyncMock(side_effect=httpx.ConnectError("refused")))
    report = await outbox_worker.process_outbox_batch()
    assert report["failed"] == 1
    assert worker_env["failed"].call_args.kwargs["dead"] is False


@pytest.mark.asyncio
async def test_provider_timeout_after_send_is_dead_lettered_not_duplicated(monkeypatch, worker_env):
    """B3: timeout depois do envio -> delivery_unknown, sem novo envio automatico."""
    _install(monkeypatch, [_row()], AsyncMock(side_effect=httpx.ReadTimeout("read")))
    report = await outbox_worker.process_outbox_batch()
    assert report["dead"] == 1
    kwargs = worker_env["failed"].call_args.kwargs
    assert kwargs["dead"] is True
    assert kwargs["error"].startswith(DELIVERY_UNKNOWN_ERROR)


@pytest.mark.asyncio
async def test_exhausted_attempts_reach_dead_letter(monkeypatch, worker_env):
    _install(monkeypatch, [_row(attempts=8, max_attempts=8)], AsyncMock(return_value={"ok": False, "error": "down"}))
    report = await outbox_worker.process_outbox_batch()
    assert report["dead"] == 1


@pytest.mark.asyncio
async def test_inline_dispatch_applies_the_same_policy(monkeypatch):
    row = _row()
    monkeypatch.setattr(outbox, "claim_outbox_for_send", lambda _: row)
    monkeypatch.setattr(outbox, "get_settings", lambda: SimpleNamespace(database_url="x"))
    monkeypatch.setattr(outbox_worker, "_resend_outbox_row",
                        AsyncMock(return_value={"ok": False, "error": "ycloud_delivery_unknown",
                                                "delivery_unknown": True}))
    failed = Mock()
    monkeypatch.setattr(outbox, "mark_outbox_failed", failed)
    await outbox.dispatch_accepted_outbound(4, AsyncMock())
    assert failed.call_args.kwargs["dead"] is True


# --- entrypoint ----------------------------------------------------------------


def test_outbox_cron_route_reuses_the_single_consumer(monkeypatch):
    import api.index as index
    from fastapi.testclient import TestClient

    drain = AsyncMock(return_value={"ok": True, "claimed": 0})
    monkeypatch.setattr("app.ingress.outbox_worker.process_outbox_batch", drain)
    index.app.dependency_overrides[index.verify_remarketing_cron] = lambda: None
    try:
        response = TestClient(index.app).post("/api/cron/process-outbox")
    finally:
        index.app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json()["claimed"] == 0
    drain.assert_awaited_once()


def test_outbox_cron_route_requires_cron_auth(monkeypatch):
    import api.index as index
    from fastapi.testclient import TestClient

    import app.security as security

    monkeypatch.setattr(security, "get_settings", lambda: SimpleNamespace(remarketing_cron_secret="s3cret"))
    drain = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("app.ingress.outbox_worker.process_outbox_batch", drain)
    client = TestClient(index.app)

    assert client.post("/api/cron/process-outbox").status_code == 401
    assert client.post("/api/cron/process-outbox", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/api/cron/process-outbox", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    drain.assert_awaited_once()
