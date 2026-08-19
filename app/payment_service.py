from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .checkout_service import checkout_capabilities
from .commerce_context import (
    CommerceConversationState,
    checkout_missing_fields,
    normalize_variant_identity,
)
from .models import AgentResult


ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def _payment_failure_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "payment_failure_status": "status_code",
        "payment_failure_code": "tray_error_code",
        "payment_failure_name": "tray_error_name",
        "payment_failure_type": "tray_error_type",
        "payment_failure_field": "tray_error_field",
        "payment_failure_fields": "tray_error_fields",
        "payment_failure_causes": "tray_error_causes",
        "payment_failure_message": "tray_error_message",
    }
    return {
        target: payload[source]
        for target, source in mapping.items()
        if payload.get(source) not in (None, "", [])
    }


def _no_cart() -> AgentResult:
    return AgentResult(
        reply_text="Ainda não há um carrinho ativo para consultar.",
        intent="commerce",
        handoff_required=False,
        safety_reason="cart_validation_error",
    )


async def inspect_current_cart(
    *,
    state: CommerceConversationState,
    execute: ToolExecutor,
) -> AgentResult:
    if not state.cart_session_id:
        return _no_cart()
    cart = await execute(
        "get_cart_complete",
        {"session_id": state.cart_session_id},
    )
    if "error" in cart:
        return AgentResult(
            reply_text="Não consegui consultar o carrinho neste momento.",
            intent="commerce",
            handoff_required=False,
            safety_reason="cart_technical_failure",
            response_metadata={"used_tray": True},
        )
    print("[sales.cart.verify]", {
        "item_count": len(cart.get("items") or []),
        "has_total": any(
            cart.get(key) is not None
            for key in ("total", "current_total", "subtotal")
        ),
    })
    return AgentResult(
        reply_text="Consultei o estado atual do seu carrinho.",
        intent="commerce",
        handoff_required=False,
        commercial_data={
            "cart": _safe_cart_facts(cart, state),
            "checkout": checkout_capabilities(state),
        },
        response_metadata={
            "domain": "commerce",
            "purchase_stage": "cart_created",
            "used_tray": True,
        },
    )


def checkout_payment_blockers(
    state: CommerceConversationState,
) -> list[str]:
    if state.checkout_channel_preference != "whatsapp":
        return []
    blockers: list[str] = []
    if not state.shipping_quote_zipcode and not state.shipping_quotes:
        blockers.append("shipping_zipcode_missing")
    elif not state.selected_shipping:
        blockers.append("shipping_not_selected")
    missing = checkout_missing_fields(state.checkout_draft)
    if missing:
        blockers.append("checkout_data_missing")
    return blockers


def _blocked_payment_advance(
    state: CommerceConversationState,
    blockers: list[str],
) -> AgentResult:
    if "shipping_zipcode_missing" in blockers:
        pending_action = "awaiting_shipping_zipcode"
        purchase_stage = "shipping"
    elif "shipping_not_selected" in blockers:
        pending_action = "awaiting_shipping_selection"
        purchase_stage = "shipping"
    else:
        pending_action = "awaiting_checkout_data"
        purchase_stage = "checkout_ready"
    print("[sales.checkout.blocked]", {
        "purchase_stage": purchase_stage,
        "pending_action": pending_action,
        "blocker_codes": blockers,
    })
    return AgentResult(
        reply_text="O avanço do checkout está bloqueado por requisitos factuais pendentes.",
        intent="commerce",
        handoff_required=False,
        safety_reason="checkout_requirements_missing",
        commercial_data={
            "checkout_ready_for_payment": False,
            "checkout_blockers": blockers,
            "missing_checkout_requirements": blockers,
            "payment_method": {
                "type": state.selected_payment_method,
                "name": (
                    state.selected_payment_option.name
                    if state.selected_payment_option else None
                ),
                "available": bool(state.selected_payment_option),
            },
            "hosted_payment": {
                "order_created": bool(state.order_id),
                "payment_url_available": bool(
                    state.order_id and state.order_payment_url
                ),
            },
        },
        response_metadata={
            "domain": "commerce",
            "purchase_stage": purchase_stage,
            "pending_action": pending_action,
            "pending_action_product_ids": [],
            "used_tray": False,
        },
    )


def _safe_cart_facts(
    cart: dict[str, Any],
    state: CommerceConversationState,
) -> dict[str, Any]:
    safe = {
        key: value
        for key, value in cart.items()
        if key != "cart_url"
    }
    if state.checkout_channel_preference == "site" and cart.get("cart_url"):
        safe["cart_url"] = cart["cart_url"]
    return safe


