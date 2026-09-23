from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts import process_queues


@pytest.mark.asyncio
async def test_single_cycle_drains_both_queues_and_reports_failure(monkeypatch):
    monkeypatch.setattr(process_queues, "get_settings", lambda: SimpleNamespace(database_url="test-only"))
    dispatch = AsyncMock(return_value={"ok": False, "inbox": {"ok": False}, "outbox": {"ok": True}})
    monkeypatch.setattr(process_queues, "process_pending_queues", dispatch)
    assert await process_queues.run_worker(once=True, batch_size=3) is False
    dispatch.assert_awaited_once_with(limit=3)


@pytest.mark.asyncio
async def test_unconfigured_worker_stops_before_dispatch(monkeypatch):
    monkeypatch.setattr(process_queues, "get_settings", lambda: SimpleNamespace(database_url=""))
    dispatch = AsyncMock()
    monkeypatch.setattr(process_queues, "process_pending_queues", dispatch)
    with pytest.raises(ValueError, match="DATABASE_URL"):
        await process_queues.run_worker(once=True)
    dispatch.assert_not_awaited()
