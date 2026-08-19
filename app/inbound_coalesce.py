"""Coalesce Brevo photo+caption split into a single agent turn.

Brevo often delivers one WhatsApp image+caption as two webhooks with different
message_ids: (1) image with caption, (2) text-only echo of the same caption.
"""

from __future__ import annotations

from typing import Any

from .db import get_conn, resolve_context_filter
from .models import IncomingMessage

CAPTION_ECHO_WINDOW_SECONDS = 60


def normalize_caption_text(text: str | None) -> str:
    """Normalize caption for exact echo matching; placeholders become empty."""
    value = " ".join(str(text or "").strip().split())
    if not value:
        return ""
    lowered = value.casefold()
    if lowered.startswith("[imagem recebida") or lowered.startswith("[sticker recebido"):
        return ""
    if lowered.startswith("[arquivo recebido"):
        return ""
    return lowered


def _metadata_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    # psycopg Jsonb / mapping-like
    try:
        return dict(value)
    except Exception:
        return {}


def recent_image_inbound_for_echo(
    *,
    conversation_id: str | None,
    sender_key: str | None,
    sender_phone: str | None = None,
    window_seconds: int = CAPTION_ECHO_WINDOW_SECONDS,
) -> dict[str, Any] | None:
    """Return the newest inbound with an image in the echo window, if any."""
    from .config import get_settings

    settings = get_settings()
    if not settings.database_url:
        return None

    where_clause, params = resolve_context_filter(
        conversation_id,
        sender_key,
        sender_phone,
    )
    if not where_clause:
        return None

    params = {
        **params,
        "window_seconds": max(1, int(window_seconds)),
    }
    query = f"""
        SELECT
          id,
          text,
          channel_metadata,
          created_at
        FROM public.ai_inbound_messages AS inbound
        WHERE {where_clause}
          AND created_at >= NOW() - (%(window_seconds)s * INTERVAL '1 second')
          AND (
            COALESCE(channel_metadata->>'image_url_present', 'false') = 'true'
            OR NULLIF(TRIM(COALESCE(channel_metadata->>'image_url', '')), '') IS NOT NULL
          )
        ORDER BY created_at DESC, id DESC
        LIMIT 1
    """
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                row = cur.fetchone()
    except Exception as exc:
        print("[inbound.coalesce] recent_image_lookup_failed", {
            "error_type": type(exc).__name__,
        })
        return None
    if not row:
        return None
    return {
        "id": row[0],
        "text": row[1] or "",
        "channel_metadata": _metadata_dict(row[2]),
        "created_at": row[3],
    }


def is_caption_echo(
    incoming: IncomingMessage,
    recent: dict[str, Any] | None,
) -> bool:
    """True only when text-only inbound exactly matches a recent photo caption."""
    if recent is None:
        return False
    if (incoming.image_url or "").strip():
        return False
    current = normalize_caption_text(incoming.text)
    if not current:
        return False
    caption = normalize_caption_text(recent.get("text"))
    if not caption:
        return False
    return current == caption


def is_caption_echo_of_recent_image(incoming: IncomingMessage) -> bool:
    if (incoming.image_url or "").strip():
        return False
    if not normalize_caption_text(incoming.text):
        return False
    recent = recent_image_inbound_for_echo(
        conversation_id=incoming.conversation_id,
        sender_key=incoming.sender_key,
        sender_phone=incoming.sender_phone,
    )
    matched = is_caption_echo(incoming, recent)
    if matched and recent is not None:
        print("[brevo.webhook] caption_echo_skipped", {
            "conversation_id_present": bool(incoming.conversation_id),
            "sender_key_present": bool(incoming.sender_key),
            "recent_inbound_id": recent.get("id"),
            "text_preview": (incoming.text or "")[:40],
        })
    return matched


FOLLOWUP_IMAGE_WINDOW_SECONDS = 300


def attach_recent_image_for_followup(
    incoming: IncomingMessage,
    *,
    window_seconds: int = FOLLOWUP_IMAGE_WINDOW_SECONDS,
) -> IncomingMessage:
    """Bind a short price follow-up to the latest inbound image in-window.

    Meta/Brevo often deliver photo and later "valor" as separate messages.
    """
    if (incoming.image_url or "").strip():
        return incoming
    from app.brevo_instagram_media import is_bare_price_request
    from app.commerce_router import is_deictic_product_price_request

    text = incoming.text or ""
    if not (
        is_bare_price_request(text)
        or is_deictic_product_price_request(text)
    ):
        return incoming

    recent = recent_image_inbound_for_echo(
        conversation_id=incoming.conversation_id,
        sender_key=incoming.sender_key,
        sender_phone=incoming.sender_phone,
        window_seconds=window_seconds,
    )
    if not recent:
        return incoming
    meta = _metadata_dict(recent.get("channel_metadata"))
    image_url = str(meta.get("image_url") or "").strip()
    if not image_url:
        return incoming

    incoming.image_url = image_url
    incoming.attachment_type = incoming.attachment_type or "image"
    modality = (incoming.input_modality or "text").lower()
    if modality == "text":
        incoming.input_modality = "text_with_image"
    channel_metadata = dict(incoming.channel_metadata or {})
    channel_metadata["image_url_present"] = True
    channel_metadata["image_url"] = image_url
    channel_metadata["attached_from_recent_inbound_id"] = recent.get("id")
    incoming.channel_metadata = channel_metadata
    print("[inbound.coalesce] attached_recent_image", {
        "recent_inbound_id": recent.get("id"),
        "channel": incoming.channel,
        "text_preview": (incoming.text or "")[:40],
    })
    return incoming
