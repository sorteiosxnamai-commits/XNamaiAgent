from __future__ import annotations

import json
import hashlib
from json import JSONDecodeError
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.security import verify_brevo_webhook, verify_admin_token, verify_remarketing_cron
from app.persona_admin_api import router as persona_admin_router
from app.pix_webhook_api import router as pix_payments_router
from app.webhook_parser import (
    inbound_skip_reason,
    parse_brevo_conversations_payload,
    select_effective_inbound_message,
    selected_message_info,
    webhook_event_skip_reason,
)
from app.repository import find_customer_profile_by_phone
from app.message_pipeline import process_incoming_message
from app.brevo_client import send_brevo_reply
from app.models import AgentResult
from app.db import (
    claim_inbound_message,
    has_successful_agent_response,
    inbound_message_exists,
    insert_agent_response,
    insert_inbound_message,
    is_latest_inbound_message,
)
from app.inbound_coalesce import is_caption_echo_of_recent_image
from app.config import get_allowed_channels, get_settings
from app.rollout import build_rollout_status
from app.conversation_lock import (
    ConversationLockUnavailable,
    acquire_conversation_lock,
    conversation_lock_key,
    release_conversation_lock,
)
from app.handoff_service import handoff_provider_payload
from app.remarketing import run_remarketing_batch, sync_remarketing_interaction
from app.product_image_index import run_product_image_index_batch
from app.runtime_context import (
    get_current_turn,
    reset_current_turn,
    runtime_stage,
    set_current_turn,
)
from app.tray_adapter_client import TrayAdapterClient, TrayAdapterError
from app.turn_runtime import LLMCallBudget, TurnRuntimeContext
from app.observability import (
    log_event,
    log_exception,
    redact_text,
    summarize_webhook_payload,
)

app = FastAPI(title="NewStoreAgent Webhook", version="1.0.0")
app.include_router(persona_admin_router)
app.include_router(pix_payments_router)


def _request_trace_id(request: Request) -> str:
    supplied = (request.headers.get("x-request-id") or "").strip()
    if supplied and len(supplied) <= 64 and all(
        char.isalnum() or char in {"-", "_"} for char in supplied
    ):
        return supplied
    return uuid4().hex


@app.middleware("http")
async def turn_runtime_middleware(request: Request, call_next):
    settings = get_settings()
    path = request.url.path
    monitored_path = path.startswith(("/api/webhooks/", "/api/test/agent"))
    http_obs = bool(getattr(settings, "agent_http_obs_logs", False))
    runtime_enabled = bool(getattr(settings, "agent_runtime_enabled", True))

    if not runtime_enabled and not http_obs:
        return await call_next(request)

    # Always attach a turn context for webhook/test; optionally for every route.
    attach_turn = runtime_enabled and (monitored_path or http_obs)
    if not attach_turn:
        return await call_next(request)

    from app.llm_call_policy import build_llm_call_budget

    budget_cfg = build_llm_call_budget(execution_path="normal")
    context = TurnRuntimeContext(
        trace_id=_request_trace_id(request),
        llm_budget=LLMCallBudget(
            max_calls=int(budget_cfg.get("max_calls") or 2),
            enforce=bool(budget_cfg.get("enforce", True)),
        ),
    )
    context.execution_path = str(budget_cfg.get("execution_path") or "normal")
    context.start_stage("request")
    token = set_current_turn(context)
    status_code = 500
    try:
        raw_query = str(request.url.query or "")
        safe_query = None
        if raw_query:
            # Never log webhook/admin tokens even when http obs is on.
            import re as _re

            safe_query = _re.sub(
                r"(?i)(token|secret|api_key|apikey|authorization)=([^&]*)",
                r"\1=[REDACTED]",
                raw_query,
            )[:200]
        log_event(
            "http.request",
            {
                "method": request.method,
                "path": path,
                "query": safe_query,
                "content_type": request.headers.get("content-type"),
                "user_agent": (request.headers.get("user-agent") or "")[:120] or None,
                "monitored_path": monitored_path,
            },
        )
        response = await call_next(request)
        status_code = getattr(response, "status_code", 200) or 200
        response.headers["X-Trace-ID"] = context.trace_id
        return response
    except Exception as exc:
        if not isinstance(exc, HTTPException):
            log_exception(
                "http.exception",
                exc,
                {"method": request.method, "path": path},
            )
        raise
    finally:
        await release_conversation_lock(
            getattr(request.state, "conversation_lock_handle", None)
        )
        context.finish_stage("request")
        summary = context.safe_summary()
        log_event(
            "http.response",
            {
                "method": request.method,
                "path": path,
                "status_code": status_code,
                "latency_ms": context.stage_durations_ms.get("request"),
            },
        )
        if monitored_path:
            print("[agent.runtime]", summary)
            log_event("runtime.summary", summary)
        reset_current_turn(token)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log_exception(
        "app.unhandled_exception",
        exc,
        {
            "method": request.method,
            "path": request.url.path,
        },
    )
    return JSONResponse(
        status_code=500,
        content={"ok": False, "error": "internal_server_error"},
    )


def _webhook_event_name(payload: dict | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = (
        payload.get("eventName")
        or payload.get("event")
        or payload.get("eventType")
    )
    return str(value) if value is not None else None


def _skip_webhook_event(
    *,
    event_name: str | None,
    reason: str,
    error_type: str | None = None,
) -> JSONResponse:
    skipped = {
        "event_name": event_name,
        "should_process": False,
        "reason": reason,
    }
    if error_type is not None:
        skipped["error_type"] = error_type
    log_event("brevo.webhook.routing", skipped)
    log_event("brevo.webhook.skipped", skipped)
    return JSONResponse({"ok": True, "skipped": True, "reason": reason})


async def read_request_payload(request: Request) -> dict:
    """Read request body defensively.

    Accepts:
    - application/json
    - x-www-form-urlencoded with payload/data/body/json containing JSON
    - plain JSON body

    Never lets JSONDecodeError crash the ASGI app.
    """
    raw_body = await request.body()

    if not raw_body:
        return {}

    content_type = (request.headers.get("content-type") or "").lower()
    raw_text = raw_body.decode("utf-8", errors="replace").strip()

    # 1) JSON direto
    try:
        parsed = json.loads(raw_text)
        if isinstance(parsed, dict):
            return parsed
        return {"value": parsed}
    except JSONDecodeError:
        pass

    # 2) Form-data / x-www-form-urlencoded
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        try:
            form = await request.form()
            form_data = dict(form)

            for key in ("payload", "data", "body", "json"):
                value = form_data.get(key)
                if isinstance(value, str) and value.strip():
                    try:
                        parsed = json.loads(value)
                        if isinstance(parsed, dict):
                            return parsed
                    except JSONDecodeError:
                        continue

            return form_data
        except Exception:
            pass

    # 3) Erro controlado, sem derrubar ASGI/Vercel
    raise HTTPException(
        status_code=400,
        detail={
            "error": "invalid_json_body",
            "message": "O body recebido não é um JSON válido.",
            "content_type": content_type,
            "raw_preview": raw_text[:200],
            "hint": "Envie Content-Type: application/json com propriedades entre aspas duplas.",
        },
    )


async def _probe_meta_ig_graph() -> dict:
    from app.channels.meta_instagram import probe_instagram_graph_subscriptions

    try:
        return await probe_instagram_graph_subscriptions()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__}


