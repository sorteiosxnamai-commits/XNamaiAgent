"""Repository for approved / pending instruction extensions."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from .db import get_conn, get_returning_id, to_jsonb


def hash_instruction(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _norm_scope_key(scope_key: str | None) -> str:
    return (scope_key or "").strip()


def list_active_extensions(
    *,
    tenant_id: str,
    channel: str | None = None,
    sender_key: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM public.ai_agent_instruction_extensions
                WHERE tenant_id = %s
                  AND status = 'active'
                  AND (expires_at IS NULL OR expires_at > now())
                  AND (
                        scope = 'tenant'
                     OR (scope = 'channel' AND scope_key_norm = %s)
                     OR (scope = 'contact' AND scope_key_norm = %s)
                  )
                ORDER BY importance DESC NULLS LAST, id DESC
                LIMIT %s
                """,
                (
                    tenant_id,
                    _norm_scope_key(channel),
                    _norm_scope_key(sender_key),
                    limit,
                ),
            )
            return list(cur.fetchall() or [])


def list_pending_extensions(*, tenant_id: str, limit: int = 50) -> list[dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM public.ai_agent_instruction_extensions
                WHERE tenant_id = %s
                  AND status = 'pending_review'
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (tenant_id, limit),
            )
            return list(cur.fetchall() or [])


def create_extension_proposal(
    *,
    tenant_id: str,
    extension_key: str,
    instruction_text: str,
    category: str,
    scope: str = "tenant",
    scope_key: str | None = None,
    source: str = "model_proposal",
    importance: float | None = None,
    confidence: float | None = None,
    proposed_by_inbound_id: int | None = None,
    proposed_by_response_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.ai_agent_instruction_extensions (
                    tenant_id, scope, scope_key, scope_key_norm, extension_key,
                    category, instruction_text, instruction_hash, source, status,
                    importance, confidence, proposed_by_inbound_id,
                    proposed_by_response_id, first_seen_at, last_seen_at, metadata
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending_review',
                    %s, %s, %s, %s, %s, %s, %s
                )
                RETURNING *
                """,
                (
                    tenant_id,
                    scope,
                    scope_key,
                    _norm_scope_key(scope_key),
                    extension_key,
                    category,
                    instruction_text,
                    hash_instruction(instruction_text),
                    source,
                    importance,
                    confidence,
                    proposed_by_inbound_id,
                    proposed_by_response_id,
                    now,
                    now,
                    to_jsonb(metadata or {}),
                ),
            )
            row = cur.fetchone()
    if not row:
        raise RuntimeError("extension_create_failed")
    return dict(row)


def approve_extension(
    extension_id: int,
    *,
    tenant_id: str,
    approved_by: str = "admin",
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM public.ai_agent_instruction_extensions
                WHERE id = %s AND tenant_id = %s
                LIMIT 1
                FOR UPDATE
                """,
                (extension_id, tenant_id),
            )
            target = cur.fetchone()
            if not target:
                raise ValueError("extension_not_found")
            cur.execute(
                """
                UPDATE public.ai_agent_instruction_extensions
                SET status = 'superseded',
                    updated_at = %s
                WHERE tenant_id = %s
                  AND scope = %s
                  AND scope_key_norm = %s
                  AND extension_key = %s
                  AND status = 'active'
                  AND id <> %s
                """,
                (
                    now,
                    tenant_id,
                    target["scope"],
                    target["scope_key_norm"],
                    target["extension_key"],
                    extension_id,
                ),
            )
            cur.execute(
                """
                UPDATE public.ai_agent_instruction_extensions
                SET status = 'active',
                    approved_by = %s,
                    approved_at = %s,
                    updated_at = %s
                WHERE id = %s
                RETURNING *
                """,
                (approved_by, now, now, extension_id),
            )
            row = cur.fetchone()
    if not row:
        raise RuntimeError("extension_approve_failed")
    return dict(row)


def reject_extension(
    extension_id: int,
    *,
    tenant_id: str,
    rejected_by: str = "admin",
    rejection_reason: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.ai_agent_instruction_extensions
                SET status = 'rejected',
                    rejected_by = %s,
                    rejected_at = %s,
                    rejection_reason = %s,
                    updated_at = %s
                WHERE id = %s
                  AND tenant_id = %s
                RETURNING *
                """,
                (
                    rejected_by,
                    now,
                    rejection_reason,
                    now,
                    extension_id,
                    tenant_id,
                ),
            )
            row = cur.fetchone()
    if not row:
        raise ValueError("extension_not_found")
    return dict(row)


def format_approved_extensions_block(extensions: list[dict[str, Any]]) -> str:
    if not extensions:
        return "<approved_instruction_extensions>\n</approved_instruction_extensions>"
    lines = ["<approved_instruction_extensions>"]
    for item in extensions:
        key = item.get("extension_key") or "extension"
        text = (item.get("instruction_text") or "").strip()
        if text:
            lines.append(f"- [{key}] {text}")
    lines.append("</approved_instruction_extensions>")
    return "\n".join(lines)
