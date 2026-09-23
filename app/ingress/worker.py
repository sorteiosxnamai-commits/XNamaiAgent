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
    renew_inbox_lease,
)
from app.ingress.outbox import (
    enqueue_accepted_outbound, get_accepted_outbound, result_from_outbox_row,
    dispatch_accepted_outbound,
)
from app.ingress.reconstruct import incoming_from_inbox_payload
from app.models import AgentResult, IncomingMessage
from app.observability import log_event, log_exception


async def _customer_context_for(incoming: IncomingMessage) -> dict[str, Any]:
    # Contrato de retorno preservado do baseline 201bd16 (Task L). A funcao
    # chamada e um stub fail-closed: nao abre conexao, nao consulta fonte
    # externa e devolve sempre {"found": False}. Remover a chamada mudava o
    # FORMATO do contexto sem nenhum ganho de neutralizacao.
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
    if provider == "ycloud":
        from app.channels.ycloud_whatsapp import send_ycloud_reply

        return await send_ycloud_reply(incoming, result)

    if provider == "meta" or (not provider and
        (incoming.channel or "").lower() == "instagram"
        and str(getattr(get_settings(), "instagram_ingress_provider", "meta")).lower()
        in {"meta", "dual"}
    ):
        from app.channels.meta_instagram import send_meta_instagram_reply

        return await send_meta_instagram_reply(incoming, result)

    from app.brevo_client import send_brevo_reply

    send_result = await send_brevo_reply(incoming, result)
    info = {
        "ok": bool(send_result.ok),
        "status_code": send_result.status_code,
        "provider_response": send_result.model_dump(),
        "error": send_result.error,
    }
    if not send_result.ok and send_result.status_code:
        from app.http_resilience import classify_send_status

        outcome = classify_send_status(send_result.status_code)
        info["permanent"] = outcome == "permanent"
        info["delivery_unknown"] = outcome == "unknown"
    return info


async def process_inbox_row(row: dict[str, Any]) -> dict[str, Any]:
    """Keep concurrent workers from generating two turns for one conversation."""
    from app.conversation_lock import (
        acquire_conversation_lock, release_conversation_lock, conversation_lock_key,
    )
    incoming = incoming_from_inbox_payload(row.get("payload_json"))
    if incoming is None:
        return await _process_inbox_row_locked(row)
    key = conversation_lock_key(conversation_id=incoming.conversation_id or row.get("conversation_key"),
                                sender_key=incoming.sender_key, sender_phone=incoming.sender_phone,
                                visitor_id=incoming.visitor_id)
    if not key:
        mark_inbox_failed(int(row["id"]), error="missing_conversation_identity", dead=True,
                          owner=row.get("lease_owner"))
        return {"ok": False, "error": "missing_conversation_identity"}
    handle = await acquire_conversation_lock(key, database_url=get_settings().database_url or "")
    try:
        if row.get("lease_owner") and not renew_inbox_lease(
            row, lease_seconds=int(getattr(get_settings(), "agent_inbox_lease_seconds", 120)),
        ):
            return {"ok": False, "inbox_id": row["id"], "error": "lease_lost"}
        return await _process_inbox_row_locked(row)
    finally:
        await release_conversation_lock(handle)


async def _process_inbox_row_locked(row: dict[str, Any]) -> dict[str, Any]:
    inbox_id = int(row["id"])
    owner = row.get("lease_owner")
    incoming = incoming_from_inbox_payload(row.get("payload_json"))
    if incoming is None:
        mark_inbox_failed(inbox_id, error="invalid_inbox_payload", dead=True, owner=owner)
        return {"ok": False, "inbox_id": inbox_id, "error": "invalid_inbox_payload"}

    incoming = attach_recent_image_for_followup(incoming)

    try:
        from app.human_takeover import human_takeover_active

        # ChatBô takeover state is Brevo-only. Meta Instagram must not inherit
        # assigned_to from old Conversations threads.
        if (incoming.provider or "").lower() != "meta" and human_takeover_active(
            incoming
        ):
            mark_inbox_processed(inbox_id, owner=owner)
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
        mark_inbox_failed(inbox_id, error=type(exc).__name__, owner=owner)
        return {"ok": False, "inbox_id": inbox_id, "error": "claim_failed"}

    if not claimed and not inbound_id:
        mark_inbox_failed(inbox_id, error="duplicate_without_inbound_id", owner=owner)
        return {"ok": False, "inbox_id": inbox_id, "error": "duplicate_without_inbound_id"}

    if isinstance(incoming.raw, dict):
        incoming.raw["inbound_id"] = inbound_id
        incoming.raw["inbox_id"] = inbox_id

    if has_successful_agent_response(inbound_id):
        mark_inbox_processed(inbox_id, processed_inbound_id=inbound_id, owner=owner)
        return {"ok": True, "inbox_id": inbox_id, "skipped": "already_sent"}

    from app.message_pipeline import process_incoming_message

    accepted = get_accepted_outbound(inbound_id)
    if accepted is not None:
        result = result_from_outbox_row(accepted)
        outbox_id = int(accepted["id"])
    else:
        from app.llm_call_policy import build_llm_call_budget
        from app.runtime_context import set_current_turn, reset_current_turn
        from app.turn_runtime import TurnRuntimeContext, LLMCallBudget

        budget = build_llm_call_budget(execution_path="normal")
        runtime = TurnRuntimeContext(trace_id=f"inbox-{inbox_id}", inbound_id=inbound_id,
                                    llm_budget=LLMCallBudget(max_calls=budget.get("max_calls", 2),
                                                            max_transport_attempts=budget.get("max_transport_attempts", 8),
                                                            enforce=budget.get("enforce", True)))
        token = set_current_turn(runtime)
        try:
            customer_context = await _customer_context_for(incoming)
            result = await process_incoming_message(incoming, customer_context)
            from app.observability import redact_value
            result.response_metadata["turn_trace"] = redact_value(runtime.safe_summary())
        finally:
            reset_current_turn(token)
        # Persist the accepted response BEFORE talking to the channel provider.
        outbox_id = enqueue_accepted_outbound(incoming=incoming, result=result,
                                             inbox_id=inbox_id, inbound_id=inbound_id)
        if outbox_id is None and get_settings().database_url:
            raise RuntimeError("outbox_acceptance_failed")
    send_info = (await dispatch_accepted_outbound(outbox_id, _send_reply)
                 if outbox_id is not None else await _send_reply(incoming, result))
    if send_info.get("skipped"):
        mark_inbox_processed(inbox_id, processed_inbound_id=inbound_id, owner=owner)
        return {"ok": True, "inbox_id": inbox_id, "queued": bool(send_info.get("queued")),
                "skipped": send_info.get("status")}
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
                "response_metadata": result.response_metadata,
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
        # From here the outbox owns delivery; the agent must not run again.
        mark_inbox_processed(inbox_id, processed_inbound_id=inbound_id, owner=owner)
        return {
            "ok": False,
            "inbox_id": inbox_id,
            "inbound_id": inbound_id,
            "error": "send_failed",
            "send": send_info,
        }

    mark_inbox_processed(inbox_id, processed_inbound_id=inbound_id, owner=owner)
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
                dead=attempts >= int(row.get("max_attempts") or 8),
                owner=row.get("lease_owner"),
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
