"""Durable inbound inbox (FASE 2 — async webhook processing)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from app.config import get_settings
from app.db import ensure_tables, get_conn, to_jsonb
from app.observability import log_event


def build_idempotency_key(
    *,
    provider: str,
    message_id: str | None,
    payload: dict[str, Any],
) -> str:
    if message_id and str(message_id).strip():
        return f"{provider}:msg:{str(message_id).strip()}"
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:32]
    return f"{provider}:hash:{digest}"


def enqueue_inbound(
    *,
    provider: str,
    channel: str,
    message_id: str | None,
    conversation_key: str | None,
    visitor_id: str | None,
    sender_key: str | None,
    event_name: str | None,
    payload: dict[str, Any],
) -> tuple[bool, int | None]:
    """Insert inbox row. Returns (created, inbox_id). Duplicate → (False, existing_id)."""
    settings = get_settings()
    if not settings.database_url:
        return False, None

    ensure_tables()
    idempotency_key = build_idempotency_key(
        provider=provider,
        message_id=message_id,
        payload=payload,
    )
    # Sanitize: never store huge binary; payload is JSON from webhook.
    safe_payload = payload if isinstance(payload, dict) else {}

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.ai_inbound_inbox (
                  provider, channel, message_id, idempotency_key,
                  conversation_key, visitor_id, sender_key, event_name,
                  payload_json, status
                ) VALUES (
                  %(provider)s, %(channel)s, %(message_id)s, %(idempotency_key)s,
                  %(conversation_key)s, %(visitor_id)s, %(sender_key)s, %(event_name)s,
                  %(payload_json)s, 'pending'
                )
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING id
                """,
                {
                    "provider": provider,
                    "channel": (channel or "unknown").lower(),
                    "message_id": message_id,
                    "idempotency_key": idempotency_key,
                    "conversation_key": conversation_key,
                    "visitor_id": visitor_id,
                    "sender_key": sender_key,
                    "event_name": event_name,
                    "payload_json": to_jsonb(safe_payload),
                },
            )
            row = cur.fetchone()
            if row:
                inbox_id = int(row["id"] if isinstance(row, dict) else row[0])
                log_event(
                    "inbox.enqueued",
                    {
                        "inbox_id": inbox_id,
                        "provider": provider,
                        "channel": channel,
                        "message_id_present": bool(message_id),
                    },
                )
                return True, inbox_id

            cur.execute(
                """
                SELECT id FROM public.ai_inbound_inbox
                WHERE idempotency_key = %(idempotency_key)s
                LIMIT 1
                """,
                {"idempotency_key": idempotency_key},
            )
            existing = cur.fetchone()
            existing_id = (
                int(existing["id"] if isinstance(existing, dict) else existing[0])
                if existing
                else None
            )
            log_event(
                "inbox.duplicate",
                {
                    "inbox_id": existing_id,
                    "provider": provider,
                    "channel": channel,
                },
            )
            return False, existing_id


def claim_pending_inbox(
    *,
    limit: int = 5,
    lease_seconds: int = 90,
    owner: str | None = None,
) -> list[dict[str, Any]]:
    settings = get_settings()
    if not settings.database_url:
        return []
    ensure_tables()
    lease_owner = owner or f"worker:{uuid4().hex[:12]}"
    expires = datetime.now(timezone.utc) + timedelta(seconds=max(15, lease_seconds))
    claimed: list[dict[str, Any]] = []

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH next_rows AS (
                  SELECT id
                  FROM public.ai_inbound_inbox
                  WHERE status IN ('pending', 'failed')
                    AND attempts < max_attempts
                    AND (
                      lease_expires_at IS NULL
                      OR lease_expires_at < now()
                      OR status = 'pending'
                    )
                  ORDER BY created_at ASC
                  FOR UPDATE SKIP LOCKED
                  LIMIT %(limit)s
                )
                UPDATE public.ai_inbound_inbox AS inbox
                SET status = 'leased',
                    lease_owner = %(owner)s,
                    lease_expires_at = %(expires)s,
                    attempts = inbox.attempts + 1,
                    updated_at = now()
                FROM next_rows
                WHERE inbox.id = next_rows.id
                RETURNING
                  inbox.id, inbox.provider, inbox.channel, inbox.message_id,
                  inbox.idempotency_key, inbox.conversation_key, inbox.visitor_id,
                  inbox.sender_key, inbox.event_name, inbox.payload_json,
                  inbox.attempts
                """,
                {
                    "limit": max(1, min(int(limit), 25)),
                    "owner": lease_owner,
                    "expires": expires,
                },
            )
            rows = cur.fetchall() or []

    for row in rows:
        if isinstance(row, dict):
            claimed.append(dict(row))
        else:
            claimed.append(
                {
                    "id": row[0],
                    "provider": row[1],
                    "channel": row[2],
                    "message_id": row[3],
                    "idempotency_key": row[4],
                    "conversation_key": row[5],
                    "visitor_id": row[6],
                    "sender_key": row[7],
                    "event_name": row[8],
                    "payload_json": row[9],
                    "attempts": row[10],
                }
            )
    return claimed


def mark_inbox_processed(
    inbox_id: int,
    *,
    processed_inbound_id: int | None = None,
) -> None:
    settings = get_settings()
    if not settings.database_url:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.ai_inbound_inbox
                SET status = 'processed',
                    processed_inbound_id = %(inbound_id)s,
                    processed_at = now(),
                    updated_at = now(),
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    last_error = NULL
                WHERE id = %(id)s
                """,
                {"id": inbox_id, "inbound_id": processed_inbound_id},
            )
    log_event("inbox.processed", {"inbox_id": inbox_id})


def mark_inbox_failed(inbox_id: int, *, error: str, dead: bool = False) -> None:
    settings = get_settings()
    if not settings.database_url:
        return
    status = "dead" if dead else "failed"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.ai_inbound_inbox
                SET status = %(status)s,
                    last_error = %(error)s,
                    updated_at = now(),
                    lease_owner = NULL,
                    lease_expires_at = NULL
                WHERE id = %(id)s
                """,
                {
                    "id": inbox_id,
                    "status": status,
                    "error": (error or "")[:500],
                },
            )
    log_event(
        "inbox.failed",
        {"inbox_id": inbox_id, "dead": dead, "error_type": type(error).__name__},
    )
