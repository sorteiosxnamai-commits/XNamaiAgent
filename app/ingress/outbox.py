"""Outbound outbox (FASE 2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from app.models import IncomingMessage

from app.config import get_settings
from app.db import ensure_tables, get_conn, to_jsonb
from app.observability import log_event


def build_outbound_envelope(incoming: Any, result: Any) -> dict[str, Any]:
    incoming_payload: dict[str, Any] = {}
    if incoming is not None and hasattr(incoming, "model_dump"):
        incoming_payload = incoming.model_dump(mode="json", exclude={"raw", "instagram_story"})
    elif isinstance(incoming, dict):
        incoming_payload = dict(incoming)
    result_payload = result.model_dump(mode="json", exclude={"reply_audio_bytes"}) if hasattr(result, "model_dump") else {
            "reply_text": getattr(result, "reply_text", None),
            "intent": getattr(result, "intent", None),
            "safety_reason": getattr(result, "safety_reason", None),
    }
    # Metadata is excluded from the public model, but media must survive retries.
    result_payload["response_metadata"] = dict(getattr(result, "response_metadata", {}) or {})
    return {
        "incoming": incoming_payload,
        "result": result_payload,
        "provider": incoming_payload.get("provider"),
        "channel": incoming_payload.get("channel"),
    }


def enqueue_accepted_outbound(
    *,
    incoming: Any,
    result: Any,
    inbox_id: int | None = None,
    inbound_id: int | None = None,
) -> int | None:
    return enqueue_outbound(
        provider=str(getattr(incoming, "provider", None) or "brevo"),
        channel=str(getattr(incoming, "channel", None) or "unknown"),
        reply_text=str(getattr(result, "reply_text", None) or ""),
        inbox_id=inbox_id,
        inbound_id=inbound_id,
        conversation_key=(
            getattr(incoming, "conversation_id", None)
            or getattr(incoming, "sender_key", None)
        ),
        visitor_id=getattr(incoming, "visitor_id", None),
        sender_key=getattr(incoming, "sender_key", None),
        recipient_external_id=getattr(incoming, "sender_external_id", None),
        reply_payload=build_outbound_envelope(incoming, result),
    )


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
            if inbound_id is not None:
                # Serialize create/reuse even when no outbox row exists yet.
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (-int(inbound_id),))
                cur.execute(
                    """
                    SELECT id, status
                    FROM public.ai_outbound_outbox
                    WHERE inbound_id = %(inbound_id)s
                    ORDER BY
                      CASE status
                        WHEN 'sent' THEN 0
                        WHEN 'leased' THEN 1
                        WHEN 'pending' THEN 2
                        WHEN 'failed' THEN 3
                        ELSE 4
                      END,
                      id DESC
                    LIMIT 1
                    """,
                    {"inbound_id": inbound_id},
                )
                existing = cur.fetchone()
                if existing:
                    existing_id = int(
                        existing["id"] if isinstance(existing, dict) else existing[0]
                    )
                    existing_status = str(
                        existing["status"] if isinstance(existing, dict) else existing[1]
                    )
                    if existing_status in {"sent", "leased", "pending", "failed", "dead", "skipped"}:
                        return existing_id
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
    lease_seconds: int = 180,
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
            # Do not replay stale replies after the conversation moves on.
            cur.execute(
                """
                UPDATE public.ai_outbound_outbox AS outbox
                SET status = 'dead',
                    last_error = CASE
                      WHEN EXISTS (
                        SELECT 1
                        FROM public.ai_inbound_messages AS later
                        WHERE later.created_at > outbox.created_at
                           AND later.provider = outbox.provider AND later.channel = outbox.channel
                          AND (
                            (outbox.conversation_key IS NULL AND outbox.sender_key IS NOT NULL
                             AND later.sender_key = outbox.sender_key)
                            OR
                            (outbox.conversation_key IS NOT NULL
                             AND later.conversation_id = outbox.conversation_key)
                          )
                      ) THEN 'superseded_by_later_inbound'
                      WHEN outbox.attempts >= outbox.max_attempts THEN 'attempts_exhausted'
                      ELSE 'outbox_retry_window_expired'
                    END,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    updated_at = now()
                WHERE (
                    outbox.status IN ('pending', 'failed')
                    OR (
                      outbox.status = 'leased'
                      AND outbox.lease_expires_at IS NOT NULL
                      AND outbox.lease_expires_at < now()
                    )
                  )
                  AND (
                    outbox.attempts >= outbox.max_attempts
                    OR outbox.created_at < now() - interval '15 minutes'
                    OR EXISTS (
                      SELECT 1
                      FROM public.ai_inbound_messages AS later
                      WHERE later.created_at > outbox.created_at
                           AND later.provider = outbox.provider AND later.channel = outbox.channel
                        AND (
                          (outbox.conversation_key IS NULL AND outbox.sender_key IS NOT NULL
                           AND later.sender_key = outbox.sender_key)
                          OR
                          (outbox.conversation_key IS NOT NULL
                           AND later.conversation_id = outbox.conversation_key)
                        )
                    )
                  )
                """
            )
            cur.execute(
                """
                WITH next_rows AS (
                  SELECT id
                  FROM public.ai_outbound_outbox
                  WHERE attempts < max_attempts
                    AND (status <> 'failed' OR updated_at + make_interval(secs =>
                        LEAST(%(retry_max)s, %(retry_base)s * power(2, LEAST(GREATEST(attempts - 1, 0), 10)))::int) <= now())
                    AND (
                      status IN ('pending', 'failed')
                      OR (
                        status = 'leased'
                        AND lease_expires_at IS NOT NULL
                        AND lease_expires_at < now()
                      )
                    )
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
                  outbox.reply_text, outbox.reply_payload, outbox.attempts, outbox.lease_owner, outbox.max_attempts
                """,
                {
                    "limit": max(1, min(int(limit), 25)),
                    "owner": lease_owner,
                    "expires": expires,
                    "retry_base": getattr(settings, "agent_queue_retry_base_seconds", 30),
                    "retry_max": getattr(settings, "agent_queue_retry_max_seconds", 300),
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
                    "lease_owner": row[12],
                    "max_attempts": row[13],
                }
            )
    return result


def incoming_from_outbox_row(row: dict[str, Any]) -> IncomingMessage:
    payload = row.get("reply_payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    incoming_data = payload.get("incoming")
    if isinstance(incoming_data, dict) and incoming_data:
        try:
            return IncomingMessage.model_validate(incoming_data)
        except Exception as exc:
            from app.observability import log_exception

            log_exception("outbox.incoming_payload_invalid", exc)
    return IncomingMessage(
        text="",
        channel=str(row.get("channel") or "whatsapp"),
        provider=str(row.get("provider") or "brevo"),
        conversation_id=row.get("conversation_key"),
        sender_key=row.get("sender_key"),
        visitor_id=row.get("visitor_id"),
        sender_external_id=row.get("recipient_external_id"),
        sender_phone=(
            incoming_data.get("sender_phone")
            if isinstance(incoming_data, dict)
            else None
        ),
        raw=incoming_data.get("raw") if isinstance(incoming_data, dict) else {},
        image_url=incoming_data.get("image_url") if isinstance(incoming_data, dict) else None,
        audio_url=incoming_data.get("audio_url") if isinstance(incoming_data, dict) else None,
        instagram_story=(
            incoming_data.get("instagram_story")
            if isinstance(incoming_data, dict)
            else None
        ),
    )


def mark_outbox_sent(
    outbox_id: int,
    *,
    provider_response: dict[str, Any] | None = None,
    owner: str | None = None,
) -> bool:
    settings = get_settings()
    if not settings.database_url:
        return False
    owner_guard = ""
    params: dict[str, Any] = {
        "id": outbox_id,
        "provider_response": to_jsonb(provider_response or {}),
    }
    if owner:
        owner_guard = "AND status = 'leased' AND lease_owner = %(owner)s AND lease_expires_at > now()"
        params["owner"] = owner
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE public.ai_outbound_outbox
                SET status = 'sent',
                    sent_at = now(),
                    updated_at = now(),
                    provider_response = %(provider_response)s,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    last_error = NULL
                WHERE id = %(id)s AND status NOT IN ('dead', 'skipped')
                  {owner_guard}
                """,
                params,
            )
            changed = cur.rowcount == 1
    if not changed:
        log_event("outbox.receipt_rejected", {"outbox_id": outbox_id})
        return False
    log_event("outbox.sent", {"outbox_id": outbox_id})
    return True


