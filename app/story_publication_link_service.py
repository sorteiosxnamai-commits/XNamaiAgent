"""Register deterministic Story↔product links at publication time."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .fact_authority import catalog_item_key_for
from .instagram_story_models import StoryProductAssociation
from .story_product_repository import StoryProductRepository


def register_published_story(
    *,
    tenant_id: str,
    instagram_account_id: str,
    story_media_id: str,
    catalog_item_key: str,
    product_id: str,
    variant_id: str | None = None,
    media_type: str = "image",
    source_timestamp: datetime | None = None,
    expires_at: datetime | None = None,
    provider: str = "brevo",
    story_permalink: str | None = None,
    confirmed_by: str = "publication_metadata",
) -> StoryProductAssociation:
    if not tenant_id or not instagram_account_id or not story_media_id:
        raise ValueError("story_publication_identity_required")
    if not product_id:
        raise ValueError("story_publication_product_required")
    # Always rebuild key server-side.
    key = catalog_item_key_for(product_id, variant_id) or catalog_item_key
    if not key:
        raise ValueError("story_publication_product_required")
    repo = StoryProductRepository()
    pending = repo.create_pending(
        tenant_id=tenant_id,
        provider=provider,
        instagram_account_id=instagram_account_id,
        story_media_id=story_media_id,
        story_permalink=story_permalink,
        media_type=media_type if media_type in {"image", "video", "carousel", "unknown"} else "unknown",
        source_timestamp=source_timestamp,
        story_expires_at=expires_at,
    )
    if pending is None:
        raise RuntimeError("story_publication_persist_failed")
    confirmed = repo.confirm_match(
        tenant_id=tenant_id,
        provider=provider,
        instagram_account_id=instagram_account_id,
        story_media_id=story_media_id,
        catalog_item_key=key,
        product_id=str(product_id),
        variant_id=str(variant_id) if variant_id is not None else None,
        match_source="publication_metadata",
        match_confidence=1.0,
        match_status="matched",
        confirmed_by=confirmed_by,
        explanation={"source": "publication_metadata"},
    )
    if confirmed is None:
        raise RuntimeError("story_publication_confirm_failed")
    return confirmed


def validate_link_payload(body: dict[str, Any]) -> dict[str, Any]:
    required = (
        "tenant_id",
        "instagram_account_id",
        "story_media_id",
        "product_id",
    )
    missing = [key for key in required if not str(body.get(key) or "").strip()]
    if missing:
        raise ValueError(f"missing_fields:{','.join(missing)}")
    product_id = str(body["product_id"]).strip()
    variant_id = (
        str(body["variant_id"]).strip()
        if body.get("variant_id") not in (None, "")
        else None
    )
    # Backend builds catalog_item_key — ignore client key if product_id present.
    catalog_item_key = catalog_item_key_for(product_id, variant_id)
    cleaned = {
        "tenant_id": str(body["tenant_id"]).strip(),
        "instagram_account_id": str(body["instagram_account_id"]).strip(),
        "story_media_id": str(body["story_media_id"]).strip(),
        "catalog_item_key": catalog_item_key,
        "product_id": product_id,
        "variant_id": variant_id,
        "media_type": str(body.get("media_type") or "image").strip(),
        "provider": str(body.get("provider") or "brevo").strip(),
        "story_permalink": body.get("story_permalink"),
    }
    return cleaned
