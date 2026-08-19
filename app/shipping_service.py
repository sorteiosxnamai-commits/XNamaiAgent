from __future__ import annotations

import hashlib
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable

from .checkout_data_service import normalize_zipcode
from .commerce_context import (
    CommerceCartItem,
    CommerceConversationState,
    ShippingQuote,
    normalize_variant_identity,
)
from .models import AgentResult


ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def _failure_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "shipping_failure_status": "status_code",
        "shipping_failure_code": "tray_error_code",
        "shipping_failure_name": "tray_error_name",
        "shipping_failure_type": "tray_error_type",
        "shipping_failure_field": "tray_error_field",
        "shipping_failure_fields": "tray_error_fields",
        "shipping_failure_causes": "tray_error_causes",
        "shipping_failure_message": "tray_error_message",
    }
    return {
        target: payload[source]
        for target, source in mapping.items()
        if payload.get(source) not in (None, "", [])
    }


def _session_tag(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]


def cart_shipping_products(
    cart: dict[str, Any],
    factual_items: list[CommerceCartItem] | None = None,
) -> list[dict[str, Any]]:
    factual_prices = {
        (item.product_id, normalize_variant_identity(item.variant_id)): item.unit_price
        for item in factual_items or []
        if item.unit_price is not None
    }
    products: list[dict[str, Any]] = []
    for item in cart.get("items") or []:
        if not isinstance(item, dict):
            continue
        product_id = item.get("product_id") or item.get("id")
        quantity = item.get("quantity")
        price = item.get("unit_price")
        if price is None:
            price = item.get("price")
        variant_id = normalize_variant_identity(item.get("variant_id"))
        if price is None and product_id is not None:
            price = factual_prices.get((str(product_id), variant_id))
        try:
            amount = Decimal(str(price))
            parsed_quantity = int(quantity)
        except (InvalidOperation, TypeError, ValueError):
            continue
        if product_id is None or parsed_quantity < 1 or amount <= 0:
            continue
        products.append({
            "product_id": str(product_id),
            "variant_id": variant_id,
            "price": format(amount.quantize(Decimal("0.01")), "f"),
            "quantity": parsed_quantity,
        })
    return products


def _parse_quote(option: dict[str, Any]) -> ShippingQuote | None:
    shipping_id = option.get("shipping_id")
    name = str(option.get("name") or "").strip()
    try:
        amount = Decimal(str(option.get("price")))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if shipping_id is None or not name or amount < 0:
        return None
    payload = {
        **option,
        "shipping_id": shipping_id,
        "quotation_id": (
            str(option["quotation_id"]).strip() or None
            if option.get("quotation_id") is not None else None
        ),
        "identifier": (
            str(option["identifier"])
            if option.get("identifier") is not None else None
        ),
        "name": name,
        "price": format(amount.quantize(Decimal("0.01")), "f"),
    }
    try:
        return ShippingQuote.model_validate(payload)
    except (TypeError, ValueError):
        return None


