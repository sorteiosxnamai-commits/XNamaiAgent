"""Versioned normalization of Brevo Instagram Story webhook payloads.

Tenant from the external payload is never trusted.
Signed URLs stay in SecretStr; logs use SafeMediaReference only.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr

from .instagram_story_models import SafeMediaReference
from .instagram_story_parser import (
    extract_instagram_story_context,
    safe_media_reference,
    sanitize_instagram_story_reference,
)
from .observability import log_event

SCHEMA_VERSION = "story_event.v1"
MAX_RAW_PAYLOAD_BYTES = 256_000


class StoryEventType(str, Enum):
    REPLY_TO_STORY = "reply_to_story"
    STORY_MENTION = "story_mention"
    UNKNOWN = "unknown"
    MALFORMED = "malformed"


class NormalizedStoryMediaItem(BaseModel):
    index: int = 0
    media_id: str | None = None
    media_type: Literal["image", "video", "carousel", "unknown"] = "unknown"
    media_url_private: SecretStr | None = Field(default=None, exclude=True, repr=False)
    thumbnail_url_private: SecretStr | None = Field(default=None, exclude=True, repr=False)
    media_log_reference: SafeMediaReference | None = None
    sha256: str | None = None


class NormalizedStoryEvent(BaseModel):
    provider: Literal["brevo"] = "brevo"
    schema_version: str = SCHEMA_VERSION
    event_id: str
    event_type: StoryEventType = StoryEventType.UNKNOWN
    tenant_id: str | None = None  # never filled from external payload
    integration_account_id: str | None = None
    conversation_id: str | None = None
    contact_id: str | None = None
    message_id: str | None = None
    story_media_id: str | None = None
    story_url: SecretStr | None = Field(default=None, exclude=True, repr=False)
    media_type: Literal["image", "video", "carousel", "unknown"] = "unknown"
    media_items: list[NormalizedStoryMediaItem] = Field(default_factory=list)
    user_text: str | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    incomplete: bool = False
    incomplete_reasons: list[str] = Field(default_factory=list)
    dedupe_key: str = ""

    def operational_story_url(self) -> str | None:
        if self.story_url is None:
            return None
        return self.story_url.get_secret_value()


def _stable_event_id(payload: dict[str, Any], message: dict[str, Any] | None) -> str:
    mid = ""
    if isinstance(message, dict):
        mid = str(message.get("id") or message.get("messageId") or "")
    conv = str(payload.get("conversationId") or payload.get("conversation_id") or "")
    raw = f"brevo:{conv}:{mid}"
    if mid or conv:
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    blob = json.dumps(sanitize_instagram_story_reference(payload), sort_keys=True)[:2000]
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def normalize_brevo_story_event(
    payload: dict[str, Any],
    *,
    message: dict[str, Any] | None = None,
    channel: str = "instagram",
    visitor: dict[str, Any] | None = None,
) -> NormalizedStoryEvent:
    """Normalize a Brevo Conversations fragment into a versioned Story event."""
    if not isinstance(payload, dict):
        log_event("story_payload_unknown", {"reason": "not_object"})
        return NormalizedStoryEvent(
            event_id="malformed",
            event_type=StoryEventType.MALFORMED,
            incomplete=True,
            incomplete_reasons=["payload_not_object"],
            dedupe_key="malformed",
        )

    try:
        raw_size = len(json.dumps(payload, default=str).encode("utf-8"))
    except Exception:
        raw_size = MAX_RAW_PAYLOAD_BYTES + 1
    if raw_size > MAX_RAW_PAYLOAD_BYTES:
        log_event("story_payload_unknown", {"reason": "payload_too_large"})
        return NormalizedStoryEvent(
            event_id="oversized",
            event_type=StoryEventType.MALFORMED,
            incomplete=True,
            incomplete_reasons=["payload_too_large"],
            dedupe_key="oversized",
        )

    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    msg = message
    if msg is None:
        for candidate in messages:
            if isinstance(candidate, dict) and str(candidate.get("type") or "").casefold() in {
                "visitor",
                "inbound",
                "customer",
            }:
                msg = candidate
                break
        if msg is None and messages and isinstance(messages[0], dict):
            msg = messages[0]
    if not isinstance(msg, dict):
        msg = {}

    visitor_obj = visitor if isinstance(visitor, dict) else (
        payload.get("visitor") if isinstance(payload.get("visitor"), dict) else {}
    )
    ctx = extract_instagram_story_context(
        payload=payload,
        message=msg,
        channel=channel,
        visitor=visitor_obj,
    )
    event_id = _stable_event_id(payload, msg)
    occurred = datetime.now(timezone.utc)
    created = msg.get("createdAt") or msg.get("timestamp") or payload.get("createdAt")
    if isinstance(created, str) and created:
        try:
            occurred = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            pass

    if ctx is None:
        log_event(
            "story_payload_incomplete",
            {"event_id_prefix": event_id[:12], "reason": "no_story_context"},
        )
        return NormalizedStoryEvent(
            event_id=event_id,
            event_type=StoryEventType.UNKNOWN,
            integration_account_id=str(visitor_obj.get("sourceChannelRef") or "") or None,
            conversation_id=str(payload.get("conversationId") or "") or None,
            contact_id=str(visitor_obj.get("id") or "") or None,
            message_id=str(msg.get("id") or "") or None,
            user_text=str(msg.get("text") or "")[:500] or None,
            occurred_at=occurred,
            incomplete=True,
            incomplete_reasons=["no_story_context"],
            dedupe_key=event_id,
        )

    if ctx.mentioned_in_story:
        event_type = StoryEventType.STORY_MENTION
    elif ctx.replied_to_story:
        event_type = StoryEventType.REPLY_TO_STORY
    else:
        event_type = StoryEventType.UNKNOWN

    incomplete_reasons: list[str] = []
    if not ctx.story_media_id:
        incomplete_reasons.append("missing_media_id")
    if not ctx.operational_media_url() and not ctx.operational_thumbnail_url():
        incomplete_reasons.append("missing_media_url")

    items: list[NormalizedStoryMediaItem] = []
    for item in ctx.media_items or []:
        items.append(
            NormalizedStoryMediaItem(
                index=item.index,
                media_id=item.media_id,
                media_type=item.media_type if item.media_type in {"image", "video", "carousel", "unknown"} else "unknown",  # type: ignore[arg-type]
                media_url_private=item.media_url_private,
                thumbnail_url_private=item.thumbnail_url_private,
                media_log_reference=item.media_log_reference,
                sha256=item.sha256,
            )
        )
    if not items and (ctx.operational_media_url() or ctx.operational_thumbnail_url()):
        items.append(
            NormalizedStoryMediaItem(
                index=0,
                media_id=ctx.story_media_id,
                media_type=ctx.media_type,
                media_url_private=ctx.story_media_url_private,
                thumbnail_url_private=ctx.story_thumbnail_url_private,
                media_log_reference=ctx.story_media_log_reference or safe_media_reference(
                    ctx.operational_media_url()
                ),
            )
        )

    op_url = ctx.operational_media_url()
    event = NormalizedStoryEvent(
        event_id=event_id,
        event_type=event_type,
        tenant_id=None,  # resolved later via authenticated mapping
        integration_account_id=ctx.instagram_account_id or None,
        conversation_id=str(payload.get("conversationId") or "") or None,
        contact_id=str(visitor_obj.get("id") or "") or None,
        message_id=ctx.story_message_id or str(msg.get("id") or "") or None,
        story_media_id=ctx.story_media_id,
        story_url=SecretStr(op_url) if op_url else None,
        media_type=ctx.media_type,
        media_items=items,
        user_text=str(msg.get("text") or "")[:500] or None,
        occurred_at=occurred,
        incomplete=bool(incomplete_reasons),
        incomplete_reasons=incomplete_reasons,
        dedupe_key=f"{ctx.instagram_account_id}:{ctx.story_media_id}:{event_id}",
    )
    log_event(
        "story_payload_detected",
        {
            "event_type": event_type.value,
            "schema_version": SCHEMA_VERSION,
            "incomplete": event.incomplete,
            "media_type": event.media_type,
            "media_id_present": bool(event.story_media_id),
            "url_present": bool(op_url),
            "media_log": (
                event.media_items[0].media_log_reference.model_dump(mode="json")
                if event.media_items and event.media_items[0].media_log_reference
                else None
            ),
            "event_id_prefix": event_id[:12],
        },
    )
    return event
