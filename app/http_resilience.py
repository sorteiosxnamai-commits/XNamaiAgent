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


# --- Non-idempotent sends (outbound messages) ------------------------------
# A message POST is not idempotent and no provider used here offers an
# idempotency key. Only failures that provably happened BEFORE the request
# reached the provider are safe to repeat; everything after that is ambiguous.

#: Raised before any byte reached the provider: safe to repeat the POST.
NOT_SENT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
)
#: "Not accepted, try later".
RETRYABLE_SEND_STATUS = frozenset({408, 425, 429, 502, 503})
#: The provider may already have processed the request.
UNKNOWN_SEND_STATUS = frozenset({500, 504})


def classify_send_exception(exc: BaseException) -> str:
    """``retryable`` only when the request never left; otherwise ``unknown``."""
    if isinstance(exc, NOT_SENT_EXCEPTIONS):
        return "retryable"
    # Read/write timeout, reset mid-response, or a failure we cannot place
    # before the POST (e.g. while parsing the response): may have landed.
    return "unknown"


def classify_send_status(status_code: int | None) -> str:
    """``retryable`` | ``permanent`` | ``unknown`` for a non-2xx send response."""
    if status_code is None:
        return "unknown"
    if status_code in RETRYABLE_SEND_STATUS:
        return "retryable"
    if status_code in UNKNOWN_SEND_STATUS or status_code >= 500:
        return "unknown"
    if 400 <= status_code < 500:
        return "permanent"
    return "retryable"
