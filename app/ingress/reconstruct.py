"""Reconstruct IncomingMessage from durable inbox payload."""

from __future__ import annotations

from typing import Any

from pydantic import SecretStr

from app.models import IncomingMessage


def incoming_from_inbox_payload(payload: Any) -> IncomingMessage | None:
    data = payload
    if hasattr(payload, "obj"):
        data = payload.obj
    if not isinstance(data, dict):
        try:
            data = dict(data)
        except Exception:
            return None

    normalized = data.get("normalized")
    if not isinstance(normalized, dict):
        # Allow storing IncomingMessage dump at top level.
        if "channel" in data or "text" in data:
            normalized = data
        else:
            return None

    story_raw = normalized.get("instagram_story")
    story_obj = None
    if isinstance(story_raw, dict):
        try:
            from app.instagram_story_models import InstagramStoryContext
            from app.instagram_story_parser import safe_media_reference

            image_url = str(normalized.get("image_url") or "").strip() or None
            media_private = story_raw.get("story_media_url_private")
            if isinstance(media_private, dict):
                media_private = media_private.get("secret_value") or media_private.get(
                    "value"
                )
            media_url = str(media_private or "").strip() or image_url
            story_obj = InstagramStoryContext(
                provider=str(story_raw.get("provider") or "meta"),
                instagram_account_id=str(
                    story_raw.get("instagram_account_id") or ""
                ),
                story_media_id=story_raw.get("story_media_id"),
                story_message_id=story_raw.get("story_message_id"),
                story_permalink=story_raw.get("story_permalink"),
                story_media_url_private=(
                    SecretStr(media_url) if media_url else None
                ),
                story_media_log_reference=(
                    safe_media_reference(media_url) if media_url else None
                ),
                media_type=story_raw.get("media_type") or "unknown",
                replied_to_story=bool(story_raw.get("replied_to_story")),
                mentioned_in_story=bool(story_raw.get("mentioned_in_story")),
                raw_reference=(
                    story_raw.get("raw_reference")
                    if isinstance(story_raw.get("raw_reference"), dict)
                    else {}
                ),
            )
            if media_url and not normalized.get("image_url"):
                normalized = {**normalized, "image_url": media_url}
                if not normalized.get("attachment_type"):
                    normalized["attachment_type"] = "image"
        except Exception:
            story_obj = None

    clean = {
        key: value
        for key, value in normalized.items()
        if key != "instagram_story"
    }
    raw = clean.get("raw")
    if not isinstance(raw, dict):
        raw = data.get("raw") if isinstance(data.get("raw"), dict) else {}
    clean["raw"] = raw
    try:
        incoming = IncomingMessage.model_validate(clean)
    except Exception:
        return None
    if story_obj is not None:
        incoming.instagram_story = story_obj
    return incoming