async def quote_shipping(
    *,
    state: CommerceConversationState,
    zipcode: str,
    execute: ToolExecutor,
    cart_snapshot: dict[str, Any] | None = None,
) -> AgentResult:
    if state.checkout_channel_preference != "whatsapp":
        return AgentResult(
            reply_text="O canal WhatsApp e necessario para cotar frete pelo agente.",
            intent="commerce",
            safety_reason="whatsapp_order_channel_required",
            commercial_data={"success": False, "stage": "shipping_quote"},
            response_metadata={"domain": "commerce", "used_tray": False},
        )
    normalized_zipcode = normalize_zipcode(zipcode)
    if normalized_zipcode is None:
        return AgentResult(
            reply_text="CEP inv\u00e1lido para cota\u00e7\u00e3o.",
            intent="commerce",
            safety_reason="shipping_zipcode_invalid",
            commercial_data={"success": False, "stage": "shipping_quote"},
            response_metadata={"domain": "commerce", "used_tray": False},
        )
    if not state.cart_session_id:
        return AgentResult(
            reply_text="N\u00e3o h\u00e1 carrinho ativo para cota\u00e7\u00e3o.",
            intent="commerce",
            safety_reason="cart_validation_error",
            commercial_data={"success": False, "stage": "shipping_quote"},
            response_metadata={"domain": "commerce", "used_tray": False},
        )

    session_tag = _session_tag(state.cart_session_id)
    print("[sales.shipping.quote.request]", {
        "session": session_tag,
        "zipcode_valid": True,
    })
    if cart_snapshot is not None:
        cart = cart_snapshot
    else:
        try:
            cart = await execute("get_cart_complete", {"session_id": state.cart_session_id})
        except Exception as exc:
            cart = {"error": "commerce_upstream_error", "error_type": type(exc).__name__}
    if "error" in cart:
        print("[sales.shipping.quote.result]", {
            "session": session_tag, "success": False, "option_count": 0,
        })
        return AgentResult(
            reply_text="A cota\u00e7\u00e3o de frete n\u00e3o p\u00f4de ser conclu\u00edda.",
            intent="commerce",
            safety_reason="shipping_quote_technical_failure",
            commercial_data={
                "success": False,
                "stage": "shipping_quote",
                "recoverable": cart.get("status_code") in {502, 503, 504},
            },
            response_metadata={
                "domain": "commerce", "used_tray": True,
                **_failure_metadata(cart),
            },
        )
    products = cart_shipping_products(cart, state.cart_items)
    if not products or len(products) != len(cart.get("items") or []):
        return AgentResult(
            reply_text="Os itens reais do carrinho n\u00e3o puderam ser validados para o frete.",
            intent="commerce",
            safety_reason="shipping_cart_validation_error",
            commercial_data={"success": False, "stage": "shipping_quote"},
            response_metadata={"domain": "commerce", "used_tray": True},
        )
    try:
        result = await execute(
            "quote_shipping",
            {"zipcode": normalized_zipcode, "products": products},
        )
    except Exception as exc:
        result = {"error": "commerce_upstream_error", "error_type": type(exc).__name__}
    if "error" in result or result.get("success") is False:
        print("[sales.shipping.quote.result]", {
            "session": session_tag, "success": False, "option_count": 0,
        })
        return AgentResult(
            reply_text="A cota\u00e7\u00e3o de frete n\u00e3o p\u00f4de ser conclu\u00edda.",
            intent="commerce",
            safety_reason=(
                "shipping_quote_upstream_validation_failed"
                if result.get("status_code") == 422
                else "shipping_quote_technical_failure"
            ),
            commercial_data={
                "success": False,
                "stage": "shipping_quote",
                "recoverable": result.get("status_code") in {502, 503, 504},
            },
            response_metadata={
                "domain": "commerce", "used_tray": True,
                **_failure_metadata(result),
            },
        )
    quotes: list[ShippingQuote] = []
    for option in result.get("options") or []:
        if not isinstance(option, dict):
            continue
        parsed = _parse_quote(option)
        if parsed is not None:
            quotes.append(parsed)
    quote_payload = [quote.model_dump(mode="json") for quote in quotes]
    factual_original_prices = {
        (item.product_id, normalize_variant_identity(item.variant_id)): item.original_price
        for item in state.cart_items
        if item.original_price is not None
    }
    known_item_names: dict[tuple[str, str | None], list[str]] = {}
    for item in state.cart_items:
        if not item.name:
            continue
        key = (item.product_id, normalize_variant_identity(item.variant_id))
        known_item_names.setdefault(key, []).append(item.name)

    def reconciled_item_name(product: dict[str, Any]) -> str | None:
        key = (
            str(product["product_id"]),
            normalize_variant_identity(product.get("variant_id")),
        )
        names = known_item_names.get(key, [])
        return names[0] if len(names) == 1 else None

    draft = state.checkout_draft.model_copy(deep=True)
    draft.address.zip_code = normalized_zipcode
    print("[sales.shipping.quote.result]", {
        "session": session_tag,
        "success": True,
        "option_count": len(quotes),
    })
    return AgentResult(
        reply_text="Cota\u00e7\u00e3o de frete consultada.",
        intent="commerce",
        safety_reason="shipping_options_empty" if not quotes else None,
        commercial_data={
            "success": True,
            "zipcode": normalized_zipcode,
            "options": quote_payload,
        },
        response_metadata={
            "domain": "commerce",
            "cart_state": {
                "cart_id": state.cart_id,
                "cart_session_id": state.cart_session_id,
                "cart_url": state.cart_url,
                "cart_product_id": products[-1]["product_id"],
                "cart_variant_id": products[-1]["variant_id"],
                "cart_quantity": products[-1]["quantity"],
                "cart_items": [
                    {
                        "product_id": product["product_id"],
                        "variant_id": product["variant_id"],
                        "quantity": product["quantity"],
                        "unit_price": product["price"],
                        "original_price": factual_original_prices.get((
                            product["product_id"],
                            normalize_variant_identity(product["variant_id"]),
                        )),
                        "name": reconciled_item_name(product),
                    }
                    for product in products
                ],
            },
            "cart_snapshot": cart,
            "shipping_state": {
                "shipping_quote_zipcode": normalized_zipcode,
                "shipping_quotes": quote_payload,
                "selected_shipping": None,
            },
            "checkout_state": {"checkout_draft": draft.model_dump(mode="json")},
            "purchase_stage": "shipping_quote",
            **(
                {
                    "pending_action": "awaiting_shipping_selection",
                    "pending_action_product_ids": [],
                }
                if quotes else {"clear_pending_action": True}
            ),
            "used_tray": True,
        },
    )


