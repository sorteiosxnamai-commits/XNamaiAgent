from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx


T = TypeVar("T")

TRANSIENT_HTTP_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_BACKOFF_SECONDS = 0.2


async def with_retries(
    operation: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
    retry_exceptions: tuple[type[BaseException], ...] = (
        httpx.TimeoutException,
        httpx.ConnectError,
        httpx.NetworkError,
    ),
    should_retry: Callable[[BaseException], bool] | None = None,
) -> T:
    attempts = max(1, int(max_attempts))
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except retry_exceptions as exc:
            last_error = exc
            if attempt >= attempts:
                raise
            if should_retry is not None and not should_retry(exc):
                raise
            await asyncio.sleep(backoff_seconds * attempt)
    assert last_error is not None
    raise last_error


def is_transient_status(status_code: int | None) -> bool:
    return status_code in TRANSIENT_HTTP_STATUS