@app.get("/")
async def root():
    settings = get_settings()
    return {
        "ok": True,
        "service": settings.app_name,
        "dry_run": settings.dry_run,
        "environment": settings.environment,
    }


AGENT_VERSION = "openai-db-context-multichannel-runtime-v7"


@app.get("/api/health")
async def health():
    settings = get_settings()
    openai_key = settings.openai_api_key
    allowed_channels = get_allowed_channels(settings)
    ordered_channels = [
        channel
        for channel in ("whatsapp", "instagram", "facebook")
        if channel in allowed_channels
    ]
    ordered_channels.extend(sorted(allowed_channels.difference(ordered_channels)))
    return {
        "ok": True,
        "agent_version": AGENT_VERSION,
        "agent_mode": "openai_with_db_context",
        "openai_configured": bool(openai_key),
        "openai_key_format_ok": openai_key.startswith(("sk-", "sk-proj-")),
        "openai_key_length": len(openai_key),
        "openai_model": settings.openai_model,
        "agent_runtime_enabled": getattr(settings, "agent_runtime_enabled", True),
        "agent_full_obs_logs": getattr(settings, "agent_full_obs_logs", False),
        "agent_http_obs_logs": getattr(settings, "agent_http_obs_logs", False),
        "agent_llm_budget_enabled": getattr(
            settings,
            "agent_llm_budget_enabled",
            False,
        ),
        "agent_max_llm_calls_per_turn": getattr(
            settings,
            "agent_max_llm_calls_per_turn",
            2,
        ),
        "agent_policy_mode": getattr(settings, "agent_policy_mode", "shadow"),
        "agent_factual_validation_mode": getattr(
            settings,
            "agent_factual_validation_mode",
            "shadow",
        ),
        "agent_conversation_lock_enabled": getattr(
            settings,
            "agent_conversation_lock_enabled",
            True,
        ),
        "agent_quality_judge_mode": getattr(
            settings,
            "agent_quality_judge_mode",
            "shadow",
        ),
        "agent_critique_mode": getattr(settings, "agent_critique_mode", "enforce"),
        "agent_critique_max_retries": getattr(
            settings,
            "agent_critique_max_retries",
            2,
        ),
        "agent_history_limit": getattr(settings, "agent_history_limit", 80),
        "agent_send_idempotency_enabled": getattr(
            settings,
            "agent_send_idempotency_enabled",
            True,
        ),
        "database_configured": bool(settings.database_url),
        "sorteio_database_configured": bool(
            getattr(settings, "sorteio_database_url", "") or settings.database_url
        ),
        "sorteio_database_dedicated": bool(
            str(getattr(settings, "sorteio_database_url", "") or "").strip()
        ),
        "brevo_send_configured": bool(
            settings.brevo_api_key
            and (
                settings.brevo_agent_id
                or (settings.brevo_agent_email and settings.brevo_agent_name)
                or settings.brevo_sender_number
            )
        ),
        "brevo_conversations_configured": bool(
            settings.brevo_api_key
            and (
                settings.brevo_agent_id
                or (settings.brevo_agent_email and settings.brevo_agent_name)
            )
        ),
        "brevo_whatsapp_configured": bool(
            settings.brevo_api_key and settings.brevo_sender_number
        ),
        "brevo_social_channels_enabled": getattr(settings, "brevo_social_channels_enabled", True),
        "brevo_allowed_channels": ordered_channels,
        "brevo_reply_mode": settings.brevo_reply_mode,
        "brevo_live_send_enabled": (not settings.dry_run and settings.brevo_reply_mode.lower() != "dry_run"),
        "brevo_webhook_secret_configured": bool(settings.brevo_webhook_secret),
        "audio_inbound_enabled": settings.audio_inbound_enabled,
        "audio_outbound_enabled": settings.audio_outbound_enabled,
        "supabase_storage_configured": bool(settings.supabase_url and settings.supabase_service_key),
        "dry_run": settings.dry_run,
        "tray_adapter_configured": bool(settings.tray_adapter_url and settings.tray_adapter_token),
        "tray_tools_enabled": bool(settings.tray_adapter_url and settings.tray_adapter_token),
        "pix_direct_enabled": bool(getattr(settings, "pix_direct_enabled", False)),
        "pix_mp_configured": bool(
            (
                getattr(settings, "resolved_mp_access_token", None)()
                if callable(getattr(settings, "resolved_mp_access_token", None))
                else (
                    getattr(settings, "mp_access_token", "")
                    or getattr(settings, "mercadopago_access_token", "")
                )
            )
        ),
        "pix_public_url_configured": bool(getattr(settings, "public_url", "")),
        "pix_webhook_path": "/api/payments/webhook",
        "remarketing_enabled": getattr(settings, "remarketing_enabled", False),
        "remarketing_cron_configured": bool(
            getattr(settings, "remarketing_cron_secret", "")
        ),
        "remarketing_touch_hours": getattr(
            settings,
            "remarketing_touch_hours",
            "1,12,23",
        ),
        "remarketing_meta_window_hours": getattr(
            settings,
            "remarketing_meta_window_hours",
            24,
        ),
        "meta_webhook_enabled": bool(getattr(settings, "meta_webhook_enabled", False)),
        "meta_app_secret_len": len(str(getattr(settings, "meta_app_secret", "") or "").strip()),
        "meta_ig_app_secret_len": len(str(getattr(settings, "meta_ig_app_secret", "") or "").strip()),
        "meta_verify_token_len": len(str(getattr(settings, "meta_verify_token", "") or "").strip()),
        "meta_ig_secret_same_as_app_secret": (
            bool(str(getattr(settings, "meta_ig_app_secret", "") or "").strip())
            and str(getattr(settings, "meta_ig_app_secret", "") or "").strip()
            == str(getattr(settings, "meta_app_secret", "") or "").strip()
        ),
        "meta_ig_graph": await _probe_meta_ig_graph(),
        "rollout": build_rollout_status(settings),
    }


@app.get("/api/integrations/tray/test", dependencies=[Depends(verify_admin_token)])
async def test_tray_integration():
    try:
        await TrayAdapterClient().search_products(limit=1)
    except TrayAdapterError as exc:
        print("[tray.integration] diagnostic_failed", {"status_code": exc.status_code})
        return JSONResponse(
            status_code=503,
            content={
                "success": False,
                "tray_adapter_connected": False,
                "products_accessible": False,
                "error": "tray_adapter_unavailable",
            },
        )
    return {
        "success": True,
        "tray_adapter_connected": True,
        "products_accessible": True,
    }


