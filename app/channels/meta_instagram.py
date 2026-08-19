"""Meta Instagram Messaging webhook adapter (FASE 3).

Requires META_* env vars. Signature verification uses X-Hub-Signature-256.
Media URLs are fetched via Graph when not present on the attachment.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from app.config import get_settings
from app.models import AgentResult, IncomingMessage
from app.observability import log_event


def _agent_debug_log(
    *,
    hypothesis_id: str,
    location: str,
    message: str,
    data: dict[str, Any],
) -> None:
    # #region agent log
    try:
        import time
        from pathlib import Path

        payload = {
            "sessionId": "38b290",
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        Path("debug-38b290.log").open("a", encoding="utf-8").write(
            json.dumps(payload, ensure_ascii=False) + "\n"
        )
        log_event(
            "debug.meta",
            {"hypothesisId": hypothesis_id, "message": message, **data},
        )
    except Exception:
        pass
    # #endregion


def payload_skeleton(value: Any, *, depth: int = 0) -> Any:
    """PII-free nested key/type map of a Meta webhook payload."""
    if depth > 6:
        return "max_depth"
    if isinstance(value, dict):
        return {
            str(key)[:48]: payload_skeleton(item, depth=depth + 1)
            for key, item in list(value.items())[:24]
        }
    if isinstance(value, list):
        return [payload_skeleton(item, depth=depth + 1) for item in value[:8]]
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit() and len(stripped) >= 6:
            return f"id_len:{len(stripped)}"
        return f"str:{len(stripped)}"
    if value is None or isinstance(value, (bool, int, float)):
        return type(value).__name__
    return type(value).__name__


def instagram_event_skip_reason(event: dict[str, Any]) -> str:
    if _normalize_instagram_event(event):
        return "parsed"
    raw_message = event.get("message")
    if isinstance(raw_message, dict) and raw_message.get("is_echo"):
        return "echo"
    if "read" in event and not event.get("message"):
        return "read_receipt"
    if "delivery" in event and not event.get("message"):
        return "delivery"
    edit = event.get("message_edit") if isinstance(event.get("message_edit"), dict) else {}
    if edit:
        has_text = bool(str(edit.get("text") or "").strip())
        nested_sender = edit.get("sender") if isinstance(edit.get("sender"), dict) else {}
        nested_from = edit.get("from") if isinstance(edit.get("from"), dict) else {}
        has_sender = bool(str(nested_sender.get("id") or nested_from.get("id") or "").strip())
        if not has_text:
            return "message_edit_no_text"
        if not has_sender:
            return "message_edit_no_sender"
        return "message_edit_unparsed"
    if not event.get("message") and not str(event.get("text") or "").strip():
        return "no_inbound_message"
    return "missing_sender"


async def probe_instagram_graph_subscriptions() -> dict[str, Any]:
    """Runtime check: which webhook fields the IG token is subscribed to."""
    import httpx

    settings = get_settings()
    token = str(getattr(settings, "meta_page_access_token", "") or "").strip()
    if not token:
        return {"ok": False, "error": "meta_page_access_token_missing"}
    result: dict[str, Any] = {"ok": False}
    try:
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=6.0) as client:
            me = await client.get(
                "https://graph.instagram.com/v21.0/me",
                params={"fields": "id,user_id,username"},
                headers=headers,
            )
            subs = await client.get(
                "https://graph.instagram.com/v21.0/me/subscribed_apps",
                headers=headers,
            )
            convos = await client.get(
                "https://graph.instagram.com/v21.0/me/conversations",
                params={"fields": "id,updated_time", "limit": "3", "platform": "instagram"},
                headers=headers,
            )
        me_json = me.json() if me.content else {}
        subs_json = subs.json() if subs.content else {}
        convos_json = convos.json() if convos.content else {}
        fields: list[str] = []
        app_ids: list[int] = []
        data = subs_json.get("data") if isinstance(subs_json, dict) else None
        if isinstance(data, list):
            for item in data[:6]:
                if not isinstance(item, dict):
                    continue
                app_id = str(item.get("id") or "").strip()
                if app_id:
                    app_ids.append(len(app_id))
                raw_fields = item.get("subscribed_fields")
                if isinstance(raw_fields, list):
                    fields.extend(str(field)[:48] for field in raw_fields[:20])
                elif isinstance(raw_fields, str) and raw_fields.strip():
                    fields.extend(part.strip()[:48] for part in raw_fields.split(",")[:20])
        me_error = me_json.get("error") if isinstance(me_json, dict) else None
        subs_error = subs_json.get("error") if isinstance(subs_json, dict) else None
        convo_error = convos_json.get("error") if isinstance(convos_json, dict) else None
        convo_data = convos_json.get("data") if isinstance(convos_json, dict) else None
        result = {
            "ok": me.status_code == 200 and subs.status_code == 200,
            "me_status": me.status_code,
            "subs_status": subs.status_code,
            "conversations_status": convos.status_code,
            "conversations_count": len(convo_data) if isinstance(convo_data, list) else 0,
            "conversations_error_code": (
                convo_error.get("code") if isinstance(convo_error, dict) else None
            ),
            "me_id_len": len(str(me_json.get("id") or "")) if isinstance(me_json, dict) else 0,
            "me_has_username": bool(
                isinstance(me_json, dict) and str(me_json.get("username") or "").strip()
            ),
            "subscribed_fields": sorted(set(fields)),
            "subscribed_app_count": len(app_ids),
            "has_messages_field": "messages" in fields,
            "has_standby_field": "standby" in fields,
            "has_handover_field": "messaging_handover" in fields,
            "me_error_code": (
                me_error.get("code") if isinstance(me_error, dict) else None
            ),
            "subs_error_code": (
                subs_error.get("code") if isinstance(subs_error, dict) else None
            ),
            "subs_error_type": (
                str(subs_error.get("type") or "")[:48]
                if isinstance(subs_error, dict)
                else ""
            ),
        }
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "error": type(exc).__name__}
    _agent_debug_log(
        hypothesis_id="B",
        location="meta_instagram.py:probe_instagram_graph_subscriptions",
        message="graph_subscribed_apps",
        data=result,
    )
    return result


def meta_webhook_enabled() -> bool:
    settings = get_settings()
    return bool(getattr(settings, "meta_webhook_enabled", False))


def _normalize_meta_secret(value: str | None) -> str:
    cleaned = (value or "").strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {'"', "'"}:
        cleaned = cleaned[1:-1].strip()
    return cleaned


def verify_meta_signature(*, app_secret: str, body: bytes, signature_header: str | None) -> bool:
    return verify_meta_signatures(
        app_secrets=[app_secret],
        body=body,
        signature_header_sha256=signature_header,
        signature_header_sha1=None,
    )


def verify_meta_signatures(
    *,
    app_secrets: list[str],
    body: bytes,
    signature_header_sha256: str | None,
    signature_header_sha1: str | None = None,
) -> bool:
    """HMAC-verify Meta webhook signatures.

    Instagram Login often signs with the Instagram app secret, which can differ
    from the Facebook app secret. Also accept legacy X-Hub-Signature (sha1).
    """
    secrets = [_normalize_meta_secret(secret) for secret in app_secrets]
    secrets = [secret for secret in secrets if secret]
    if not secrets:
        return False

    sha256_header = (signature_header_sha256 or "").strip()
    sha1_header = (signature_header_sha1 or "").strip()
    provided_sha256 = _signature_digest(sha256_header, "sha256")
    provided_sha1 = _signature_digest(sha1_header, "sha1")

    for secret in secrets:
        key = secret.encode("utf-8")
        if provided_sha256 and _digests_match(
            hmac.new(key, body, hashlib.sha256).hexdigest(),
            provided_sha256,
        ):
            return True
        if provided_sha1 and _digests_match(
            hmac.new(key, body, hashlib.sha1).hexdigest(),
            provided_sha1,
        ):
            return True
    return False


def _signature_digest(header: str, algorithm: str) -> str:
    if not header:
        return ""
    prefix = f"{algorithm}="
    if header.lower().startswith(prefix):
        return header.split("=", 1)[1].strip()
    return header


def _digests_match(expected_hex: str, provided: str) -> bool:
    left = (expected_hex or "").strip().lower()
    right = (provided or "").strip().lower()
    if not left or not right or len(left) != len(right):
        return False
    try:
        return hmac.compare_digest(left, right)
    except ValueError:
        return False


def handle_meta_verify_challenge(
    *,
    mode: str | None,
    verify_token: str | None,
    challenge: str | None,
    expected_token: str,
) -> str | None:
    if mode != "subscribe":
        return None
    if not expected_token or verify_token != expected_token:
        return None
    return challenge or ""


def messaging_event_shapes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """PII-free shape of Meta messaging events, for webhook diagnostics."""
    shapes: list[dict[str, Any]] = []
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return shapes
    for entry in entries[:4]:
        if not isinstance(entry, dict):
            continue
        for event in _instagram_messaging_events(entry)[:8]:
            raw_message = event.get("message")
            edit = event.get("message_edit") if isinstance(event.get("message_edit"), dict) else {}
            text_len = 0
            if isinstance(raw_message, dict):
                text_len = len(str(raw_message.get("text") or ""))
            elif isinstance(raw_message, str):
                text_len = len(raw_message.strip())
            else:
                text_len = len(str(event.get("text") or "").strip())
            edit_text_len = len(str(edit.get("text") or "").strip())
            sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
            from_obj = event.get("from") if isinstance(event.get("from"), dict) else {}
            edit_from = edit.get("from") if isinstance(edit.get("from"), dict) else {}
            edit_sender = edit.get("sender") if isinstance(edit.get("sender"), dict) else {}
            shapes.append(
                {
                    "keys": sorted(str(key) for key in event.keys())[:16],
                    "has_sender_id": bool(str(sender.get("id") or "").strip()),
                    "has_from_id": bool(str(from_obj.get("id") or "").strip()),
                    "message_type": type(raw_message).__name__,
                    "is_echo": isinstance(raw_message, dict) and bool(raw_message.get("is_echo")),
                    "text_len": text_len,
                    "edit_keys": sorted(str(key) for key in edit.keys())[:12],
                    "edit_text_len": edit_text_len,
                    "edit_has_sender": bool(
                        str(edit_sender.get("id") or edit_from.get("id") or "").strip()
                    ),
                    "has_read": "read" in event,
                    "has_delivery": "delivery" in event,
                    "has_reaction": "reaction" in event,
                    "has_postback": "postback" in event,
                }
            )
    return shapes


def _instagram_messaging_events(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect messaging events from Messenger-style and Instagram changes payloads."""
    events: list[dict[str, Any]] = []
    for key in ("messaging", "standby"):
        block = entry.get(key)
        if isinstance(block, list):
            events.extend(item for item in block if isinstance(item, dict))

    changes = entry.get("changes")
    if not isinstance(changes, list):
        return events
    for change in changes:
        if not isinstance(change, dict):
            continue
        field = str(change.get("field") or "").strip().lower()
        if field and field not in {"messages", "messaging", "message", "standby"}:
            continue
        value = change.get("value")
        if isinstance(value, list):
            events.extend(item for item in value if isinstance(item, dict))
            continue
        if not isinstance(value, dict):
            continue
        nested = value.get("messaging") or value.get("standby")
        if isinstance(nested, list):
            events.extend(item for item in nested if isinstance(item, dict))
        elif any(key in value for key in ("sender", "from", "message", "text")):
            events.append(value)
    return events


