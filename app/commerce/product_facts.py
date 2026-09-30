"""Normalize product detail contracts and preserve per-fact freshness limits."""
from __future__ import annotations

from typing import Any


def unconfirmed_facts(payload: dict) -> set[str]:
    freshness = payload.get("freshness")
    freshness = freshness if isinstance(freshness, dict) else {}
    blocked = set()
    for kind in ("price", "stock"):
        verdict = freshness.get(kind)
        state = verdict.get("freshness") if isinstance(verdict, dict) else None
        if (payload.get(f"{kind}_confirmed") is False
                or freshness.get(f"{kind}_confirmed") is False
                or state in {"unconfirmed", "unknown", "stale", "expired"}):
            blocked.add(kind)
    return blocked


def without_unconfirmed_facts(value: Any, blocked: frozenset[str] = frozenset()) -> Any:
    """Remove expired values, including nested aliases; retain identity/valid facts.

    A child cannot override a parent's explicit freshness rejection. Missing
    freshness metadata preserves the contract of providers returning live data.
    """
    if isinstance(value, list):
        return [without_unconfirmed_facts(item, blocked) for item in value]
    if not isinstance(value, dict):
        return value
    blocked = blocked | unconfirmed_facts(value)
    cleaned = {}
    for key, item in value.items():
        folded = str(key).casefold()
        if folded == "freshness" or folded in {"price_confirmed", "stock_confirmed"}:
            cleaned[key] = item
            continue
        price = any(token in folded for token in (
            "price", "preco", "preço", "installment", "parcel", "discount", "desconto", "total", "amount",
        )) or folded == "value"
        stock = any(token in folded for token in (
            "stock", "inventory", "available", "availability", "disponib", "lead_time", "immediate_delivery",
            "delivery_days", "ready_to_ship",
        ))
        if ("price" in blocked and price) or ("stock" in blocked and stock):
            continue
        cleaned[key] = without_unconfirmed_facts(item, blocked)
    return cleaned


def normalize_product_detail(result: Any, product_id: str) -> dict | None:
    """Accept Mercos' envelope and legacy flat detail, without old search facts."""
    if not isinstance(result, dict) or result.get("error") or result.get("ok") is False:
        return None
    product = result.get("product", result)
    if not isinstance(product, dict) or str(product.get("id") or "") != str(product_id):
        return None
    product = without_unconfirmed_facts(product)
    local = result.get("source") == "local_index" or product.get("source") == "local_index"
    product["_factual_source"] = "local_database" if local else "commerce_live"
    product["_revalidated"] = not local and not unconfirmed_facts(product)
    return product
