"""Outbound outbox (FASE 2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from app.config import get_settings
from app.db import ensure_tables, get_conn, to_jsonb
from app.observability import log_event


def enqueue_outbound(
    *,
    provider: str,
    channel: str,
    reply_text: str,
    inbox_id: int | None = None,
    inbound_id: int | None = None,
    conversation_key: str | None = None,
    visitor_id: str | None = None,
    sender_key: str | None = None,
    recipient_external_id: str | None = None,
    reply_payload: dict[str, Any] | None = None,
) -> int | None:
    settings = get_settings()
    if not settings.database_url:
        return None
    ensure_tables()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.ai_outbound_outbox (
                  inbox_id, inbound_id, provider, channel, conversation_key,
                  visitor_id, sender_key, recipient_external_id,
                  reply_text, reply_payload, status
                ) VALUES (
                  %(inbox_id)s, %(inbound_id)s, %(provider)s, %(channel)s,
                  %(conversation_key)s, %(visitor_id)s, %(sender_key)s,
                  %(recipient_external_id)s, %(reply_text)s, %(reply_payload)s,
                  'pending'
                )
                RETURNING id
                """,
                {
                    "inbox_id": inbox_id,
                    "inbound_id": inbound_id,
                    "provider": provider,
                    "channel": (channel or "unknown").lower(),
                    "conversation_key": conversation_key,
                    "visitor_id": visitor_id,
                    "sender_key": sender_key,
                    "recipient_external_id": recipient_external_id,
                    "reply_text": reply_text or "",
                    "reply_payload": to_jsonb(reply_payload or {}),
                },
            )
            row = cur.fetchone()
    outbox_id = int(row["id"] if isinstance(row, dict) else row[0]) if row else None
    log_event(
        "outbox.enqueued",
        {"outbox_id": outbox_id, "provider": provider, "channel": channel},
    )
    return outbox_id


def claim_pending_outbox(
    *,
    limit: int = 10,
    lease_seconds: int = 60,
    owner: str | None = None,
) -> list[dict[str, Any]]:
    settings = get_settings()
    if not settings.database_url:
        return []
    ensure_tables()
    lease_owner = owner or f"outbox:{uuid4().hex[:12]}"
    expires = datetime.now(timezone.utc) + timedelta(seconds=max(15, lease_seconds))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH next_rows AS (
                  SELECT id
                  FROM public.ai_outbound_outbox
                  WHERE status IN ('pending', 'failed')
                    AND attempts < max_attempts
                  ORDER BY created_at ASC
                  FOR UPDATE SKIP LOCKED
                  LIMIT %(limit)s
                )
                UPDATE public.ai_outbound_outbox AS outbox
                SET status = 'leased',
                    lease_owner = %(owner)s,
                    lease_expires_at = %(expires)s,
                    attempts = outbox.attempts + 1,
                    updated_at = now()
                FROM next_rows
                WHERE outbox.id = next_rows.id
                RETURNING
                  outbox.id, outbox.inbox_id, outbox.inbound_id, outbox.provider,
                  outbox.channel, outbox.conversation_key, outbox.visitor_id,
                  outbox.sender_key, outbox.recipient_external_id,
                  outbox.reply_text, outbox.reply_payload, outbox.attempts
                """,
                {
                    "limit": max(1, min(int(limit), 25)),
                    "owner": lease_owner,
                    "expires": expires,
                },
            )
            rows = cur.fetchall() or []
    result: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            result.append(dict(row))
        else:
            result.append(
                {
                    "id": row[0],
                    "inbox_id": row[1],
                    "inbound_id": row[2],
                    "provider": row[3],
                    "channel": row[4],
                    "conversation_key": row[5],
                    "visitor_id": row[6],
                    "sender_key": row[7],
                    "recipient_external_id": row[8],
                    "reply_text": row[9],
                    "reply_payload": row[10],
                    "attempts": row[11],
                }
            )
    return result


def mark_outbox_sent(
    outbox_id: int,
    *,
    provider_response: dict[str, Any] | None = None,
) -> None:
    settings = get_settings()
    if not settings.database_url:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.ai_outbound_outbox
                SET status = 'sent',
                    sent_at = now(),
                    updated_at = now(),
                    provider_response = %(provider_response)s,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    last_error = NULL
                WHERE id = %(id)s
                """,
                {
                    "id": outbox_id,
                    "provider_response": to_jsonb(provider_response or {}),
                },
            )
    log_event("outbox.sent", {"outbox_id": outbox_id})


def mark_outbox_failed(outbox_id: int, *, error: str, dead: bool = False) -> None:
    settings = get_settings()
    if not settings.database_url:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.ai_outbound_outbox
                SET status = %(status)s,
                    last_error = %(error)s,
                    updated_at = now(),
                    lease_owner = NULL,
                    lease_expires_at = NULL
                WHERE id = %(id)s
                """,
                {
                    "id": outbox_id,
                    "status": "dead" if dead else "failed",
                    "error": (error or "")[:500],
                },
            )
    log_event("outbox.failed", {"outbox_id": outbox_id, "dead": dead})