async def list_shipping_methods(*, execute: ToolExecutor) -> AgentResult:
    try:
        result = await execute("list_shipping_methods", {})
    except Exception as exc:
        result = {"error": "commerce_upstream_error", "error_type": type(exc).__name__}
    if "error" in result:
        return AgentResult(
            reply_text="Os m\u00e9todos de envio n\u00e3o puderam ser consultados.",
            intent="commerce",
            safety_reason="shipping_methods_technical_failure",
            commercial_data={"success": False, "stage": "shipping_methods"},
            response_metadata={
                "domain": "commerce",
                "used_tray": True,
                **_failure_metadata(result),
            },
        )
    return AgentResult(
        reply_text="M\u00e9todos de envio consultados.",
        intent="commerce",
        commercial_data={
            "success": True,
            "methods": result.get("options") or result.get("methods") or [],
        },
        response_metadata={"domain": "commerce", "used_tray": True},
    )


def select_shipping(
    state: CommerceConversationState,
    selection_id: str | None = None,
    selection_position: int | None = None,
) -> AgentResult:
    if state.checkout_channel_preference != "whatsapp":
        return AgentResult(
            reply_text="O canal WhatsApp e necessario para selecionar frete pelo agente.",
            intent="commerce",
            safety_reason="whatsapp_order_channel_required",
            commercial_data={"success": False, "stage": "shipping_selection"},
            response_metadata={"domain": "commerce", "used_tray": False},
        )
    requested = str(selection_id or "").strip()
    if selection_position is not None and 1 <= selection_position <= len(state.shipping_quotes):
        matches = [state.shipping_quotes[selection_position - 1]]
    else:
        matches = [
            quote for quote in state.shipping_quotes
            if requested in {
                str(value) for value in (
                    quote.shipping_id, quote.quotation_id, quote.identifier,
                ) if value is not None
            }
            or requested.casefold() == quote.name.casefold()
        ]
    if len(matches) != 1:
        return AgentResult(
            reply_text="A op\u00e7\u00e3o de frete n\u00e3o corresponde \u00e0 cota\u00e7\u00e3o ativa.",
            intent="commerce",
            safety_reason="shipping_selection_invalid",
            commercial_data={
                "success": False,
                "stage": "shipping_selection",
                "options": [quote.model_dump(mode="json") for quote in state.shipping_quotes],
            },
            response_metadata={"domain": "commerce", "used_tray": False},
        )
    selected = matches[0]
    print("[sales.shipping.select]", {
        "shipping_id": selected.shipping_id,
        "quotation_id_present": bool(selected.quotation_id),
    })
    return AgentResult(
        reply_text="Op\u00e7\u00e3o de frete validada.",
        intent="commerce",
        commercial_data={"success": True, "selected_shipping": selected.model_dump(mode="json")},
        response_metadata={
            "domain": "commerce",
            "shipping_state": {"selected_shipping": selected.model_dump(mode="json")},
            "purchase_stage": "checkout_data",
            "pending_action": "awaiting_checkout_data",
            "pending_action_product_ids": [],
            "used_tray": False,
        },
    )
