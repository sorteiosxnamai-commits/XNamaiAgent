"""Extract and sanitize Instagram Story references from Brevo/Meta-shaped payloads.

Provider today: Brevo Conversations (omnichannel). Meta Graph fields may be
forwarded under reply_to / replyTo / attachments / nested meta blobs.
No real Story fixtures existed at implementation time — diagnostics mode
records field names/types only (never tokens or signed URLs).
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse, urlunparse

from .instagram_story_models import InstagramStoryContext, SafeMediaReference, StoryMediaItem
from .observability import log_event, redact_text


_SENSITIVE_KEY_RE = re.compile(
    r"(token|secret|password|authorization|signature|access_token|signed|sig|oh=)",
    re.I,
)
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:access_token|token|signature|sig|oh|oe|__a|hash)=)[^&]*"
)


def strip_signed_url(url: str | None) -> str | None:
    """Keep scheme+host+path only — for observability / SafeMediaReference.

    NEVER use the result for CDN download (signatures live in the query string).
    """
    if not url or not isinstance(url, str):
        return None
    text = url.strip()
    if not text:
        return None
    try:
        parsed = urlparse(text)
        if not parsed.scheme or not parsed.netloc:
            return None
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    except Exception:
        return None


def safe_media_reference(url: str | None) -> SafeMediaReference | None:
    if not url or not str(url).strip():
        return SafeMediaReference(present=False)
    cleaned = strip_signed_url(url)
    if not cleaned:
        return SafeMediaReference(present=True, host=None, path_hash=None)
    parsed = urlparse(cleaned)
    path_hash = hashlib.sha256((parsed.path or "").encode("utf-8")).hexdigest()[:16]
    return SafeMediaReference(
        present=True,
        host=parsed.hostname,
        path_hash=path_hash,
    )


def sanitize_instagram_story_reference(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep structural breadcrumbs only — no tokens, signed URLs, or PII blobs."""
    if not isinstance(payload, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in list(payload.items())[:40]:
        key_s = str(key)
        if _SENSITIVE_KEY_RE.search(key_s):
            out[key_s] = "[REDACTED]"
            continue
        if isinstance(value, str):
            if "://" in value:
                cleaned = strip_signed_url(value)
                out[key_s] = cleaned or "[url_redacted]"
            else:
                out[key_s] = redact_text(value, max_chars=80)
        elif isinstance(value, (int, float, bool)) or value is None:
            out[key_s] = value
        elif isinstance(value, dict):
            out[key_s] = {"_keys": sorted(str(k) for k in list(value.keys())[:30])}
        elif isinstance(value, list):
            out[key_s] = {"_len": len(value), "_item_types": sorted({type(x).__name__ for x in value[:8]})}
        else:
            out[key_s] = type(value).__name__
    return out


def diagnose_payload_structure(payload: dict[str, Any]) -> dict[str, Any]:
    """Field-name/type map for controlled diagnostics (no values)."""

    def walk(node: Any, path: str, depth: int = 0) -> list[dict[str, str]]:
        if depth > 5:
            return [{"path": path, "type": "truncated"}]
        if isinstance(node, dict):
            rows: list[dict[str, str]] = []
            for key, value in list(node.items())[:50]:
                key_s = str(key)
                if _SENSITIVE_KEY_RE.search(key_s):
                    rows.append({"path": f"{path}.{key_s}", "type": "redacted_key"})
                    continue
                child_path = f"{path}.{key_s}" if path else key_s
                rows.append({"path": child_path, "type": type(value).__name__})
                if isinstance(value, (dict, list)):
                    rows.extend(walk(value, child_path, depth + 1))
            return rows
        if isinstance(node, list):
            if not node:
                return [{"path": path, "type": "list[empty]"}]
            return walk(node[0], f"{path}[0]", depth + 1)
        return []

    return {
        "field_map": walk(payload, "root")[:200],
        "top_keys": sorted(str(k) for k in payload.keys())[:40] if isinstance(payload, dict) else [],
    }


def _first(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _parse_ts(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric >= 10_000_000_000:
            numeric /= 1000.0
        return datetime.fromtimestamp(numeric, tz=timezone.utc)
    text = str(value).strip()
    try:
        numeric = float(text)
        if numeric >= 10_000_000_000:
            numeric /= 1000.0
        return datetime.fromtimestamp(numeric, tz=timezone.utc)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _looks_story_source(value: Any) -> bool:
    text = str(value or "").casefold()
    return any(
        token in text
        for token in (
            "story",
            "stories",
            "ig_story",
            "story_reply",
            "story_mention",
            "reel_share",
        )
    )


def extract_instagram_story_context(
    *,
    payload: dict[str, Any],
    message: dict[str, Any],
    channel: str,
    visitor: dict[str, Any] | None = None,
) -> InstagramStoryContext | None:
    """Best-effort Story context from Brevo Conversations (+ nested Meta shapes)."""
    if channel != "instagram":
        return None

    visitor = visitor or {}
    reply_to = _as_dict(
        message.get("reply_to")
        or message.get("replyTo")
        or message.get("replied_to")
        or message.get("repliedTo")
        or _as_dict(message.get("meta")).get("reply_to")
        or _as_dict(message.get("raw")).get("reply_to")
    )
    story_blob = _as_dict(
        reply_to.get("story")
        or reply_to.get("Story")
        or message.get("story")
        or message.get("storyReply")
        or message.get("story_reply")
    )
    referral = _as_dict(
        message.get("referral")
        or payload.get("referral")
        or _as_dict(message.get("meta")).get("referral")
    )

    attachments = message.get("attachments")
    attachment_story: dict[str, Any] = {}
    mentioned = False
    if isinstance(attachments, list):
        for item in attachments:
            if not isinstance(item, dict):
                continue
            atype = str(item.get("type") or item.get("fileType") or "").casefold()
            if "story_mention" in atype or atype == "story":
                mentioned = True
                attachment_story = item
                break
            payload_att = _as_dict(item.get("payload"))
            if _looks_story_source(payload_att.get("source")) or _looks_story_source(atype):
                mentioned = True
                attachment_story = item
                break

    replied = bool(story_blob) or _looks_story_source(reply_to.get("type")) or bool(
        reply_to.get("story")
    )
    if referral and _looks_story_source(referral.get("source") or referral.get("type")):
        replied = True

    # Soft signal: visitor source link / integration attributes mentioning story.
    soft = _looks_story_source(
        visitor.get("source")
        or visitor.get("sourceChannelLink")
        or message.get("type")
        or message.get("messageType")
    )
    if not replied and not mentioned and not soft and not attachment_story:
        return None

    story_media_id = _first(
        story_blob.get("id"),
        story_blob.get("media_id"),
        story_blob.get("mediaId"),
        reply_to.get("mid"),
        reply_to.get("id"),
        referral.get("story_id"),
        referral.get("storyId"),
        referral.get("ads_context_data") if isinstance(referral.get("ads_context_data"), str) else None,
        _as_dict(attachment_story.get("payload")).get("id"),
        attachment_story.get("id"),
        message.get("storyId"),
        message.get("story_id"),
        message.get("storyMediaId"),
    )
    media_url = _first(
        story_blob.get("url"),
        story_blob.get("media_url"),
        story_blob.get("mediaUrl"),
        _as_dict(attachment_story.get("payload")).get("url"),
        attachment_story.get("link"),
        attachment_story.get("url"),
        message.get("storyUrl"),
        message.get("story_url"),
    )
    thumb = _first(
        story_blob.get("thumbnail_url"),
        story_blob.get("thumbnailUrl"),
        story_blob.get("preview_url"),
        _as_dict(attachment_story.get("payload")).get("thumbnail_url"),
    )
    permalink = _first(
        story_blob.get("permalink"),
        story_blob.get("permalink_url"),
        referral.get("ref"),
    )
    media_type_raw = str(
        story_blob.get("media_type")
        or story_blob.get("type")
        or attachment_story.get("type")
        or "unknown"
    ).casefold()
    if "video" in media_type_raw or "reel" in media_type_raw:
        media_type = "video"
    elif "carousel" in media_type_raw or "album" in media_type_raw:
        media_type = "carousel"
    elif "image" in media_type_raw or "photo" in media_type_raw or media_url:
        media_type = "image"
    else:
        media_type = "unknown"

    account_id = _first(
        visitor.get("sourceChannelRef"),
        _as_dict(payload.get("integration")).get("accountId"),
        _as_dict(payload.get("integration")).get("id"),
        payload.get("instagramAccountId"),
        payload.get("pageId"),
        "unknown",
    ) or "unknown"

    raw_ref = sanitize_instagram_story_reference(
        {
            "reply_to_keys": sorted(reply_to.keys()) if reply_to else [],
            "story_keys": sorted(story_blob.keys()) if story_blob else [],
            "referral_keys": sorted(referral.keys()) if referral else [],
            "attachment_type": attachment_story.get("type"),
            "replied": replied or soft,
            "mentioned": mentioned,
            "story_media_id_present": bool(story_media_id),
            "media_url_host": (urlparse(media_url).netloc if media_url else None),
        }
    )

    # Without a media id we still mark replied_to_story when soft/replied signals
    # exist so the service can ask for a print instead of inventing a product.
    if not story_media_id and (replied or mentioned or soft):
        # Stable synthetic id from message id so repository lookups stay idempotent
        # within the same inbound message (never invent a product from this alone).
        mid = _first(message.get("id"), message.get("messageId"), message.get("uuid"))
        if mid:
            digest = hashlib.sha256(f"{account_id}:{mid}".encode("utf-8")).hexdigest()[:24]
            story_media_id = f"synthetic:{digest}"

    if not story_media_id and not media_url and not (replied or mentioned):
        return None

    from pydantic import SecretStr

    items: list[StoryMediaItem] = []
    if media_type == "carousel":
        # Best-effort: Brevo may flatten carousel; keep primary as item 0.
        items.append(
            StoryMediaItem(
                index=0,
                media_id=story_media_id,
                media_type="image",
                media_url_private=SecretStr(media_url) if media_url else None,
                thumbnail_url_private=SecretStr(thumb) if thumb else None,
                media_log_reference=safe_media_reference(media_url),
                thumbnail_log_reference=safe_media_reference(thumb),
            )
        )
    elif media_url or thumb:
        items.append(
            StoryMediaItem(
                index=0,
                media_id=story_media_id,
                media_type=media_type,
                media_url_private=SecretStr(media_url) if media_url else None,
                thumbnail_url_private=SecretStr(thumb) if thumb else None,
                media_log_reference=safe_media_reference(media_url),
                thumbnail_log_reference=safe_media_reference(thumb),
            )
        )

    return InstagramStoryContext(
        provider="brevo",
        instagram_account_id=str(account_id),
        story_media_id=story_media_id,
        story_message_id=_first(message.get("id"), message.get("messageId")),
        story_permalink=strip_signed_url(permalink) if permalink else None,
        # CRITICAL: preserve full signed URL for download; never strip here.
        story_media_url_private=SecretStr(media_url) if media_url else None,
        story_thumbnail_url_private=SecretStr(thumb) if thumb else None,
        story_media_log_reference=safe_media_reference(media_url),
        story_thumbnail_log_reference=safe_media_reference(thumb),
        media_type=media_type,  # type: ignore[arg-type]
        replied_to_story=bool(replied or soft),
        mentioned_in_story=mentioned,
        source_timestamp=_parse_ts(
            story_blob.get("timestamp")
            or message.get("createdAt")
            or message.get("timestamp")
        ),
        expires_at=_parse_ts(story_blob.get("expires_at") or story_blob.get("expiresAt")),
        media_items=items,
        raw_reference=raw_ref,
    )


def maybe_log_story_payload_diagnostics(payload: dict[str, Any]) -> None:
    try:
        from .config import get_settings

        if not bool(getattr(get_settings(), "instagram_story_payload_diagnostics", False)):
            return
    except Exception:
        return
    diagnosis = diagnose_payload_structure(payload)
    log_event(
        "instagram_story.payload_diagnostics",
        {
            "top_keys": diagnosis.get("top_keys"),
            "field_count": len(diagnosis.get("field_map") or []),
            "sample_paths": [
                row.get("path") for row in (diagnosis.get("field_map") or [])[:40]
            ],
        },
    )