def mark_outbox_failed(outbox_id: int, *, error: str, dead: bool = False, owner: str | None = None) -> None:
    settings = get_settings()
    if not settings.database_url:
        return
    owner_guard = ""
    params: dict[str, Any] = {
        "id": outbox_id,
        "status": "dead" if dead else "failed",
        "error": (error or "")[:500],
    }
    if owner:
        owner_guard = "AND status = 'leased' AND lease_owner = %(owner)s AND lease_expires_at > now()"
        params["owner"] = owner
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE public.ai_outbound_outbox
                SET status = %(status)s,
                    last_error = %(error)s,
                    updated_at = now(),
                    lease_owner = NULL,
                    lease_expires_at = NULL
                WHERE id = %(id)s AND status NOT IN ('sent', 'dead', 'skipped')
                  {owner_guard}
                """,
                params,
            )
    log_event("outbox.failed", {"outbox_id": outbox_id, "dead": dead})


def get_outbox_status(outbox_id: int) -> str | None:
    if not get_settings().database_url:
        return None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM public.ai_outbound_outbox WHERE id = %s", (outbox_id,))
            row = cur.fetchone()
    return str(row["status"] if isinstance(row, dict) else row[0]) if row else None


def has_sent_outbound(inbound_id: int | None) -> bool:
    if inbound_id is None or not get_settings().database_url:
        return False
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM public.ai_outbound_outbox WHERE inbound_id = %s AND status = 'sent' LIMIT 1", (inbound_id,))
            return cur.fetchone() is not None


def get_accepted_outbound(inbound_id: int | None) -> dict[str, Any] | None:
    """Recover a previously accepted turn before any model/tool regeneration."""
    if inbound_id is None or not get_settings().database_url:
        return None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, status, reply_text, reply_payload
                   FROM public.ai_outbound_outbox WHERE inbound_id = %s
                   ORDER BY CASE status WHEN 'sent' THEN 0 WHEN 'leased' THEN 1
                     WHEN 'pending' THEN 2 WHEN 'failed' THEN 3 ELSE 4 END, id DESC
                   LIMIT 1""", (inbound_id,),
            )
            row = cur.fetchone()
    if not row:
        return None
    return dict(row) if isinstance(row, dict) else dict(zip(
        ("id", "status", "reply_text", "reply_payload"), row))


