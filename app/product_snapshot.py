"""ProductSnapshot model + short TTL cache for Tray reads (Phase 13)."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, Field

from .config import get_settings
from .runtime_context import get_current_turn


class ProductSnapshot(BaseModel):
    product_id: str
    variant_id: str | None = None
    name: str = ""
    brand: str | None = None
    model: str | None = None
    reference: str | None = None
    ean: str | None = None
    color: str | None = None
    price: Decimal | None = None
    promotional_price: Decimal | None = None
    stock_quantity: int | None = None
    available: bool = False
    sellable: bool = False
    url: str | None = None
    images: list[str] = Field(default_factory=list)
    retrieved_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    source: Literal["tray_adapter"] = "tray_adapter"
    match_kind: Literal["exact", "similar", "unknown"] = "unknown"
    tenant_id: str = "default"


class _CacheEntry(BaseModel):
    snapshot: ProductSnapshot
    payload: dict[str, Any] = Field(default_factory=dict)
    expires_at: float
    kind: str
    product_id: str = ""


class ProductSnapshotCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, _CacheEntry] = {}
        self.hits = 0
        self.misses = 0
        self.stale_served = 0
        self.invalidations = 0

    def _key(self, *, tenant_id: str, kind: str, entity_id: str) -> str:
        return f"{tenant_id}|{kind}|{entity_id}"

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "hits": self.hits,
                "misses": self.misses,
                "stale_served": self.stale_served,
                "invalidations": self.invalidations,
                "size": len(self._entries),
            }

    def invalidate(
        self,
        *,
        tenant_id: str,
        entity_id: str | None = None,
        kinds: list[str] | None = None,
    ) -> None:
        with self._lock:
            if entity_id is None and not kinds:
                count = len(self._entries)
                self._entries.clear()
                self.invalidations += count
                return
            remove: list[str] = []
            for key, entry in self._entries.items():
                if not key.startswith(f"{tenant_id}|"):
                    continue
                if kinds and entry.kind not in kinds:
                    continue
                if entity_id and entry.product_id != str(entity_id):
                    continue
                remove.append(key)
            for key in remove:
                self._entries.pop(key, None)
            self.invalidations += len(remove)

    def get(
        self,
        *,
        tenant_id: str,
        kind: str,
        entity_id: str,
        allow_stale: bool = False,
    ) -> ProductSnapshot | None:
        entry = self.get_entry(
            tenant_id=tenant_id,
            kind=kind,
            entity_id=entity_id,
            allow_stale=allow_stale,
        )
        return entry.snapshot.model_copy(deep=True) if entry else None

    def get_payload(
        self,
        *,
        tenant_id: str,
        kind: str,
        entity_id: str,
        allow_stale: bool = False,
    ) -> dict[str, Any] | None:
        entry = self.get_entry(
            tenant_id=tenant_id,
            kind=kind,
            entity_id=entity_id,
            allow_stale=allow_stale,
        )
        return dict(entry.payload) if entry and entry.payload else None

    def get_entry(
        self,
        *,
        tenant_id: str,
        kind: str,
        entity_id: str,
        allow_stale: bool = False,
    ) -> _CacheEntry | None:
        key = self._key(tenant_id=tenant_id, kind=kind, entity_id=str(entity_id))
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                _record_cache_metric("miss")
                return None
            if entry.expires_at >= now:
                self.hits += 1
                _record_cache_metric("hit")
                return entry
            if allow_stale:
                self.stale_served += 1
                _record_cache_metric("stale")
                return entry
            # Keep expired entry for optional stale-if-error on non-critical kinds.
            self.misses += 1
            _record_cache_metric("miss")
            return None

    def put(
        self,
        snapshot: ProductSnapshot,
        *,
        kind: str,
        ttl_seconds: float,
        entity_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        key = self._key(
            tenant_id=snapshot.tenant_id,
            kind=kind,
            entity_id=str(entity_id or snapshot.product_id),
        )
        with self._lock:
            self._entries[key] = _CacheEntry(
                snapshot=snapshot,
                payload=dict(payload or {}),
                expires_at=time.monotonic() + max(0.0, ttl_seconds),
                kind=kind,
                product_id=str(entity_id or snapshot.product_id),
            )


_CACHE = ProductSnapshotCache()


def get_product_snapshot_cache() -> ProductSnapshotCache:
    return _CACHE


def _record_cache_metric(kind: str) -> None:
    runtime = get_current_turn()
    if runtime is None:
        return
    bucket = runtime.context_snapshot.setdefault("cache", {"hits": 0, "misses": 0, "stale": 0})
    if not isinstance(bucket, dict):
        return
    bucket[kind] = int(bucket.get(kind) or 0) + 1


def _money(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        text = str(value).replace("R$", "").strip()
        if "," in text:
            text = text.replace(".", "").replace(",", ".")
        return Decimal(text).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def product_dict_to_snapshot(
    payload: dict[str, Any],
    *,
    tenant_id: str = "default",
    match_kind: Literal["exact", "similar", "unknown"] = "unknown",
) -> ProductSnapshot:
    product_id = str(
        payload.get("id")
        or payload.get("product_id")
        or payload.get("ProductID")
        or ""
    )
    stock_raw = payload.get("stock")
    if isinstance(stock_raw, dict):
        stock_qty = stock_raw.get("quantity") or stock_raw.get("stock")
        available = bool(
            stock_raw.get("available")
            if stock_raw.get("available") is not None
            else (int(stock_qty or 0) > 0)
        )
    else:
        stock_qty = stock_raw
        available = bool(
            payload.get("available")
            if payload.get("available") is not None
            else (int(stock_qty or 0) > 0 if stock_qty is not None else False)
        )
    try:
        stock_quantity = int(stock_qty) if stock_qty is not None else None
    except (TypeError, ValueError):
        stock_quantity = None
    images: list[str] = []
    for key in ("images", "image_urls"):
        value = payload.get(key)
        if isinstance(value, list):
            images.extend(str(item) for item in value if item)
    for key in ("primary_image_url", "image_url", "url_image"):
        if payload.get(key):
            images.append(str(payload.get(key)))
    # de-dupe preserve order
    seen: set[str] = set()
    uniq_images: list[str] = []
    for image in images:
        if image not in seen:
            seen.add(image)
            uniq_images.append(image)
    return ProductSnapshot(
        product_id=product_id,
        variant_id=(
            str(payload.get("variant_id"))
            if payload.get("variant_id") is not None
            else None
        ),
        name=str(payload.get("name") or payload.get("title") or ""),
        brand=(str(payload["brand"]) if payload.get("brand") is not None else None),
        model=(str(payload["model"]) if payload.get("model") is not None else None),
        reference=(
            str(payload["reference"]) if payload.get("reference") is not None else None
        ),
        ean=(str(payload["ean"]) if payload.get("ean") is not None else None),
        color=(str(payload["color"]) if payload.get("color") is not None else None),
        price=_money(
            payload.get("current_price")
            or payload.get("price")
            or payload.get("Price")
        ),
        promotional_price=_money(
            payload.get("promotional_price") or payload.get("sale_price")
        ),
        stock_quantity=stock_quantity,
        available=available,
        sellable=bool(
            payload.get("sellable")
            if payload.get("sellable") is not None
            else available
        ),
        url=(
            str(payload.get("url") or payload.get("link") or "")
            or None
        ),
        images=uniq_images[:12],
        tenant_id=tenant_id,
        match_kind=match_kind,
    )


def cache_ttl_for_kind(kind: str) -> float:
    settings = get_settings()
    mapping = {
        "product": float(getattr(settings, "agent_product_cache_ttl_seconds", 180)),
        "price": float(getattr(settings, "agent_price_cache_ttl_seconds", 45)),
        "stock": float(getattr(settings, "agent_stock_cache_ttl_seconds", 20)),
        "search": float(getattr(settings, "agent_search_cache_ttl_seconds", 45)),
        "image": float(getattr(settings, "agent_image_cache_ttl_seconds", 300)),
    }
    return mapping.get(kind, 60.0)


def product_cache_enabled() -> bool:
    return bool(getattr(get_settings(), "agent_product_cache_enabled", True))
