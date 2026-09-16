import asyncio

import pytest

from app.conversation_lock import (
    ConversationLockUnavailable,
    acquire_conversation_lock,
    conversation_lock_key,
    release_conversation_lock,
)


def test_conversation_lock_key_uses_context_priority():
    assert conversation_lock_key(
        conversation_id="conversation",
        sender_key="sender",
        sender_phone="phone",
        visitor_id="visitor",
    ) == "conversation"
    assert conversation_lock_key(
        sender_key="sender",
        sender_phone="phone",
        visitor_id="visitor",
    ) == "sender"
    assert conversation_lock_key(visitor_id="visitor") == "visitor"
    assert conversation_lock_key() is None


@pytest.mark.asyncio
async def test_same_conversation_is_serialized_locally():
    first = await acquire_conversation_lock(
        "conversation:test",
        timeout_seconds=1,
    )
    second_acquired = asyncio.Event()

    async def acquire_second():
        second = await acquire_conversation_lock(
            "conversation:test",
            timeout_seconds=1,
        )
        second_acquired.set()
        await release_conversation_lock(second)

    task = asyncio.create_task(acquire_second())
    await asyncio.sleep(0)
    assert second_acquired.is_set() is False

    await release_conversation_lock(first)
    await task
    assert second_acquired.is_set() is True


@pytest.mark.asyncio
async def test_different_conversations_do_not_block_each_other():
    first = await acquire_conversation_lock("conversation:a")
    second = await acquire_conversation_lock("conversation:b")

    await release_conversation_lock(second)
    await release_conversation_lock(first)


@pytest.mark.asyncio
async def test_lock_timeout_is_typed_and_does_not_release_owner():
    first = await acquire_conversation_lock(
        "conversation:timeout",
        timeout_seconds=1,
    )
    try:
        with pytest.raises(
            ConversationLockUnavailable,
            match="local_lock_timeout",
        ):
            await acquire_conversation_lock(
                "conversation:timeout",
                timeout_seconds=0.01,
            )
    finally:
        await release_conversation_lock(first)
        await release_conversation_lock(first)


@pytest.mark.asyncio
async def test_database_lock_contention_is_busy_not_unavailable(monkeypatch):
    from app import conversation_lock as lock_mod

    def boom(*_args, **_kwargs):
        raise RuntimeError("canceling statement due to lock timeout")

    monkeypatch.setattr(lock_mod, "_acquire_database_lock", boom)

    with pytest.raises(ConversationLockUnavailable, match="database_lock_busy"):
        await acquire_conversation_lock(
            "conversation:db-busy",
            database_url="postgresql://example/db",
            timeout_seconds=1,
        )


@pytest.mark.asyncio
async def test_database_infra_failure_is_unavailable(monkeypatch):
    from app import conversation_lock as lock_mod

    def boom(*_args, **_kwargs):
        raise RuntimeError("could not connect to server")

    monkeypatch.setattr(lock_mod, "_acquire_database_lock", boom)

    with pytest.raises(
        ConversationLockUnavailable,
        match="database_lock_unavailable",
    ):
        await acquire_conversation_lock(
            "conversation:db-down",
            database_url="postgresql://example/db",
            timeout_seconds=1,
        )


def test_database_lock_pins_transaction_and_releases_it(monkeypatch):
    from unittest.mock import MagicMock
    from app import conversation_lock as module

    connection = MagicMock()
    connect = MagicMock(return_value=connection)
    monkeypatch.setattr(module.psycopg, "connect", connect)
    assert module._acquire_database_lock("test-only", 123, 1) is connection
    assert connect.call_args.kwargs["prepare_threshold"] is None
    assert connection.autocommit is False
    queries = [call.args[0] for call in connection.cursor.return_value.__enter__.return_value.execute.call_args_list]
    assert "set_config('lock_timeout'" in queries[0]
    assert "pg_advisory_xact_lock" in queries[1]
    module._release_database_lock(connection, 123)
    connection.rollback.assert_called_once()
    connection.close.assert_called_once()


@pytest.mark.asyncio
async def test_cancellation_releases_lock_acquired_by_background_thread(monkeypatch):
    import threading
    from app import conversation_lock as module

    started, finish = threading.Event(), threading.Event()
    released = asyncio.Event()
    loop = asyncio.get_running_loop()
    connection = object()

    def acquire(*args):
        started.set()
        assert finish.wait(3)
        return connection

    def release(value, lock_id):
        assert value is connection
        loop.call_soon_threadsafe(released.set)

    monkeypatch.setattr(module, "_acquire_database_lock", acquire)
    monkeypatch.setattr(module, "_release_database_lock", release)
    task = asyncio.create_task(acquire_conversation_lock("cancel-late", database_url="test-only"))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The cancelled request no longer owns the local conversation lock.
        handle = await acquire_conversation_lock("cancel-late", timeout_seconds=0.1)
        await release_conversation_lock(handle)
    finally:
        finish.set()
    await asyncio.wait_for(released.wait(), 3)
