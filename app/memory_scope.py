"""Server-owned workspace boundaries for conversation memory.

Versioned namespaces deliberately do not adopt older global identities or
snapshots. Their ownership cannot be established after historical merges.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

MEMORY_SCOPE_VERSION = 1


def trusted_workspace(settings: Any, workspace_id: str | None = None) -> str | None:
    configured = str(getattr(settings, "chatbo_workspace_id", "") or "").strip()
    supplied = str(workspace_id or "").strip()
    try:
        configured = str(UUID(configured)) if configured else ""
        supplied = str(UUID(supplied)) if supplied else ""
    except (ValueError, TypeError, AttributeError):
        return None
    # A message belongs to the workspace selected by this server deployment.
    # Never use an arbitrary incoming workspace as a replacement for its config.
    if not configured or (supplied and supplied != configured):
        return None
    return configured


def scoped_key(workspace_id: str, value: str) -> str | None:
    prefix = f"workspace:{workspace_id}:v{MEMORY_SCOPE_VERSION}:"
    value = str(value or "").strip()
    if not value:
        return None
    if value.startswith("workspace:"):
        return value if value.startswith(prefix) else None
    return prefix + value


def unscoped_key(workspace_id: str, value: str) -> str | None:
    prefix = f"workspace:{workspace_id}:v{MEMORY_SCOPE_VERSION}:"
    return value[len(prefix):] if value.startswith(prefix) else None


def stamp_state(state: dict[str, Any], workspace_id: str | None) -> dict[str, Any]:
    result = dict(state)
    result.pop("_memory_scope", None)
    if workspace_id:
        result["_memory_scope"] = {"version": MEMORY_SCOPE_VERSION, "workspace_id": workspace_id}
    return result


def trusted_state(state: Any, workspace_id: str) -> bool:
    return isinstance(state, dict) and state.get("_memory_scope") == {
        "version": MEMORY_SCOPE_VERSION, "workspace_id": workspace_id,
    }


def restore_state(state: Any, workspace_id: str) -> dict[str, Any]:
    if not trusted_state(state, workspace_id):
        return {}
    return {key: value for key, value in state.items() if key != "_memory_scope"}