def claim_outbox_for_send(outbox_id: int, *, lease_seconds: int = 180) -> dict[str, Any] | None:
    """Claim and return the immutable accepted envelope, never a regenerated reply."""
    if not get_settings().database_url:
        return None
    owner = f"inline:{uuid4().hex}"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE public.ai_outbound_outbox
                   SET status = 'leased', lease_owner = %s,
                       lease_expires_at = now() + (%s * interval '1 second'),
                       attempts = attempts + 1, updated_at = now()
                   WHERE id = %s AND status = 'pending'
                     AND attempts < max_attempts
                   RETURNING id, inbound_id, provider, channel, reply_text, reply_payload, lease_owner, attempts, max_attempts, inbox_id""",
                (owner, max(15, lease_seconds), outbox_id),
            )
            row = cur.fetchone()
    if not row:
        return None
    return dict(row) if isinstance(row, dict) else dict(zip(
        ("id", "inbound_id", "provider", "channel", "reply_text", "reply_payload", "lease_owner", "attempts", "max_attempts", "inbox_id"), row
    ))


def result_from_outbox_row(row: dict[str, Any]):
    from app.models import AgentResult

    payload = row.get("reply_payload") or {}
    data = dict(payload.get("result") or {}) if isinstance(payload, dict) else {}
    data["reply_text"] = str(row.get("reply_text") or data.get("reply_text") or "")
    data["intent"] = data.get("intent") or "commerce"
    return AgentResult.model_validate(data)


def outbox_lease_current(row: dict[str, Any]) -> bool:
    """A claimed batch can expire while an earlier provider request is slow."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT 1 FROM public.ai_outbound_outbox
                WHERE id = %s AND status = 'leased' AND lease_owner = %s
                  AND lease_expires_at > now()""", (row["id"], row.get("lease_owner")))
            return cur.fetchone() is not None


def has_later_queued_inbound(row: dict[str, Any]) -> bool:
    """Include accepted messages still waiting for agent processing."""
    if not row.get("inbox_id") or not get_settings().database_url:
        return False
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT 1 FROM public.ai_inbound_inbox AS original
                JOIN public.ai_inbound_inbox AS later
                  ON later.id > original.id
                 AND later.provider = original.provider AND later.channel = original.channel
                 AND ((original.conversation_key IS NOT NULL AND later.conversation_key = original.conversation_key)
                   OR (original.conversation_key IS NULL AND original.sender_key IS NOT NULL
                       AND later.sender_key = original.sender_key))
                WHERE original.id = %s AND later.status NOT IN ('dead', 'skipped')
                LIMIT 1""", (row["inbox_id"],))
            return cur.fetchone() is not None


async def dispatch_accepted_outbound(outbox_id: int, send) -> dict[str, Any]:
    """Both immediate dispatch and retries acquire ownership before sending."""
    row = claim_outbox_for_send(outbox_id)
    if row is None:
        status = get_outbox_status(outbox_id)
        return {"ok": status == "sent", "queued": status in {"pending", "failed", "leased"},
                "skipped": True, "status": status}
    try:
        from .outbox_worker import _resend_outbox_row
        info = await _resend_outbox_row(row, send=send, verify_lease=False)
    except Exception as exc:
        info = {"ok": False, "error": type(exc).__name__}
    if info.get("ok"):
        persisted = mark_outbox_sent(outbox_id, provider_response=info, owner=row["lease_owner"])
        info = {**info, "receipt_persisted": bool(persisted)}
    else:
        mark_outbox_failed(outbox_id, error=str(info.get("error") or "send_failed"), owner=row["lease_owner"],
            dead=bool(info.get("permanent")) or int(row.get("attempts") or 1) >= int(row.get("max_attempts") or 5))
    return info