def _normalize_instagram_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """Map Messenger-style and Instagram Login payloads onto one shape."""
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    if not sender and isinstance(event.get("from"), dict):
        sender = event["from"]
    recipient = event.get("recipient") if isinstance(event.get("recipient"), dict) else {}
    if not recipient and isinstance(event.get("to"), dict):
        recipient = event["to"]

    raw_message = event.get("message")
    if isinstance(raw_message, dict):
        message = dict(raw_message)
    elif isinstance(raw_message, str) and raw_message.strip():
        message = {
            "mid": str(event.get("id") or event.get("mid") or "").strip(),
            "text": raw_message.strip(),
        }
    else:
        message = {}
        text = str(event.get("text") or "").strip()
        if text:
            message = {
                "mid": str(event.get("id") or event.get("mid") or "").strip(),
                "text": text,
            }

    if not message or message.get("is_echo"):
        return None
    sender_id = str(sender.get("id") or "").strip()
    if not sender_id:
        return None
    return {
        "sender": sender,
        "recipient": recipient,
        "message": message,
        "sender_id": sender_id,
        "recipient_id": str(recipient.get("id") or "").strip(),
    }


def parse_meta_instagram_messaging(payload: dict[str, Any]) -> list[IncomingMessage]:
    """Normalize Meta Instagram messaging webhooks into IncomingMessage list."""
    settings = get_settings()
    account_id = str(getattr(settings, "meta_ig_business_account_id", "") or "").strip()
    messages: list[IncomingMessage] = []
    entries = payload.get("entry")
    if not isinstance(entries, list):
        log_event(
            "meta.instagram.parsed",
            {"messages": 0, "entries": 0, "reason": "no_entry"},
        )
        return messages

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        messaging = _instagram_messaging_events(entry)
        for event in messaging:
            if not isinstance(event, dict):
                continue
            skip_reason = instagram_event_skip_reason(event)
            if skip_reason != "parsed":
                _agent_debug_log(
                    hypothesis_id="A",
                    location="meta_instagram.py:parse_meta_instagram_messaging",
                    message="event_skipped",
                    data={
                        "reason": skip_reason,
                        "event_keys": sorted(str(key) for key in event.keys())[:16],
                        "skeleton": payload_skeleton(event),
                        "has_standby_entry": "standby" in entry,
                    },
                )
            normalized = _normalize_instagram_event(event)
            if not normalized:
                continue
            sender = normalized["sender"]
            recipient = normalized["recipient"]
            message = normalized["message"]
            sender_id = normalized["sender_id"]
            recipient_id = normalized["recipient_id"] or account_id
            message_id = str(message.get("mid") or "").strip() or None
            text = str(message.get("text") or "").strip()
            image_url = None
            attachment_type = None
            input_modality = "text"
            attachments = message.get("attachments")
            if isinstance(attachments, list):
                for attachment in attachments:
                    if not isinstance(attachment, dict):
                        continue
                    atype = str(attachment.get("type") or "").lower()
                    payload_obj = (
                        attachment.get("payload")
                        if isinstance(attachment.get("payload"), dict)
                        else {}
                    )
                    url = str(payload_obj.get("url") or "").strip() or None
                    if atype in {"image", "story_mention"} and url:
                        image_url = url
                        attachment_type = "image"
                        input_modality = "text_with_image" if text else "image"
                        break
                    if atype == "video" and url:
                        # Treated as media for Story/video pipeline later.
                        image_url = url
                        attachment_type = "video"
                        input_modality = "text_with_image" if text else "image"
                        break

            story_ctx = None
            reply_to = message.get("reply_to")
            if isinstance(reply_to, dict) and isinstance(reply_to.get("story"), dict):
                from pydantic import SecretStr

                from app.instagram_story_models import InstagramStoryContext
                from app.instagram_story_parser import safe_media_reference

                story = reply_to["story"]
                story_url = str(story.get("url") or "").strip() or None
                story_ctx = InstagramStoryContext(
                    replied_to_story=True,
                    mentioned_in_story=False,
                    story_media_id=str(story.get("id") or "").strip() or None,
                    story_media_url_private=(
                        SecretStr(story_url) if story_url else None
                    ),
                    story_media_log_reference=(
                        safe_media_reference(story_url) if story_url else None
                    ),
                    media_type="image",
                    provider="meta",
                    instagram_account_id=recipient_id or "",
                    raw_reference={"reply_to": reply_to},
                )
                if story_url and not image_url:
                    image_url = story_url
                    attachment_type = attachment_type or "image"
                    input_modality = "text_with_image" if text else "image"

            if not text and not image_url:
                continue

            if not text and image_url:
                text = "[Imagem recebida via Instagram]"

            messages.append(
                IncomingMessage(
                    provider="meta",
                    channel="instagram",
                    event_type="meta_messaging",
                    message_id=message_id,
                    conversation_id=f"ig:{sender_id}" if sender_id else None,
                    visitor_id=sender_id or None,
                    sender_key=f"instagram:{sender_id}" if sender_id else None,
                    sender_external_id=sender_id or None,
                    text=text,
                    image_url=image_url,
                    attachment_type=attachment_type,
                    input_modality=input_modality,
                    instagram_story=story_ctx,
                    raw={"meta_event": event, "entry_id": entry.get("id")},
                )
            )

    log_event(
        "meta.instagram.parsed",
        {
            "messages": len(messages),
            "entries": len(entries),
            "object": str(payload.get("object") or "")[:32],
        },
    )
    return messages


