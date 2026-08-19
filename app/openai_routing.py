"""Sticky canary routing for Responses traffic percentage."""

from __future__ import annotations

import hashlib
from typing import Literal

from .config import get_settings
from .runtime_context import get_current_turn


ApiRoute = Literal["chat_completions", "responses"]


def routing_key_from_turn() -> str | None:
    """Sticky key = tenant_id + conversation_id (never raw phone alone)."""
    runtime = get_current_turn()
    if runtime is None:
        return None
    tenant = ""
    try:
        from .config import get_settings

        tenant = str(
            getattr(get_settings(), "agent_persona_tenant_id", None) or "newstore"
        ).strip()
    except Exception:
        tenant = "newstore"
    conversation = (runtime.conversation_key or "").strip()
    if conversation and conversation != "unresolved":
        # Hash the conversation side so logs never need the raw id.
        digest = hashlib.sha256(conversation.encode("utf-8")).hexdigest()[:16]
        return f"{tenant}:{digest}"
    if runtime.inbound_id is not None:
        return f"{tenant}:inbound:{runtime.inbound_id}"
    if runtime.trace_id:
        return f"{tenant}:trace:{runtime.trace_id}"
    return None


def sticky_routing_key(*, tenant_id: str, conversation_id: str) -> str:
    """Public helper for canary sticky routing tests / ops."""
    tenant = str(tenant_id or "newstore").strip() or "newstore"
    conv = str(conversation_id or "").strip()
    digest = hashlib.sha256(conv.encode("utf-8")).hexdigest()[:16]
    return f"{tenant}:{digest}"


def bucket_for_key(routing_key: str) -> int:
    digest = hashlib.sha256(routing_key.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 10_000


def select_api_route(
    *,
    routing_key: str | None = None,
    traffic_percent: float | None = None,
    sticky: bool | None = None,
) -> ApiRoute:
    """Return which API should serve this turn.

    ``traffic_percent`` is 0..1. Sticky hashing keeps the same conversation
    on the same API for the whole rollout window.
    """
    settings = get_settings()
    if traffic_percent is not None:
        percent = traffic_percent
    else:
        from .rollout import resolve_responses_traffic_percent

        percent = resolve_responses_traffic_percent(settings)
    percent = max(0.0, min(1.0, percent))
    if percent <= 0.0:
        return "chat_completions"
    if percent >= 1.0:
        return "responses"

    use_sticky = (
        sticky
        if sticky is not None
        else bool(getattr(settings, "openai_canary_sticky_routing", True))
    )
    key = routing_key if use_sticky else None
    if not key:
        key = routing_key_from_turn() or "anonymous"
    threshold = int(round(percent * 10_000))
    return "responses" if bucket_for_key(key) < threshold else "chat_completions"


def remember_route(route: ApiRoute) -> None:
    runtime = get_current_turn()
    if runtime is None:
        return
    runtime.openai_api_route = route
