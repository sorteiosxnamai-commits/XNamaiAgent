from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from .checkout_data_service import checkout_data_template
from .commerce_context import (
    CHECKOUT_REQUIRED_FIELDS,
    CommerceConversationState,
    checkout_fields_view,
    checkout_missing_fields,
)
from .config import get_settings
from .models import AgentResult


CheckoutChannel = Literal["whatsapp", "site"]

# The agent can create the Tray order after an explicit review.
# Native PIX in chat is gated by PIX_DIRECT_ENABLED + MP token.
WHATSAPP_ORDER_SUPPORTED = True
WHATSAPP_HOSTED_PAYMENT_SUPPORTED = True
WHATSAPP_PAYMENT_SUPPORTED = False  # card/tokenization still outside this backend
WHATSAPP_CHECKOUT_SUPPORTED = False


def _site_url(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    parsed = urlparse(candidate)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        return None
    return candidate


def checkout_capabilities(
    state: CommerceConversationState,
    *,
    selected_channel: CheckoutChannel | None = None,
) -> dict:
    effective_channel = (
        selected_channel
        if selected_channel is not None
        else state.checkout_channel_preference
    )
    official_cart_url = _site_url(state.cart_url)
    cart_ready = bool(state.cart_session_id and official_cart_url)
    settings = get_settings()
    pix_direct = bool(
        cart_ready
        and settings.pix_direct_enabled
        and settings.resolved_mp_access_token()
    )
    supported = {
        "whatsapp": bool(cart_ready and WHATSAPP_ORDER_SUPPORTED),
        "site": bool(cart_ready and official_cart_url),
    }
    facts = {
        "cart_ready": cart_ready,
        "whatsapp_checkout_supported": bool(cart_ready and WHATSAPP_CHECKOUT_SUPPORTED),
        "whatsapp_order_supported": bool(cart_ready and WHATSAPP_ORDER_SUPPORTED),
        "whatsapp_hosted_payment_supported": bool(
            cart_ready and WHATSAPP_HOSTED_PAYMENT_SUPPORTED
        ),
        "whatsapp_native_payment_supported": pix_direct,
        "whatsapp_payment_supported": bool(cart_ready and WHATSAPP_PAYMENT_SUPPORTED),
        "pix_direct_enabled": pix_direct,
        "site_checkout_supported": supported["site"],
        "requires_channel_choice": bool(
            cart_ready
            and (
                effective_channel is None
                or not supported[effective_channel]
            )
        ),
        "selected_channel": effective_channel,
        "selected_channel_supported": (
            supported[effective_channel]
            if effective_channel is not None
            else None
        ),
        "sensitive_payment_data_allowed_in_chat": False,
    }
    if effective_channel == "site" and official_cart_url:
        facts["cart_url"] = official_cart_url
    return facts

def select_checkout_channel(
    state: CommerceConversationState,
    channel: CheckoutChannel,
) -> AgentResult:
    facts = checkout_capabilities(state, selected_channel=channel)
    if not facts["cart_ready"]:
        return AgentResult(
            reply_text="Ainda não há um carrinho pronto para escolher o canal de checkout.",
            intent="commerce",
            handoff_required=False,
            safety_reason="cart_validation_error",
            commercial_data={"checkout": facts},
            response_metadata={"domain": "commerce"},
        )

    supported = bool(facts["selected_channel_supported"])
    missing_checkout_fields = (
        checkout_missing_fields(state.checkout_draft)
        if supported and channel == "whatsapp"
        else []
    )
    whatsapp_needs_data = bool(missing_checkout_fields)
    whatsapp_needs_zipcode = bool(
        supported
        and channel == "whatsapp"
        and not whatsapp_needs_data
        and not state.shipping_quotes
        and not state.selected_shipping
    )
    if whatsapp_needs_data:
        print("[sales.checkout.next_requirement]", {
            "purchase_stage": "checkout_data",
            "pending_action": "awaiting_checkout_data",
            "missing_fields": missing_checkout_fields,
        })
    elif whatsapp_needs_zipcode:
        print("[sales.checkout.next_requirement]", {
            "purchase_stage": "shipping",
            "pending_action": "awaiting_shipping_zipcode",
            "blocker_codes": ["shipping_zipcode_missing"],
        })
    template = checkout_data_template(missing_checkout_fields)
    return AgentResult(
        reply_text=(
            template
            if template
            else "Canal de checkout registrado."
            if supported
            else "O canal solicitado ainda não possui suporte técnico para concluir esta compra."
        ),
        intent="commerce",
        handoff_required=False,
        safety_reason=None if supported else "checkout_channel_unavailable",
        commercial_data={
            "checkout": facts,
            "checkout_fields": checkout_fields_view(state.checkout_draft),
            "required_fields": list(CHECKOUT_REQUIRED_FIELDS),
            "missing_fields": missing_checkout_fields,
            "input_template": template or None,
            "cart": {
                "status": "cart_ready",
                "items": [
                    item.model_dump(mode="json")
                    for item in state.cart_items
                ],
                **(
                    {"cart_url": facts["cart_url"]}
                    if "cart_url" in facts else {}
                ),
            }
        },
        response_metadata={
            "domain": "commerce",
            "checkout_channel_preference": channel,
            "purchase_stage": (
                "checkout_data"
                if whatsapp_needs_data
                else "shipping"
                if whatsapp_needs_zipcode
                else "checkout_ready"
                if supported
                else "checkout_channel_selection"
            ),
            "clear_pending_action": (
                supported
                and not whatsapp_needs_data
                and not whatsapp_needs_zipcode
            ),
            **(
                {
                    "pending_action": "choose_checkout_channel",
                    "pending_action_product_ids": [],
                }
                if not supported
                else {
                    "pending_action": "awaiting_checkout_data",
                    "pending_action_product_ids": [],
                }
                if whatsapp_needs_data
                else {
                    "pending_action": "awaiting_shipping_zipcode",
                    "pending_action_product_ids": [],
                }
                if whatsapp_needs_zipcode
                else {}
            ),
            "used_tray": False,
        },
    )
