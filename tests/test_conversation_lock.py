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
