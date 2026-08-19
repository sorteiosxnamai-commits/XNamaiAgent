"""Lightweight contact-memory consolidation (expire / prune)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .config import get_settings
from .db import get_conn


def expire_contact_memories(*, tenant_id: str | None = None) -> int:
    """Mark expired active memories as expired. Returns affected rows."""
    settings = get_settings()
    tenant = tenant_id or str(getattr(settings, "agent_persona_tenant_id", "newstore"))
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.ai_contact_memories
                SET status = 'expired',
                    use_in_instructions = false,
                    updated_at = %s
                WHERE tenant_id = %s
                  AND status = 'active'
                  AND expires_at IS NOT NULL
                  AND expires_at <= %s
                """,
                (now, tenant, now),
            )
            return int(cur.rowcount or 0)


def prune_excess_active_memories(
    *,
    tenant_id: str,
    sender_key: str,
    keep: int | None = None,
) -> int:
    """Keep the top-N active memories by importance; supersede the rest."""
    settings = get_settings()
    limit = int(
        keep
        if keep is not None
        else getattr(settings, "agent_max_active_contact_memories", 20)
    )
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM public.ai_contact_memories
                WHERE tenant_id = %s
                  AND sender_key = %s
                  AND status = 'active'
                ORDER BY importance DESC, last_confirmed_at DESC NULLS LAST, id DESC
                """,
                (tenant_id, sender_key),
            )
            rows = cur.fetchall() or []
            if len(rows) <= limit:
                return 0
            drop_ids = [int(row["id"]) for row in rows[limit:]]
            cur.execute(
                """
                UPDATE public.ai_contact_memories
                SET status = 'superseded',
                    use_in_instructions = false,
                    updated_at = %s
                WHERE id = ANY(%s)
                """,
                (now, drop_ids),
            )
            return int(cur.rowcount or 0)


def consolidate_contact_memories(
    *,
    tenant_id: str | None = None,
    sender_key: str | None = None,
) -> dict[str, Any]:
    """Run safe consolidation steps. Never touches persona versions."""
    settings = get_settings()
    tenant = tenant_id or str(getattr(settings, "agent_persona_tenant_id", "newstore"))
    expired = expire_contact_memories(tenant_id=tenant)
    pruned = 0
    if sender_key:
        pruned = prune_excess_active_memories(
            tenant_id=tenant,
            sender_key=sender_key,
        )
    return {
        "tenant_id": tenant,
        "expired": expired,
        "pruned": pruned,
        "sender_key": sender_key,
    }
