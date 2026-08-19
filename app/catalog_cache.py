"""Durable catalog pool cache for brand/category recommendation searches."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from .config import get_settings
from .db import ensure_tables, get_conn, to_jsonb

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]

_MEMORY: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _ttl_seconds() -> int:
    settings = get_settings()
    try:
        return max(
            60,
            int(getattr(settings, "agent_catalog_cache_ttl_seconds", 3600) or 3600),
        )
    except (TypeError, ValueError):
        return 3600


def _fold_key(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def cache_key_for_brand(brand: str) -> str:
    return f"brand:{_fold_key(brand)}"


def cache_key_for_category(category_id: str) -> str:
    return f"category:{_fold_key(str(category_id))}"


def compact_catalog_product(product: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(product, dict) or product.get("id") is None:
        return None
    return {
        key: value
        for key, value in {
            "id": str(product.get("id")),
            "name": product.get("name"),
            "brand": product.get("brand"),
            "model": product.get("model"),
            "reference": product.get("reference"),
            "color": product.get("color"),
            "category": product.get("category") or product.get("category_name"),
            "description": str(product.get("description") or "")[:240] or None,
            "price": product.get("current_price")
            or product.get("promotional_price")
            or product.get("price"),
            "stock": product.get("stock"),
            "available": product.get("available"),
            "available_in_store": product.get("available_in_store"),
            "url": product.get("url"),
            "primary_image_url": product.get("primary_image_url")
            or product.get("image_url"),
        }.items()
        if value is not None
    }


def _memory_get(cache_key: str) -> list[dict[str, Any]] | None:
    entry = _MEMORY.get(cache_key)
    if not entry:
        return None
    expires_at, products = entry
    if time.time() >= expires_at:
        _MEMORY.pop(cache_key, None)
        return None
    return [dict(item) for item in products]


def _memory_put(cache_key: str, products: list[dict[str, Any]]) -> None:
    _MEMORY[cache_key] = (time.time() + _ttl_seconds(), [dict(item) for item in products])


def load_catalog_cache(cache_key: str) -> list[dict[str, Any]] | None:
    memorized = _memory_get(cache_key)
    if memorized is not None:
        return memorized
    try:
        ensure_tables()
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT products
                    FROM public.ai_catalog_cache
                    WHERE cache_key = %s
                      AND expires_at > now()
                    LIMIT 1
                    """,
                    (cache_key,),
                )
                row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        print("[catalog.cache.load_error]", {
            "cache_key": cache_key,
            "error_type": type(exc).__name__,
        })
        return None
    if not row:
        return None
    products = row.get("products") if isinstance(row, dict) else None
    if isinstance(products, str):
        try:
            products = json.loads(products)
        except json.JSONDecodeError:
            return None
    if not isinstance(products, list):
        return None
    compact = [item for item in products if isinstance(item, dict) and item.get("id")]
    _memory_put(cache_key, compact)
    return compact


def store_catalog_cache(cache_key: str, products: list[dict[str, Any]]) -> None:
    compact = [
        item
        for item in (compact_catalog_product(product) for product in products)
        if item is not None
    ]
    # Dedupe by id preserving order.
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in compact:
        product_id = str(item["id"])
        if product_id in seen:
            continue
        seen.add(product_id)
        unique.append(item)
    _memory_put(cache_key, unique)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=_ttl_seconds())
    try:
        ensure_tables()
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.ai_catalog_cache (
                        cache_key, products, product_count, refreshed_at, expires_at, metadata
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (cache_key) DO UPDATE SET
                        products = EXCLUDED.products,
                        product_count = EXCLUDED.product_count,
                        refreshed_at = EXCLUDED.refreshed_at,
                        expires_at = EXCLUDED.expires_at,
                        metadata = EXCLUDED.metadata
                    """,
                    (
                        cache_key,
                        to_jsonb(unique),
                        len(unique),
                        now,
                        expires,
                        to_jsonb({"source": "tray_brand_or_category"}),
                    ),
                )
    except Exception as exc:  # noqa: BLE001
        print("[catalog.cache.store_error]", {
            "cache_key": cache_key,
            "error_type": type(exc).__name__,
            "count": len(unique),
        })


async def fetch_and_cache_brand_pool(
    brand: str,
    execute_tool: ToolExecutor,
    *,
    pages: int = 5,
    limit: int = 20,
) -> list[dict[str, Any]]:
    cache_key = cache_key_for_brand(brand)
    cached = load_catalog_cache(cache_key)
    if cached:
        print("[catalog.cache.hit]", {"cache_key": cache_key, "count": len(cached)})
        return cached

    products: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in range(1, max(1, pages) + 1):
        result = await execute_tool(
            "search_products",
            {
                "brand": brand,
                "available": True,
                "available_in_store": True,
                "limit": limit,
                "page": page,
            },
        )
        if "error" in result:
            break
        batch = result.get("products") if isinstance(result.get("products"), list) else []
        if not batch:
            break
        for product in batch:
            if not isinstance(product, dict) or product.get("id") is None:
                continue
            product_id = str(product["id"])
            if product_id in seen:
                continue
            seen.add(product_id)
            products.append(product)
    store_catalog_cache(cache_key, products)
    print("[catalog.cache.miss_filled]", {
        "cache_key": cache_key,
        "count": len(products),
        "pages": pages,
    })
    return products


async def ensure_brand_pool_in_candidates(
    *,
    brand: str | None,
    candidates: list[dict[str, Any]],
    seen_ids: set[str],
    execute_tool: ToolExecutor,
    limit: int = 120,
) -> list[dict[str, Any]]:
    """Merge cached brand catalog into the live candidate pool."""
    if not brand:
        return candidates
    pool = await fetch_and_cache_brand_pool(brand, execute_tool)
    merged = list(candidates)
    for product in pool:
        product_id = str(product.get("id") or "")
        if not product_id or product_id in seen_ids:
            continue
        if len(merged) >= limit:
            break
        seen_ids.add(product_id)
        merged.append(product)
    return merged
