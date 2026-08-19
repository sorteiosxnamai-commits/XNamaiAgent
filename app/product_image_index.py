from __future__ import annotations

import base64
import hashlib
from typing import Any

import httpx
from openai import APIError, AsyncOpenAI
from pydantic import BaseModel, Field

from .config import get_settings
from .db import get_conn
from .openai_runtime import execute_openai_call
from .product_media import official_product_image
from .tray_tools import execute_tool
from .turn_runtime import LLMCallBudgetExceeded


VISUAL_FINGERPRINT_INSTRUCTIONS = """\
Você descreve relógios para indexação visual de catálogo e-commerce.
Gere um fingerprint estável e comparável entre fotos do mesmo modelo.

Inclua no caption (em português, uma frase densa):
marca, linha/modelo, cor do mostrador, formato/tamanho aparente da caixa,
tipo de pulseira/bracelete, estilo da luneta, e traços distintivos visíveis.
Não invente referência se não estiver legível.
"""


class VisualProductFingerprint(BaseModel):
    brand: str | None = None
    model: str | None = None
    reference: str | None = None
    dial_color: str | None = None
    case_shape: str | None = None
    bezel: str | None = None
    distinctive_features: list[str] = Field(default_factory=list)
    caption: str


def _embedding_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{value:.8f}" for value in values) + "]"


def source_hash_for_image(image_url: str, *, content: bytes | None = None) -> str:
    digest = hashlib.sha256()
    digest.update(image_url.strip().encode("utf-8"))
    if content:
        digest.update(b"|")
        digest.update(content)
    return digest.hexdigest()


def get_indexed_source_hash(product_id: str) -> str | None:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT source_hash
                    FROM public.ai_product_image_index
                    WHERE product_id = %s
                    """,
                    (str(product_id),),
                )
                row = cur.fetchone()
    except Exception as exc:
        print("[sales.image.index.db]", {
            "op": "get_source_hash",
            "error_type": type(exc).__name__,
        })
        return None
    if not row:
        return None
    return str(row.get("source_hash") or "") or None


def upsert_product_image_index(
    *,
    product_id: str,
    image_url: str,
    brand: str | None,
    model: str | None,
    reference: str | None,
    name: str | None,
    visual_caption: str,
    embedding: list[float],
    source_hash: str,
) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.ai_product_image_index (
                  product_id, image_url, brand, model, reference, name,
                  visual_caption, embedding, source_hash, updated_at
                ) VALUES (
                  %s, %s, %s, %s, %s, %s,
                  %s, %s::vector, %s, now()
                )
                ON CONFLICT (product_id) DO UPDATE SET
                  image_url = EXCLUDED.image_url,
                  brand = EXCLUDED.brand,
                  model = EXCLUDED.model,
                  reference = EXCLUDED.reference,
                  name = EXCLUDED.name,
                  visual_caption = EXCLUDED.visual_caption,
                  embedding = EXCLUDED.embedding,
                  source_hash = EXCLUDED.source_hash,
                  updated_at = now()
                """,
                (
                    str(product_id),
                    image_url,
                    brand,
                    model,
                    reference,
                    name,
                    visual_caption,
                    _embedding_literal(embedding),
                    source_hash,
                ),
            )


def search_visual_neighbors(
    embedding: list[float],
    *,
    top_k: int | None = None,
    max_distance: float | None = None,
) -> list[dict[str, Any]]:
    settings = get_settings()
    limit = int(top_k or getattr(settings, "agent_visual_top_k", 3))
    distance_limit = float(
        max_distance
        if max_distance is not None
        else getattr(settings, "agent_visual_max_distance", 0.45)
    )
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                      product_id,
                      image_url,
                      brand,
                      model,
                      reference,
                      name,
                      visual_caption,
                      (embedding <=> %s::vector) AS distance
                    FROM public.ai_product_image_index
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (
                        _embedding_literal(embedding),
                        _embedding_literal(embedding),
                        max(limit * 3, limit),
                    ),
                )
                rows = cur.fetchall() or []
    except Exception as exc:
        print("[sales.image.index.db]", {
            "op": "search_neighbors",
            "error_type": type(exc).__name__,
            "error": str(exc)[:240],
        })
        return []

    matches: list[dict[str, Any]] = []
    for row in rows:
        try:
            distance = float(row.get("distance"))
        except (TypeError, ValueError):
            continue
        if distance > distance_limit:
            continue
        matches.append({
            "product_id": str(row.get("product_id")),
            "image_url": row.get("image_url"),
            "brand": row.get("brand"),
            "model": row.get("model"),
            "reference": row.get("reference"),
            "name": row.get("name"),
            "visual_caption": row.get("visual_caption"),
            "distance": distance,
        })
        if len(matches) >= limit:
            break
    return matches


