"""Inbox worker: process leased Meta/Brevo inbound rows through the agent."""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.db import (
    claim_inbound_message,
    has_successful_agent_response,
    insert_agent_response,
)
from app.inbound_coalesce import attach_recent_image_for_followup
from app.ingress.inbox import (
    claim_pending_inbox,
    mark_inbox_failed,
    mark_inbox_processed,
)
from app.ingress.outbox import enqueue_outbound
from app.ingress.reconstruct import incoming_from_inbox_payload
from app.models import AgentResult, IncomingMessage
from app.observability import log_event, log_exception


async def _customer_context_for(incoming: IncomingMessage) -> dict[str, Any]:
    if incoming.sender_phone:
        from app.repository import find_customer_profile_by_phone

        return find_customer_profile_by_phone(incoming.sender_phone)
    return {
        "found": False,
        "channel": incoming.channel,
        "sender_key": incoming.sender_key,
        "display_name": incoming.sender_name,
    }


async def _send_reply(incoming: IncomingMessage, result: AgentResult) -> dict[str, Any]:
    provider = (incoming.provider or "").lower()
    if provider == "meta" or (
        (incoming.channel or "").lower() == "instagram"
        and str(getattr(get_settings(), "instagram_ingress_provider", "meta")).lower()
        in {"meta", "dual"}
    ):
        from app.channels.meta_instagram import send_meta_instagram_reply

        return await send_meta_instagram_reply(incoming, result)

    from app.brevo_client import send_brevo_reply

    send_result = await send_brevo_reply(incoming, result)
    return {
        "ok": bool(send_result.ok),
        "status_code": send_result.status_code,
        "provider_response": send_result.model_dump(),
        "error": send_result.error,
    }


async def process_inbox_row(row: dict[str, Any]) -> dict[str, Any]:
    inbox_id = int(row["id"])
    incoming = incoming_from_inbox_payload(row.get("payload_json"))
    if incoming is None:
        mark_inbox_failed(inbox_id, error="invalid_inbox_payload", dead=True)
        return {"ok": False, "inbox_id": inbox_id, "error": "invalid_inbox_payload"}

    incoming = attach_recent_image_for_followup(incoming)

    try:
        from app.human_takeover import human_takeover_active

        # ChatBô takeover state is Brevo-only. Meta Instagram must not inherit
        # assigned_to from old Conversations threads.
        if (incoming.provider or "").lower() != "meta" and human_takeover_active(
            incoming
        ):
            mark_inbox_processed(inbox_id)
            log_event(
                "inbox.skipped_human_takeover",
                {"inbox_id": inbox_id, "channel": incoming.channel},
            )
            return {"ok": True, "inbox_id": inbox_id, "skipped": "human_takeover"}
    except Exception as exc:  # noqa: BLE001
        log_exception(
            "inbox.human_takeover_check_failed",
            exc,
            {"inbox_id": inbox_id},
        )

    try:
        claimed, inbound_id = claim_inbound_message(incoming.model_dump(mode="json"))
    except Exception as exc:
        log_exception("inbox.claim_inbound_failed", exc, {"inbox_id": inbox_id})
        mark_inbox_failed(inbox_id, error=type(exc).__name__)
        return {"ok": False, "inbox_id": inbox_id, "error": "claim_failed"}

    if not claimed:
        mark_inbox_processed(inbox_id, processed_inbound_id=inbound_id)
        return {"ok": True, "inbox_id": inbox_id, "skipped": "duplicate_message"}

    if isinstance(incoming.raw, dict):
        incoming.raw["inbound_id"] = inbound_id
        incoming.raw["inbox_id"] = inbox_id

    if has_successful_agent_response(inbound_id):
        mark_inbox_processed(inbox_id, processed_inbound_id=inbound_id)
        return {"ok": True, "inbox_id": inbox_id, "skipped": "already_sent"}

    from app.message_pipeline import process_incoming_message

    customer_context = await _customer_context_for(incoming)
    result = await process_incoming_message(incoming, customer_context)
    send_info = await _send_reply(incoming, result)
    send_ok = bool(send_info.get("ok"))

    try:
        insert_agent_response(
            {
                "inbound_id": inbound_id,
                "channel": incoming.channel,
                "sender_key": incoming.sender_key,
                "sender_phone": incoming.sender_phone,
                "reply_text": result.reply_text,
                "intent": result.intent,
                "handoff_required": result.handoff_required,
                "safety_reason": result.safety_reason,
                "provider_send_ok": send_ok,
                "provider_response": send_info,
            }
        )
    except Exception as exc:  # noqa: BLE001
        log_exception(
            "inbox.response_persist_failed",
            exc,
            {"inbox_id": inbox_id, "inbound_id": inbound_id},
        )

    if not send_ok:
        enqueue_outbound(
            provider=incoming.provider or "meta",
            channel=incoming.channel,
            reply_text=result.reply_text or "",
            inbox_id=inbox_id,
            inbound_id=inbound_id,
            conversation_key=incoming.conversation_id or incoming.sender_key,
            visitor_id=incoming.visitor_id,
            sender_key=incoming.sender_key,
            recipient_external_id=incoming.sender_external_id,
            reply_payload={"send_error": send_info},
        )
        mark_inbox_failed(
            inbox_id,
            error=str(send_info.get("error") or "send_failed"),
        )
        return {
            "ok": False,
            "inbox_id": inbox_id,
            "inbound_id": inbound_id,
            "error": "send_failed",
            "send": send_info,
        }

    mark_inbox_processed(inbox_id, processed_inbound_id=inbound_id)
    log_event(
        "inbox.agent_turn_completed",
        {
            "inbox_id": inbox_id,
            "inbound_id": inbound_id,
            "channel": incoming.channel,
            "provider": incoming.provider,
            "image_url_present": bool((incoming.image_url or "").strip()),
            "story_present": incoming.instagram_story is not None,
            "reply_chars": len(result.reply_text or ""),
        },
    )
    return {
        "ok": True,
        "inbox_id": inbox_id,
        "inbound_id": inbound_id,
        "send": send_info,
    }


async def process_inbox_batch(
    *,
    limit: int | None = None,
    lease_seconds: int | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    batch = int(limit or getattr(settings, "agent_inbox_batch_size", 5) or 5)
    lease = int(
        lease_seconds or getattr(settings, "agent_inbox_lease_seconds", 120) or 120
    )
    rows = claim_pending_inbox(limit=batch, lease_seconds=lease)
    processed = 0
    failed = 0
    results: list[dict[str, Any]] = []
    for row in rows:
        inbox_id = int(row["id"])
        try:
            item = await process_inbox_row(row)
            results.append(item)
            if item.get("ok"):
                processed += 1
            else:
                failed += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            attempts = int(row.get("attempts") or 1)
            log_exception(
                "inbox.worker_item_failed",
                exc,
                {"inbox_id": inbox_id},
            )
            mark_inbox_failed(
                inbox_id,
                error=type(exc).__name__,
                dead=attempts >= 8,
            )
            results.append(
                {
                    "ok": False,
                    "inbox_id": inbox_id,
                    "error": type(exc).__name__,
                }
            )
    return {
        "ok": True,
        "claimed": len(rows),
        "processed": processed,
        "failed": failed,
        "results": results,
    }
