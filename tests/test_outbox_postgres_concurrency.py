"""Concorrencia REAL da outbox contra PostgreSQL (FOR UPDATE SKIP LOCKED + lease).

Mock de cursor nao prova exclusao mutua. Este teste so roda com um banco
descartavel: ``TEST_DATABASE_URL=postgresql://... pytest -m integration``.
Sem a variavel ele e PULADO — nunca conta como aprovado.
"""

from __future__ import annotations

import os
import threading
from uuid import uuid4

import pytest

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set (needs a disposable PostgreSQL)"),
]


@pytest.fixture
def pg_outbox(monkeypatch):
    from app import db
    from app.config import get_settings
    from app.ingress import outbox

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("AUTO_CREATE_TABLES", "true")
    monkeypatch.setenv("AGENT_OUTBOX_LEASE_SECONDS", "60")
    get_settings.cache_clear()
    db.ensure_tables()
    marker = f"it-{uuid4().hex[:10]}"
    try:
        yield outbox, marker
    finally:
        with db.get_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM public.ai_outbound_outbox WHERE provider = %s", (marker,))
        get_settings.cache_clear()


def _enqueue(outbox, marker: str, count: int) -> set[int]:
    return {
        outbox.enqueue_outbound(provider=marker, channel="whatsapp", reply_text=f"r{i}",
                                conversation_key=f"{marker}:{i}")
        for i in range(count)
    }


def test_two_workers_never_claim_the_same_row(pg_outbox):
    outbox, marker = pg_outbox
    ids = _enqueue(outbox, marker, 12)
    claimed: dict[str, list[int]] = {}
    barrier = threading.Barrier(2)

    def run(owner: str) -> None:
        barrier.wait()
        rows = outbox.claim_pending_outbox(limit=12, owner=owner)
        claimed[owner] = [int(r["id"]) for r in rows if int(r["id"]) in ids]

    threads = [threading.Thread(target=run, args=(name,)) for name in ("w1", "w2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not set(claimed["w1"]) & set(claimed["w2"])
    assert set(claimed["w1"]) | set(claimed["w2"]) == ids


def test_expired_lease_is_recovered_and_old_owner_loses_the_receipt(pg_outbox):
    from app import db

    outbox, marker = pg_outbox
    (row_id,) = _enqueue(outbox, marker, 1)
    first = outbox.claim_pending_outbox(limit=1, owner="crashed")
    assert [int(r["id"]) for r in first] == [row_id]

    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE public.ai_outbound_outbox SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
            (row_id,),
        )
    second = outbox.claim_pending_outbox(limit=1, owner="recovery")
    assert [int(r["id"]) for r in second] == [row_id]

    assert outbox.mark_outbox_sent(row_id, owner="crashed") is False
    assert outbox.mark_outbox_sent(row_id, owner="recovery") is True


def _set(row_id: int, sql_set: str, params: tuple = ()) -> None:
    from app import db

    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE public.ai_outbound_outbox SET {sql_set} WHERE id = %s", (*params, row_id))


def test_concurrent_retry_of_failed_rows_is_claimed_once(pg_outbox):
    """Linhas 'failed' com backoff vencido: dois workers, uma unica retentativa cada."""
    outbox, marker = pg_outbox
    ids = _enqueue(outbox, marker, 6)
    for row_id in ids:
        _set(row_id, "status = 'failed', attempts = 1, updated_at = now() - interval '10 minutes'")
    claimed: dict[str, list[int]] = {}
    barrier = threading.Barrier(2)

    def run(owner: str) -> None:
        barrier.wait()
        claimed[owner] = [int(r["id"]) for r in outbox.claim_pending_outbox(limit=6, owner=owner) if int(r["id"]) in ids]

    threads = [threading.Thread(target=run, args=(name,)) for name in ("w1", "w2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not set(claimed["w1"]) & set(claimed["w2"])
    assert set(claimed["w1"]) | set(claimed["w2"]) == ids


def test_failed_transaction_rolls_back_the_claim(pg_outbox):
    """Erro no meio da transacao: nada fica leased pela metade."""
    from app import db

    outbox, marker = pg_outbox
    (row_id,) = _enqueue(outbox, marker, 1)
    with pytest.raises(RuntimeError):
        with db.get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE public.ai_outbound_outbox SET status = 'leased', lease_owner = 'doomed' WHERE id = %s",
                (row_id,),
            )
            raise RuntimeError("worker crashed mid-transaction")
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT status, lease_owner FROM public.ai_outbound_outbox WHERE id = %s", (row_id,))
        row = cur.fetchone()
    assert (row["status"], row["lease_owner"]) == ("pending", None)


def test_delivery_unknown_is_reconciled_only_by_provider_evidence(pg_outbox):
    from app import db
    from app.ingress.delivery_reconciliation import reconcile_provider_status

    outbox, marker = pg_outbox
    (row_id,) = _enqueue(outbox, marker, 1)
    _set(row_id, "status = 'dead', provider = 'ycloud', last_error = 'delivery_unknown:ReadTimeout'")

    first = reconcile_provider_status(provider="ycloud", external_id=f"outbox:{row_id}",
                                      provider_status="delivered", event_id="evt-a")
    again = reconcile_provider_status(provider="ycloud", external_id=f"outbox:{row_id}",
                                      provider_status="delivered", event_id="evt-a")
    late_failure = reconcile_provider_status(provider="ycloud", external_id=f"outbox:{row_id}",
                                             provider_status="failed", event_id="evt-b")
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT status, last_error FROM public.ai_outbound_outbox WHERE id = %s", (row_id,))
        row = cur.fetchone()
    _set(row_id, "provider = %s", (marker,))  # so the fixture cleanup finds it

    assert first["outcome"] == "confirmed_sent"
    assert again["outcome"] == "duplicate_event"
    assert late_failure["outcome"] == "no_transition"
    assert (row["status"], row["last_error"]) == ("sent", None)
