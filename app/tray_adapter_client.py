from __future__ import annotations

import asyncio
import re
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from .config import get_settings
from .http_resilience import DEFAULT_BACKOFF_SECONDS, is_transient_status
from .runtime_context import register_integration_failure, register_tray_call


class TrayAdapterError(RuntimeError):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        *,
        diagnostics: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        safe = diagnostics or {}
        self.error = _safe_diagnostic_text(safe.get("error"), limit=100)
        self.tray_error_code = _safe_diagnostic_text(safe.get("tray_error_code"), limit=100)
        self.tray_error_name = _safe_diagnostic_text(safe.get("tray_error_name"), limit=100)
        self.tray_error_type = _safe_diagnostic_text(safe.get("tray_error_type"), limit=100)
        self.tray_error_field = _safe_error_field(safe.get("tray_error_field"))
        raw_fields = safe.get("tray_error_fields")
        self.tray_error_fields = (
            list(dict.fromkeys(
                field for field in (_safe_error_field(value) for value in raw_fields) if field
            ))[:20]
            if isinstance(raw_fields, (list, tuple, set)) else []
        )
        self.tray_error_message = _safe_diagnostic_text(safe.get("tray_error_message"), limit=300)
        raw_causes = safe.get("tray_error_causes")
        self.tray_error_causes = [
            cause for cause in (
                _safe_diagnostic_text(value, limit=200)
                for value in (raw_causes if isinstance(raw_causes, list) else [])[:20]
            ) if cause
        ]
        self.diagnostics = {
            "error": self.error,
            "tray_error_code": self.tray_error_code,
            "tray_error_name": self.tray_error_name,
            "tray_error_type": self.tray_error_type,
            "tray_error_field": self.tray_error_field,
            "tray_error_fields": self.tray_error_fields,
            "tray_error_message": self.tray_error_message,
            "tray_error_causes": self.tray_error_causes,
        }


def _safe_diagnostic_text(value: Any, *, limit: int = 300) -> str | None:
    if not isinstance(value, (str, int, float, bool)):
        return None
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r"(?i)bearer\s+\S+", "Bearer ***", text)
    text = re.sub(r"(?i)\bsk-(?:proj-)?\S+", "sk-***", text)
    text = re.sub(
        r"(?i)(authorization|api[-_ ]?key|token|secret)\s*[:=]\s*\S+",
        r"\1=***",
        text,
    )
    text = re.sub(
        r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        "***",
        text,
    )
    text = re.sub(r"https?://\S+", "<url>", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<!\d)(?:\d[\s.-]?){8,}(?!\d)", "***", text)
    return text[:limit]


def _safe_error_field(value: Any) -> str | None:
    text = _safe_diagnostic_text(value, limit=100)
    if not text:
        return None
    cleaned = re.sub(r"[^A-Za-z0-9_.\[\]-]", "", text)
    return cleaned or None


