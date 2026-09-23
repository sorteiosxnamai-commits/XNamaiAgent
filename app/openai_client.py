"""Shared OpenAI clients for the agent runtime.

Business modules should obtain clients from here (or via openai_gateway)
instead of constructing AsyncOpenAI/OpenAI on every call.
"""

from __future__ import annotations

from openai import AsyncOpenAI, DefaultAsyncHttpxClient, DefaultHttpxClient, OpenAI

from .config import get_settings

_async_client: AsyncOpenAI | None = None
_sync_client: OpenAI | None = None
_async_client_fingerprint: tuple[str, float, int] | None = None
_sync_client_fingerprint: tuple[str, float, int] | None = None


def _client_fingerprint(key: str) -> tuple[str, float, int]:
    settings = get_settings()
    timeout = float(getattr(settings, "openai_timeout_seconds", 45.0) or 45.0)
    retries = int(getattr(settings, "openai_max_retries", 2) or 0)
    return key, timeout, retries


def _count_sdk_request() -> None:
    """Charge every real HTTP request (SDK retries included) to the turn budget."""
    from .runtime_context import get_current_turn

    runtime = get_current_turn()
    if runtime is not None:
        runtime.register_sdk_http_request()


async def _count_sdk_request_async(_request) -> None:
    _count_sdk_request()


def _count_sdk_request_sync(_request) -> None:
    _count_sdk_request()


def get_async_openai_client(*, api_key: str | None = None) -> AsyncOpenAI:
    """Return a process-wide AsyncOpenAI client (recreated if key/timeouts change)."""
    global _async_client, _async_client_fingerprint
    settings = get_settings()
    key = (api_key if api_key is not None else settings.openai_api_key) or ""
    fingerprint = _client_fingerprint(key)
    if _async_client is None or _async_client_fingerprint != fingerprint:
        _async_client = AsyncOpenAI(
            api_key=key,
            timeout=fingerprint[1],
            max_retries=fingerprint[2],
            http_client=DefaultAsyncHttpxClient(
                event_hooks={"request": [_count_sdk_request_async]}
            ),
        )
        _async_client_fingerprint = fingerprint
    return _async_client


def get_sync_openai_client(*, api_key: str | None = None) -> OpenAI:
    """Return a process-wide sync OpenAI client (audio/embeddings helpers)."""
    global _sync_client, _sync_client_fingerprint
    settings = get_settings()
    key = (api_key if api_key is not None else settings.openai_api_key) or ""
    fingerprint = _client_fingerprint(key)
    if _sync_client is None or _sync_client_fingerprint != fingerprint:
        _sync_client = OpenAI(
            api_key=key,
            timeout=fingerprint[1],
            max_retries=fingerprint[2],
            http_client=DefaultHttpxClient(
                event_hooks={"request": [_count_sdk_request_sync]}
            ),
        )
        _sync_client_fingerprint = fingerprint
    return _sync_client


def reset_openai_clients() -> None:
    """Clear cached clients (tests / settings reload)."""
    global _async_client, _sync_client
    global _async_client_fingerprint, _sync_client_fingerprint
    _async_client = None
    _sync_client = None
    _async_client_fingerprint = None
    _sync_client_fingerprint = None