async def send_meta_instagram_reply(
    incoming: IncomingMessage,
    result: AgentResult,
) -> dict[str, Any]:
    """Send text reply via Instagram Messaging Graph API."""
    import httpx

    settings = get_settings()
    token = str(getattr(settings, "meta_page_access_token", "") or "").strip()
    if not token:
        return {"ok": False, "error": "meta_page_access_token_missing"}
    recipient_id = (
        incoming.sender_external_id
        or (incoming.sender_key or "").split(":", 1)[-1]
        or incoming.visitor_id
    )
    if not recipient_id:
        return {"ok": False, "error": "meta_recipient_missing"}

    ig_account_id = str(
        getattr(settings, "meta_ig_business_account_id", "") or ""
    ).strip()
    text = (result.reply_text or "")[:2000]
    payload = {
        "recipient": {"id": recipient_id},
        "messaging_type": "RESPONSE",
        "message": {"text": text},
    }

    # Instagram Login tokens (IGAA...) use graph.instagram.com /me first.
    # The dashboard IG Business ID (17841…) is not always the send path ID.
    endpoints: list[str] = []
    if token.startswith("IGAA"):
        endpoints.append("https://graph.instagram.com/v21.0/me/messages")
        if ig_account_id:
            endpoints.append(
                f"https://graph.instagram.com/v21.0/{ig_account_id}/messages"
            )
    elif ig_account_id:
        endpoints.append(
            f"https://graph.instagram.com/v21.0/{ig_account_id}/messages"
        )
        endpoints.append("https://graph.instagram.com/v21.0/me/messages")
    endpoints.append("https://graph.facebook.com/v21.0/me/messages")

    last_body: dict[str, Any] = {}
    last_status = 0
    async with httpx.AsyncClient(timeout=20) as client:
        for url in endpoints:
            resp = await client.post(
                url,
                params={"access_token": token},
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
            last_status = resp.status_code
            try:
                last_body = resp.json()
            except Exception:
                last_body = {"raw": (resp.text or "")[:200]}
            if resp.status_code < 300:
                log_event(
                    "meta.instagram.send",
                    {
                        "ok": True,
                        "status_code": resp.status_code,
                        "endpoint_host": url.split("/")[2],
                        "recipient_present": True,
                    },
                )
                return {
                    "ok": True,
                    "status_code": resp.status_code,
                    "provider_response": last_body,
                    "endpoint": url.split("/v21.0/", 1)[-1],
                }

            error_code = None
            error_type = None
            if isinstance(last_body.get("error"), dict):
                error_code = last_body["error"].get("code")
                error_type = str(last_body["error"].get("type") or "")[:40]
            log_event(
                "meta.instagram.send",
                {
                    "ok": False,
                    "status_code": last_status,
                    "recipient_present": True,
                    "error_code": error_code,
                    "error_type": error_type,
                },
            )
            print(
                "[meta.instagram.send]",
                {
                    "ok": False,
                    "status_code": last_status,
                    "error_code": error_code,
                    "error_type": error_type,
                    "error_message": str(
                        (last_body.get("error") or {}).get("message") or ""
                    )[:180],
                },
            )
    return {
        "ok": False,
        "status_code": last_status,
        "provider_response": last_body,
        "error": "meta_send_failed",
    }
