"""Tenant-scoped catalog index repository (read + upsert by catalog_item_key)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .catalog_index import CanonicalCatalogItem
from .config import get_settings
from .fact_authority import catalog_item_key_for


def make_catalog_item_key(product_id: str, variant_id: str | None = None) -> str:
    return catalog_item_key_for(product_id, variant_id)


def _ttl_cutoff() -> datetime | None:
    settings = get_settings()
    max_age = int(getattr(settings, "agent_catalog_index_max_age_seconds", 0) or 0)
    if max_age <= 0:
        return None
    from datetime import timedelta

    return datetime.now(timezone.utc) - timedelta(seconds=max_age)


class CatalogIndexRepository:
    """All queries require explicit tenant_id."""

    def search_exact(
        self,
        *,
        tenant_id: str,
        ean: str | None = None,
        sku: str | None = None,
        reference: str | None = None,
        product_id: str | None = None,
        variant_id: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        tenant = str(tenant_id or "").strip()
        if not tenant:
            raise ValueError("tenant_id required")
        clauses: list[str] = ["tenant_id = %(tenant_id)s"]
        params: dict[str, Any] = {
            "tenant_id": tenant,
            "limit": max(1, min(int(limit), 100)),
        }
        cutoff = _ttl_cutoff()
        if cutoff is not None:
            clauses.append(
                "coalesce(freshness_at, updated_at) >= %(cutoff)s"
            )
            params["cutoff"] = cutoff
        if ean:
            clauses.append("ean = %(ean)s")
            params["ean"] = str(ean).strip()
        elif sku:
            clauses.append("sku = %(sku)s")
            params["sku"] = str(sku).strip()
        elif reference:
            clauses.append("reference = %(reference)s")
            params["reference"] = str(reference).strip()
        elif variant_id:
            clauses.append("variant_id = %(variant_id)s")
            params["variant_id"] = str(variant_id).strip()
        elif product_id:
            clauses.append("product_id = %(product_id)s")
            params["product_id"] = str(product_id).strip()
        else:
            return []
        sql = f"""
            SELECT *
            FROM public.ai_catalog_index
            WHERE {" AND ".join(clauses)}
            ORDER BY freshness_at DESC NULLS LAST
            LIMIT %(limit)s
        """
        return self._fetch(sql, params)

    def search_lexical(
        self,
        *,
        tenant_id: str,
        query: str,
        brand: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        tenant = str(tenant_id or "").strip()
        if not tenant:
            raise ValueError("tenant_id required")
        q = str(query or "").strip()
        if not q:
            return []
        settings = get_settings()
        lim = int(
            limit
            or getattr(settings, "agent_catalog_index_candidate_limit", 30)
            or 30
        )
        params: dict[str, Any] = {
            "tenant_id": tenant,
            "q": f"%{q.lower()}%",
            "limit": max(1, min(lim, 100)),
        }
        ttl_sql = ""
        cutoff = _ttl_cutoff()
        if cutoff is not None:
            ttl_sql = " AND coalesce(freshness_at, updated_at) >= %(cutoff)s"
            params["cutoff"] = cutoff
        brand_sql = ""
        if brand:
            brand_sql = " AND lower(coalesce(brand, '')) = lower(%(brand)s)"
            params["brand"] = str(brand).strip()
        sql = f"""
            SELECT *
            FROM public.ai_catalog_index
            WHERE tenant_id = %(tenant_id)s
              AND (
                    lower(title_normalized) LIKE %(q)s
                 OR lower(coalesce(model, '')) LIKE %(q)s
                 OR lower(coalesce(reference, '')) LIKE %(q)s
              )
              {brand_sql}
              {ttl_sql}
            ORDER BY freshness_at DESC NULLS LAST
            LIMIT %(limit)s
        """
        return self._fetch(sql, params)

    def search_by_constraints(
        self,
        *,
        tenant_id: str,
        brand: str | None = None,
        mechanism: str | None = None,
        gender: str | None = None,
        max_price: float | None = None,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        tenant = str(tenant_id or "").strip()
        if not tenant:
            raise ValueError("tenant_id required")
        clauses = ["tenant_id = %(tenant_id)s"]
        params: dict[str, Any] = {
            "tenant_id": tenant,
            "limit": max(1, min(int(limit), 100)),
        }
        cutoff = _ttl_cutoff()
        if cutoff is not None:
            clauses.append("coalesce(freshness_at, updated_at) >= %(cutoff)s")
            params["cutoff"] = cutoff
        if brand:
            clauses.append("lower(coalesce(brand, '')) = lower(%(brand)s)")
            params["brand"] = str(brand).strip()
        if mechanism:
            clauses.append("lower(coalesce(mechanism, '')) LIKE lower(%(mechanism)s)")
            params["mechanism"] = f"%{str(mechanism).strip()}%"
        if gender:
            clauses.append("lower(coalesce(gender, '')) LIKE lower(%(gender)s)")
            params["gender"] = f"%{str(gender).strip()}%"
        if max_price is not None:
            clauses.append("price IS NOT NULL AND price <= %(max_price)s")
            params["max_price"] = float(max_price)
        sql = f"""
            SELECT *
            FROM public.ai_catalog_index
            WHERE {" AND ".join(clauses)}
            ORDER BY freshness_at DESC NULLS LAST
            LIMIT %(limit)s
        """
        return self._fetch(sql, params)

    def get_by_product_and_variant(
        self,
        *,
        tenant_id: str,
        product_id: str,
        variant_id: str | None = None,
    ) -> dict[str, Any] | None:
        key = make_catalog_item_key(product_id, variant_id)
        rows = self._fetch(
            """
            SELECT *
            FROM public.ai_catalog_index
            WHERE tenant_id = %(tenant_id)s
              AND catalog_item_key = %(catalog_item_key)s
            LIMIT 1
            """,
            {
                "tenant_id": str(tenant_id).strip(),
                "catalog_item_key": key,
            },
        )
        return rows[0] if rows else None

    def upsert_items(self, items: list[CanonicalCatalogItem]) -> int:
        from .catalog_index import upsert_canonical_items

        return upsert_canonical_items(items)

    def mark_stale(self, *, tenant_id: str, catalog_item_keys: list[str]) -> int:
        tenant = str(tenant_id or "").strip()
        if not tenant or not catalog_item_keys:
            return 0
        try:
            from .db import get_conn

            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE public.ai_catalog_index
                        SET factual_source = 'catalog_index',
                            updated_at = now()
                        WHERE tenant_id = %(tenant_id)s
                          AND catalog_item_key = ANY(%(keys)s)
                        """,
                        {"tenant_id": tenant, "keys": list(catalog_item_keys)},
                    )
                    count = cur.rowcount or 0
                conn.commit()
            return int(count)
        except Exception as exc:
            print("[catalog.index.mark_stale.error]", {"error_type": type(exc).__name__})
            return 0

    def delete_missing_items(
        self,
        *,
        tenant_id: str,
        keep_keys: list[str],
    ) -> int:
        """Optional GC — only deletes when keep_keys non-empty."""
        tenant = str(tenant_id or "").strip()
        if not tenant or not keep_keys:
            return 0
        try:
            from .db import get_conn

            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        DELETE FROM public.ai_catalog_index
                        WHERE tenant_id = %(tenant_id)s
                          AND NOT (catalog_item_key = ANY(%(keys)s))
                        """,
                        {"tenant_id": tenant, "keys": list(keep_keys)},
                    )
                    count = cur.rowcount or 0
                conn.commit()
            return int(count)
        except Exception as exc:
            print("[catalog.index.delete_missing.error]", {"error_type": type(exc).__name__})
            return 0

    def _fetch(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if not str(params.get("tenant_id") or "").strip():
            raise ValueError("tenant_id required")
        try:
            from .db import get_conn

            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    rows = list(cur.fetchall() or [])
            return [dict(row) for row in rows]
        except Exception as exc:
            print("[catalog.index.read.error]", {"error_type": type(exc).__name__})
            return []


def row_to_product_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Map index row to Tray-like product dict for discovery (not final authority)."""
    freshness = row.get("freshness_at")
    return {
        "id": row.get("product_id"),
        "product_id": row.get("product_id"),
        "variant_id": row.get("variant_id"),
        "sku": row.get("sku"),
        "ean": row.get("ean"),
        "reference": row.get("reference"),
        "brand": row.get("brand"),
        "model": row.get("model"),
        "name": row.get("title_normalized") or row.get("model"),
        "title": row.get("title_normalized"),
        "price": float(row["price"]) if row.get("price") is not None else None,
        "promotional_price": (
            float(row["promotional_price"])
            if row.get("promotional_price") is not None
            else None
        ),
        "stock": row.get("stock"),
        "available": row.get("available"),
        "url": row.get("url"),
        "image_url": row.get("image_url"),
        "tenant_id": row.get("tenant_id"),
        "_factual_source": "catalog_index",
        "_revalidated": False,
        "_freshness_at": (
            freshness.isoformat() if isinstance(freshness, datetime) else freshness
        ),
        "_catalog_item_key": row.get("catalog_item_key"),
        "_from_catalog_index": True,
    }