async def handle_brevo_conversations_webhook(request: Request) -> JSONResponse:
    try:
        payload = await read_request_payload(request)
    except HTTPException:
        log_event(
            "brevo.webhook.routing",
            {
                "event_name": None,
                "should_process": False,
                "reason": "invalid_payload",
            },
        )
        log_event(
            "brevo.webhook.skipped",
            {"event_name": None, "reason": "invalid_payload"},
        )
        raise

    event_name = _webhook_event_name(payload)
    log_event(
        "brevo.webhook.received",
        {
            "content_type": request.headers.get("content-type"),
            "has_body": True,
            **summarize_webhook_payload(payload if isinstance(payload, dict) else {}),
        },
    )

    event_skip_reason = webhook_event_skip_reason(payload)
    if event_skip_reason:
        return _skip_webhook_event(
            event_name=event_name,
            reason=event_skip_reason,
        )

    try:
        incoming = parse_brevo_conversations_payload(payload)
    except Exception as exc:
        log_exception(
            "brevo.webhook.parse_failed",
            exc,
            {
                "event_name": payload.get("eventName") if isinstance(payload, dict) else None,
                "parsed": False,
            },
        )
        log_event(
            "brevo.webhook.parsed",
            {
                "parsed": False,
                "event_name": payload.get("eventName") if isinstance(payload, dict) else None,
                "channel": "unknown",
                "sender_key_present": False,
                "visitor_id_present": False,
                "source_conversation_ref_present": False,
                "message_id_present": False,
                "conversation_id_present": False,
                "sender_phone_present": False,
                "text_present": False,
                "input_modality": None,
                "attachment_type": None,
                "direction": None,
            },
        )
        return _skip_webhook_event(
            event_name=event_name,
            reason="invalid_payload",
            error_type=type(exc).__name__,
        )

    runtime = get_current_turn()
    if runtime is not None:
        runtime.channel = incoming.channel
        runtime.conversation_key = (
            incoming.conversation_id
            or incoming.sender_key
            or incoming.visitor_id
            or incoming.sender_phone
            or "unresolved"
        )

    log_event(
        "brevo.webhook.parsed",
        {
            "parsed": True,
            "event_name": incoming.event_type,
            "channel": incoming.channel,
            "sender_key_present": bool(incoming.sender_key),
            "visitor_id_present": bool(incoming.visitor_id),
            "source_conversation_ref_present": bool(incoming.source_conversation_ref),
            "message_id_present": bool(incoming.message_id),
            "conversation_id_present": bool(incoming.conversation_id),
            "sender_phone_present": bool(incoming.sender_phone),
            "sender_name_present": bool(incoming.sender_name),
            "text_present": bool(incoming.text),
            "text_chars": len(incoming.text or ""),
            "text_preview": redact_text(incoming.text, max_chars=200),
            "input_modality": incoming.input_modality,
            "attachment_type": incoming.attachment_type,
            "image_url_present": bool((incoming.image_url or "").strip()),
            "audio_url_present": bool(incoming.audio_url),
            "direction": selected_message_info(payload).get("role"),
        },
    )

    selected = select_effective_inbound_message(payload)
    selection_info = selected_message_info(payload, selected)
    log_event(
        "brevo.webhook.selected_message",
        {
            "message_id_present": bool(incoming.message_id),
            "role": selection_info.get("role"),
            "timestamp_present": selection_info.get("timestamp_present"),
            "text_length": len(incoming.text or ""),
            "text_hash": hashlib.sha256((incoming.text or "").encode("utf-8")).hexdigest()[:12],
            "ordering_fallback": selection_info.get("ordering_fallback"),
            "channel": incoming.channel,
            "event_name": incoming.event_type,
            "input_modality": incoming.input_modality,
            "attachment_type": incoming.attachment_type,
        },
    )

    settings = get_settings()
    allowed_channels = get_allowed_channels(settings)

    skip_reason = inbound_skip_reason(payload)
    if skip_reason:
        # Brevo labels Instagram Story / unsupported IG media as an "agent"
        # placeholder without attachment URL. Do not treat that as human takeover,
        # and guide the visitor to resend a normal photo.
        from app.brevo_instagram_media import (
            UNVIEWABLE_MEDIA_GUIDE_REPLY,
            is_brevo_unviewable_media_text,
        )

        if (
            skip_reason in {"agent_message", "outbound_message"}
            and is_brevo_unviewable_media_text(incoming.text)
            and (incoming.channel or "").lower() == "instagram"
            and "instagram" in allowed_channels
            and bool(getattr(settings, "brevo_social_channels_enabled", True))
        ):
            log_event(
                "brevo.instagram_media_unviewable",
                {
                    "event_name": event_name,
                    "channel": incoming.channel,
                    "conversation_id_present": bool(incoming.conversation_id),
                    "visitor_id_present": bool(incoming.visitor_id),
                    "message_id_present": bool(incoming.message_id),
                    "skip_reason": skip_reason,
                },
            )
            if incoming.message_id and inbound_message_exists(
                incoming.provider, incoming.message_id
            ):
                return _skip_webhook_event(
                    event_name=event_name,
                    reason="instagram_media_unviewable_duplicate",
                )
            try:
                claimed, inbound_id = claim_inbound_message(incoming.model_dump())
            except Exception as exc:
                log_exception(
                    "brevo.webhook.unviewable_inbound_failed",
                    exc,
                    {"event_name": event_name},
                )
                return _skip_webhook_event(
                    event_name=event_name,
                    reason="instagram_media_unviewable",
                )
            if not claimed:
                return _skip_webhook_event(
                    event_name=event_name,
                    reason="instagram_media_unviewable_duplicate",
                )
            guide = AgentResult(
                reply_text=UNVIEWABLE_MEDIA_GUIDE_REPLY,
                intent="commerce",
                handoff_required=False,
                safety_reason="instagram_media_unviewable",
                response_metadata={
                    "domain": "commerce",
                    "response_source": "deterministic_fallback",
                    "fallback_reason": "brevo_instagram_media_unviewable",
                },
            )
            send_result = await send_brevo_reply(incoming, guide)
            try:
                insert_agent_response(
                    {
                        "inbound_id": inbound_id,
                        "channel": incoming.channel,
                        "sender_key": incoming.sender_key,
                        "sender_phone": incoming.sender_phone,
                        "reply_text": guide.reply_text,
                        "intent": guide.intent,
                        "handoff_required": guide.handoff_required,
                        "safety_reason": guide.safety_reason,
                        "provider_send_ok": bool(send_result.ok),
                        "provider_response": send_result.model_dump(),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                log_exception(
                    "brevo.webhook.unviewable_response_persist_failed",
                    exc,
                    {"inbound_id": inbound_id},
                )
            log_event(
                "brevo.webhook.unviewable_media_guided",
                {
                    "inbound_id": inbound_id,
                    "send_ok": bool(send_result.ok),
                    "channel": incoming.channel,
                },
            )
            return JSONResponse(
                {
                    "ok": True,
                    "skipped": False,
                    "reason": "instagram_media_unviewable_guided",
                    "inbound_id": inbound_id,
                }
            )

        if skip_reason in {"agent_message", "outbound_message"}:
            try:
                from app.human_takeover import touch_human_activity

                touch_human_activity(incoming)
            except Exception as exc:  # noqa: BLE001
                log_exception(
                    "brevo.webhook.human_takeover_touch_failed",
                    exc,
                    {"skip_reason": skip_reason},
                )
        return _skip_webhook_event(
            event_name=event_name,
            reason=skip_reason,
        )

    if incoming.channel not in allowed_channels:
        return _skip_webhook_event(
            event_name=event_name,
            reason="channel_not_allowed",
        )
    if incoming.channel in {"instagram", "facebook"} and not settings.brevo_social_channels_enabled:
        return _skip_webhook_event(
            event_name=event_name,
            reason="social_channels_disabled",
        )

    if not any((incoming.sender_key, incoming.visitor_id, incoming.conversation_id)):
        return _skip_webhook_event(
            event_name=event_name,
            reason="missing_sender_identity",
        )

    if (
        not incoming.text.strip()
        and not incoming.audio_url
        and not (incoming.image_url or "").strip()
    ):
        return _skip_webhook_event(
            event_name=event_name,
            reason="no_text",
        )

    # FASE 2: optional durable enqueue — HTTP 200 before agent turn.
    if bool(getattr(settings, "agent_async_ingress_enabled", False)):
        from app.ingress.inbox import enqueue_inbound

        created, inbox_id = enqueue_inbound(
            provider=incoming.provider or "brevo",
            channel=incoming.channel,
            message_id=incoming.message_id,
            conversation_key=incoming.conversation_id or incoming.sender_key,
            visitor_id=incoming.visitor_id,
            sender_key=incoming.sender_key,
            event_name=event_name,
            payload=payload if isinstance(payload, dict) else {},
        )
        return JSONResponse(
            {
                "ok": True,
                "queued": True,
                "created": created,
                "inbox_id": inbox_id,
                "async_ingress": True,
            }
        )

    # Cheap duplicate check before waiting on the conversation lock. Brevo often
    # redelivers the same fragment while Vision/catalog still holds the lock.
    if incoming.message_id and inbound_message_exists(incoming.provider, incoming.message_id):
        return _skip_webhook_event(
            event_name=event_name,
            reason="duplicate_message",
        )

    if getattr(settings, "agent_conversation_lock_enabled", True):
        lock_key = conversation_lock_key(
            conversation_id=incoming.conversation_id,
            sender_key=incoming.sender_key,
            sender_phone=incoming.sender_phone,
            visitor_id=incoming.visitor_id,
        )
        if lock_key:
            lock_timeout = float(
                getattr(
                    settings,
                    "agent_conversation_lock_timeout_seconds",
                    15.0,
                )
            )
            # Keep waits short: the in-flight turn owns the work. Returning 503
            # makes Brevo retry and amplifies lock contention during photo turns.
            if (incoming.image_url or "").strip() or (
                (incoming.attachment_type or "").lower() == "image"
            ):
                lock_timeout = min(max(lock_timeout, 5.0), 12.0)
            try:
                with runtime_stage("conversation_lock_wait"):
                    request.state.conversation_lock_handle = (
                        await acquire_conversation_lock(
                            lock_key,
                            database_url=getattr(
                                settings,
                                "database_url",
                                "",
                            ),
                            timeout_seconds=lock_timeout,
                        )
                    )
            except ConversationLockUnavailable as exc:
                lock_error = str(exc)
                log_event(
                    "agent.lock.unavailable",
                    {
                        "error": lock_error,
                        "channel": incoming.channel,
                        "conversation_id_present": bool(
                            incoming.conversation_id
                        ),
                        "sender_key_present": bool(incoming.sender_key),
                        "inbound_image": bool((incoming.image_url or "").strip()),
                        "lock_timeout_seconds": lock_timeout,
                    },
                )
                # Contention: another worker holds the conversation — acknowledge
                # and stop Brevo retries. DB lock failure is different: dropping
                # the event loses photo turns, so fall back to a local-only lock.
                if lock_error == "database_lock_unavailable":
                    try:
                        request.state.conversation_lock_handle = (
                            await acquire_conversation_lock(
                                lock_key,
                                database_url="",
                                timeout_seconds=min(lock_timeout, 5.0),
                            )
                        )
                        log_event(
                            "agent.lock.local_fallback",
                            {
                                "channel": incoming.channel,
                                "inbound_image": bool(
                                    (incoming.image_url or "").strip()
                                ),
                            },
                        )
                    except ConversationLockUnavailable:
                        return _skip_webhook_event(
                            event_name=event_name,
                            reason="conversation_busy",
                        )
                else:
                    return _skip_webhook_event(
                        event_name=event_name,
                        reason="conversation_busy",
                    )

    # Brevo often redelivers the caption as a second text-only webhook after the
    # photo+caption turn. Skip exact caption echoes so we don't reply twice.
    if not (incoming.image_url or "").strip() and is_caption_echo_of_recent_image(
        incoming
    ):
        return _skip_webhook_event(
            event_name=event_name,
            reason="caption_echo",
        )

    try:
        claimed, inbound_id = claim_inbound_message(incoming.model_dump())
    except Exception as exc:
        log_exception(
            "brevo.webhook.inbound_insert_failed",
            exc,
            {
                "event_type": incoming.event_type,
                "channel": incoming.channel,
                "sender_key_present": bool(incoming.sender_key),
                "visitor_id_present": bool(incoming.visitor_id),
                "conversation_id_present": bool(incoming.conversation_id),
                "sender_phone_present": bool(incoming.sender_phone),
                "message_id_present": bool(incoming.message_id),
                "input_modality": incoming.input_modality,
                "attachment_type": incoming.attachment_type,
            },
        )
        raise HTTPException(status_code=500, detail={"error": "inbound_insert_failed"}) from exc
    if not claimed:
        return _skip_webhook_event(
            event_name=event_name,
            reason="duplicate_message",
        )

    # Central ChatBô: se um humano assumiu, grava inbound mas não responde.
    try:
        from app.human_takeover import human_takeover_active

        if human_takeover_active(incoming):
            return _skip_webhook_event(
                event_name=event_name,
                reason="human_takeover",
            )
    except Exception as exc:  # noqa: BLE001
        log_exception(
            "brevo.webhook.human_takeover_check_failed",
            exc,
            {"inbound_id": inbound_id},
        )

    if runtime is not None:
        runtime.inbound_id = inbound_id
    if isinstance(incoming.raw, dict):
        incoming.raw["inbound_id"] = inbound_id
    if incoming.sender_phone:
        customer_context = find_customer_profile_by_phone(incoming.sender_phone)
    else:
        customer_context = {
            "found": False,
            "channel": incoming.channel,
            "sender_key": incoming.sender_key,
            "display_name": incoming.sender_name,
        }
    log_event(
        "brevo.webhook.routing",
        {
            "event_name": event_name,
            "should_process": True,
            "reason": "inbound_message",
            "inbound_id": inbound_id,
            "customer_found": bool(customer_context.get("found")),
        },
    )
    log_event(
        "brevo.webhook.processing",
        {
            "channel": incoming.channel,
            "sender_key_present": bool(incoming.sender_key),
            "visitor_id_present": bool(incoming.visitor_id),
            "source_conversation_ref_present": bool(incoming.source_conversation_ref),
            "conversation_id_present": bool(incoming.conversation_id),
            "sender_phone_present": bool(incoming.sender_phone),
            "message_id_present": bool(incoming.message_id),
            "event_name": incoming.event_type,
            "input_modality": incoming.input_modality,
            "attachment_type": incoming.attachment_type,
            "text_preview": redact_text(incoming.text, max_chars=200),
            "inbound_id": inbound_id,
            "customer_found": bool(customer_context.get("found")),
        },
    )
    agent_result = await process_incoming_message(incoming, customer_context)

    log_event(
        "brevo.webhook.agent_result",
        {
            "intent": agent_result.intent,
            "handoff_required": agent_result.handoff_required,
            "safety_reason": agent_result.safety_reason,
            "reply_length": len(agent_result.reply_text or ""),
            "reply_preview": redact_text(agent_result.reply_text, max_chars=200),
            "channel": incoming.channel,
            "input_modality": incoming.input_modality,
            "attachment_type": incoming.attachment_type,
            "transcription_failed": incoming.transcription_failed,
            "response_source": (agent_result.response_metadata or {}).get(
                "response_source"
            ),
            "domain": (agent_result.response_metadata or {}).get("domain"),
            "goal": (agent_result.response_metadata or {}).get("goal"),
            "used_openai_interpreter": bool(
                (agent_result.response_metadata or {}).get("used_openai_interpreter")
            ),
            "used_openai_responder": bool(
                (agent_result.response_metadata or {}).get("used_openai_responder")
            ),
            "used_tray": bool((agent_result.response_metadata or {}).get("used_tray")),
        },
    )

    if not is_latest_inbound_message(
        inbound_id,
        incoming.conversation_id,
        incoming.sender_key,
        incoming.sender_phone,
    ):
        log_event(
            "brevo.webhook.skipped_reply",
            {"reason": "stale_inbound", "inbound_id": inbound_id},
        )
        send_result = None
        provider_send_ok = False
        provider_response = {"skipped": True, "reason": "stale_inbound"}
    elif (
        getattr(settings, "agent_send_idempotency_enabled", True)
        and has_successful_agent_response(inbound_id)
    ):
        log_event(
            "brevo.webhook.skipped_reply",
            {
                "reason": "already_sent",
                "inbound_id": inbound_id,
            },
        )
        send_result = None
        provider_send_ok = True
        provider_response = {"skipped": True, "reason": "already_sent"}
    else:
        send_result = await send_brevo_reply(incoming, agent_result)
        provider_send_ok = send_result.ok
        provider_response = send_result.model_dump()
        log_event(
            "brevo.webhook.send_result",
            {
                "ok": send_result.ok,
                "dry_run": send_result.dry_run,
                "status_code": send_result.status_code,
                "error": send_result.error,
                "channel": incoming.channel,
                "inbound_id": inbound_id,
                "reply_chars": len(agent_result.reply_text or ""),
            },
        )
    commerce_state = (agent_result.response_metadata or {}).get("commerce_state")
    decision_snapshot = (agent_result.response_metadata or {}).get(
        "decision_snapshot"
    )
    factual_validation = (agent_result.response_metadata or {}).get(
        "factual_validation"
    )
    quality_judge = (agent_result.response_metadata or {}).get("quality_judge")
    handoff_payload = handoff_provider_payload(agent_result)
    runtime_summary = None
    if runtime is not None:
        runtime.register_fallback(
            (agent_result.response_metadata or {}).get("fallback_reason")
            or agent_result.safety_reason
        )
        runtime_summary = runtime.safe_summary()
        agent_result.response_metadata["turn_runtime"] = runtime_summary
    if isinstance(provider_response, dict):
        agent_context: dict[str, object] = {}
        if isinstance(commerce_state, dict):
            agent_context["commerce_state"] = commerce_state
        if isinstance(decision_snapshot, dict):
            agent_context["decision_snapshot"] = decision_snapshot
        if isinstance(factual_validation, dict):
            agent_context["factual_validation"] = factual_validation
        if isinstance(quality_judge, dict):
            agent_context["quality_judge"] = quality_judge
        if isinstance(handoff_payload, dict):
            agent_context["handoff"] = handoff_payload
        if agent_context:
            provider_response["_agent_context"] = agent_context
    if isinstance(provider_response, dict) and isinstance(runtime_summary, dict):
        provider_response["_agent_runtime"] = runtime_summary

    try:
        insert_agent_response(
            {
                "inbound_id": inbound_id,
                "channel": incoming.channel,
                "sender_key": incoming.sender_key,
                "sender_phone": incoming.sender_phone,
                "reply_text": agent_result.reply_text,
                "intent": agent_result.intent,
                "handoff_required": agent_result.handoff_required,
                "safety_reason": agent_result.safety_reason,
                "provider_send_ok": provider_send_ok,
                "provider_response": provider_response,
            }
        )
    except Exception as exc:
        log_exception(
            "brevo.webhook.response_insert_failed",
            exc,
            {
                "inbound_id": inbound_id,
                "channel": incoming.channel,
            },
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": "response_insert_failed",
                "message": "Falha ao registrar resposta do agente.",
            },
        ) from exc

    try:
        sync_remarketing_interaction(
            incoming,
            inbound_id=inbound_id,
            response_metadata=(
                agent_result.response_metadata
                if provider_send_ok
                else {}
            ),
            handoff_required=agent_result.handoff_required,
        )
    except Exception as exc:
        log_exception(
            "remarketing.sync.failed",
            exc,
            {
                "inbound_id": inbound_id,
                "channel": incoming.channel,
            },
        )

    return JSONResponse(
        {
            "ok": True,
            "inbound_id": inbound_id,
            "reply_dry_run": send_result.dry_run if send_result else False,
            "reply_sent": send_result.ok if send_result else False,
            "handoff_required": agent_result.handoff_required,
            "skipped_reply": not bool(send_result),
        }
    )


@app.get(
    "/api/cron/remarketing",
    dependencies=[Depends(verify_remarketing_cron)],
)
async def remarketing_cron():
    result = await run_remarketing_batch()
    log_event("remarketing.cron.completed", result if isinstance(result, dict) else {"result": result})
    return {"ok": True, **result}


@app.post(
    "/api/cron/remarketing",
    dependencies=[Depends(verify_remarketing_cron)],
)
async def remarketing_cron_manual():
    return await remarketing_cron()


@app.get(
    "/api/cron/product-image-index",
    dependencies=[Depends(verify_remarketing_cron)],
)
async def product_image_index_cron():
    result = await run_product_image_index_batch()
    log_event(
        "product_image_index.cron.completed",
        result if isinstance(result, dict) else {"result": result},
    )
    return result


@app.post(
    "/api/cron/product-image-index",
    dependencies=[Depends(verify_remarketing_cron)],
)
async def product_image_index_cron_manual():
    return await product_image_index_cron()


@app.get(
    "/api/cron/attendance-learning",
    dependencies=[Depends(verify_remarketing_cron)],
)
async def attendance_learning_cron():
    from app.attendance_learning import run_attendance_learning_batch

    result = await run_attendance_learning_batch()
    log_event(
        "attendance.learning.cron.completed",
        result if isinstance(result, dict) else {"result": result},
    )
    return result


@app.post(
    "/api/cron/attendance-learning",
    dependencies=[Depends(verify_remarketing_cron)],
)
async def attendance_learning_cron_manual():
    return await attendance_learning_cron()


@app.post("/api/webhooks/brevo/conversations")
async def brevo_conversations_webhook(
    request: Request,
    _: None = Depends(verify_brevo_webhook),
):
    return await handle_brevo_conversations_webhook(request)


@app.post("/api/webhooks/brevo/whatsapp")
async def brevo_whatsapp_webhook(
    request: Request,
    _: None = Depends(verify_brevo_webhook),
):
    return await handle_brevo_conversations_webhook(request)


@app.get("/api/webhooks/meta")
async def meta_webhook_verify(request: Request):
    """Meta hub challenge for Instagram Messaging subscriptions."""
    from app.channels.meta_instagram import handle_meta_verify_challenge, meta_webhook_enabled

    settings = get_settings()
    if not meta_webhook_enabled():
        raise HTTPException(status_code=404, detail={"error": "meta_webhook_disabled"})
    params = request.query_params
    challenge = handle_meta_verify_challenge(
        mode=params.get("hub.mode"),
        verify_token=params.get("hub.verify_token"),
        challenge=params.get("hub.challenge"),
        expected_token=str(getattr(settings, "meta_verify_token", "") or ""),
    )
    if challenge is None:
        raise HTTPException(status_code=403, detail={"error": "meta_verify_failed"})
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse(challenge)


@app.post("/api/webhooks/meta")
async def meta_instagram_webhook(request: Request):
    """Meta Instagram Messaging webhook → durable inbox (FASE 3)."""
    from app.channels.meta_instagram import (
        messaging_event_shapes,
        meta_webhook_enabled,
        parse_meta_instagram_messaging,
        payload_skeleton,
        probe_instagram_graph_subscriptions,
        verify_meta_signatures,
    )
    from app.ingress.inbox import enqueue_inbound

    settings = get_settings()
    if not meta_webhook_enabled():
        raise HTTPException(status_code=404, detail={"error": "meta_webhook_disabled"})

    body = await request.body()
    signature_sha256 = (
        request.headers.get("x-hub-signature-256")
        or request.headers.get("X-Hub-Signature-256")
    )
    signature_sha1 = (
        request.headers.get("x-hub-signature")
        or request.headers.get("X-Hub-Signature")
    )
    secrets = [
        str(getattr(settings, "meta_app_secret", "") or ""),
        str(getattr(settings, "meta_ig_app_secret", "") or ""),
    ]
    if not verify_meta_signatures(
        app_secrets=secrets,
        body=body,
        signature_header_sha256=signature_sha256,
        signature_header_sha1=signature_sha1,
    ):
        app_secret = str(getattr(settings, "meta_app_secret", "") or "").strip()
        ig_secret = str(getattr(settings, "meta_ig_app_secret", "") or "").strip()
        verify_token = str(getattr(settings, "meta_verify_token", "") or "").strip()
        rejected = {
            "has_sha256": bool((signature_sha256 or "").strip()),
            "has_sha1": bool((signature_sha1 or "").strip()),
            "sha256_prefix": (signature_sha256 or "")[:7],
            "sha256_len": len((signature_sha256 or "").strip()),
            "secret_slots": sum(1 for item in secrets if (item or "").strip()),
            "app_secret_len": len(app_secret),
            "ig_secret_len": len(ig_secret),
            "ig_same_as_app": bool(ig_secret) and ig_secret == app_secret,
            "ig_looks_like_verify_token": bool(ig_secret) and ig_secret == verify_token,
            "ig_looks_like_igaa": ig_secret.startswith("IGAA"),
            "body_bytes": len(body),
            "header_keys": sorted(request.headers.keys()),
        }
        print("[meta.webhook.signature_rejected]", rejected)
        log_event("meta.webhook.signature_rejected", rejected)
        raise HTTPException(status_code=401, detail={"error": "invalid_meta_signature"})

    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_json"}) from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail={"error": "invalid_payload"})

    entries = payload.get("entry") if isinstance(payload.get("entry"), list) else []
    log_event(
        "meta.webhook.received",
        {
            "object": str(payload.get("object") or "")[:32],
            "entries": len(entries),
            "has_messaging": any(
                isinstance(entry, dict)
                and (
                    isinstance(entry.get("messaging"), list)
                    or isinstance(entry.get("changes"), list)
                )
                for entry in entries
            ),
            "has_standby": any(
                isinstance(entry, dict) and isinstance(entry.get("standby"), list)
                for entry in entries
            ),
            "payload_skeleton": payload_skeleton(payload),
            "bytes": len(body),
        },
    )

    messages = parse_meta_instagram_messaging(payload)
    queued: list[dict] = []
    for incoming in messages:
        created, inbox_id = enqueue_inbound(
            provider="meta",
            channel="instagram",
            message_id=incoming.message_id,
            conversation_key=incoming.conversation_id or incoming.sender_key,
            visitor_id=incoming.visitor_id,
            sender_key=incoming.sender_key,
            event_name="meta_messaging",
            payload={
                "normalized": incoming.model_dump(mode="json"),
                "raw": incoming.raw,
            },
        )
        queued.append({"created": created, "inbox_id": inbox_id})

    # Process immediately so Instagram DMs don't wait for the minute cron.
    worker_result = None
    if queued:
        try:
            from app.ingress.worker import process_inbox_batch

            worker_result = await process_inbox_batch(limit=max(5, len(queued)))
        except Exception as exc:  # noqa: BLE001
            log_exception(
                "meta.webhook.inline_worker_failed",
                exc,
                {"queued": len(queued)},
            )

    change_fields: list[str] = []
    entry_keys: list[list[str]] = []
    for entry in entries[:4]:
        if not isinstance(entry, dict):
            continue
        entry_keys.append(sorted(str(key) for key in entry.keys()))
        changes = entry.get("changes")
        if isinstance(changes, list):
            for change in changes[:6]:
                if isinstance(change, dict):
                    change_fields.append(str(change.get("field") or "")[:40])
    result_log = {
        "object": str(payload.get("object") or "")[:32],
        "entries": len(entries),
        "entry_keys": entry_keys,
        "change_fields": change_fields,
        "messaging_shapes": messaging_event_shapes(payload),
        "payload_skeleton": payload_skeleton(payload),
        "messages": len(messages),
        "queued": queued,
        "worker": worker_result,
    }
    if not messages:
        try:
            result_log["graph_subscriptions"] = await probe_instagram_graph_subscriptions()
        except Exception as exc:  # noqa: BLE001
            result_log["graph_subscriptions"] = {"ok": False, "error": type(exc).__name__}
    print("[meta.webhook.result]", result_log)
    log_event("meta.webhook.result", result_log)

    return JSONResponse(
        {
            "ok": True,
            "provider": "meta",
            "messages": len(messages),
            "queued": queued,
            "worker": worker_result,
        }
    )


@app.get("/api/admin/rollout", dependencies=[Depends(verify_admin_token)])
async def admin_rollout_status():
    """Read-only rollout snapshot + rollback checklist (mutate via Vercel env)."""
    return {"ok": True, **build_rollout_status()}


@app.get("/api/admin/instagram/stories/health", dependencies=[Depends(verify_admin_token)])
async def admin_instagram_story_health():
    from app.config import get_settings

    settings = get_settings()
    real_ok = bool(getattr(settings, "instagram_story_real_payload_validated", False))
    mode = str(getattr(settings, "instagram_story_rollout_mode", "off") or "off")
    canary_or_full_allowed = real_ok or mode in {"off", "diagnostics", "shadow"}
    return {
        "ok": True,
        "recognition_enabled": bool(
            getattr(settings, "instagram_story_recognition_enabled", False)
        ),
        "rollout_mode": mode,
        "real_payload_validated": real_ok,
        "video_frame_analysis_enabled": bool(
            getattr(settings, "instagram_story_video_frame_analysis_enabled", False)
        ),
        "canary_full_gate_ok": canary_or_full_allowed,
        "note": (
            "Set INSTAGRAM_STORY_REAL_PAYLOAD_VALIDATED=true only after a real "
            "sanitized Brevo Story payload is covered by tests. "
            "If a past ZIP exposed VERCEL_OIDC_TOKEN, revoke it in Vercel."
        ),
    }


@app.get("/api/admin/instagram/stories", dependencies=[Depends(verify_admin_token)])
async def admin_list_instagram_stories(
    request: Request,
    tenant_id: str | None = None,
    status: str | None = None,
    instagram_account_id: str | None = None,
    product_id: str | None = None,
    limit: int = 50,
):
    from app.config import get_settings
    from app.request_principal import principal_from_admin_token
    from app.story_product_repository import StoryProductRepository

    settings = get_settings()
    if not bool(getattr(settings, "instagram_story_admin_api_enabled", True)):
        return JSONResponse({"ok": False, "error": "admin_api_disabled"}, status_code=403)
    # Tenant from server persona / validated header — never from unauthenticated body alone.
    resolved_tenant = str(
        request.headers.get("x-tenant-id")
        or getattr(settings, "agent_persona_tenant_id", "")
        or tenant_id
        or ""
    ).strip()
    if not resolved_tenant:
        return JSONResponse({"ok": False, "error": "tenant_required"}, status_code=400)
    principal = principal_from_admin_token(
        subject_id=str(
            request.headers.get("x-admin-actor")
            or request.headers.get("x-admin-id")
            or "admin_token"
        ),
        tenant_ids=[resolved_tenant, "*"],
    )
    try:
        principal.require_tenant(resolved_tenant)
    except PermissionError:
        return JSONResponse({"ok": False, "error": "tenant_forbidden"}, status_code=403)
    rows = StoryProductRepository().list_stories(
        tenant_id=resolved_tenant,
        status=status,
        instagram_account_id=instagram_account_id,
        product_id=product_id,
        limit=limit,
    )
    return {
        "ok": True,
        "tenant_id": resolved_tenant,
        "items": [row.model_dump(mode="json") for row in rows],
    }


@app.get("/api/admin/instagram/stories/{row_id}", dependencies=[Depends(verify_admin_token)])
async def admin_get_instagram_story(row_id: int, request: Request, tenant_id: str | None = None):
    from app.config import get_settings
    from app.story_product_repository import StoryProductRepository

    settings = get_settings()
    if not bool(getattr(settings, "instagram_story_admin_api_enabled", True)):
        return JSONResponse({"ok": False, "error": "admin_api_disabled"}, status_code=403)
    resolved_tenant = str(
        tenant_id
        or request.headers.get("x-tenant-id")
        or getattr(settings, "agent_persona_tenant_id", "")
        or ""
    ).strip()
    if not resolved_tenant:
        return JSONResponse({"ok": False, "error": "tenant_required"}, status_code=400)
    row = StoryProductRepository().get_by_id(tenant_id=resolved_tenant, row_id=row_id)
    if row is None:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    return {"ok": True, "item": row.model_dump(mode="json")}


@app.post(
    "/api/admin/instagram/stories/link-product",
    dependencies=[Depends(verify_admin_token)],
)
async def admin_link_instagram_story_product(request: Request):
    from app.config import get_settings
    from app.story_publication_link_service import (
        register_published_story,
        validate_link_payload,
    )

    settings = get_settings()
    if not bool(getattr(settings, "instagram_story_admin_api_enabled", True)):
        return JSONResponse({"ok": False, "error": "admin_api_disabled"}, status_code=403)
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    actor = str(
        request.headers.get("x-admin-actor")
        or request.headers.get("x-admin-id")
        or "admin_token"
    ).strip()[:120]
    try:
        cleaned = validate_link_payload(body)
        cleaned["confirmed_by"] = actor
        assoc = register_published_story(**cleaned)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": type(exc).__name__},
            status_code=500,
        )
    return {"ok": True, "item": assoc.model_dump(mode="json")}


