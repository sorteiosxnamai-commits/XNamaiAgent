"""Keeping the customer index inside its 1h MAX_AGE: an incremental sync
scheduled often enough, serialised so an overlapping tick skips instead of
racing a previous run, and never touching Mercos beyond reads.

No real Postgres needed here: the advisory-lock wrapper is tested against a
fake connection/cursor. The DB-dependent rebuild/failure-preserves-index
behaviour of `run_customer_sync` itself is already covered under
`@pytest.mark.integration` in test_customer_document_index_postgres.py.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.commerce.health import run_configured_customer_sync


class _FakeCursor:
    def __init__(self, lock_acquired: bool, calls: list[str]):
        self._lock_acquired = lock_acquired
        self._calls = calls
        self._last = None

    def execute(self, sql, params=None):
        self._calls.append(sql.strip().split("\n")[0])
        if "pg_try_advisory_lock" in sql:
            self._last = (self._lock_acquired,)
        elif "pg_advisory_unlock" in sql:
            self._calls.append("UNLOCK")

    def fetchone(self):
        return self._last

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, lock_acquired: bool, calls: list[str]):
        self._lock_acquired = lock_acquired
        self._calls = calls

    def cursor(self):
        return _FakeCursor(self._lock_acquired, self._calls)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_provider(runner):
    return SimpleNamespace(run_customer_sync=runner)


@pytest.mark.asyncio
async def test_overlapping_sync_skips_instead_of_running_twice(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(database_url="postgresql://x/y"),
    )
    monkeypatch.setattr("app.db.get_conn", lambda: _FakeConn(False, calls))
    runner_calls = []

    async def runner():
        runner_calls.append(1)
        return {"ok": True}

    monkeypatch.setattr(
        "app.commerce.provider.get_commerce_provider",
        lambda: _fake_provider(runner),
    )

    result = await run_configured_customer_sync()
    assert result == {"ok": False, "error": "sync_already_running"}
    assert runner_calls == []  # the second sync never actually ran
    assert "UNLOCK" not in calls  # never acquired, nothing to release


@pytest.mark.asyncio
async def test_sync_runs_and_releases_the_lock_when_acquired(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(database_url="postgresql://x/y"),
    )
    monkeypatch.setattr("app.db.get_conn", lambda: _FakeConn(True, calls))
    runner_calls = []

    async def runner():
        runner_calls.append(1)
        return {"ok": True, "pages": 1, "records": 3}

    monkeypatch.setattr(
        "app.commerce.provider.get_commerce_provider",
        lambda: _fake_provider(runner),
    )

    result = await run_configured_customer_sync()
    assert result == {"ok": True, "pages": 1, "records": 3}
    assert runner_calls == [1]
    assert "UNLOCK" in calls


@pytest.mark.asyncio
async def test_sync_runs_unlocked_without_a_database_configured(monkeypatch):
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(database_url=""),
    )

    def explode():  # pragma: no cover - must never be reached
        raise AssertionError("should not touch the database when it is not configured")

    monkeypatch.setattr("app.db.get_conn", explode)
    runner_calls = []

    async def runner():
        runner_calls.append(1)
        return {"ok": True}

    monkeypatch.setattr(
        "app.commerce.provider.get_commerce_provider",
        lambda: _fake_provider(runner),
    )

    result = await run_configured_customer_sync()
    assert result == {"ok": True}
    assert runner_calls == [1]


@pytest.mark.asyncio
async def test_cron_route_calls_the_same_official_sync_service(monkeypatch):
    """The scheduled trigger (GitHub Actions -> this route) must go through
    the exact same function the admin route already uses — no separate
    implementation for the cron path."""
    import api.index as api_module

    calls = []

    async def fake_sync():
        calls.append("cron")
        return {"ok": True, "pages": 0, "records": 0}

    monkeypatch.setattr(api_module, "run_configured_customer_sync", fake_sync, raising=False)
    from app.commerce import health as health_module

    monkeypatch.setattr(health_module, "run_configured_customer_sync", fake_sync)

    result = await api_module.commerce_customer_sync_cron()
    assert result["ok"] is True
    assert calls == ["cron"]


@pytest.mark.asyncio
async def test_customer_sync_never_sends_a_mutation_request():
    """The paging/writer path used by the sync only ever reads from Mercos —
    a POST/PUT here would mean the sync itself is creating or editing
    customers, which it must never do."""
    from app.commerce.mercos.client import MercosAdaptorClient
    from app.commerce.mercos.sync import sync_resource

    seen_methods: list[str] = []

    def handler(request: httpx.Request):
        seen_methods.append(request.method)
        assert request.method == "GET"
        return httpx.Response(200, json={
            "resource": "clientes", "count": 1,
            "pageCursor": "2026-03-01T10:00:00", "nextCursor": None,
            "data": [{"id": 1, "ultima_alteracao": "2026-03-01T10:00:00"}],
        })

    class _NoOpStore:
        def read(self, provider, resource):
            from app.commerce.mercos.sync_state import SyncState
            return SyncState(provider=provider, resource=resource)

        def get_cursor(self, provider, resource):
            return None

        def set_cursor(self, provider, resource, cursor):
            pass

        def record_success(self, provider, resource, *, records, pages):
            pass

        def record_failure(self, provider, resource, *, error_code):
            pass

    class _RecordingWriter:
        async def write_page(self, resource, records):
            return len(records)

    client = MercosAdaptorClient(
        base_url="https://a.example.com", api_key="k", timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )
    outcome = await sync_resource(
        client=client, resource="clientes", store=_NoOpStore(),
        writer=_RecordingWriter(), provider="mercos", strict_records=True,
    )
    assert outcome.ok is True
    assert seen_methods == ["GET"]