def build_caption_from_fingerprint(fingerprint: VisualProductFingerprint) -> str:
    caption = (fingerprint.caption or "").strip()
    if caption:
        return caption
    parts = [
        fingerprint.brand,
        fingerprint.model,
        fingerprint.reference,
        fingerprint.dial_color,
        fingerprint.case_shape,
        fingerprint.bezel,
        *fingerprint.distinctive_features,
    ]
    return " ".join(str(part).strip() for part in parts if part).strip()


async def embed_text(text: str) -> list[float]:
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("openai_api_key_missing")
    model = str(
        getattr(settings, "agent_visual_embedding_model", "text-embedding-3-small")
        or "text-embedding-3-small"
    ).strip()
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    response = await execute_openai_call(
        call_type="visual_embedding",
        model=model,
        messages=[{"role": "user", "content": text[:4000]}],
        operation=lambda: client.embeddings.create(
            model=model,
            input=text[:4000],
        ),
    )
    data = getattr(response, "data", None) or []
    if not data:
        raise ValueError("embedding_empty")
    values = list(getattr(data[0], "embedding", None) or [])
    if not values:
        raise ValueError("embedding_empty")
    return [float(value) for value in values]


def _vision_model() -> str:
    settings = get_settings()
    configured = str(getattr(settings, "agent_image_search_model", "") or "").strip()
    return configured or settings.openai_model


async def fingerprint_image_bytes(
    image_bytes: bytes,
    *,
    content_type: str = "image/jpeg",
    hint: str | None = None,
) -> VisualProductFingerprint:
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("openai_api_key_missing")
    encoded = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:{content_type};base64,{encoded}"
    user_text = "Gere o fingerprint visual deste relógio para busca por similaridade."
    if hint:
        user_text += f"\nContexto do catálogo: {hint}"
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": VISUAL_FINGERPRINT_INSTRUCTIONS},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        },
    ]
    model = _vision_model()
    from .openai_gateway import parse_structured_output

    parse_result = await parse_structured_output(
        model=model,
        text_format=VisualProductFingerprint,
        messages=messages,
        temperature=0,
        call_type="visual_fingerprint",
    )
    fingerprint = parse_result.parsed
    if not isinstance(fingerprint, VisualProductFingerprint):
        raise ValueError("visual_fingerprint_schema_missing")
    if not (fingerprint.caption or "").strip():
        fingerprint.caption = build_caption_from_fingerprint(fingerprint)
    return fingerprint


async def fingerprint_image_url(
    image_url: str,
    *,
    hint: str | None = None,
) -> tuple[VisualProductFingerprint, str]:
    from .image_product_id import download_image_file

    image_bytes, content_type = await download_image_file(image_url)
    fingerprint = await fingerprint_image_bytes(
        image_bytes,
        content_type=content_type,
        hint=hint,
    )
    return fingerprint, source_hash_for_image(image_url, content=image_bytes)


def caption_from_identification(identified: Any) -> str:
    features = getattr(identified, "features", None) or []
    feature_text = " ".join(str(item).strip() for item in features if item)
    parts = [
        getattr(identified, "brand", None),
        getattr(identified, "model", None),
        getattr(identified, "reference", None),
        getattr(identified, "color", None),
        getattr(identified, "case_finish", None),
        feature_text or None,
        getattr(identified, "notes", None),
    ]
    return " ".join(str(part).strip() for part in parts if part).strip()


async def index_product_image(
    product: dict[str, Any],
    *,
    force: bool = False,
) -> dict[str, Any]:
    product_id = product.get("id")
    if product_id is None:
        return {"status": "skipped", "reason": "missing_product_id"}
    image_url = official_product_image(product)
    if not image_url:
        return {"status": "skipped", "reason": "missing_image", "product_id": str(product_id)}

    existing_hash = None if force else get_indexed_source_hash(str(product_id))
    provisional_hash = source_hash_for_image(image_url)
    if existing_hash and existing_hash == provisional_hash:
        return {"status": "skipped", "reason": "unchanged", "product_id": str(product_id)}

    hint = " ".join(
        str(part)
        for part in (
            product.get("brand"),
            product.get("model"),
            product.get("name"),
            product.get("reference"),
        )
        if part
    ).strip() or None
    fingerprint, content_hash = await fingerprint_image_url(image_url, hint=hint)
    if existing_hash and existing_hash == content_hash and not force:
        return {"status": "skipped", "reason": "unchanged", "product_id": str(product_id)}

    caption = build_caption_from_fingerprint(fingerprint)
    embedding = await embed_text(caption)
    upsert_product_image_index(
        product_id=str(product_id),
        image_url=image_url,
        brand=fingerprint.brand or product.get("brand"),
        model=fingerprint.model or product.get("model"),
        reference=fingerprint.reference or product.get("reference"),
        name=product.get("name"),
        visual_caption=caption,
        embedding=embedding,
        source_hash=content_hash,
    )
    return {
        "status": "indexed",
        "product_id": str(product_id),
        "caption_chars": len(caption),
    }


