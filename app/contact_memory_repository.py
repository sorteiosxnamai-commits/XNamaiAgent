"""Repository for durable per-contact memories."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .db import get_conn, get_returning_id, to_jsonb
from .memory_models import ContactMemory


def _row_to_memory(row: dict[str, Any]) -> ContactMemory:
    return ContactMemory.model_validate(row)


def get_active_contact_memories(
    *,
    tenant_id: str,
    sender_key: str,
    limit: int = 20,
) -> list[ContactMemory]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM public.ai_contact_memories
                WHERE tenant_id = %s
                  AND sender_key = %s
                  AND status = 'active'
                  AND sensitive = false
                  AND (expires_at IS NULL OR expires_at > now())
                ORDER BY importance DESC, last_confirmed_at DESC NULLS LAST, id DESC
                LIMIT %s
                """,
                (tenant_id, sender_key, limit),
            )
            rows = cur.fetchall() or []
    return [_row_to_memory(row) for row in rows]


def select_relevant_memories(
    *,
    tenant_id: str,
    sender_key: str,
    domain: str | None = None,
    limit: int = 20,
    max_chars: int = 3000,
) -> list[ContactMemory]:
    """Deterministic relevance filter (no embeddings)."""
    active = get_active_contact_memories(
        tenant_id=tenant_id,
        sender_key=sender_key,
        limit=max(limit * 2, 20),
    )
    usable = [item for item in active if item.use_in_instructions]
    domain_norm = (domain or "").strip().lower()
    if domain_norm in {"payment", "order", "checkout", "shipping"}:
        prefer = {
            "preferred_name",
            "communication_style",
            "do_not_repeat",
            "stable_customer_fact",
        }
        usable = [
            item
            for item in usable
            if item.memory_kind in prefer
            or not str(item.memory_kind).endswith("_preference")
        ] or usable[:3]
    elif domain_norm in {"commerce", "product", "catalog"}:
        prefer = {
            "brand_preference",
            "product_preference",
            "price_preference",
            "color_preference",
            "material_preference",
            "size_preference",
            "explicit_no_preference",
            "preferred_name",
            "communication_style",
        }
        ranked = [item for item in usable if item.memory_kind in prefer]
        usable = ranked or usable
    elif domain_norm in {"greeting", "general"}:
        prefer = {"preferred_name", "communication_style", "do_not_repeat"}
        usable = [item for item in usable if item.memory_kind in prefer] or usable[:2]

    selected: list[ContactMemory] = []
    chars = 0
    for item in usable:
        chunk = len(item.safe_summary or "") + len(str(item.value))
        if selected and chars + chunk > max_chars:
            break
        selected.append(item)
        chars += chunk
        if len(selected) >= limit:
            break
    return selected


def upsert_contact_memory(
    *,
    tenant_id: str,
    sender_key: str,
    memory_key: str,
    memory_kind: str,
    value: Any,
    safe_summary: str | None = None,
    source: str = "model_proposal",
    importance: float = 0.0,
    confidence: float = 0.0,
    use_in_instructions: bool = True,
    sensitive: bool = False,
    source_inbound_id: int | None = None,
    source_response_id: int | None = None,
    expires_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
    status: str = "active",
) -> ContactMemory:
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM public.ai_contact_memories
                WHERE tenant_id = %s
                  AND sender_key = %s
                  AND memory_key = %s
                  AND status = 'active'
                LIMIT 1
                FOR UPDATE
                """,
                (tenant_id, sender_key, memory_key),
            )
            existing = cur.fetchone()
            if existing:
                old_id = int(existing["id"])
                cur.execute(
                    """
                    UPDATE public.ai_contact_memories
                    SET status = 'superseded',
                        updated_at = %s
                    WHERE id = %s
                    """,
                    (now, old_id),
                )
            cur.execute(
                """
                INSERT INTO public.ai_contact_memories (
                    tenant_id, sender_key, memory_key, memory_kind,
                    value, safe_summary, source, status, importance, confidence,
                    use_in_instructions, sensitive, source_inbound_id,
                    source_response_id, last_confirmed_at, expires_at, metadata
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s
                )
                RETURNING id
                """,
                (
                    tenant_id,
                    sender_key,
                    memory_key,
                    memory_kind,
                    to_jsonb(value if isinstance(value, (dict, list)) else {"value": value}),
                    safe_summary,
                    source,
                    status,
                    importance,
                    confidence,
                    use_in_instructions,
                    sensitive,
                    source_inbound_id,
                    source_response_id,
                    now,
                    expires_at,
                    to_jsonb(metadata or {}),
                ),
            )
            new_id = get_returning_id(cur.fetchone())
            if existing and new_id is not None:
                cur.execute(
                    """
                    UPDATE public.ai_contact_memories
                    SET superseded_by_id = %s
                    WHERE id = %s
                    """,
                    (new_id, int(existing["id"])),
                )
    memories = get_active_contact_memories(
        tenant_id=tenant_id, sender_key=sender_key, limit=100
    )
    for item in memories:
        if item.id == new_id:
            return item
    raise RuntimeError("contact_memory_upsert_failed")


def forget_contact_memory(
    *,
    tenant_id: str,
    sender_key: str,
    memory_key: str,
) -> int:
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.ai_contact_memories
                SET status = 'forgotten',
                    use_in_instructions = false,
                    updated_at = %s
                WHERE tenant_id = %s
                  AND sender_key = %s
                  AND memory_key = %s
                  AND status = 'active'
                """,
                (now, tenant_id, sender_key, memory_key),
            )
            return int(cur.rowcount or 0)


def format_customer_memory_block(memories: list[ContactMemory]) -> str:
    if not memories:
        return "<customer_memory>\n</customer_memory>"
    lines = ["<customer_memory>"]
    for item in memories:
        value = item.value
        if isinstance(value, dict) and "value" in value and len(value) == 1:
            rendered = value["value"]
        else:
            rendered = value
        if item.safe_summary:
            lines.append(f"{item.memory_key}: {item.safe_summary}")
        else:
            lines.append(f"{item.memory_key}: {rendered}")
    lines.append("</customer_memory>")
    return "\n".join(lines)
