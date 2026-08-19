from __future__ import annotations

import uuid

import httpx

from .config import get_settings


async def upload_public_audio(
    audio_bytes: bytes,
    content_type: str = "audio/ogg; codecs=opus",
    filename: str = "resposta.ogg",
) -> str:
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_service_key:
        raise RuntimeError("supabase_storage_not_configured")
    if not settings.supabase_audio_bucket:
        raise RuntimeError("supabase_audio_bucket_missing")

    object_name = f"agent-replies/{uuid.uuid4().hex}-{filename}"
    upload_url = (
        f"{settings.supabase_url.rstrip('/')}/storage/v1/object/"
        f"{settings.supabase_audio_bucket}/{object_name}"
    )
    headers = {
        "Authorization": f"Bearer {settings.supabase_service_key}",
        "Content-Type": content_type,
        "x-upsert": "true",
    }

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(upload_url, content=audio_bytes, headers=headers)
        if response.status_code >= 400:
            raise RuntimeError(f"supabase_upload_failed_{response.status_code}")

    return (
        f"{settings.supabase_url.rstrip('/')}/storage/v1/object/public/"
        f"{settings.supabase_audio_bucket}/{object_name}"
    )
