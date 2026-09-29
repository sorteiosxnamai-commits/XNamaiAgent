"""Durable mirror from the XNamai operational DB into the ChatBô inbox."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import get_settings
from app.db import ensure_tables, get_conn, to_jsonb
from app.observability import log_event, log_exception


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return _clean(value) or None


def _configured() -> tuple[bool, str, str, str]:
    settings = get_settings()
    enabled = bool(getattr(settings, "chatbo_sync_enabled", True))
    base_url = _clean(getattr(settings, "chatbo_api_url", "")).rstrip("/")
    token = _clean(getattr(settings, "chatbo_internal_token", ""))
    workspace_id = _clean(getattr(settings, "chatbo_workspace_id", ""))
    return enabled and bool(base_url and token and workspace_id), base_url, token, workspace_id


def _load_turn(inbound_id: int, response_id: int | None = None) -> dict[str, Any] | None:
    ensure_tables()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT inbound.*,
                       response.id AS response_id,
                       response.reply_text,
                       response.intent,
                       response.handoff_required,
                       response.safety_reason,
                       response.provider_send_ok,
                       response.provider_response,
                       response.created_at AS response_created_at
                FROM public.ai_inbound_messages AS inbound
                LEFT JOIN LATERAL (
                  SELECT response.*
                  FROM public.ai_agent_responses AS response
                  WHERE response.inbound_id = inbound.id
                    AND (%(response_id)s::bigint IS NULL OR response.id = %(response_id)s)
                  ORDER BY response.id DESC
                  LIMIT 1
                ) AS response ON true
                WHERE inbound.id = %(inbound_id)s
                LIMIT 1
                """,
                {"inbound_id": inbound_id, "response_id": response_id},
            )
            row = cur.fetchone()
    return dict(row) if row else None


def _payload(row: dict[str, Any], workspace_id: str) -> dict[str, Any]:
    metadata = dict(row.get("channel_metadata") or {})
    inbound = {
        "provider": _clean(row.get("provider")) or "ycloud",
        "messageId": _clean(row.get("message_id")),
        "eventType": row.get("event_type"),
        "conversationId": row.get("conversation_id"),
        "channel": _clean(row.get("channel")) or "whatsapp",
        "senderKey": row.get("sender_key"),
        "senderExternalId": row.get("sender_external_id"),
        "visitorId": row.get("visitor_id"),
        "senderUsername": row.get("sender_username"),
        "sourceChannelRef": row.get("source_channel_ref"),
        "sourceChannelLink": row.get("source_channel_link"),
        "sourceConversationRef": row.get("source_conversation_ref"),
        "senderPhone": row.get("sender_phone"),
        "senderName": row.get("sender_name"),
        "text": row.get("text") or "",
        "channelMetadata": {
            **metadata,
            "chatbo_workspace_id": workspace_id,
        },
        "createdAt": _iso(row.get("created_at")),
    }
    result: dict[str, Any] = {"inbound": inbound}
    if row.get("response_id") is not None and _clean(row.get("reply_text")):
        result["response"] = {
            "replyText": row.get("reply_text"),
            "intent": row.get("intent"),
            "handoffRequired": bool(row.get("handoff_required")),
            "safetyReason": row.get("safety_reason"),
            "providerSendOk": bool(row.get("provider_send_ok")),
            "providerResponse": dict(row.get("provider_response") or {}),
            "createdAt": _iso(row.get("response_created_at")),
        }
    return result


def _mark_synced(inbound_id: int, response_id: int | None) -> None:
    marker = {
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "response_id": response_id,
        "error": None,
    }
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE public.ai_inbound_messages
                   SET raw=COALESCE(raw, '{}'::jsonb)
                           || jsonb_build_object('_chatbo_sync', %s::jsonb)
                   WHERE id=%s""",
                (to_jsonb(marker), inbound_id),
            )


def _mark_failed(inbound_id: int, response_id: int | None, error: str) -> None:
    marker = {
        "synced_at": None,
        "response_id": response_id,
        "error": (error or "chatbo_sync_failed")[:500],
    }
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE public.ai_inbound_messages
                   SET raw=COALESCE(raw, '{}'::jsonb)
                           || jsonb_build_object('_chatbo_sync', %s::jsonb)
                   WHERE id=%s""",
                (to_jsonb(marker), inbound_id),
            )


async def sync_chatbo_turn(*, inbound_id: int, response_id: int | None = None) -> dict[str, Any]:
    configured, base_url, token, workspace_id = _configured()
    if not configured:
        return {"ok": False, "skipped": "chatbo_sync_not_configured"}
    row = _load_turn(inbound_id, response_id)
    if not row or not _clean(row.get("message_id")):
        return {"ok": False, "skipped": "chatbo_sync_source_missing"}

    url = f"{base_url}/internal/workspaces/{workspace_id}/conversation-turns"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                url,
                json=_payload(row, workspace_id),
                headers={"X-NITRUS-Internal-Token": token},
            )
        response.raise_for_status()
        _mark_synced(inbound_id, row.get("response_id"))
        log_event(
            "chatbo.sync.completed",
            {"inbound_id": inbound_id, "response_present": row.get("response_id") is not None},
        )
        return {"ok": True, "inbound_id": inbound_id}
    except Exception as exc:  # noqa: BLE001 - provider delivery must remain independent
        try:
            _mark_failed(inbound_id, row.get("response_id"), type(exc).__name__)
        except Exception as mark_exc:  # noqa: BLE001
            log_exception("chatbo.sync.failure_mark_failed", mark_exc, {"inbound_id": inbound_id})
        log_exception("chatbo.sync.failed", exc, {"inbound_id": inbound_id})
        return {"ok": False, "inbound_id": inbound_id, "error": type(exc).__name__}


def _pending_turns(limit: int) -> list[tuple[int, int | None]]:
    ensure_tables()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT inbound.id AS inbound_id, response.id AS response_id
                FROM public.ai_inbound_messages AS inbound
                LEFT JOIN LATERAL (
                  SELECT r.id
                  FROM public.ai_agent_responses r
                  WHERE r.inbound_id=inbound.id
                  ORDER BY r.id DESC LIMIT 1
                ) response ON true
                WHERE inbound.provider='ycloud'
                  AND (
                    inbound.raw #>> '{_chatbo_sync,synced_at}' IS NULL
                    OR COALESCE(inbound.raw #>> '{_chatbo_sync,response_id}', '')
                       <> COALESCE(response.id::text, '')
                  )
                ORDER BY inbound.id ASC
                LIMIT %s
                """,
                (max(1, min(int(limit), 1000)),),
            )
            rows = cur.fetchall() or []
    return [(int(row["inbound_id"]), row.get("response_id")) for row in rows]


async def sync_pending_chatbo_turns(*, limit: int = 100) -> dict[str, Any]:
    configured, _, _, _ = _configured()
    if not configured:
        return {"ok": True, "configured": False, "processed": 0, "failed": 0}
    pending = _pending_turns(limit)
    processed = failed = 0
    for inbound_id, response_id in pending:
        result = await sync_chatbo_turn(inbound_id=inbound_id, response_id=response_id)
        if result.get("ok"):
            processed += 1
        else:
            failed += 1
    return {
        "ok": failed == 0,
        "configured": True,
        "pending": len(pending),
        "processed": processed,
        "failed": failed,
    }
