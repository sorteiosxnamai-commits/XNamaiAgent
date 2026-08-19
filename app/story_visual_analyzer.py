"""Structured visual analysis for Instagram Stories via OpenAI Responses gateway."""

from __future__ import annotations

import base64
import time
from typing import Any

from .config import get_settings
from .instagram_story_models import StoryVisualUnderstanding
from .observability import log_event
from .openai_gateway import parse_structured_output


STORY_VISION_INSTRUCTIONS = """\
Você analisa a mídia de um Instagram Story de uma loja de relógios.

Você NÃO conhece o catálogo oficial.
Não invente IDs, preços, estoque ou disponibilidade.
Extraia apenas o que é visualmente verificável.
Marca, coleção e modelo são hipóteses até validação no catálogo.
Não trate aparência semelhante como identificação exata.
Se houver mais de um produto, represente a ambiguidade (multiple_products=true).
Se houver preço escrito na arte, coloque em visible_advertised_price — isso NÃO é preço atual.
confidence deve refletir legibilidade real; imagens ruins → image_quality=poor e confiança baixa.
"""


async def analyze_story_image(
    *,
    image_bytes: bytes,
    content_type: str = "image/jpeg",
    media_sha256: str | None = None,
    media_type: str = "image",
    extra_frame_bytes: list[bytes] | None = None,
) -> StoryVisualUnderstanding:
    settings = get_settings()
    detail = str(getattr(settings, "instagram_story_analysis_detail", "high") or "high")
    version = str(getattr(settings, "instagram_story_analysis_version", "v2") or "v2")
    mime = content_type if content_type.startswith("image/") else "image/jpeg"
    data_url = f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    # Model resolution: INSTAGRAM_STORY_VISION_MODEL → OPENAI_MAIN_MODEL → OPENAI_MODEL
    model = (
        str(getattr(settings, "instagram_story_vision_model", "") or "").strip()
        or str(getattr(settings, "openai_main_model", "") or "").strip()
        or str(settings.openai_model)
    )
    content_parts: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "Analise esta mídia de Story. "
                f"media_type_hint={media_type}. "
                "Retorne somente evidências visuais. "
                "Se houver múltiplos frames, use todos."
            ),
        },
        {
            "type": "image_url",
            "image_url": {"url": data_url, "detail": detail},
        },
    ]
    for frame in (extra_frame_bytes or [])[:2]:
        if not frame:
            continue
        frame_url = f"data:image/jpeg;base64,{base64.b64encode(frame).decode('ascii')}"
        content_parts.append(
            {"type": "image_url", "image_url": {"url": frame_url, "detail": detail}}
        )
    log_event(
        "instagram_story.visual_analysis_started",
        {
            "media_sha256_prefix": (media_sha256 or "")[:12] or None,
            "detail": detail,
            "bytes": len(image_bytes),
            "model": model,
            "analysis_version": version,
            "extra_frames": len(extra_frame_bytes or []),
        },
    )
    started = time.perf_counter()
    parse_result = await parse_structured_output(
        model=model,
        text_format=StoryVisualUnderstanding,
        messages=[
            {"role": "system", "content": STORY_VISION_INSTRUCTIONS},
            {"role": "user", "content": content_parts},
        ],
        call_type="story_visual_analysis",
        temperature=0.0,
    )
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    parsed = parse_result.parsed
    if not isinstance(parsed, StoryVisualUnderstanding):
        raise ValueError("story_visual_schema_missing")
    if parsed.visible_advertised_price:
        parsed.ambiguity_reasons = list(
            dict.fromkeys(
                [*parsed.ambiguity_reasons, "advertised_price_not_authoritative"]
            )
        )
    metrics = parse_result.metrics
    log_event(
        "instagram_story.visual_analysis_completed",
        {
            "media_sha256_prefix": (media_sha256 or "")[:12] or None,
            "latency_ms": latency_ms,
            "model": model,
            "detail": detail,
            "analysis_version": version,
            "image_quality": parsed.image_quality,
            "multiple_products": parsed.multiple_products,
            "watch_count": parsed.watch_count,
            "identity_confidence": parsed.product_identity_confidence,
            "input_tokens": getattr(metrics, "input_tokens", None) if metrics else None,
            "output_tokens": getattr(metrics, "output_tokens", None) if metrics else None,
            "success": True,
        },
    )
    log_event(
        "story_visual_latency",
        {"latency_ms": latency_ms, "model": model, "analysis_version": version},
    )
    log_event(
        "story_visual_tokens",
        {
            "input_tokens": getattr(metrics, "input_tokens", None) if metrics else None,
            "output_tokens": getattr(metrics, "output_tokens", None) if metrics else None,
        },
    )
    return parsed


# In-memory visual cache (process-local). Price/stock must never be stored here.
_VISUAL_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def visual_cache_key(*, tenant_id: str, media_sha256: str, analysis_version: str) -> str:
    return f"{tenant_id}|{media_sha256}|{analysis_version}"


def get_cached_visual_analysis(
    *,
    tenant_id: str,
    media_sha256: str,
) -> StoryVisualUnderstanding | None:
    settings = get_settings()
    if not bool(getattr(settings, "instagram_story_visual_cache_enabled", True)):
        return None
    version = str(getattr(settings, "instagram_story_analysis_version", "v1") or "v1")
    key = visual_cache_key(
        tenant_id=tenant_id,
        media_sha256=media_sha256,
        analysis_version=version,
    )
    row = _VISUAL_CACHE.get(key)
    if not row:
        return None
    expires_at, payload = row
    if time.time() > expires_at:
        _VISUAL_CACHE.pop(key, None)
        return None
    log_event("instagram_story.visual_cache_hit", {"tenant_id": tenant_id})
    return StoryVisualUnderstanding.model_validate(payload)


def put_cached_visual_analysis(
    *,
    tenant_id: str,
    media_sha256: str,
    analysis: StoryVisualUnderstanding,
) -> None:
    settings = get_settings()
    if not bool(getattr(settings, "instagram_story_visual_cache_enabled", True)):
        return
    version = str(getattr(settings, "instagram_story_analysis_version", "v1") or "v1")
    ttl_days = int(getattr(settings, "instagram_story_visual_cache_ttl_days", 30) or 30)
    key = visual_cache_key(
        tenant_id=tenant_id,
        media_sha256=media_sha256,
        analysis_version=version,
    )
    payload = analysis.model_dump(mode="json")
    # Hard strip any accidental commercial authority fields.
    payload.pop("price", None)
    payload.pop("stock", None)
    payload.pop("availability", None)
    _VISUAL_CACHE[key] = (time.time() + ttl_days * 86400, payload)


def clear_visual_cache_for_tests() -> None:
    _VISUAL_CACHE.clear()