def _tray_error_diagnostics(payload: Any) -> dict[str, Any]:
    """Extract only bounded contract diagnostics; never retain the raw body."""
    if not isinstance(payload, (dict, list)):
        return {}

    containers: list[dict[str, Any]] = []
    detail_items: list[dict[str, Any]] = []

    def visit(value: Any, *, depth: int = 0) -> None:
        if depth > 4:
            return
        if isinstance(value, dict):
            containers.append(value)
            for key in ("error", "detail", "errors", "causes", "tray_error_causes"):
                nested = value.get(key)
                if isinstance(nested, dict):
                    visit(nested, depth=depth + 1)
                elif isinstance(nested, list):
                    for item in nested[:20]:
                        if isinstance(item, dict):
                            detail_items.append(item)
                            visit(item, depth=depth + 1)
        elif isinstance(value, list):
            for item in value[:20]:
                if isinstance(item, dict):
                    detail_items.append(item)
                    visit(item, depth=depth + 1)

    visit(payload)

    def first_scalar(*keys: str) -> str | None:
        for container in containers:
            for key in keys:
                value = _safe_diagnostic_text(container.get(key))
                if value:
                    return value
        return None

    fields: list[str] = []
    for container in [*containers, *detail_items]:
        direct = _safe_error_field(
            container.get("tray_error_field") or container.get("field")
        )
        if direct:
            fields.append(direct)
        loc = container.get("loc")
        if isinstance(loc, (list, tuple)) and loc:
            location = ".".join(
                part
                for part in (
                    _safe_error_field(item)
                    for item in loc
                )
                if part
            )
            if location:
                fields.append(location)
        raw_fields = container.get("tray_error_fields") or container.get("fields")
        if isinstance(raw_fields, (list, tuple, set)):
            fields.extend(
                field
                for field in (
                    _safe_error_field(item)
                    for item in raw_fields
                )
                if field
            )
        elif isinstance(raw_fields, dict):
            fields.extend(
                field
                for field in (
                    _safe_error_field(item)
                    for item in raw_fields.keys()
                )
                if field
            )

    unique_fields = list(dict.fromkeys(fields))[:20]
    diagnostics = {
        "error": first_scalar("error"),
        "tray_error_code": first_scalar("tray_error_code", "code", "error_code"),
        "tray_error_name": first_scalar("tray_error_name", "error_name"),
        "tray_error_type": first_scalar("tray_error_type", "type", "error_type"),
        "tray_error_field": unique_fields[0] if unique_fields else None,
        "tray_error_fields": unique_fields,
        "tray_error_message": first_scalar(
            "tray_error_message", "message", "msg", "detail"
        ),
        "tray_error_causes": list(dict.fromkeys(
            cause for cause in (
                _safe_diagnostic_text(item.get("msg") or item.get("message"), limit=200)
                for item in detail_items
            ) if cause
        ))[:20],
    }
    return {
        key: value
        for key, value in diagnostics.items()
        if value not in (None, "", [])
    }