@app.post(
    "/api/admin/instagram/stories/{row_id}/confirm",
    dependencies=[Depends(verify_admin_token)],
)
async def admin_confirm_instagram_story(row_id: int, request: Request):
    from app.config import get_settings
    from app.fact_authority import catalog_item_key_for
    from app.catalog_index_repository import CatalogIndexRepository
    from app.request_principal import principal_from_admin_token
    from app.story_product_repository import StoryProductRepository
    from app.observability import log_event
    from app.db import ensure_tables, get_conn

    settings = get_settings()
    if not bool(getattr(settings, "instagram_story_admin_api_enabled", True)):
        return JSONResponse({"ok": False, "error": "admin_api_disabled"}, status_code=403)
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    # Never trust body.confirmed_by / body.actor. Tenant from header/persona first.
    resolved_tenant = str(
        request.headers.get("x-tenant-id")
        or getattr(settings, "agent_persona_tenant_id", "")
        or body.get("tenant_id")
        or ""
    ).strip()
    if not resolved_tenant:
        return JSONResponse({"ok": False, "error": "tenant_required"}, status_code=400)
    actor = str(
        request.headers.get("x-admin-actor")
        or request.headers.get("x-admin-id")
        or "admin_token"
    ).strip()[:120]
    principal = principal_from_admin_token(
        subject_id=actor,
        tenant_ids=[resolved_tenant, "*"],
    )
    try:
        principal.require_tenant(resolved_tenant)
    except PermissionError:
        return JSONResponse({"ok": False, "error": "tenant_forbidden"}, status_code=403)
    repo = StoryProductRepository()
    existing = repo.get_by_id(tenant_id=resolved_tenant, row_id=row_id)
    if existing is None:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    product_id = str(body.get("product_id") or "").strip()
    if not product_id:
        return JSONResponse({"ok": False, "error": "product_required"}, status_code=400)
    variant_id = (
        str(body["variant_id"]).strip()
        if body.get("variant_id") not in (None, "")
        else None
    )
    catalog_item_key = catalog_item_key_for(product_id, variant_id)
    index_row = CatalogIndexRepository().get_by_product_and_variant(
        tenant_id=resolved_tenant,
        product_id=product_id,
        variant_id=variant_id,
    )
    if index_row is not None and str(index_row.get("tenant_id") or "") != resolved_tenant:
        return JSONResponse({"ok": False, "error": "tenant_mismatch"}, status_code=403)
    confirmed = repo.confirm_match(
        tenant_id=resolved_tenant,
        provider=existing.provider,
        instagram_account_id=existing.instagram_account_id,
        story_media_id=existing.story_media_id,
        catalog_item_key=catalog_item_key,
        product_id=product_id,
        variant_id=variant_id,
        match_source="manual",
        match_confidence=1.0,
        match_status="manually_confirmed",
        confirmed_by=actor,
        explanation={
            "reason": str(body.get("reason") or "manual_confirm")[:200],
            "previous_product_id": existing.product_id,
            "previous_variant_id": existing.variant_id,
            "previous_catalog_item_key": existing.catalog_item_key,
            "admin_id": actor,
            "tenant_id": resolved_tenant,
            "story_row_id": row_id,
        },
    )
    try:
        ensure_tables()
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.story_product_association_audit (
                        tenant_id, story_row_id, story_media_id, actor_id, action,
                        previous_product_id, previous_variant_id, previous_catalog_item_key,
                        new_product_id, new_variant_id, new_catalog_item_key, reason
                    ) VALUES (
                        %s, %s, %s, %s, 'confirm',
                        %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        resolved_tenant,
                        row_id,
                        existing.story_media_id,
                        actor,
                        existing.product_id,
                        existing.variant_id,
                        existing.catalog_item_key,
                        product_id,
                        variant_id,
                        catalog_item_key,
                        str(body.get("reason") or "manual_confirm")[:200],
                    ),
                )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        log_event(
            "instagram_story.admin_audit_failed",
            {"error_type": type(exc).__name__},
        )
    log_event(
        "instagram_story.admin_confirm",
        {
            "tenant_id": resolved_tenant,
            "story_row_id": row_id,
            "admin_id": actor,
            "product_id": product_id,
            "variant_id": variant_id,
            "previous_product_id": existing.product_id,
        },
    )
    return {"ok": True, "item": confirmed.model_dump(mode="json") if confirmed else None}