async def run_product_image_index_batch(
    *,
    batch_size: int | None = None,
    start_page: int = 1,
) -> dict[str, Any]:
    settings = get_settings()
    if not bool(getattr(settings, "agent_product_image_index_enabled", True)):
        return {"ok": False, "reason": "index_disabled", "indexed": 0, "skipped": 0}
    if not settings.database_url:
        return {"ok": False, "reason": "database_url_missing", "indexed": 0, "skipped": 0}

    limit = int(
        batch_size
        or getattr(settings, "agent_product_image_index_batch_size", 40)
    )
    page = max(int(start_page), 1)
    indexed = 0
    skipped = 0
    errors = 0
    scanned = 0
    pages_read = 0

    while scanned < limit:
        page_limit = min(20, limit - scanned)
        result = await execute_tool(
            "search_products",
            {"limit": page_limit, "page": page},
        )
        pages_read += 1
        if "error" in result:
            return {
                "ok": False,
                "reason": "tray_adapter_unavailable",
                "indexed": indexed,
                "skipped": skipped,
                "errors": errors,
                "scanned": scanned,
                "pages_read": pages_read,
            }
        products = result.get("products") if isinstance(result.get("products"), list) else []
        if not products:
            break
        for product in products:
            if not isinstance(product, dict):
                skipped += 1
                scanned += 1
                continue
            try:
                outcome = await index_product_image(product)
                if outcome.get("status") == "indexed":
                    indexed += 1
                else:
                    skipped += 1
            except (
                APIError,
                LLMCallBudgetExceeded,
                httpx.HTTPError,
                ValueError,
                RuntimeError,
                Exception,
            ) as exc:
                errors += 1
                print("[sales.image.index.product.error]", {
                    "product_id": product.get("id"),
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:240],
                })
            scanned += 1
            if scanned >= limit:
                break
        if len(products) < page_limit:
            break
        page += 1

    summary = {
        "ok": True,
        "indexed": indexed,
        "skipped": skipped,
        "errors": errors,
        "scanned": scanned,
        "pages_read": pages_read,
        "next_page": page,
    }
    print("[sales.image.index.batch]", summary)
    return summary


async def hydrate_visual_matches(
    matches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    for match in matches:
        product_id = match.get("product_id")
        if not product_id:
            continue
        result = await execute_tool("get_product", {"product_id": str(product_id)})
        if "error" in result or not isinstance(result, dict):
            # Fall back to indexed metadata if Tray lookup fails.
            products.append({
                "id": product_id,
                "name": match.get("name"),
                "brand": match.get("brand"),
                "model": match.get("model"),
                "reference": match.get("reference"),
                "primary_image_url": match.get("image_url"),
                "visual_distance": match.get("distance"),
            })
            continue
        product = dict(result)
        product["visual_distance"] = match.get("distance")
        products.append(product)
    return products


async def visual_search_from_caption(caption: str) -> list[dict[str, Any]]:
    settings = get_settings()
    if not bool(getattr(settings, "agent_visual_search_enabled", True)):
        return []
    if not caption.strip():
        return []
    if not str(getattr(settings, "database_url", "") or "").strip():
        return []
    embedding = await embed_text(caption)
    matches = search_visual_neighbors(embedding)
    print("[sales.image.visual.search]", {
        "caption_chars": len(caption),
        "match_count": len(matches),
        "best_distance": matches[0]["distance"] if matches else None,
    })
    if not matches:
        return []
    return await hydrate_visual_matches(matches)


async def visual_search_from_image_url(image_url: str) -> list[dict[str, Any]]:
    settings = get_settings()
    if not bool(getattr(settings, "agent_visual_search_enabled", True)):
        return []
    fingerprint, _ = await fingerprint_image_url(image_url)
    caption = build_caption_from_fingerprint(fingerprint)
    return await visual_search_from_caption(caption)
