from __future__ import annotations

from typing import Any

from .commerce_context import CommerceConversationState, checkout_fields_view


WORKING_MEMORY_USAGE_POLICY = (
    "Memória interna silenciosa. Use para continuidade e precisão. "
    "Não revele pedido, link de pagamento, CPF, e-mail ou endereço "
    "a menos que o cliente peça ou continue explicitamente esse assunto."
)


def build_working_memory(
    state: CommerceConversationState | dict[str, Any] | None,
) -> dict[str, Any]:
    """Compact facts for LLM context — never for unsolicited customer dumps."""
    payload = (
        state
        if isinstance(state, CommerceConversationState)
        else CommerceConversationState.from_payload(state)
    )
    active = payload.active_product
    # Only field presence flags — never raw CPF/email/address in the prompt dump.
    known_checkout = {
        key: True
        for key, present in checkout_fields_view(payload.checkout_draft).items()
        if present
    }
    payment_pending = bool(
        payload.order_id
        and (
            payload.pending_action == "awaiting_payment"
            or payload.order_payment_url
            or payload.order_payment_status in {"pending", "not_available", "unknown"}
        )
    )
    return {
        "usage_policy": WORKING_MEMORY_USAGE_POLICY,
        "active_domain": payload.active_domain,
        "purchase_stage": payload.purchase_stage,
        "pending_action": payload.pending_action,
        "has_cart": bool(payload.cart_session_id),
        "cart_item_count": len(payload.cart_items),
        "active_product": (
            {
                "name": active.name,
                "reference": active.reference,
                "brand": active.brand,
            }
            if active
            else None
        ),
        "last_presented_products": [
            {
                "position": item.position,
                "name": item.name,
                "reference": item.reference,
            }
            for item in payload.last_presented_products[:5]
        ],
        "has_open_order": bool(payload.order_id),
        "order_id": payload.order_id,
        "payment_pending": payment_pending,
        "payment_url_available": bool(payload.order_payment_url),
        "payment_url": payload.order_payment_url,
        "order_payment_status": payload.order_payment_status,
        "known_checkout_fields": known_checkout,
        "selected_payment_method": payload.selected_payment_method
        or payload.payment_method_preference,
    }


def format_working_memory_block(
    state: CommerceConversationState | dict[str, Any] | None,
) -> str:
    import json

    memory = build_working_memory(state)
    if not any(
        [
            memory.get("has_open_order"),
            memory.get("has_cart"),
            memory.get("active_product"),
            memory.get("last_presented_products"),
            memory.get("known_checkout_fields"),
            memory.get("pending_action"),
        ]
    ):
        return ""
    return (
        "WORKING_MEMORY (uso interno; não despejar no cliente):\n"
        + json.dumps(memory, ensure_ascii=False)
    )