def _product_payment_restrictions(
    cart: dict[str, Any],
    option: dict[str, Any] | None,
) -> dict[str, Any]:
    """Apply factual PaymentMethodByProduct rules only when a product supplies them."""
    if not isinstance(option, dict) or option.get("id") is None:
        return {"blocked": False, "max_plots": None}
    option_id = str(option["id"])
    caps: list[int] = []
    for item in cart.get("items") or []:
        if not isinstance(item, dict):
            continue
        methods = item.get("payment_methods")
        if not isinstance(methods, list) or not methods:
            continue
        for rule in methods:
            if not isinstance(rule, dict):
                continue
            rule_id = rule.get("payment_method_id") or rule.get("id")
            if rule_id is None or str(rule_id) != option_id:
                continue
            if str(rule.get("blocked", "0")).strip() in {"1", "true", "True"}:
                return {"blocked": True, "max_plots": None}
            try:
                cap = int(rule.get("max_plots") or 0)
            except (TypeError, ValueError):
                cap = 0
            if cap > 0:
                caps.append(cap)
    return {"blocked": False, "max_plots": min(caps) if caps else None}


def _cap_option_plots(option: dict[str, Any], max_plots: int | None) -> dict[str, Any]:
    if not max_plots:
        return option
    capped = dict(option)
    plots = option.get("plots")
    if isinstance(plots, list):
        capped_plots = []
        for plot in plots:
            if not isinstance(plot, dict):
                continue
            try:
                count = int(plot.get("count") or 0)
            except (TypeError, ValueError):
                continue
            if count <= max_plots:
                capped_plots.append(plot)
        capped["plots"] = capped_plots
    return capped


