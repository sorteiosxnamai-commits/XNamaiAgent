from __future__ import annotations

import re
import time
import html
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

from .commerce_context import normalize_variant_identity
from .product_media import official_product_url
from .tray_adapter_client import TrayAdapterClient, TrayAdapterError


TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": "search_products", "description": "Pesquisar produtos reais na loja.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "name": {"type": "string"}, "reference": {"type": "string"}, "ean": {"type": "string"}, "brand": {"type": "string"}, "tokens": {"type": "array", "items": {"type": "string"}, "description": "AND search tokens (ILIKE %token% each)"}, "category_id": {"type": "string"}, "available": {"type": "boolean"}, "available_in_store": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}, "page": {"type": "integer", "minimum": 1}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_product", "description": "Consultar detalhes atuais de um produto.", "parameters": {"type": "object", "properties": {"product_id": {"type": "string"}}, "required": ["product_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_product_link", "description": "Obter o link oficial de um produto real já identificado.", "parameters": {"type": "object", "properties": {"product_id": {"type": "string"}}, "required": ["product_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "check_inventory", "description": "Confirmar estoque e regras de disponibilidade de um produto.", "parameters": {"type": "object", "properties": {"product_id": {"type": "string"}}, "required": ["product_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_cart", "description": "Consultar um carrinho já identificado por sua sessão.", "parameters": {"type": "object", "properties": {"session_id": {"type": "string"}}, "required": ["session_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_cart_complete", "description": "Consultar itens e totais atuais de um carrinho já identificado.", "parameters": {"type": "object", "properties": {"session_id": {"type": "string"}}, "required": ["session_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_payment_options", "description": "Consultar opções reais de pagamento de um carrinho ou pedido existente. Informe exatamente um escopo.", "parameters": {"type": "object", "properties": {"cart_session_id": {"type": "string"}, "order_id": {"type": "string"}}, "oneOf": [{"required": ["cart_session_id"]}, {"required": ["order_id"]}], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "search_customer", "description": "Pesquisar um cliente com filtro específico, quando necessário.", "parameters": {"type": "object", "properties": {"email": {"type": "string"}, "cpf": {"type": "string"}, "cnpj": {"type": "string"}, "name": {"type": "string"}, "limit": {"type": "integer", "maximum": 5}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_customer", "description": "Consultar um cliente identificado.", "parameters": {"type": "object", "properties": {"customer_id": {"type": "string"}}, "required": ["customer_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "list_coupons", "description": "Consultar cupons quando a conversa precisar disso.", "parameters": {"type": "object", "properties": {"code": {"type": "string"}, "limit": {"type": "integer", "maximum": 5}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_coupon", "description": "Consultar detalhes de um cupom.", "parameters": {"type": "object", "properties": {"coupon_id": {"type": "string"}}, "required": ["coupon_id"], "additionalProperties": False}}},
]

TOOL_REGISTRY = {
    "commerce": ("search_products", "get_product", "get_product_link", "check_inventory", "list_categories", "get_category", "get_category_tree", "list_product_variants", "get_product_variant", "search_customer", "get_customer", "list_coupons", "get_coupon", "create_cart", "get_cart", "get_cart_complete", "set_cart_item_quantity", "delete_cart", "get_payment_options", "quote_shipping", "list_shipping_methods", "create_order", "list_orders", "get_order", "get_order_complete", "get_order_payment"),
    "raffle": ("rules", "balance", "coupon_code", "raffle_history", "current_raffle", "simulation"),
}

_PRODUCT_FIELDS = ("id", "name", "reference", "ean", "brand", "model", "description", "category", "category_name", "category_id", "attributes", "properties", "color", "style", "material", "price", "promotional_price", "current_price", "stock", "available", "availability", "available_in_store", "available_for_purchase", "upon_request", "when_stock_runs_out", "has_variation", "order_days_availability", "lead_time", "lead_time_days", "availability_days", "delivery_days", "immediate_delivery", "ready_to_ship", "ProductSettings", "payment_option", "payment_option_details", "url", "primary_image_url", "primary_image", "image_url", "image", "images")
_CATEGORY_FIELDS = ("id", "name", "parent_id", "parent", "slug", "path")
_VARIANT_FIELDS = ("id", "variant_id", "product_id", "name", "value", "color", "size", "version", "choices", "properties", "attributes", "options", "option", "variation", "Variation", "reference", "sku", "Sku", "price", "promotional_price", "current_price", "stock", "available", "available_in_store", "availability", "VariationSettings", "primary_image_url", "primary_image", "image_url", "image", "images")
_CUSTOMER_FIELDS = ("id", "name", "email", "city", "state", "last_purchase", "total_orders")
_COUPON_FIELDS = ("id", "code", "description", "starts_at", "ends_at", "value", "type", "value_start", "value_end", "usage_counter_limit", "usage_counter_limit_customer", "coupon_type", "local_application", "freight_application")
_CART_CREATE_FIELDS = ("cart_id", "session_id", "cart_url", "message", "code")
_CART_FIELDS = ("cart_id", "session_id", "message", "code")
_CART_COMPLETE_FIELDS = ("cart_id", "session_id", "subtotal", "total", "current_total", "currency", "message", "code")
_CART_ITEM_FIELDS = ("product_id", "variant_id", "quantity", "price", "unit_price", "original_price", "total", "name", "reference", "payment_methods")
_SHIPPING_FIELDS = (
    "shipping_id", "quotation_id", "name", "identifier", "price",
    "min_period", "max_period", "estimated_delivery_date", "information",
    "tax_name", "tax_value",
)
_ORDER_FIELDS = (
    "success", "order_id", "id", "code", "session_id", "status",
    "status_group", "created_at", "order_created_at", "total", "subtotal",
    "shipping", "shipment", "sending_code", "tracking_url", "sending_date",
    "estimated_delivery_date", "products", "items", "payment",
    "customer_id", "customer",
)
_ORDER_PAYMENT_FIELDS = (
    "method_id", "method", "type", "has_payment", "payment_date",
    "payment_url",
)


def _clean_text(value: str) -> str:
    return html.unescape(value).strip()


def _clean_value(value: Any) -> Any:
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, list):
        return [_clean_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _clean_value(item) for key, item in value.items()}
    return value


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(Decimal(str(value).replace(",", ".")))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().casefold() in {"1", "true", "yes", "sim"}


def _payment_label(item: dict[str, Any]) -> str:
    value = " ".join(
        str(item.get(key) or "")
        for key in ("name", "text")
    )
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(char)
    ).casefold()


def _normalize_payment_plot(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    count = _integer(value.get("installments"))
    if count is None:
        return None
    normalized: dict[str, Any] = {
        "count": count,
        "value": _number(value.get("value")),
        "interest": _truthy(value.get("interest")),
        "interest_value": _number(value.get("interest_value")),
        "discount_value": _number(value.get("discount_value")),
        "base_value": _number(value.get("base_value")),
        "order_total": _number(value.get("order_total")),
    }
    return {
        key: item
        for key, item in normalized.items()
        if item is not None
    }


def _normalize_payment_options(value: Any) -> Any:
    if not isinstance(value, list):
        return _clean_value(value)
    normalized: dict[str, Any] = {
        "options": [],
        "installments": [],
    }
    for item in value:
        if not isinstance(item, dict):
            continue
        plots = [
            plot
            for plot in (
                _normalize_payment_plot(candidate)
                for candidate in (item.get("plots") or [])
            )
            if plot is not None
        ]
        option = {
            key: _clean_value(item[key])
            for key in ("id", "name", "text", "integration_code", "payment_method_id")
            if item.get(key) is not None
        }
        option.update({
            key: number
            for key, number in {
                "discount_value": _number(item.get("discount_value")),
                "increase_value": _number(item.get("increase_value")),
                "application_value": _number(item.get("application_value")),
                "total_base": _number(item.get("total_base")),
                "tax_value": _number(item.get("tax_value")),
            }.items()
            if number is not None
        })
        option["card"] = _integer(item.get("card"))
        option["plots"] = plots
        normalized["options"].append(option)

        label = _payment_label(item)
        if _integer(item.get("card")) == 1:
            normalized["card"] = option
            normalized["installments"].extend(plots)
        if "pix" in label.split():
            normalized["pix"] = option
        if "boleto" in label.split():
            normalized["boleto"] = option

    if not normalized["installments"]:
        normalized.pop("installments")
    if not normalized["options"]:
        normalized.pop("options")
    return normalized


def _items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "products", "categories", "variants", "variations", "customers", "coupons", "orders", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return [payload] if isinstance(payload, dict) else []


def _unwrap_entity(payload: Any, keys: tuple[str, ...]) -> Any:
    current = payload
    for _ in range(3):
        if not isinstance(current, dict):
            return current
        nested = next(
            (
                current.get(key)
                for key in keys
                if isinstance(current.get(key), dict)
            ),
            None,
        )
        if nested is None:
            return current
        current = nested
    return current


def _reduce(item: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"value": item}
    result: dict[str, Any] = {}
    for key in fields:
        if key not in item or item[key] is None:
            continue
        result[key] = _normalize_payment_options(item[key]) if key in {"payment_option", "payment_option_details"} else _clean_value(item[key])
    return result


def _reduce_products(payload: Any, limit: int = 5) -> dict[str, Any]:
    safe_limit = min(max(int(limit), 1), 20)
    result = {
        "products": [
            _reduce(item, _PRODUCT_FIELDS)
            for item in _items(payload)[:safe_limit]
        ]
    }
    paging = _reduce_paging(payload)
    if paging:
        result["paging"] = paging
    return result


def _looks_like_reference_code(value: str) -> bool:
    """Compact Tray refs like C050.607.44.011.02 or C63-36ADA4-S00P0-B0."""
    text = value.strip()
    if " " in text:
        return False
    if not re.search(r"\d", text) or not re.search(r"[A-Za-z]", text):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9]+(?:[./_-][A-Za-z0-9]+)+", text))


def _query_filters(query: str) -> list[dict[str, str]]:
    value = query.strip()
    ean_match = re.fullmatch(r"ean\s+(\d{8,14})", value, flags=re.IGNORECASE)
    if ean_match:
        return [{"ean": ean_match.group(1)}]
    if re.fullmatch(r"\d{8,14}", value):
        return [{"ean": value}, {"reference": value}]
    if _looks_like_reference_code(value):
        return [{"reference": value}, {"name": value}]
    # Compact model codes (PH2000M, C63): try name first, then reference.
    if " " not in value and re.search(r"\d", value) and re.search(r"[A-Za-z]", value):
        return [{"name": value}, {"reference": value}]
    return [{"name": value}]


def _fold_catalog_text(value: Any) -> str:
    text = str(value or "")
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", text).lower()
        if not unicodedata.combining(char)
    ).strip()


def _parse_search_tokens(value: Any) -> list[str]:
    if isinstance(value, str):
        parts = re.split(r"[,|]+", value)
    elif isinstance(value, (list, tuple)):
        parts = [str(item) for item in value]
    else:
        return []
    tokens: list[str] = []
    for part in parts:
        for token in re.findall(r"[a-z0-9]+", _fold_catalog_text(part)):
            if len(token) >= 2 and token not in tokens:
                tokens.append(token)
    return tokens


def _product_matches_search_tokens(
    product: dict[str, Any],
    tokens: list[str],
) -> bool:
    if not tokens:
        return True
    text = _fold_catalog_text(
        " ".join(
            str(product.get(field) or "")
            for field in ("name", "brand", "model", "reference", "description")
        )
    )
    return all(token in text for token in tokens)


async def _token_search_polyfill(
    client: TrayAdapterClient,
    *,
    tokens: list[str],
    brand: str | None,
    limit: int,
) -> dict[str, Any]:
    """Local AND/LIKE filter when adaptor /search is unavailable."""
    matched: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _absorb(raw: list[Any]) -> None:
        for item in raw:
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            product_id = str(item["id"])
            if product_id in seen:
                continue
            reduced = _reduce(item, _PRODUCT_FIELDS)
            if not _product_matches_search_tokens(reduced, tokens):
                continue
            seen.add(product_id)
            matched.append(reduced)

    # Name probes only — do NOT re-page the whole brand here (sales discovery
    # already does that; duplicating 8 brand pages blows the Vercel budget).
    joined = " ".join(tokens)
    attempts: list[dict[str, Any]] = [{"name": joined, "page": 1}]
    if brand:
        attempts.append({"name": joined, "brand": brand, "page": 1})
        if len(tokens) >= 2:
            attempts.append({
                "name": " ".join(tokens[-2:]),
                "brand": brand,
                "page": 1,
            })
        # Color-heavy token last (e.g. rosa) — Tray often indexes short hues.
        hue = tokens[-1]
        if hue and hue not in {"automatico", "quartz", "cronografo"}:
            attempts.append({"name": hue, "brand": brand, "page": 1})
            attempts.append({"name": hue, "brand": brand, "page": 2})
            attempts.append({"name": hue, "brand": brand, "page": 3})
    for filters in attempts:
        payload = await client.search_products(**filters, limit=limit)
        _absorb(_items(payload))
        if len(matched) >= limit:
            return {"products": matched[:limit]}

    return {"products": matched[:limit]}


async def search_products_by_tokens(
    client: TrayAdapterClient,
    *,
    tokens: list[str],
    brand: str | None = None,
    limit: int = 20,
    page: int = 1,
) -> dict[str, Any]:
    safe_limit = min(max(int(limit), 1), 20)
    if not tokens:
        return {"products": []}
    try:
        payload = await client.search_products_by_tokens(
            tokens=tokens,
            brand=brand,
            limit=safe_limit,
            page=page,
        )
        result = _reduce_products(payload, safe_limit)
        # Adaptor may return a loose set — enforce AND locally.
        result["products"] = [
            product
            for product in result["products"]
            if _product_matches_search_tokens(product, tokens)
        ]
        print("[tray.search.tokens]", {
            "source": "adaptor",
            "token_count": len(tokens),
            "result_count": len(result["products"]),
            "has_brand": bool(brand),
        })
        return result
    except TrayAdapterError as exc:
        # 404 = route not deployed yet → polyfill. Other errors bubble as empty
        # so the agent can still GPT-match against brand discovery results.
        if exc.status_code not in {404, 405, 501}:
            print("[tray.search.tokens]", {
                "source": "adaptor_error",
                "status_code": exc.status_code,
                "token_count": len(tokens),
            })
            raise
        result = await _token_search_polyfill(
            client,
            tokens=tokens,
            brand=brand,
            limit=safe_limit,
        )
        print("[tray.search.tokens]", {
            "source": "polyfill",
            "token_count": len(tokens),
            "result_count": len(result.get("products") or []),
            "has_brand": bool(brand),
        })
        return result


async def search_products(client: TrayAdapterClient, **args: Any) -> dict[str, Any]:
    tokens = _parse_search_tokens(args.pop("tokens", None))
    brand = args.get("brand")
    limit = min(max(int(args.get("limit", 5)), 1), 20)
    page = int(args.get("page") or 1)
    if tokens:
        return await search_products_by_tokens(
            client,
            tokens=tokens,
            brand=str(brand).strip() if brand else None,
            limit=limit,
            page=page,
        )

    query = (args.pop("query", None) or "").strip()
    supported = ("name", "reference", "ean", "brand", "category_id", "available", "available_in_store", "stock", "promotion", "page")
    explicit = {key: args.get(key) for key in supported if args.get(key) is not None}
    # Merge free-text query filters with explicit filters (e.g. brand + name from query).
    # Previously `explicit or filters` dropped the query entirely whenever brand/name was set.
    if query:
        attempts = []
        for filters in _query_filters(query):
            merged = {**filters}
            for key, value in explicit.items():
                if key not in merged:
                    merged[key] = value
            attempts.append(merged)
    else:
        attempts = [explicit]
    for filters in attempts:
        if not filters:
            continue
        payload = await client.search_products(**filters, limit=limit)
        result = _reduce_products(payload, limit)
        if result["products"] or len(attempts) == 1:
            return result
    return {"products": []}


def _reduce_variant(item: Any) -> dict[str, Any]:
    reduced = _reduce(item, _VARIANT_FIELDS)
    if reduced.get("variant_id") is None and reduced.get("id") is not None:
        reduced["variant_id"] = reduced["id"]
    return reduced


def _reduce_category_tree(value: Any) -> Any:
    if isinstance(value, list):
        return [_reduce_category_tree(item) for item in value]
    if not isinstance(value, dict):
        return value
    reduced = _reduce(value, _CATEGORY_FIELDS)
    for key in ("children", "subcategories", "categories", "items", "data", "tree", "category"):
        if key in value:
            reduced[key] = _reduce_category_tree(value[key])
    return reduced


def _reduce_paging(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    paging = payload.get("paging")
    if not isinstance(paging, dict):
        data = payload.get("data")
        paging = data.get("paging") if isinstance(data, dict) else None
    if not isinstance(paging, dict):
        return None
    reduced = {
        key: paging.get(key)
        for key in ("total", "page", "limit")
        if paging.get(key) is not None
    }
    return reduced or None


def _reduce_cart_complete(payload: Any) -> dict[str, Any]:
    cart = _unwrap_entity(payload, ("cart", "data", "result"))
    reduced = _reduce(cart, _CART_COMPLETE_FIELDS)
    item_values: Any = None
    if isinstance(cart, dict):
        for key in ("items", "products", "cart_items"):
            if isinstance(cart.get(key), list):
                item_values = cart[key]
                break
    if item_values is None and isinstance(payload, dict):
        for key in ("items", "products", "cart_items"):
            if isinstance(payload.get(key), list):
                item_values = payload[key]
                break
    reduced_items = []
    for item in item_values or []:
        if not isinstance(item, dict):
            continue
        normalized = _reduce(item, _CART_ITEM_FIELDS)
        normalized["variant_id"] = normalize_variant_identity(
            normalized.get("variant_id")
        )
        product = item.get("product") or item.get("Product")
        if isinstance(product, dict):
            if normalized.get("product_id") is None and product.get("id") is not None:
                normalized["product_id"] = product["id"]
            for key in ("name", "reference"):
                if normalized.get(key) is None and product.get(key) is not None:
                    normalized[key] = _clean_value(product[key])
        reduced_items.append(normalized)
    reduced["items"] = reduced_items
    return reduced


def _payment_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("payment_options", "options", "methods", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        data = payload.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return _payment_items(data)
    return []


def _semantic_payment_options(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    for container_key in ("payment_options", "data", "result"):
        nested = payload.get(container_key)
        if isinstance(nested, dict):
            semantic = _semantic_payment_options(nested)
            if semantic is not None:
                return semantic
    if any(key in payload for key in ("pix", "card", "boleto", "installments", "options")):
        return _clean_value({
            key: payload[key]
            for key in ("pix", "card", "boleto", "installments", "options")
            if key in payload
        })
    return None


def _shipping_options(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("options", "methods", "shippings", "items"):
            if isinstance(payload.get(key), list):
                return payload[key]
        for key in ("data", "result", "shipping"):
            nested = payload.get(key)
            if isinstance(nested, (dict, list)):
                found = _shipping_options(nested)
                if found:
                    return found
    return []


def _reduce_shipping(payload: Any) -> dict[str, Any]:
    base = _unwrap_entity(payload, ("data", "result", "shipping"))
    source = base if isinstance(base, dict) else payload
    return {
        "success": bool(source.get("success", True)) if isinstance(source, dict) else True,
        "zipcode": source.get("zipcode") if isinstance(source, dict) else None,
        "options": [
            _reduce(option, _SHIPPING_FIELDS)
            for option in _shipping_options(payload)
            if isinstance(option, dict)
        ],
    }


def _reduce_order(payload: Any) -> dict[str, Any]:
    order = _unwrap_entity(payload, ("order", "data", "result"))
    return _reduce(order, _ORDER_FIELDS)


def _reduce_orders(payload: Any) -> dict[str, Any]:
    values = _items(payload)
    return {"orders": [_reduce_order(value) for value in values]}


def _reduce_order_payment(payload: Any) -> dict[str, Any]:
    source = _unwrap_entity(payload, ("data", "result"))
    source = source if isinstance(source, dict) else {}
    payment = source.get("payment")
    if not isinstance(payment, dict):
        return {
            "success": bool(source.get("success", True)),
            "order_id": source.get("order_id"),
            "payment": None,
        }
    reduced_payment = {
        key: (
            payment.get(key)
            if key == "payment_url"
            else _clean_value(payment.get(key))
        )
        for key in _ORDER_PAYMENT_FIELDS
    }
    return {
        "success": bool(source.get("success", True)),
        "order_id": source.get("order_id"),
        "payment": reduced_payment,
    }


async def _execute_tool(name: str, arguments: dict[str, Any], client: TrayAdapterClient | None = None) -> dict[str, Any]:
    client = client or TrayAdapterClient()
    try:
        if name == "search_products":
            return await search_products(client, **arguments)
        if name == "get_product":
            payload = await client.get_product(arguments["product_id"])
            return _reduce(
                _unwrap_entity(payload, ("product", "data", "result")),
                _PRODUCT_FIELDS,
            )
        if name == "get_product_link":
            payload = await client.get_product(arguments["product_id"])
            product = _reduce(
                _unwrap_entity(payload, ("product", "data", "result")),
                _PRODUCT_FIELDS,
            )
            return {
                "product_id": str(
                    product.get("id")
                    or arguments["product_id"]
                ),
                "product_name": product.get("name"),
                "product_url": official_product_url(product),
            }
        if name == "check_inventory":
            return _reduce(await client.get_product_stock(arguments["product_id"]), ("product_id", "stock", "available", "available_in_store", "available_for_purchase", "upon_request", "availability", "when_stock_runs_out"))
        if name == "list_categories":
            payload = await client.list_categories(**arguments)
            result = {"categories": [_reduce(item, _CATEGORY_FIELDS) for item in _items(payload)]}
            paging = _reduce_paging(payload)
            if paging:
                result["paging"] = paging
            return result
        if name == "get_category":
            return _reduce(await client.get_category(arguments["category_id"]), _CATEGORY_FIELDS)
        if name == "get_category_tree":
            return {"tree": _reduce_category_tree(await client.get_category_tree(arguments["category_id"]))}
        if name == "list_product_variants":
            payload = await client.list_product_variants(arguments["product_id"])
            return {"variants": [_reduce_variant(item) for item in _items(payload)][:20]}
        if name == "get_product_variant":
            payload = await client.get_product_variant(arguments["variant_id"])
            return _reduce_variant(
                _unwrap_entity(payload, ("variant", "variation", "data", "result"))
            )
        if name == "search_customer":
            return {"customers": [_reduce(item, _CUSTOMER_FIELDS) for item in _items(await client.list_customers(**arguments))[:5]]}
        if name == "get_customer":
            return _reduce(await client.get_customer(arguments["customer_id"]), _CUSTOMER_FIELDS)
        if name == "list_coupons":
            return {"coupons": [_reduce(item, _COUPON_FIELDS) for item in _items(await client.list_coupons(**arguments))[:5]]}
        if name == "get_coupon":
            return _reduce(await client.get_coupon(arguments["coupon_id"]), _COUPON_FIELDS)
        if name == "create_cart":
            payload = await client.create_cart(**arguments)
            return _reduce(
                _unwrap_entity(payload, ("cart", "data", "result")),
                _CART_CREATE_FIELDS,
            )
        if name == "get_cart":
            payload = await client.get_cart(arguments["session_id"])
            return _reduce(
                _unwrap_entity(payload, ("cart", "data", "result")),
                _CART_FIELDS,
            )
        if name == "get_cart_complete":
            return _reduce_cart_complete(
                await client.get_cart_complete(arguments["session_id"])
            )
        if name == "set_cart_item_quantity":
            payload = await client.set_cart_item_quantity(**arguments)
            reduced = _reduce_cart_complete(payload)
            if isinstance(payload, dict):
                for key in ("success", "changed", "already_satisfied"):
                    if payload.get(key) is not None:
                        reduced[key] = payload[key]
            return reduced
        if name == "delete_cart":
            await client.delete_cart(arguments["session_id"])
            return {"success": True}
        if name == "get_payment_options":
            cart_session_id = arguments.get("cart_session_id")
            order_id = arguments.get("order_id")
            if bool(cart_session_id) == bool(order_id):
                return {
                    "error": "commerce_validation_error",
                    "error_type": "payment_options_scope_invalid",
                }
            payload = (
                await client.get_payment_options(cart_session_id)
                if cart_session_id else await client.get_payment_options(order_id=order_id)
            )
            semantic = _semantic_payment_options(payload)
            return {
                "payment_options": (
                    semantic
                    if semantic is not None
                    else _normalize_payment_options(_payment_items(payload))
                )
            }
        if name == "quote_shipping":
            return _reduce_shipping(await client.quote_shipping(**arguments))
        if name == "list_shipping_methods":
            return _reduce_shipping(await client.list_shipping_methods())
        if name == "create_order":
            return _reduce_order(await client.create_order(arguments))
        if name == "list_orders":
            return _reduce_orders(await client.list_orders(**arguments))
        if name == "get_order":
            return _reduce_order(await client.get_order(arguments["order_id"]))
        if name == "get_order_complete":
            return _reduce_order(
                await client.get_order_complete(arguments["order_id"])
            )
        if name == "get_order_payment":
            return _reduce_order_payment(
                await client.get_order_payment(arguments["order_id"])
            )
        raise ValueError(f"unknown_tool:{name}")
    except TrayAdapterError as exc:
        print("[tray.tool] request_failed", {
            "tool": name,
            "status": exc.status_code,
            **exc.diagnostics,
        })
        result = {
            "error": "commerce_upstream_error",
            "status_code": exc.status_code,
            "error_type": type(exc).__name__,
            "tray_error_code": exc.tray_error_code,
            "tray_error_type": exc.tray_error_type,
            "tray_error_field": exc.tray_error_field,
            "tray_error_fields": exc.tray_error_fields,
            "tray_error_message": exc.tray_error_message,
        }
        if exc.tray_error_name:
            result["tray_error_name"] = exc.tray_error_name
        if exc.tray_error_causes:
            result["tray_error_causes"] = exc.tray_error_causes
        if name == "list_categories":
            result["error_reason"] = (
                "category_invalid_request"
                if exc.status_code == 422
                else "category_adapter_error"
            )
        return result
async def execute_tool(name: str, arguments: dict[str, Any], client: TrayAdapterClient | None = None) -> dict[str, Any]:
    from .observability import record_tray_observation

    started = time.perf_counter()
    try:
        result = await _execute_tool(name, arguments, client)
        ok = "error" not in result
        elapsed_ms = (time.perf_counter() - started) * 1000
        record_tray_observation(
            tool=name,
            arguments=arguments,
            result=result,
            elapsed_ms=elapsed_ms,
        )
        print("[tray.tool] executed", {
            "tool": name,
            "ok": ok,
            "elapsed_ms": round(elapsed_ms),
            "status_code": result.get("status_code") if isinstance(result, dict) else None,
        })
        print("[sales.tool]", {"tool": name, "success": ok})
        return result
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        result = {
            "error": "N\u00e3o consegui consultar as informa\u00e7\u00f5es da loja neste momento. Tente novamente em instantes.",
            "error_type": type(exc).__name__,
            "status_code": getattr(exc, "status_code", None),
        }
        record_tray_observation(
            tool=name,
            arguments=arguments,
            result=result,
            elapsed_ms=elapsed_ms,
        )
        print("[tray.tool] executed", {
            "tool": name,
            "ok": False,
            "elapsed_ms": round(elapsed_ms),
            "error_type": type(exc).__name__,
        })
        print("[sales.tool]", {"tool": name, "success": False})
        return result
