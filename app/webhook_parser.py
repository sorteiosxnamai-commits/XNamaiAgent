from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .audio_service import extract_audio_attachment, is_audio_attachment, is_placeholder_audio_text
from .models import IncomingMessage


CHANNEL_ALIASES = {
    "ig": "instagram",
    "instagram_direct": "instagram",
    "instagram_dm": "instagram",
    "facebook_messenger": "facebook",
    "messenger": "facebook",
    "fb": "facebook",
    "wa": "whatsapp",
    "whats_app": "whatsapp",
    "website": "widget",
    "chat": "widget",
}


def normalize_channel(value: str | None) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not normalized:
        return "unknown"
    return CHANNEL_ALIASES.get(normalized, normalized)


def build_sender_key(
    channel: str,
    sender_phone: str | None,
    source_conversation_ref: str | None,
    visitor_id: str | None,
    conversation_id: str | None,
) -> str | None:
    normalized_channel = normalize_channel(channel)
    phone_digits = "".join(character for character in str(sender_phone or "") if character.isdigit())
    if normalized_channel == "whatsapp" and phone_digits:
        return f"whatsapp:{phone_digits}"
    if normalized_channel in {"instagram", "facebook"}:
        identity = source_conversation_ref or visitor_id
        return f"{normalized_channel}:{identity}" if identity else (
            f"conversation:{conversation_id}" if conversation_id else None
        )
    if visitor_id:
        return f"{normalized_channel}:{visitor_id}"
    if conversation_id:
        return f"conversation:{conversation_id}"
    return None