async def inspect_payment_options(
    *,
    state: CommerceConversationState,
    installment_count: int | None,
    payment_method_preference: str | None = None,
    execute: ToolExecutor,
    payment_option_id: str | None = None,
    advance_checkout: bool = False,
    reconciled_cart: dict[str, Any] | None = None,
) -> AgentResult:
    if not state.cart_session_id:
        return _no_cart()
    blockers = checkout_payment_blockers(state)
    if advance_checkout and blockers:
        return _blocked_payment_advance(state, blockers)

    cart = reconciled_cart
    # Cart state intentionally stores only stable checkout identity.  Fetch the
    # complete factual cart when product payment rules are not present there.
    has_product_payment_methods = any(
        isinstance(item, dict) and "payment_methods" in item
        for item in ((cart or {}).get("items") or [])
    )
    if cart is None or not has_product_payment_methods:
        cart = await execute(
            "get_cart_complete",
            {"session_id": state.cart_session_id},
        )
    if "error" in cart:
        return AgentResult(
            reply_text="Não consegui reconciliar o carrinho neste momento.",
            intent="commerce",
            handoff_required=False,
            safety_reason="cart_technical_failure",
            response_metadata={"used_tray": True},
        )
    print("[sales.cart.reconcile]", {
        "attempted": True,
        "found": bool(cart.get("items")),
        "item_count": len(cart.get("items") or []),
    })
    result = await execute(
        "get_payment_options",
        {"cart_session_id": state.cart_session_id},
    )
    if "error" in result:
        print("[sales.purchase.payment]", {
            "has_cart_session": True,
            "requested_method": payment_method_preference,
            "options_loaded": False,
            "method_available": None,
        })
        return AgentResult(
            reply_text="Não consegui consultar as formas de pagamento neste momento.",
            intent="commerce",
            handoff_required=False,
            safety_reason="payment_options_technical_failure",
            commercial_data={"cart": _safe_cart_facts(cart, state)},
            response_metadata={"used_tray": True},
        )
    options = result.get("payment_options")
    options = options if isinstance(options, dict) else {}
    installments = options.get("installments")
    installments = installments if isinstance(installments, list) else []
    selected = None
    if installment_count is not None:
        selected = next(
            (
                item
                for item in installments
                if isinstance(item, dict)
                and item.get("count") == installment_count
            ),
            None,
        )
    method_available: bool | None = None
    selected_option: dict[str, Any] | None = None
    available_options = options.get("options")
    available_options = available_options if isinstance(available_options, list) else []
    if payment_option_id is not None:
        selected_option = next(
            (
                option for option in available_options
                if isinstance(option, dict)
                and str(option.get("id")) == str(payment_option_id)
            ),
            None,
        )
        method_available = selected_option is not None
    elif payment_method_preference == "pix":
        selected_option = options.get("pix") if isinstance(options.get("pix"), dict) else None
        method_available = selected_option is not None
    elif payment_method_preference == "card":
        selected_option = options.get("card") if isinstance(options.get("card"), dict) else None
        method_available = selected_option is not None or bool(installments)
    elif payment_method_preference == "boleto":
        selected_option = options.get("boleto") if isinstance(options.get("boleto"), dict) else None
        method_available = selected_option is not None
    restrictions = _product_payment_restrictions(cart, selected_option)
    if restrictions["blocked"]:
        selected_option = None
        method_available = False
    elif selected_option is not None:
        selected_option = _cap_option_plots(
            selected_option, restrictions["max_plots"],
        )
    print("[sales.payment.options]", {
        "has_session_id": True,
        "option_count": (
            len(options.get("options"))
            if isinstance(options.get("options"), list)
            else len(installments) + int("pix" in options) + int("boleto" in options)
        ),
    })
    print("[sales.payment.method]", {
        "requested_method": payment_method_preference,
        "method_available": method_available,
        "order_id": state.order_id,
        "payment_url_present": bool(state.order_id and state.order_payment_url),
    })
    selected_option_facts = (
        {
            "id": (
                str(selected_option["id"])
                if selected_option.get("id") is not None else None
            ),
            "name": str(selected_option["name"]),
            "method": payment_method_preference,
            "installments": selected_option.get("plots") or [],
            **(
                {"integration_code": str(selected_option["integration_code"])}
                if selected_option.get("integration_code") is not None else {}
            ),
            **{
                key: selected_option[key]
                for key in (
                    "discount_value", "increase_value", "application_value", "total_base", "tax_value"
                )
                if selected_option.get(key) is not None
            },
        }
        if selected_option is not None and selected_option.get("name")
        else None
    )
    facts: dict[str, Any] = {
        "payment_options": options,
        "requested_method": payment_method_preference,
        "requested_payment_option_id": payment_option_id,
        "requested_method_available": method_available,
        "cart_payment_method_available": method_available is True,
        "product_payment_restrictions": restrictions,
        "requested_installment_count": installment_count,
        "requested_installment": selected,
        "payment_method": {
            "type": payment_method_preference,
            "name": selected_option.get("name") if selected_option else None,
            "available": method_available,
        },
        "selected_payment_option": selected_option_facts,
        "payment_amounts": {
            "product_subtotal": cart.get("subtotal"),
            "shipping_value": (
                state.selected_shipping.price if state.selected_shipping else None
            ),
            "cart_total": (
                cart.get("total") or cart.get("current_total")
            ),
            "payment_discount_value": (
                selected_option.get("discount_value") if selected_option else None
            ),
            "payment_application_value": (
                selected_option.get("application_value") if selected_option else None
            ),
            "payment_option_total_base": (
                selected_option.get("total_base") if selected_option else None
            ),
            # Tray's option fields do not prove a payable total including freight.
            "payment_payable_total": None,
        },
        "hosted_payment": {
            "order_created": bool(state.order_id),
            "payment_url_available": bool(
                state.order_id and state.order_payment_url
            ),
        },
        "checkout_ready_for_payment": not blockers,
        "checkout_blockers": blockers,
        "cart": _safe_cart_facts(cart, state),
        "checkout": checkout_capabilities(state),
    }
    if payment_method_preference is not None and method_available is False:
        reply = "A forma escolhida não aparece nas opções factuais deste carrinho."
    elif installment_count is not None and selected is None:
        reply = "A Tray não informou essa quantidade de parcelas para este carrinho."
    else:
        reply = "Consultei as formas de pagamento reais deste carrinho."
    cart_items = cart.get("items") if isinstance(cart.get("items"), list) else []
    previous_items = {
        (item.product_id, normalize_variant_identity(item.variant_id)): (
            item.unit_price,
            item.original_price,
            item.name,
        )
        for item in state.cart_items
    }
    normalized_cart_items = []
    for item in cart_items:
        if not isinstance(item, dict):
            continue
        product_id = item.get("product_id") or item.get("id")
        if product_id is None:
            continue
        try:
            variant_id = normalize_variant_identity(item.get("variant_id"))
            quantity = int(item.get("quantity") or 1)
        except (TypeError, ValueError):
            continue
        persisted = previous_items.get((str(product_id), variant_id), (None, None, None))
        normalized_cart_items.append({
            "product_id": str(product_id),
            "variant_id": variant_id,
            "quantity": quantity,
            "unit_price": (
                str(item.get("unit_price") or item.get("price"))
                if item.get("unit_price") is not None or item.get("price") is not None
                else persisted[0]
            ),
            "original_price": (
                str(item["original_price"])
                if item.get("original_price") is not None
                else persisted[1]
            ),
            "name": (
                str(item.get("name") or item.get("product_name"))
                if item.get("name") or item.get("product_name")
                else persisted[2]
            ),
        })
    previous_signature = sorted(
        (
            item.product_id,
            normalize_variant_identity(item.variant_id),
            item.quantity,
        )
        for item in state.cart_items
    )
    current_signature = sorted(
        (item["product_id"], item["variant_id"], item["quantity"])
        for item in normalized_cart_items
    )
    cart_state_metadata = {}
    if current_signature != previous_signature:
        cart_state_metadata = {
            "cart_materially_changed": True,
            "cart_state": {
                "cart_id": state.cart_id,
                "cart_session_id": state.cart_session_id,
                "cart_url": state.cart_url,
                "cart_product_id": (
                    normalized_cart_items[-1]["product_id"]
                    if normalized_cart_items else None
                ),
                "cart_variant_id": (
                    normalized_cart_items[-1]["variant_id"]
                    if normalized_cart_items else None
                ),
                "cart_quantity": (
                    normalized_cart_items[-1]["quantity"]
                    if normalized_cart_items else None
                ),
                "cart_items": normalized_cart_items,
            },
        }
    return AgentResult(
        reply_text=reply,
        intent="commerce",
        handoff_required=False,
        safety_reason=(
            "payment_method_unavailable"
            if (payment_method_preference is not None or payment_option_id is not None)
            and method_available is False
            else None
        ),
        commercial_data=facts,
        response_metadata={
            "domain": "commerce",
            "purchase_stage": "payment_discussion",
            **(
                {"payment_method_preference": payment_method_preference}
                if payment_method_preference is not None else {}
            ),
            **cart_state_metadata,
            **(
                {
                    "selected_payment_method": payment_method_preference,
                    "selected_payment_option": selected_option_facts,
                    **(
                        {"selected_payment_option_id": str(selected_option["id"])}
                        if selected_option.get("id") is not None else {}
                    ),
                }
                if method_available is True
                and selected_option is not None
                and selected_option.get("name")
                else {
                    "selected_payment_method": payment_method_preference,
                }
                if method_available is True
                and payment_method_preference is not None
                else {}
            ),
            **(
                {
                    "pending_action": "choose_checkout_channel",
                    "pending_action_product_ids": [],
                }
                if state.checkout_channel_preference is None
                else {
                    "pending_action": "awaiting_checkout_data",
                    "pending_action_product_ids": [],
                }
                if state.checkout_channel_preference == "whatsapp"
                and checkout_missing_fields(state.checkout_draft)
                else {"clear_pending_action": True}
            ),
            "used_tray": True,
        },
    )

