"""Drain outbound outbox retries (send failures from inbox worker)."""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.ingress.outbox import (
    claim_pending_outbox,
    mark_outbox_failed,
    mark_outbox_sent,
    outbox_lease_current,
    has_later_queued_inbound,
)
from app.ingress.delivery_policy import (
    OutboxRetryPolicy,
    classify_send_exception,
    delivery_unknown_info,
    outbox_correlation_id,
    resolve_failure,
)
from app.observability import log_event, log_exception
from app.runtime_context import reset_current_turn


async def _resend_outbox_row(row: dict[str, Any], *, send=None, verify_lease: bool = True) -> dict[str, Any]:
    provider = str(row.get("provider") or "").lower()
    channel = str(row.get("channel") or "").lower()
    reply_text = str(row.get("reply_text") or "")
    if not reply_text.strip():
        return {"ok": False, "error": "empty_reply", "permanent": True}

    from app.ingress.outbox import incoming_from_outbox_row
    from app.ingress.worker import _send_reply

    incoming = incoming_from_outbox_row(row)
    if not incoming.provider:
        incoming.provider = provider or "brevo"
    if not incoming.channel:
        incoming.channel = channel or "whatsapp"
    from app.ingress.outbox import result_from_outbox_row
    from app.human_takeover import human_takeover_active
    from app.db import is_latest_inbound_message, has_successful_agent_response
    if verify_lease and not outbox_lease_current(row):
        return {"ok": False, "error": "lease_lost"}
    if has_successful_agent_response(row.get("inbound_id")):
        return {"ok": True, "recovered_receipt": True}
    if has_later_queued_inbound(row):
        return {"ok": False, "error": "superseded_by_later_inbox", "permanent": True}
    if not is_latest_inbound_message(row.get("inbound_id"), incoming.conversation_id,
                                     incoming.sender_key, incoming.sender_phone):
        return {"ok": False, "error": "superseded_by_later_inbound", "permanent": True}
    # ChatBô ownership applies to Conversations, not legacy Meta threads.
    if incoming.provider != "meta" and human_takeover_active(incoming):
        return {"ok": False, "error": "human_takeover", "permanent": True}
    result = result_from_outbox_row(row)
    correlation_id = outbox_correlation_id(row.get("id"))
    if correlation_id:
        # Echoed by providers that support it (YCloud externalId) so an
        # ambiguous send can be reconciled from the status webhook.
        result.response_metadata["outbox_correlation_id"] = correlation_id
    try:
        return await (send or _send_reply)(incoming, result)
    except Exception as exc:  # noqa: BLE001 - classified, never re-raised blind
        outcome = classify_send_exception(exc)
        log_event(
            "outbox.send_exception",
            {
                "outbox_id": row.get("id"),
                "provider": provider,
                "error_type": type(exc).__name__,
                "outcome": outcome,
                "attempt": row.get("attempts"),
            },
        )
        if outcome == "unknown":
            return delivery_unknown_info(type(exc).__name__, provider=provider)
        return {"ok": False, "provider": provider, "error": type(exc).__name__}


def _enter_delivery_context(row: dict[str, Any], attempt: int):
    """Correlate every log of this delivery: trace, inbound, outbox, attempt."""
    from app.runtime_context import set_current_turn
    from app.turn_runtime import TurnRuntimeContext

    outbox_id = int(row["id"])
    runtime = TurnRuntimeContext(
        trace_id=f"outbox-{outbox_id}-a{attempt}",
        inbound_id=row.get("inbound_id"),
        channel=str(row.get("channel") or "unknown"),
        outbox_id=outbox_id,
        delivery_attempt=attempt,
    )
    return set_current_turn(runtime)


async def process_outbox_batch(*, limit: int | None = None) -> dict[str, Any]:
    settings = get_settings()
    batch = int(limit or getattr(settings, "agent_inbox_batch_size", 5) or 5)
    policy = OutboxRetryPolicy.from_settings(settings)
    rows = claim_pending_outbox(limit=batch)
    if not rows:
        return {"ok": True, "claimed": 0, "sent": 0, "failed": 0, "dead": 0}

    sent = failed = dead = receipt_pending = 0
    details: list[dict[str, Any]] = []
    for row in rows:
        outbox_id = int(row["id"])
        attempts = int(row.get("attempts") or 1)
        token = _enter_delivery_context(row, attempts)
        try:
            try:
                send_info = await _resend_outbox_row(row)
            except Exception as exc:  # noqa: BLE001
                log_exception("outbox.resend_failed", exc, {"outbox_id": outbox_id})
                send_info = {"ok": False, "error": type(exc).__name__}

            if send_info.get("ok"):
                persisted = mark_outbox_sent(
                    outbox_id,
                    owner=row.get("lease_owner"),
                    provider_response=send_info.get("provider_response")
                    if isinstance(send_info.get("provider_response"), dict)
                    else send_info,
                )
                try:
                    from app.db import insert_agent_response, has_successful_agent_response
                    from app.ingress.outbox import incoming_from_outbox_row, result_from_outbox_row
                    inbound_id = row.get("inbound_id")
                    if inbound_id and not has_successful_agent_response(inbound_id):
                        incoming = incoming_from_outbox_row(row)
                        result = result_from_outbox_row(row)
                        insert_agent_response({
                            "inbound_id": inbound_id, "channel": incoming.channel,
                            "sender_key": incoming.sender_key, "sender_phone": incoming.sender_phone,
                            "reply_text": result.reply_text, "intent": result.intent,
                            "handoff_required": result.handoff_required, "safety_reason": result.safety_reason,
                            "response_metadata": result.response_metadata,
                            "provider_send_ok": True, "provider_response": send_info,
                        })
                except Exception as exc:
                    log_exception("outbox.response_persist_failed", exc, {"outbox_id": outbox_id})
                if persisted:
                    sent += 1
                    details.append({"id": outbox_id, "status": "sent"})
                else:
                    receipt_pending += 1
                    details.append({"id": outbox_id, "status": "receipt_pending"})
                continue

            max_attempts = int(row.get("max_attempts") or 5)
            is_dead, error = resolve_failure(
                send_info,
                attempts=attempts,
                max_attempts=max_attempts,
                policy=policy,
            )
            mark_outbox_failed(
                outbox_id,
                error=error,
                dead=is_dead,
                owner=row.get("lease_owner"),
            )
            if is_dead:
                dead += 1
                details.append({"id": outbox_id, "status": "dead"})
            else:
                failed += 1
                details.append({"id": outbox_id, "status": "failed"})
        finally:
            reset_current_turn(token)

    summary = {
        "ok": True,
        "claimed": len(rows),
        "sent": sent,
        "failed": failed,
        "dead": dead,
        "receipt_pending": receipt_pending,
        "items": details,
    }
    log_event("outbox.batch_processed", summary)
    return summary