@app.post(
    "/api/admin/instagram/stories/{row_id}/unlink",
    dependencies=[Depends(verify_admin_token)],
)
async def admin_unlink_instagram_story(row_id: int, request: Request):
    from app.config import get_settings
    from app.story_product_repository import StoryProductRepository

    settings = get_settings()
    if not bool(getattr(settings, "instagram_story_admin_api_enabled", True)):
        return JSONResponse({"ok": False, "error": "admin_api_disabled"}, status_code=403)
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    resolved_tenant = str(
        (body or {}).get("tenant_id")
        or request.headers.get("x-tenant-id")
        or getattr(settings, "agent_persona_tenant_id", "")
        or ""
    ).strip()
    if not resolved_tenant:
        return JSONResponse({"ok": False, "error": "tenant_required"}, status_code=400)
    actor = str(
        request.headers.get("x-admin-actor")
        or request.headers.get("x-admin-id")
        or "admin_token"
    ).strip()[:120]
    row = StoryProductRepository().unlink(
        tenant_id=resolved_tenant,
        row_id=row_id,
        confirmed_by=actor,
    )
    if row is None:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    return {"ok": True, "item": row.model_dump(mode="json")}


@app.post(
    "/api/admin/instagram/stories/{row_id}/reprocess",
    dependencies=[Depends(verify_admin_token)],
)
async def admin_reprocess_instagram_story(row_id: int, request: Request):
    from app.config import get_settings
    from app.story_product_repository import StoryProductRepository

    settings = get_settings()
    if not bool(getattr(settings, "instagram_story_admin_api_enabled", True)):
        return JSONResponse({"ok": False, "error": "admin_api_disabled"}, status_code=403)
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    resolved_tenant = str(
        (body or {}).get("tenant_id")
        or request.headers.get("x-tenant-id")
        or getattr(settings, "agent_persona_tenant_id", "")
        or ""
    ).strip()
    if not resolved_tenant:
        return JSONResponse({"ok": False, "error": "tenant_required"}, status_code=400)
    actor = str(
        request.headers.get("x-admin-actor")
        or request.headers.get("x-admin-id")
        or "admin_token"
    ).strip()[:120]
    repo = StoryProductRepository()
    existing = repo.get_by_id(tenant_id=resolved_tenant, row_id=row_id)
    if existing is None:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    reset = repo.unlink(
        tenant_id=resolved_tenant,
        row_id=row_id,
        confirmed_by=f"{actor}:reprocess",
    )
    return {
        "ok": True,
        "item": reset.model_dump(mode="json") if reset else None,
        "note": "Association reset to pending; next customer reply will re-analyze once.",
    }