async def inspect_order_payment(
    *,
    state: CommerceConversationState,
    execute: ToolExecutor,
    order_id: str | None = None,
) -> AgentResult:
    from .order_service import (
        order_reference_candidates,
        resolve_order_id_via_customer_state,
    )

    seed = str(order_id or state.order_id or "").strip()
    targets = order_reference_candidates(seed)
    if not targets:
        return AgentResult(
            reply_text="Nao ha pedido identificado para consultar o pagamento.",
            intent="commerce",
            safety_reason="order_id_required",
            commercial_data={
                "success": False,
                "stage": "order_payment",
                "payment": {"status": "not_available"},
            },
            response_metadata={"domain": "commerce", "used_tray": False},
        )

    if not any(token.isdigit() for token in targets):
        resolved = await resolve_order_id_via_customer_state(
            state=state,
            execute=execute,
            preferred_codes=targets,
        )
        if resolved:
            for token in order_reference_candidates(resolved):
                if token not in targets:
                    targets.insert(0, token)

    checked_at = datetime.now(timezone.utc).isoformat()
    result: dict[str, Any] = {"error": "commerce_upstream_error"}
    target = targets[0]
    for candidate in targets:
        target = candidate
        print("[sales.payment.lookup]", {"order_id": target})
        try:
            result = await execute("get_order_payment", {"order_id": target})
        except Exception as exc:
            result = {
                "error": "commerce_upstream_error",
                "error_type": type(exc).__name__,
            }
        if "error" not in result and result.get("success") is not False:
            break
        status_code = str(result.get("status_code") or "")
        if status_code in {"404", "422"} and candidate != targets[-1]:
            continue
        if status_code in {"404", "422"}:
            resolved = await resolve_order_id_via_customer_state(
                state=state,
                execute=execute,
                preferred_codes=targets,
            )
            if resolved and resolved not in targets:
                targets.append(resolved)
                continue
            break

    if "error" in result or result.get("success") is False:
        print("[sales.payment.state]", {
            "order_id": target,
            "payment_type": None,
            "has_payment": None,
            "payment_url_present": False,
            "status": "unknown",
        })
        return AgentResult(
            reply_text="A consulta atual do pagamento nao pode ser confirmada.",
            intent="commerce",
            safety_reason="order_payment_technical_failure",
            commercial_data={
                "success": False,
                "order_id": target,
                "stage": "order_payment",
                "payment": {
                    "status": "unknown",
                    "has_payment": None,
                    "payment_url_available": False,
                },
            },
            response_metadata={
                "domain": "commerce",
                "payment_state": {
                    "order_payment_status": "unknown",
                    "order_has_payment": None,
                    "order_payment_checked_at": checked_at,
                },
                "purchase_stage": "order_created",
                "clear_pending_action": True,
                "used_tray": True,
                **_payment_failure_metadata(result),
            },
        )

    payment_payload = result.get("payment")
    payment_present = isinstance(payment_payload, dict)
    payment = payment_payload if payment_present else {}
    method_id = payment.get("method_id")
    method_id = str(method_id) if method_id is not None else None
    method = payment.get("method")
    method = str(method) if method is not None else None
    payment_type = payment.get("type")
    payment_type = str(payment_type) if payment_type is not None else None
    has_payment = payment.get("has_payment")
    has_payment = has_payment if isinstance(has_payment, bool) else None
    payment_date = payment.get("payment_date")
    payment_date = str(payment_date) if payment_date is not None else None
    raw_payment_url = payment.get("payment_url")
    payment_url = (
        raw_payment_url
        if isinstance(raw_payment_url, str) and raw_payment_url
        else None
    )
    if has_payment is True:
        status = "confirmed"
    elif has_payment is False:
        status = "pending"
    elif payment_present:
        status = "unknown"
    else:
        status = "not_available"

    facts = {
        "status": status,
        "method_id": method_id,
        "method": method,
        "type": payment_type,
        "has_payment": has_payment,
        "payment_date": payment_date,
        "payment_url": payment_url,
        "payment_url_available": payment_url is not None,
    }
    print("[sales.payment.state]", {
        "order_id": target,
        "payment_type": payment_type,
        "has_payment": has_payment,
        "payment_url_present": payment_url is not None,
        "status": status,
    })
    print("[sales.payment.hosted]", {
        "order_id": target,
        "payment_url_present": payment_url is not None,
        "status": status,
    })
    order_label = str(result.get("order_id") or target)
    # Prefer hosted URL from Tray; fall back to state URL recovered from transcript.
    effective_url = payment_url or state.order_payment_url
    if status == "confirmed":
        reply_text = f"O pagamento do pedido {order_label} já está confirmado."
    elif effective_url:
        reply_text = (
            f"Seu pedido {order_label} ainda está aguardando pagamento. "
            f"Segue o link: {effective_url}"
        )
    elif status == "pending":
        reply_text = (
            f"Seu pedido {order_label} ainda está aguardando pagamento, "
            "mas não encontrei o link agora. Posso tentar de novo em instantes."
        )
    else:
        reply_text = (
            f"Não há informação de pagamento disponível para o pedido {order_label} "
            "no momento."
        )
    return AgentResult(
        reply_text=reply_text,
        intent="commerce",
        commercial_data={
            "success": True,
            "order_id": order_label,
            "payment": {
                **facts,
                "payment_url": effective_url,
                "payment_url_available": effective_url is not None,
            },
        },
        response_metadata={
            "domain": "commerce",
            "factual_fallback_text": reply_text,
            "payment_state": {
                "order_payment_method_id": method_id,
                "order_payment_method": method,
                "order_payment_type": payment_type,
                "order_payment_url": effective_url,
                "order_payment_status": status,
                "order_has_payment": has_payment,
                "order_payment_date": payment_date,
                "order_payment_checked_at": checked_at,
            },
            "purchase_stage": (
                "payment_confirmed"
                if status == "confirmed"
                else "awaiting_payment"
                if status == "pending"
                else "order_created"
            ),
            **(
                {
                    "pending_action": "awaiting_payment",
                    "pending_action_product_ids": [],
                }
                if status == "pending"
                else {"clear_pending_action": True}
            ),
            "used_tray": True,
        },
    )
