"""YCloud WhatsApp transport adapter.

Transport only. This module moves bytes between YCloud and the existing
inbound/outbound pipeline: it verifies webhook signatures, normalizes an
inbound event into ``IncomingMessage``, checks the message was addressed to
this deployment, and posts a reply text that was already produced upstream.

It holds no business rule, no reply wording and no model call. The reply text
is forwarded byte for byte exactly as received.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, Literal

import httpx
from pydantic import BaseModel

from app.config import get_settings
from app.http_resilience import (
    NOT_SENT_EXCEPTIONS,
    classify_send_exception,
    classify_send_status,
    with_retries,
)
from app.models import AgentResult, IncomingMessage
from app.observability import log_event, log_exception
from app.repository import normalize_phone
from app.webhook_parser import build_sender_key


YCLOUD_INBOUND_EVENT = "whatsapp.inbound_message.received"
YCLOUD_STATUS_EVENT = "whatsapp.message.updated"

_SUPPORTED_MESSAGE_TYPES = frozenset({"text"})


def ycloud_webhook_enabled() -> bool:
    return bool(getattr(get_settings(), "ycloud_webhook_enabled", False))


def _clean(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    return text


def _e164(value: Any) -> str:
    digits = normalize_phone(str(value or ""))
    return f"+{digits}" if digits else ""


# ---------------------------------------------------------------------------
# Webhook signature — YCloud-Signature: t=<timestamp>,s=<hex sha256>
# signed payload is "<timestamp>." + the raw request body.
# ---------------------------------------------------------------------------


def parse_ycloud_signature_header(header: str | None) -> tuple[str, str]:
    """Return (timestamp, signature). Either is "" when the header is unusable."""
    timestamp = ""
    signature = ""
    for part in _clean(header).split(","):
        chunk = part.strip()
        if "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if key == "t":
            timestamp = value
        elif key == "s":
            signature = value
    return timestamp, signature


def verify_ycloud_signature(
    *,
    secret: str,
    body: bytes,
    signature_header: str | None,
) -> bool:
    """Timing-safe HMAC check over the raw bytes. Fail-closed on anything odd."""
    key = _clean(secret)
    if not key:
        return False

    timestamp, signature = parse_ycloud_signature_header(signature_header)
    if not timestamp or not signature:
        return False

    signed_payload = f"{timestamp}.".encode("utf-8") + (body or b"")
    expected = hmac.new(
        key.encode("utf-8"), signed_payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature.lower())


# ---------------------------------------------------------------------------
# Inbound
# ---------------------------------------------------------------------------


def ycloud_event_type(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    return _clean(payload.get("type"))


def parse_ycloud_inbound_message(payload: Any) -> IncomingMessage | None:
    """Normalize a YCloud inbound text event. Anything else returns None."""
    if not isinstance(payload, dict):
        return None
    if ycloud_event_type(payload) != YCLOUD_INBOUND_EVENT:
        return None

    message = payload.get("whatsappInboundMessage")
    if not isinstance(message, dict):
        return None

    message_id = _clean(message.get("id"))
    if not message_id:
        # Without a provider id there is no idempotency key: refuse it.
        return None

    message_type = _clean(message.get("type")).lower()
    if message_type not in _SUPPORTED_MESSAGE_TYPES:
        # Release 1 is text-only; media is skipped, never fatal.
        log_event(
            "ycloud.inbound.unsupported_type",
            {"message_type": message_type or None},
        )
        return None

    text_block = message.get("text")
    text = _clean(text_block.get("body")) if isinstance(text_block, dict) else ""
    if not text:
        return None

    sender_phone = normalize_phone(message.get("from"))
    if not sender_phone:
        return None

    profile = message.get("customerProfile")
    sender_name = _clean(profile.get("name")) if isinstance(profile, dict) else ""

    return IncomingMessage(
        provider="ycloud",
        event_type=YCLOUD_INBOUND_EVENT,
        message_id=message_id,
        channel="whatsapp",
        # Same shape Brevo produces, so one customer keeps one history when
        # the store runs both transports side by side.
        sender_key=build_sender_key("whatsapp", sender_phone, None, None, None),
        sender_external_id=_clean(message.get("fromUserId")) or None,
        conversation_id=f"wa:{sender_phone}",
        sender_phone=sender_phone,
        sender_name=sender_name or None,
        text=text,
        input_modality="text",
        channel_metadata={
            "ycloud_to": _clean(message.get("to")) or None,
            "ycloud_waba_id": _clean(message.get("wabaId")) or None,
            "ycloud_from_user_id": _clean(message.get("fromUserId")) or None,
            "ycloud_event_id": _clean(payload.get("id")) or None,
        },
        raw=message,
    )


# ---------------------------------------------------------------------------
# Deployment routing — never run the agent for someone else's number.
# ---------------------------------------------------------------------------


class YCloudTenantResolution(BaseModel):
    ok: bool = False
    tenant_id: str | None = None
    source: Literal[
        "business_number",
        "unverified_non_production",
        "unresolved",
    ] = "unresolved"
    failure_code: str | None = None


def resolve_ycloud_tenant(
    *,
    to: str | None,
    waba_id: str | None,
    settings: Any | None = None,
) -> YCloudTenantResolution:
    """Bind an unauthenticated inbound to this deployment via its own number."""
    cfg = settings or get_settings()

    configured_number = normalize_phone(_clean(getattr(cfg, "ycloud_whatsapp_from", "")))
    source: Literal["business_number", "unverified_non_production"]
    if configured_number:
        if normalize_phone(_clean(to)) != configured_number:
            return YCloudTenantResolution(failure_code="business_number_mismatch")
        source = "business_number"
    else:
        environment = _clean(getattr(cfg, "environment", "production")).lower()
        if environment == "production":
            return YCloudTenantResolution(
                failure_code="business_number_not_configured"
            )
        source = "unverified_non_production"

    configured_waba = _clean(getattr(cfg, "ycloud_waba_id", ""))
    if configured_waba and _clean(waba_id) and _clean(waba_id) != configured_waba:
        return YCloudTenantResolution(failure_code="waba_mismatch")

    tenant_id = _clean(getattr(cfg, "agent_persona_tenant_id", ""))
    if not tenant_id:
        return YCloudTenantResolution(failure_code="tenant_not_configured")

    return YCloudTenantResolution(ok=True, tenant_id=tenant_id, source=source)


# ---------------------------------------------------------------------------
# Status events — logged against the existing send record, no new state store.
# ---------------------------------------------------------------------------


def parse_ycloud_status_update(payload: Any) -> dict[str, Any] | None:
    """Summarize a delivery status event. Carries no phone numbers."""
    if not isinstance(payload, dict):
        return None
    if ycloud_event_type(payload) != YCLOUD_STATUS_EVENT:
        return None

    message = payload.get("whatsappMessage")
    if not isinstance(message, dict):
        return None

    return {
        "message_id": _clean(message.get("id")) or None,
        "wamid": _clean(message.get("wamid")) or None,
        "status": _clean(message.get("status")).lower() or None,
        "has_sender": bool(_clean(message.get("from"))),
        "has_recipient": bool(_clean(message.get("to"))),
        "external_id": _clean(message.get("externalId")) or None,
        "event_id": _clean(payload.get("id")) or None,
        "error_code": _clean(message.get("errorCode")) or None,
    }


def ycloud_status_recipient(payload: Any) -> str | None:
    """Recipient of a status event, for reconciliation checks only (never logged)."""
    message = payload.get("whatsappMessage") if isinstance(payload, dict) else None
    if not isinstance(message, dict):
        return None
    return _clean(message.get("to")) or None


def ycloud_signature_age_seconds(signature_header: str | None, *, now: float | None = None) -> float | None:
    """Age of the signed timestamp (negative = in the future). None if unusable."""
    import time

    timestamp, _signature = parse_ycloud_signature_header(signature_header)
    try:
        signed_at = float(timestamp)
    except (TypeError, ValueError):
        return None
    if signed_at > 1e12:  # milliseconds
        signed_at /= 1000.0
    return (time.time() if now is None else now) - signed_at


# ---------------------------------------------------------------------------
# Outbound
# ---------------------------------------------------------------------------


def _error_for_status(status_code: int) -> str:
    if status_code == 400:
        return "ycloud_bad_request"
    if status_code in {401, 403}:
        return "ycloud_unauthorized"
    if status_code == 429:
        return "ycloud_rate_limited"
    if status_code >= 500:
        return "ycloud_server_error"
    return "ycloud_send_failed"


async def send_ycloud_reply(
    incoming: IncomingMessage,
    result: AgentResult | str,
) -> dict[str, Any]:
    """Post an already-generated reply to YCloud. The text is never rewritten."""
    settings = get_settings()
    text = result.reply_text if isinstance(result, AgentResult) else str(result)
    recipient = _e164(incoming.sender_phone)

    if bool(getattr(settings, "dry_run", True)):
        log_event(
            "ycloud.outbound.dry_run",
            {"recipient_present": bool(recipient), "reply_chars": len(text or "")},
        )
        return {
            "ok": True,
            "dry_run": True,
            "provider": "ycloud",
            "provider_response": {"skipped": "dry_run"},
        }

    api_key = _clean(getattr(settings, "ycloud_api_key", ""))
    if not api_key:
        return {"ok": False, "provider": "ycloud", "error": "ycloud_api_key_missing"}

    sender = _e164(getattr(settings, "ycloud_whatsapp_from", ""))
    if not sender:
        return {"ok": False, "provider": "ycloud", "error": "ycloud_sender_missing"}

    if not recipient:
        return {"ok": False, "provider": "ycloud", "error": "ycloud_recipient_missing"}

    base_url = _clean(getattr(settings, "ycloud_base_url", "")) or (
        "https://api.ycloud.com/v2"
    )
    url = f"{base_url.rstrip('/')}/whatsapp/messages"
    payload = {
        "from": sender,
        "to": recipient,
        "type": "text",
        "text": {"body": text},
    }
    # Correlation only: YCloud echoes externalId in status webhooks but does
    # NOT deduplicate on it (no idempotency key is documented).
    correlation_id = (
        (result.response_metadata or {}).get("outbox_correlation_id")
        if isinstance(result, AgentResult)
        else None
    )
    if correlation_id:
        payload["externalId"] = str(correlation_id)[:128]
    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    async def _post() -> Any:
        async with httpx.AsyncClient(timeout=20) as client:
            return await client.post(url, json=payload, headers=headers)

    try:
        # Repeat the POST only when it provably never reached YCloud; a read
        # timeout may mean the message was accepted, and a repeat duplicates it.
        response = await with_retries(_post, retry_exceptions=NOT_SENT_EXCEPTIONS)
    except Exception as exc:  # noqa: BLE001 — outbound must not crash the turn
        outcome = classify_send_exception(exc)
        log_exception(
            "ycloud.outbound.network_failed",
            exc,
            {"recipient_present": True, "reply_chars": len(text or ""), "outcome": outcome},
        )
        return {
            "ok": False,
            "provider": "ycloud",
            "error": "ycloud_delivery_unknown" if outcome == "unknown" else "ycloud_network_error",
            "error_type": type(exc).__name__,
            "delivery_unknown": outcome == "unknown",
        }

    try:
        body: dict[str, Any] = response.json()
    except Exception:  # noqa: BLE001 — providers do return HTML on 5xx
        body = {"text": (getattr(response, "text", "") or "")[:500]}
    if not isinstance(body, dict):
        body = {"value": body}

    status_code = int(getattr(response, "status_code", 0) or 0)
    ok = 200 <= status_code < 300
    if not ok:
        error = _error_for_status(status_code)
        outcome = classify_send_status(status_code)
        log_event(
            "ycloud.outbound.failed",
            {"status_code": status_code, "error": error, "outcome": outcome},
        )
        return {
            "ok": False,
            "provider": "ycloud",
            "error": error,
            "status_code": status_code,
            "provider_response": body,
            "permanent": outcome == "permanent",
            "delivery_unknown": outcome == "unknown",
        }

    log_event(
        "ycloud.outbound.accepted",
        {
            "status_code": status_code,
            "reply_chars": len(text or ""),
            "has_wamid": bool(body.get("wamid")),
        },
    )
    return {
        "ok": True,
        "provider": "ycloud",
        "dry_run": False,
        "status_code": status_code,
        "provider_message_id": _clean(body.get("id")) or None,
        "wamid": _clean(body.get("wamid")) or None,
        "status": _clean(body.get("status")).lower() or None,
        "provider_response": body,
    }
