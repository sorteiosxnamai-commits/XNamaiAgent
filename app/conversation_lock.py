from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Any

import psycopg

from .runtime_context import (
    register_database_call,
    register_integration_failure,
)


class ConversationLockUnavailable(RuntimeError):
    pass


@dataclass
class _LocalLockEntry:
    lock: asyncio.Lock
    users: int = 0


@dataclass
class ConversationLockHandle:
    key_hash: str
    lock_id: int
    local_entry: _LocalLockEntry
    database_connection: Any = None
    released: bool = False


_LOCAL_LOCKS: dict[str, _LocalLockEntry] = {}
_LOCAL_LOCKS_GUARD = asyncio.Lock()


def conversation_lock_key(
    *,
    conversation_id: str | None = None,
    sender_key: str | None = None,
    sender_phone: str | None = None,
    visitor_id: str | None = None,
) -> str | None:
    value = conversation_id or sender_key or sender_phone or visitor_id
    if not value:
        return None
    return str(value).strip() or None


def _lock_identity(key: str) -> tuple[str, int]:
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
    return digest.hex(), int.from_bytes(digest, byteorder="big", signed=True)


async def _acquire_local(
    key_hash: str,
    timeout_seconds: float,
) -> _LocalLockEntry:
    async with _LOCAL_LOCKS_GUARD:
        entry = _LOCAL_LOCKS.setdefault(
            key_hash,
            _LocalLockEntry(lock=asyncio.Lock()),
        )
        entry.users += 1
    try:
        await asyncio.wait_for(
            entry.lock.acquire(),
            timeout=max(0.1, timeout_seconds),
        )
        return entry
    except BaseException:
        async with _LOCAL_LOCKS_GUARD:
            entry.users -= 1
            if entry.users <= 0 and not entry.lock.locked():
                _LOCAL_LOCKS.pop(key_hash, None)
        raise


async def _release_local(
    key_hash: str,
    entry: _LocalLockEntry,
) -> None:
    if entry.lock.locked():
        entry.lock.release()
    async with _LOCAL_LOCKS_GUARD:
        entry.users -= 1
        if entry.users <= 0 and not entry.lock.locked():
            _LOCAL_LOCKS.pop(key_hash, None)


def _acquire_database_lock(
    database_url: str,
    lock_id: int,
    timeout_seconds: float,
):
    register_database_call()
    connection = psycopg.connect(
        database_url,
        connect_timeout=min(max(int(timeout_seconds), 1), 10),
    )
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('lock_timeout', %s, false)",
                (f"{max(int(timeout_seconds * 1000), 100)}ms",),
            )
            cursor.execute("SELECT pg_advisory_lock(%s)", (lock_id,))
        return connection
    except Exception:
        connection.close()
        raise


def _release_database_lock(connection: Any, lock_id: int) -> None:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))
    finally:
        connection.close()


def _is_database_lock_contention(exc: BaseException) -> bool:
    """True when another worker likely holds the advisory lock."""
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return True
    text = str(exc).casefold()
    markers = (
        "lock timeout",
        "canceling statement due to lock timeout",
        "could not obtain lock",
        "deadlock detected",
    )
    return any(marker in text for marker in markers)


async def acquire_conversation_lock(
    key: str,
    *,
    database_url: str = "",
    timeout_seconds: float = 15.0,
) -> ConversationLockHandle:
    key_hash, lock_id = _lock_identity(key)
    try:
        local_entry = await _acquire_local(key_hash, timeout_seconds)
    except TimeoutError as exc:
        raise ConversationLockUnavailable("local_lock_timeout") from exc

    database_connection = None
    if database_url:
        try:
            database_connection = await asyncio.wait_for(
                asyncio.to_thread(
                    _acquire_database_lock,
                    database_url,
                    lock_id,
                    timeout_seconds,
                ),
                timeout=timeout_seconds + 2,
            )
        except Exception as exc:
            register_integration_failure("database_lock")
            await _release_local(key_hash, local_entry)
            # Contention: another worker holds the conversation.
            # Infra failure: caller may fall back to a local-only lock so
            # photo turns are not silently dropped.
            if _is_database_lock_contention(exc):
                raise ConversationLockUnavailable(
                    "database_lock_busy"
                ) from exc
            raise ConversationLockUnavailable(
                "database_lock_unavailable"
            ) from exc

    return ConversationLockHandle(
        key_hash=key_hash,
        lock_id=lock_id,
        local_entry=local_entry,
        database_connection=database_connection,
    )


async def release_conversation_lock(
    handle: ConversationLockHandle | None,
) -> None:
    if handle is None or handle.released:
        return
    handle.released = True
    try:
        if handle.database_connection is not None:
            await asyncio.to_thread(
                _release_database_lock,
                handle.database_connection,
                handle.lock_id,
            )
    except Exception as exc:
        register_integration_failure("database_lock_release")
        print("[agent.lock] release_failed", {
            "error_type": type(exc).__name__,
            "key_hash": handle.key_hash,
        })
    finally:
        await _release_local(handle.key_hash, handle.local_entry)