def _get_nested(data: dict[str, Any], *path: str) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first_non_empty(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
        if not isinstance(value, str):
            text = str(value).strip()
            if text:
                return text
    return None


def _message_type(message: dict[str, Any]) -> str:
    value = _first_non_empty(
        message.get("type"),
        message.get("role"),
        message.get("senderType"),
        message.get("authorType"),
        message.get("direction"),
    )
    return (value or "").lower()


def _is_visitor_message(message: dict[str, Any]) -> bool:
    return _message_type(message) in {"visitor", "client", "customer", "user", "inbound"}


def _message_id(message: dict[str, Any]) -> str | None:
    return _first_non_empty(
        message.get("id"),
        message.get("sourceMessageId"),
        message.get("messageId"),
        message.get("message_id"),
        message.get("uuid"),
    )


def _message_timestamp(message: dict[str, Any]) -> float | None:
    for field in ("createdAt", "created_at", "timestamp", "date", "updatedAt"):
        value = message.get(field)
        if value is None or value == "":
            continue
        if isinstance(value, (int, float)):
            numeric = float(value)
            return numeric / 1000 if numeric >= 10_000_000_000 else numeric
        if isinstance(value, str):
            text = value.strip()
            try:
                numeric = float(text)
                return numeric / 1000 if numeric >= 10_000_000_000 else numeric
            except ValueError:
                try:
                    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    return parsed.timestamp()
                except ValueError:
                    continue
    return None


def select_effective_inbound_message(payload: dict[str, Any]) -> dict[str, Any]:
    """Select the chronologically newest fragment item, regardless of array order."""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        primary = payload.get("message")
        return primary if isinstance(primary, dict) else {}

    valid_messages = [message for message in messages if isinstance(message, dict)]
    if not valid_messages:
        return {}

    timestamped = [
        (timestamp, index, message)
        for index, message in enumerate(valid_messages)
        if (timestamp := _message_timestamp(message)) is not None
    ]
    if timestamped:
        selected = max(timestamped, key=lambda item: (item[0], item[1]))[2]
    else:
        selected = valid_messages[-1]
    return _merge_sibling_image_caption(valid_messages, selected)


def _message_has_image_attachment(message: dict[str, Any]) -> bool:
    for attachment in _attachment_candidates(message):
        if _attachment_type(attachment) in {"image", "sticker"}:
            return True
    return False


def _visitor_text(message: dict[str, Any]) -> str:
    return (
        _first_non_empty(
            message.get("text") if isinstance(message.get("text"), str) else None,
            message.get("body") if isinstance(message.get("body"), str) else None,
            _get_nested(message, "text", "body"),
        )
        or ""
    ).strip()


def _sibling_gap_seconds(
    selected: dict[str, Any],
    candidate: dict[str, Any],
    messages: list[dict[str, Any]],
    candidate_index: int,
    *,
    max_gap_seconds: float,
) -> float | None:
    selected_ts = _message_timestamp(selected)
    candidate_ts = _message_timestamp(candidate)
    if selected_ts is not None and candidate_ts is not None:
        gap = abs(selected_ts - candidate_ts)
        return gap if gap <= max_gap_seconds else None
    selected_index = next(
        (i for i, item in enumerate(messages) if item is selected),
        -1,
    )
    if abs(selected_index - candidate_index) > 1:
        return None
    return 0.0


def _merge_sibling_image_caption(
    messages: list[dict[str, Any]],
    selected: dict[str, Any],
    *,
    max_gap_seconds: float = 2.0,
) -> dict[str, Any]:
    """Merge near-simultaneous visitor image + caption siblings in one payload."""
    if not selected or not _is_visitor_message(selected):
        return selected

    selected_has_image = _message_has_image_attachment(selected)
    selected_text = _visitor_text(selected)

    # Case A: newest item is text-only; pull image attachment from sibling.
    if not selected_has_image:
        if not selected_text:
            return selected
        best: dict[str, Any] | None = None
        best_gap: float | None = None
        for index, candidate in enumerate(messages):
            if candidate is selected or not _is_visitor_message(candidate):
                continue
            if not _message_has_image_attachment(candidate):
                continue
            gap = _sibling_gap_seconds(
                selected,
                candidate,
                messages,
                index,
                max_gap_seconds=max_gap_seconds,
            )
            if gap is None:
                continue
            if best_gap is None or gap < best_gap:
                best = candidate
                best_gap = gap
        if best is None:
            return selected
        merged = dict(best)
        merged["text"] = selected_text
        selected_id = _message_id(selected)
        if selected_id:
            merged["id"] = selected_id
            merged["messageId"] = selected_id
        return merged

    # Case B: newest item is image without real caption; pull text from sibling.
    if selected_text:
        return selected
    best_text: str | None = None
    best_gap = None
    for index, candidate in enumerate(messages):
        if candidate is selected or not _is_visitor_message(candidate):
            continue
        if _message_has_image_attachment(candidate):
            continue
        caption = _visitor_text(candidate)
        if not caption:
            continue
        gap = _sibling_gap_seconds(
            selected,
            candidate,
            messages,
            index,
            max_gap_seconds=max_gap_seconds,
        )
        if gap is None:
            continue
        if best_gap is None or gap < best_gap:
            best_text = caption
            best_gap = gap
    if not best_text:
        return selected
    merged = dict(selected)
    merged["text"] = best_text
    return merged


def selected_message_info(payload: dict[str, Any], message: dict[str, Any] | None = None) -> dict[str, Any]:
    selected = message if message is not None else select_effective_inbound_message(payload)
    return {
        "role": _message_type(selected) or None,
        "timestamp_present": _message_timestamp(selected) is not None,
        "ordering_fallback": bool(
            isinstance(payload.get("messages"), list)
            and any(isinstance(item, dict) and _message_timestamp(item) is not None for item in payload.get("messages", [])) is False
        ),
    }


def _extract_audio_from_message(message: dict[str, Any]) -> dict[str, Any] | None:
    if not message:
        return None

    file_obj = message.get("file")
    if isinstance(file_obj, dict) and is_audio_attachment(file_obj) and file_obj.get("link"):
        return file_obj

    attachments = message.get("attachments")
    if isinstance(attachments, list):
        for attachment in attachments:
            if isinstance(attachment, dict) and is_audio_attachment(attachment) and attachment.get("link"):
                return attachment

    return None


def _extract_visitor(payload: dict[str, Any]) -> dict[str, Any]:
    visitor = payload.get("visitor")
    return visitor if isinstance(visitor, dict) else {}


def should_skip_auto_reply(payload: dict[str, Any]) -> bool:
    """Skip when the effective Conversations message must not enter the pipeline."""
    selected = select_effective_inbound_message(payload)
    if not selected:
        return payload.get("eventName") == "conversationFragment"

    if not _is_visitor_message(selected):
        return True

    return bool(
        selected.get("isPushed")
        or selected.get("isTrigger")
        or _is_own_agent_message(selected)
    )


def _is_own_agent_message(message: dict[str, Any]) -> bool:
    received_from = _first_non_empty(message.get("receivedFrom"), message.get("received_from"))
    if not received_from:
        return False
    try:
        from .config import get_settings

        configured = (get_settings().brevo_received_from or "").strip()
    except Exception:
        configured = ""
    return bool(configured and received_from.casefold() == configured.casefold())


def inbound_skip_reason(payload: dict[str, Any]) -> str | None:
    """Explain why a webhook should not enter the agent pipeline."""
    messages = payload.get("messages")
    has_primary_message = isinstance(payload.get("message"), dict)
    if (not isinstance(messages, list) or not messages) and not has_primary_message:
        return "no_inbound_message" if payload.get("eventName") == "conversationFragment" else None

    selected = select_effective_inbound_message(payload)
    if not selected:
        return "invalid_payload"
    if not _is_visitor_message(selected):
        message_type = _message_type(selected)
        return "agent_message" if message_type in {"agent", "bot", "assistant"} else "outbound_message"
    if selected.get("isPushed") or selected.get("isTrigger") or _is_own_agent_message(selected):
        return "agent_message"
    return None


def webhook_event_skip_reason(payload: dict[str, Any]) -> str | None:
    """Reject webhook events that are not new inbound messages."""
    event_name = _first_non_empty(
        payload.get("eventName"),
        payload.get("event"),
        payload.get("eventType"),
    )
    if event_name == "conversationTranscript":
        return "non_inbound_event"
    return None


def _extract_primary_message(payload: dict[str, Any]) -> dict[str, Any]:
    return select_effective_inbound_message(payload)


def _scalar(value: Any) -> Any:
    return value if isinstance(value, (str, int, float)) else None


def _valid_phone(value: str | None) -> str | None:
    if not value:
        return None
    digits = "".join(character for character in value if character.isdigit())
    return value if len(digits) >= 10 else None


def _detect_channel(
    payload: dict[str, Any],
    visitor: dict[str, Any],
    sender_phone: str | None,
) -> str:
    integration = payload.get("integration")
    integration = integration if isinstance(integration, dict) else {}
    for value in (
        visitor.get("source"),
        payload.get("channel"),
        payload.get("source"),
        integration.get("source"),
    ):
        channel = normalize_channel(_first_non_empty(value))
        if channel != "unknown":
            return channel
    source_link = str(visitor.get("sourceChannelLink") or "").lower()
    if "instagram.com" in source_link:
        return "instagram"
    if "facebook.com" in source_link or "m.me/" in source_link:
        return "facebook"
    return "whatsapp" if _valid_phone(sender_phone) else "unknown"


def _attachment_candidates(message: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for key in ("file", "image", "sticker"):
        value = message.get(key)
        if isinstance(value, dict):
            candidates.append({**value, "_source_key": key})
    attachments = message.get("attachments")
    if isinstance(attachments, list):
        candidates.extend(item for item in attachments if isinstance(item, dict))
    return candidates


def _attachment_url(attachment: dict[str, Any]) -> str | None:
    return _first_non_empty(
        attachment.get("link"),
        attachment.get("url"),
        attachment.get("src"),
        attachment.get("href"),
        attachment.get("downloadUrl"),
        attachment.get("download_url"),
        attachment.get("fileUrl"),
        attachment.get("file_url"),
    )


def _looks_like_image_url(url: str | None) -> bool:
    if not url:
        return False
    path = str(url).split("?", 1)[0].casefold()
    return any(
        path.endswith(extension)
        for extension in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp")
    )


def _attachment_type(attachment: dict[str, Any]) -> str:
    explicit = _first_non_empty(
        attachment.get("type"),
        attachment.get("fileType"),
        attachment.get("contentType"),
        attachment.get("_source_key"),
    )
    normalized = str(explicit or "").lower()
    mime_type = str(
        attachment.get("mimeType")
        or attachment.get("mimetype")
        or attachment.get("contentType")
        or ""
    ).lower()
    filename = str(attachment.get("name") or attachment.get("filename") or "").lower()
    url = _attachment_url(attachment)
    if is_audio_attachment(attachment):
        return "audio"
    if "sticker" in normalized:
        return "sticker"
    if "image" in normalized or mime_type.startswith("image/"):
        return "image"
    if any(
        filename.endswith(extension)
        for extension in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp")
    ):
        return "image"
    if _looks_like_image_url(url):
        return "image"
    return "file"


def _attachment_placeholder(channel: str, attachment_type: str, filename: str | None) -> str:
    channel_label = {
        "instagram": "Instagram",
        "facebook": "Facebook",
        "whatsapp": "WhatsApp",
        "widget": "Chat",
    }.get(channel, channel.title() if channel != "unknown" else "canal de atendimento")
    if attachment_type == "image":
        return f"[Imagem recebida via {channel_label}]"
    if attachment_type == "sticker":
        return f"[Sticker recebido via {channel_label}]"
    safe_filename = (filename or "").strip()
    suffix = f": {safe_filename}" if safe_filename else ""
    return f"[Arquivo recebido via {channel_label}{suffix}]"


def parse_brevo_conversations_payload(payload: dict[str, Any]) -> IncomingMessage:
    """Normalize Brevo Conversations payloads for every supported channel."""
    visitor_obj = _extract_visitor(payload)
    effective_message = _extract_primary_message(payload)

    audio_file = _extract_audio_from_message(effective_message) or extract_audio_attachment(payload)

    message_text = _first_non_empty(
        effective_message.get("text") if isinstance(effective_message.get("text"), str) else None,
        effective_message.get("body") if isinstance(effective_message.get("body"), str) else None,
        _get_nested(effective_message, "text", "body"),
    )
    is_fragment = payload.get("eventName") == "conversationFragment"
    text = _first_non_empty(
        message_text,
        None if is_fragment else payload.get("text"),
        None if is_fragment else _scalar(payload.get("message")),
        None if is_fragment else payload.get("body"),
        None if is_fragment else payload.get("content"),
        None if is_fragment else _get_nested(payload, "text", "body"),
        None if is_fragment else _get_nested(payload, "message", "text"),
    ) or ""

    sender_phone_candidate = _first_non_empty(
        _scalar(payload.get("sender")),
        _scalar(payload.get("from")),
        payload.get("phone"),
        payload.get("contactNumber"),
        payload.get("contact_number"),
        _get_nested(payload, "contact", "phone"),
        _get_nested(payload, "contact", "whatsapp"),
        _get_nested(payload, "sender", "phone"),
        _get_nested(payload, "from", "phone"),
        _get_nested(visitor_obj, "attributes", "SMS"),
        _get_nested(visitor_obj, "attributes", "WHATSAPP"),
        _get_nested(visitor_obj, "contactAttributes", "SMS"),
        _get_nested(visitor_obj, "contactAttributes", "WHATSAPP"),
        _get_nested(visitor_obj, "formattedAttributes", "SMS"),
        _scalar(effective_message.get("from")),
    )
    channel = _detect_channel(payload, visitor_obj, sender_phone_candidate)
    sender_phone = _valid_phone(sender_phone_candidate)
    if channel in {"instagram", "facebook"}:
        sender_phone = _valid_phone(_first_non_empty(
            _get_nested(visitor_obj, "attributes", "SMS"),
            _get_nested(visitor_obj, "attributes", "WHATSAPP"),
            _get_nested(visitor_obj, "contactAttributes", "SMS"),
            _get_nested(visitor_obj, "contactAttributes", "WHATSAPP"),
        ))

    sender_name = _first_non_empty(
        _get_nested(visitor_obj, "displayedName"),
        _get_nested(visitor_obj, "attributes", "FIRSTNAME"),
        _get_nested(visitor_obj, "integrationAttributes", "FIRSTNAME"),
        _get_nested(visitor_obj, "contactAttributes", "FIRSTNAME"),
        payload.get("senderName"),
        payload.get("name"),
        payload.get("sender_name"),
        _get_nested(payload, "contact", "name"),
        _get_nested(payload, "sender", "name"),
        _get_nested(effective_message, "profile", "name"),
    )

    visitor_id = _first_non_empty(
        payload.get("visitorId"),
        visitor_obj.get("id"),
    )
    conversation_id = _first_non_empty(
        payload.get("conversationId"),
        payload.get("conversation_id"),
        payload.get("threadId"),
        visitor_obj.get("threadId"),
    )
    source_channel_ref = _first_non_empty(visitor_obj.get("sourceChannelRef"))
    source_channel_link = _first_non_empty(visitor_obj.get("sourceChannelLink"))
    source_conversation_ref = _first_non_empty(
        visitor_obj.get("sourceConversationRef"),
        _get_nested(effective_message, "from", "id"),
        _get_nested(payload, "sender", "id"),
    )
    sender_username = _first_non_empty(
        visitor_obj.get("username"),
        _get_nested(visitor_obj, "attributes", "USERNAME"),
        _get_nested(effective_message, "profile", "username"),
        _get_nested(payload, "sender", "username"),
    )

    input_modality = "text"
    attachment_type = None
    audio_url = None
    audio_mime_type = None
    audio_filename = None
    image_url = None
    image_mime_type = None
    if audio_file:
        input_modality = "audio"
        audio_url = audio_file.get("link")
        audio_mime_type = audio_file.get("mimeType")
        audio_filename = audio_file.get("name")
        attachment_type = "audio"
        if is_placeholder_audio_text(text, audio_filename):
            text = ""
    else:
        attachments = _attachment_candidates(effective_message)
        if attachments:
            attachment = attachments[0]
            attachment_type = _attachment_type(attachment)
            filename = _first_non_empty(attachment.get("name"), attachment.get("filename"))
            if attachment_type == "image":
                image_url = _attachment_url(attachment)
                image_mime_type = _first_non_empty(
                    attachment.get("mimeType"),
                    attachment.get("mimetype"),
                    attachment.get("contentType"),
                )
            if not text.strip():
                text = _attachment_placeholder(channel, attachment_type, filename)
                input_modality = attachment_type
            else:
                input_modality = f"text_with_{attachment_type}"

    sender_key = build_sender_key(
        channel,
        sender_phone,
        source_conversation_ref,
        visitor_id,
        conversation_id,
    )
    channel_metadata: dict[str, Any] = {}
    if attachment_type:
        channel_metadata["attachment_type"] = attachment_type
    if image_url:
        channel_metadata["image_url_present"] = True
        channel_metadata["image_url"] = image_url
    if input_modality:
        channel_metadata["input_modality"] = input_modality
    if source_channel_ref:
        channel_metadata["source_channel_ref_present"] = True

    instagram_story = None
    if channel == "instagram":
        try:
            from .instagram_story_parser import (
                extract_instagram_story_context,
                maybe_log_story_payload_diagnostics,
            )

            maybe_log_story_payload_diagnostics(payload)
            instagram_story = extract_instagram_story_context(
                payload=payload,
                message=effective_message if isinstance(effective_message, dict) else {},
                channel=channel,
                visitor=visitor_obj if isinstance(visitor_obj, dict) else {},
            )
            if instagram_story is not None:
                channel_metadata["instagram_story"] = {
                    "replied_to_story": instagram_story.replied_to_story,
                    "mentioned_in_story": instagram_story.mentioned_in_story,
                    "story_media_id_present": bool(instagram_story.story_media_id),
                    "media_type": instagram_story.media_type,
                    "media_url_present": bool(instagram_story.story_media_url),
                }
                from .observability import log_event

                log_event(
                    "instagram_story.context_parsed",
                    {
                        "replied_to_story": instagram_story.replied_to_story,
                        "mentioned_in_story": instagram_story.mentioned_in_story,
                        "media_type": instagram_story.media_type,
                        "story_media_id_present": bool(instagram_story.story_media_id),
                        "synthetic_id": str(
                            instagram_story.story_media_id or ""
                        ).startswith("synthetic:"),
                    },
                )
        except Exception as exc:  # noqa: BLE001
            print(
                "[instagram.story.parse.error]",
                {"error_type": type(exc).__name__},
            )

    return IncomingMessage(
        provider="brevo",
        event_type=_first_non_empty(
            payload.get("eventName"),
            payload.get("event"),
            payload.get("type"),
            payload.get("eventType"),
        ),
        message_id=_message_id(effective_message) or (
            None if is_fragment else _first_non_empty(payload.get("messageId"), payload.get("message_id"), payload.get("id"))
        ),
        channel=channel,
        sender_key=sender_key,
        sender_external_id=source_conversation_ref,
        sender_username=sender_username,
        source_channel_ref=source_channel_ref,
        source_channel_link=source_channel_link,
        source_conversation_ref=source_conversation_ref,
        conversation_id=conversation_id,
        visitor_id=visitor_id,
        sender_phone=sender_phone,
        sender_name=sender_name,
        text=text,
        input_modality=input_modality,
        attachment_type=attachment_type,
        audio_url=audio_url,
        audio_mime_type=audio_mime_type,
        audio_filename=audio_filename,
        image_url=image_url,
        image_mime_type=image_mime_type,
        channel_metadata=channel_metadata,
        instagram_story=instagram_story,
        raw=payload,
    )


def parse_brevo_whatsapp_payload(payload: dict[str, Any]) -> IncomingMessage:
    """Backward-compatible alias for the omnichannel Conversations parser."""
    return parse_brevo_conversations_payload(payload)