@app.post(
    "/api/cron/instagram-story-media-retention",
    dependencies=[Depends(verify_remarketing_cron)],
)
async def cron_instagram_story_media_retention():
    from app.story_media_retention import cleanup_expired_story_media

    result = await cleanup_expired_story_media(limit=200)
    return {
        "ok": True,
        "scanned": result.scanned,
        "deleted_storage": result.deleted_storage,
        "cleared_paths": result.cleared_paths,
        "failed": result.failed,
    }


@app.get(
    "/api/cron/instagram-story-media-retention",
    dependencies=[Depends(verify_remarketing_cron)],
)
async def cron_instagram_story_media_retention_get():
    return await cron_instagram_story_media_retention()


@app.post(
    "/api/cron/process-inbox",
    dependencies=[Depends(verify_remarketing_cron)],
)
async def cron_process_inbox():
    from app.ingress.worker import process_inbox_batch

    return await process_inbox_batch()


@app.get(
    "/api/cron/process-inbox",
    dependencies=[Depends(verify_remarketing_cron)],
)
async def cron_process_inbox_get():
    return await cron_process_inbox()


@app.post("/api/test/agent")
async def test_agent(request: Request, _: None = Depends(verify_admin_token)):
    payload = await read_request_payload(request)

    log_event(
        "agent.test.received",
        {
            "payload_keys": list(payload.keys()) if isinstance(payload, dict) else [],
            "has_phone": bool(payload.get("phone")) if isinstance(payload, dict) else False,
            "has_text": bool(payload.get("text")) if isinstance(payload, dict) else False,
            "text_preview": redact_text(
                str(payload.get("text") or "") if isinstance(payload, dict) else None,
                max_chars=120,
            )
            if isinstance(payload, dict)
            else None,
        },
    )

    incoming = parse_brevo_conversations_payload(
        {
            "text": payload.get("text", "Olá"),
            "from": payload.get("phone"),
            "name": payload.get("name", "Teste"),
            "event": "manual_test",
        }
    )
    customer_context = find_customer_profile_by_phone(incoming.sender_phone)
    agent_result = await process_incoming_message(incoming, customer_context)
    return {
        "ok": True,
        "reply_text": agent_result.reply_text,
        "reply_modality": agent_result.reply_modality,
        "input_modality": incoming.input_modality,
        "transcribed_text": incoming.text if incoming.input_modality == "audio" else None,
        "intent": agent_result.intent,
        "handoff_required": agent_result.handoff_required,
        "safety_reason": agent_result.safety_reason,
        "customer_context": customer_context,
    }


@app.post("/api/debug/echo")
async def debug_echo(request: Request, _: None = Depends(verify_admin_token)):
    payload = await read_request_payload(request)
    return {
        "ok": True,
        "content_type": request.headers.get("content-type"),
        "keys": list(payload.keys()) if isinstance(payload, dict) else [],
        "payload": payload,
    }