class TrayAdapterClient:
    timeout_seconds = 75.0
    max_get_attempts = 2
    retry_backoff_seconds = DEFAULT_BACKOFF_SECONDS
    transient_status_codes = frozenset({502, 503, 504})

    def __init__(self, base_url: str | None = None, token: str | None = None,
                 http_client: httpx.AsyncClient | None = None):
        settings = get_settings()
        self.base_url = (base_url if base_url is not None else settings.tray_adapter_url).rstrip("/")
        self.token = token if token is not None else settings.tray_adapter_token
        self._http_client = http_client

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    @staticmethod
    def _response_has_key(
        payload: Any,
        key: str,
        *,
        depth: int = 0,
    ) -> bool:
        if depth > 3 or not isinstance(payload, dict):
            return False
        if payload.get(key):
            return True
        return any(
            TrayAdapterClient._response_has_key(
                value,
                key,
                depth=depth + 1,
            )
            for value in payload.values()
            if isinstance(value, dict)
        )

    @staticmethod
    def _operation_name(path: str) -> str:
        if path.startswith("/internal/carts"):
            return "carts"
        if path.startswith("/internal/shippings"):
            return "shippings"
        if path.startswith("/internal/orders"):
            return "orders"
        if path.startswith("/internal/products"):
            return "products"
        if path.startswith("/internal/categories"):
            return "categories"
        if path.startswith("/internal/brands"):
            return "brands"
        if path.startswith("/internal/customers"):
            return "customers"
        if path.startswith("/internal/coupons"):
            return "coupons"
        return "internal_get"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        if not self.base_url or not self.token:
            raise TrayAdapterError("tray_adapter_not_configured")
        register_tray_call()
        clean_params = {key: value for key, value in (params or {}).items() if value is not None}
        own_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(timeout=self.timeout_seconds)
        max_attempts = self.max_get_attempts if method.upper() == "GET" else 1
        operation = self._operation_name(path)
        is_cart_create = method.upper() == "POST" and path == "/internal/carts"
        try:
            for attempt in range(1, max_attempts + 1):
                try:
                    request_kwargs: dict[str, Any] = {
                        "headers": self._headers(),
                        "params": clean_params,
                    }
                    if json_body is not None:
                        request_kwargs["json"] = {
                            key: value
                            for key, value in json_body.items()
                            if value is not None
                        }
                    if is_cart_create:
                        price_valid = False
                        try:
                            price_valid = (
                                Decimal(str((json_body or {}).get("price"))) > 0
                            )
                        except (InvalidOperation, TypeError, ValueError):
                            price_valid = False
                        print("[sales.cart.http.request]", {
                            "has_product_id": bool(
                                (json_body or {}).get("product_id")
                            ),
                            "has_variant_id": bool(
                                (json_body or {}).get("variant_id")
                            ),
                            "quantity": (json_body or {}).get("quantity"),
                            "price_valid": price_valid,
                            "has_session_id": bool(
                                (json_body or {}).get("session_id")
                            ),
                        })
                    response = await client.request(
                        method,
                        f"{self.base_url}{path}",
                        **request_kwargs,
                    )
                    parsed_response: Any = None
                    response_is_json = False
                    response_keys: list[str] = []
                    try:
                        parsed_response = response.json()
                        response_is_json = True
                        if isinstance(parsed_response, dict):
                            response_keys = sorted(
                                str(key) for key in parsed_response.keys()
                            )[:20]
                    except ValueError:
                        parsed_response = None
                    if is_cart_create:
                        response_log = {
                            "status_code": response.status_code,
                            "has_response": True,
                            "response_is_json": response_is_json,
                            "response_keys": response_keys,
                            "has_session_id": self._response_has_key(
                                parsed_response,
                                "session_id",
                            ),
                            "has_cart_url": self._response_has_key(
                                parsed_response,
                                "cart_url",
                            ),
                        }
                        if response.status_code >= 400:
                            response_log.update(_tray_error_diagnostics(parsed_response))
                        print("[sales.cart.http.response]", response_log)
                    if response.status_code >= 400:
                        if (
                            (
                                response.status_code in self.transient_status_codes
                                or is_transient_status(response.status_code)
                            )
                            and attempt < max_attempts
                        ):
                            print("[sales.tray.retry]", {
                                "operation": operation,
                                "status_code": response.status_code,
                                "attempt": attempt + 1,
                            })
                            await asyncio.sleep(self.retry_backoff_seconds)
                            continue
                        raise TrayAdapterError(
                            f"tray_adapter_http_{response.status_code}",
                            response.status_code,
                            diagnostics=_tray_error_diagnostics(parsed_response),
                        )
                    if not response_is_json:
                        raise ValueError("tray_adapter_response_not_json")
                    return parsed_response
                except (
                    httpx.TimeoutException,
                    httpx.ConnectError,
                    httpx.NetworkError,
                ) as exc:
                    if is_cart_create:
                        print("[sales.cart.http.response]", {
                            "status_code": None,
                            "has_response": False,
                            "response_is_json": False,
                            "response_keys": [],
                            "has_session_id": False,
                            "has_cart_url": False,
                        })
                    if attempt < max_attempts:
                        print("[sales.tray.retry]", {
                            "operation": operation,
                            "status_code": None,
                            "attempt": attempt + 1,
                        })
                        await asyncio.sleep(self.retry_backoff_seconds)
                        continue
                    raise TrayAdapterError("tray_adapter_unavailable") from exc
            raise TrayAdapterError("tray_adapter_unavailable")
        except TrayAdapterError as exc:
            register_integration_failure("tray")
            print("[tray.client] request_failed", {
                "error_type": type(exc).__name__,
                "status_code": exc.status_code,
                "timeout": isinstance(exc.__cause__, httpx.TimeoutException),
                **exc.diagnostics,
            })
            raise
        except ValueError as exc:
            register_integration_failure("tray")
            print("[tray.client] request_failed", {
                "error_type": type(exc).__name__,
                "status_code": None,
                "timeout": False,
            })
            raise TrayAdapterError("tray_adapter_invalid_response") from exc
        finally:
            if own_client:
                await client.aclose()

    async def search_products(self, *, name: str | None = None, reference: str | None = None,
                              ean: str | None = None, brand: str | None = None,
                              category_id: str | int | None = None, available: Any = None,
                              available_in_store: Any = None, stock: Any = None,
                              promotion: Any = None, limit: int = 5,
                              page: int | None = None) -> Any:
        return await self._request("GET", "/internal/products", params={
            "name": name, "reference": reference, "ean": ean, "brand": brand,
            "category_id": category_id, "available": available,
            "available_in_store": available_in_store, "stock": stock,
            "promotion": promotion, "limit": min(max(limit, 1), 20), "page": page,
        })

    async def search_products_by_tokens(
        self,
        *,
        tokens: list[str] | tuple[str, ...],
        brand: str | None = None,
        limit: int = 20,
        page: int | None = 1,
    ) -> Any:
        """Token AND search (adaptor: ILIKE %token% for each significant token).

        Expected adaptor route:
          GET /internal/products/search?tokens=sealander,rosa&brand=Christopher+Ward
        """
        cleaned = [
            str(token).strip()
            for token in tokens
            if str(token or "").strip()
        ]
        return await self._request(
            "GET",
            "/internal/products/search",
            params={
                "tokens": ",".join(cleaned),
                "brand": brand,
                "limit": min(max(limit, 1), 20),
                "page": page,
            },
        )

    async def get_product(self, product_id: str | int) -> Any:
        from .product_snapshot import (
            cache_ttl_for_kind,
            get_product_snapshot_cache,
            product_cache_enabled,
            product_dict_to_snapshot,
        )

        tenant_id = self.base_url or "default"
        cache = get_product_snapshot_cache()
        if product_cache_enabled():
            cached_payload = cache.get_payload(
                tenant_id=tenant_id,
                kind="product",
                entity_id=str(product_id),
                allow_stale=False,
            )
            if cached_payload is not None:
                return cached_payload
        payload = await self._request("GET", f"/internal/products/{product_id}")
        if product_cache_enabled() and isinstance(payload, dict):
            snapshot = product_dict_to_snapshot(payload, tenant_id=tenant_id)
            if snapshot.product_id:
                cache.put(
                    snapshot,
                    kind="product",
                    ttl_seconds=cache_ttl_for_kind("product"),
                    payload=payload,
                )
        return payload

    async def get_product_stock(self, product_id: str | int) -> Any:
        from .product_snapshot import (
            cache_ttl_for_kind,
            get_product_snapshot_cache,
            product_cache_enabled,
            product_dict_to_snapshot,
        )

        tenant_id = self.base_url or "default"
        cache = get_product_snapshot_cache()
        # Stock must not be confirmed from stale cache.
        if product_cache_enabled():
            cached_payload = cache.get_payload(
                tenant_id=tenant_id,
                kind="stock",
                entity_id=str(product_id),
                allow_stale=False,
            )
            if cached_payload is not None:
                return cached_payload
        payload = await self._request(
            "GET",
            f"/internal/products/{product_id}/stock",
        )
        if product_cache_enabled() and isinstance(payload, dict):
            merged = {"id": str(product_id), **payload}
            snapshot = product_dict_to_snapshot(merged, tenant_id=tenant_id)
            cache.put(
                snapshot,
                kind="stock",
                ttl_seconds=cache_ttl_for_kind("stock"),
                entity_id=str(product_id),
                payload=payload,
            )
        return payload

    async def list_product_variants(self, product_id: str | int) -> Any:
        return await self._request(
            "GET",
            "/internal/products/variants",
            params={"product_id": product_id},
        )

    async def get_product_variant(self, variant_id: str | int) -> Any:
        return await self._request("GET", f"/internal/products/variants/{variant_id}")

    async def create_cart(
        self,
        *,
        product_id: str | int,
        quantity: int,
        price: str,
        variant_id: str | int | None = None,
        session_id: str | None = None,
    ) -> Any:
        from .product_snapshot import get_product_snapshot_cache

        # Mutations must not reuse fresh stock/product confirmation blindly.
        get_product_snapshot_cache().invalidate(
            tenant_id=self.base_url or "default",
            entity_id=str(product_id),
            kinds=["stock", "product"],
        )
        return await self._request(
            "POST",
            "/internal/carts",
            json_body={
                "product_id": str(product_id),
                "variant_id": str(variant_id) if variant_id is not None else None,
                "quantity": quantity,
                "price": price,
                "session_id": session_id,
            },
        )

    async def get_cart(self, session_id: str) -> Any:
        return await self._request("GET", f"/internal/carts/{session_id}")

    async def get_cart_complete(self, session_id: str) -> Any:
        return await self._request("GET", f"/internal/carts/{session_id}/complete")

    async def set_cart_item_quantity(
        self,
        *,
        session_id: str,
        product_id: str | int,
        variant_id: str | int | None,
        quantity: int,
    ) -> Any:
        return await self._request(
            "PUT",
            f"/internal/carts/{session_id}/items",
            json_body={
                "product_id": str(product_id),
                "variant_id": str(variant_id) if variant_id is not None else None,
                "quantity": quantity,
            },
        )

    async def delete_cart(self, session_id: str) -> Any:
        return await self._request("DELETE", f"/internal/carts/{session_id}")

    async def get_payment_options(
        self,
        cart_session_id: str | None = None,
        order_id: str | None = None,
    ) -> Any:
        if bool(cart_session_id) == bool(order_id):
            raise ValueError("exactly one of cart_session_id or order_id is required")
        return await self._request(
            "GET",
            "/internal/payments/options",
            params=(
                {"cart_session_id": cart_session_id}
                if cart_session_id else {"order_id": order_id}
            ),
        )

    async def quote_shipping(self, *, zipcode: str, products: list[dict[str, Any]]) -> Any:
        return await self._request(
            "POST",
            "/internal/shippings/quote",
            json_body={"zipcode": zipcode, "products": products},
        )

    async def list_shipping_methods(self) -> Any:
        return await self._request("GET", "/internal/shippings/methods")

    async def create_order(self, payload: dict[str, Any]) -> Any:
        return await self._request("POST", "/internal/orders", json_body=payload)

    async def list_orders(
        self,
        *,
        session_id: str | None = None,
        customer_id: str | int | None = None,
    ) -> Any:
        return await self._request(
            "GET",
            "/internal/orders",
            params={"session_id": session_id, "customer_id": customer_id},
        )

    async def get_order(self, order_id: str | int) -> Any:
        return await self._request("GET", f"/internal/orders/{order_id}")

    async def get_order_complete(self, order_id: str | int) -> Any:
        return await self._request("GET", f"/internal/orders/{order_id}/complete")

    async def get_order_payment(self, order_id: str | int) -> Any:
        return await self._request("GET", f"/internal/orders/{order_id}/payment")

    async def list_categories(self, *, limit: int = 50, page: int = 1) -> Any:
        return await self._request(
            "GET",
            "/internal/categories",
            params={"limit": min(max(limit, 1), 50), "page": max(page, 1)},
        )

    async def get_category(self, category_id: str | int) -> Any:
        return await self._request("GET", f"/internal/categories/{category_id}")

    async def get_category_tree(self, category_id: str | int) -> Any:
        return await self._request("GET", f"/internal/categories/tree/{category_id}")

    async def list_brands(self, **params: Any) -> Any:
        return await self._request("GET", "/internal/brands", params=params)

    async def get_brand(self, brand_id: str | int) -> Any:
        return await self._request("GET", f"/internal/brands/{brand_id}")

    async def list_customers(self, **params: Any) -> Any:
        params.setdefault("limit", 5)
        return await self._request("GET", "/internal/customers", params=params)

    async def get_customer(self, customer_id: str | int) -> Any:
        return await self._request("GET", f"/internal/customers/{customer_id}")

    async def list_coupons(self, **params: Any) -> Any:
        params.setdefault("limit", 5)
        return await self._request("GET", "/internal/coupons", params=params)

    async def get_coupon(self, coupon_id: str | int) -> Any:
        return await self._request("GET", f"/internal/coupons/{coupon_id}")
