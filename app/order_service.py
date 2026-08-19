from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable

from .commerce_context import (
    CHECKOUT_REQUIRED_FIELDS,
    CommerceCartItem,
    CommerceConversationState,
    checkout_missing_fields,
    normalize_variant_identity,
)
from .models import AgentResult


ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def _failure_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "order_failure_status": "status_code",
        "order_failure_code": "tray_error_code",
        "order_failure_name": "tray_error_name",
        "order_failure_type": "tray_error_type",
        "order_failure_field": "tray_error_field",
        "order_failure_fields": "tray_error_fields",
        "order_failure_causes": "tray_error_causes",
        "order_failure_message": "tray_error_message",
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


def _valid_cpf(value: str) -> bool:
    if len(value) != 11 or len(set(value)) == 1:
        return False
    for length in (9, 10):
        total = sum(int(digit) * weight for digit, weight in zip(value[:length], range(length + 1, 1, -1)))
        check = (total * 10) % 11
        if check == 10:
            check = 0
        if check != int(value[length]):
            return False
    return True


def _valid_cnpj(value: str) -> bool:
    if len(value) != 14 or len(set(value)) == 1:
        return False
    for length, weights in (
        (12, (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)),
        (13, (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)),
    ):
        total = sum(int(digit) * weight for digit, weight in zip(value[:length], weights))
        remainder = total % 11
        check = 0 if remainder < 2 else 11 - remainder
        if check != int(value[length]):
            return False
    return True


def extract_valid_tax_document(text: str | None) -> tuple[str, str] | None:
    for candidate in re.findall(r"(?<!\d)(?:\d[\s./-]?){10,13}\d(?!\d)", text or ""):
        digits = "".join(character for character in candidate if character.isdigit())
        if _valid_cpf(digits):
            return "cpf", digits
        if _valid_cnpj(digits):
            return "cnpj", digits
    return None


def contains_tax_document_candidate(text: str | None) -> bool:
    return any(
        len("".join(character for character in candidate if character.isdigit())) in {11, 14}
        for candidate in re.findall(r"(?<!\d)(?:\d[\s./-]?){10,13}\d(?!\d)", text or "")
    )


def _fold_text(value: str | None) -> str:
    folded = "".join(
        character
        for character in unicodedata.normalize("NFKD", (value or "").casefold())
        if not unicodedata.combining(character)
    )
    return folded.replace("º", "o").replace("°", "o")


def order_reference_candidates(value: str | None) -> list[str]:
    """Expand glued store-code + internal-id references into lookup candidates.

    Customers often paste both Tray handles together, e.g.
    ``0CC131B51070AEF25400`` = store code ``0CC131B51070AEF`` + id ``25400``.
    """
    raw = str(value or "").strip().strip(".,;!?")
    if not raw:
        return []
    candidates: list[str] = []

    def add(token: str | None) -> None:
        cleaned = str(token or "").strip().strip(".,;!?")
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)

    add(raw)
    # Non-greedy hex prefix + trailing internal numeric id
    # e.g. 0CC131B51070AEF25400 -> 0CC131B51070AEF + 25400
    glued = re.fullmatch(r"([0-9a-fA-F]+?)(\d{3,12})", raw, flags=re.IGNORECASE)
    if glued:
        store_code, internal_id = glued.group(1), glued.group(2)
        if 10 <= len(store_code) <= 16 and not store_code.isdigit():
            add(store_code)
            add(internal_id)
    # Tray get_order*_ endpoints expect the numeric internal id first.
    # Store hex codes from payment URLs often return 422.
    preferred: list[str] = []
    for token in candidates:
        if token.isdigit() and token not in preferred:
            preferred.append(token)
    for token in candidates:
        if (
            re.fullmatch(r"[0-9a-fA-F]{10,16}", token)
            and not token.isdigit()
            and token not in preferred
        ):
            preferred.append(token)
    for token in candidates:
        if token not in preferred:
            preferred.append(token)
    return preferred


def _looks_like_order_token(value: str | None) -> bool:
    token = str(value or "").strip()
    if not token or len(token) < 3:
        return False
    blocked = {
        "visual",
        "aberto",
        "criado",
        "pendente",
        "pagamento",
        "carrinho",
        "produto",
        "ativo",
        "novo",
        "meu",
        "minha",
    }
    if token.casefold() in blocked:
        return False
    if re.fullmatch(r"\d{3,12}", token):
        return True
    if re.fullmatch(r"[0-9a-fA-F]{10,16}", token):
        return True
    if re.fullmatch(r"[0-9a-fA-F]{10,16}\d{3,12}", token):
        return True
    # Codes like ABC-123 / ns-9981 must include a digit.
    if any(char.isdigit() for char in token) and re.fullmatch(
        r"[a-z0-9]+(?:-[a-z0-9]+)*",
        token,
        flags=re.IGNORECASE,
    ):
        return True
    return False


def extract_order_reference(text: str | None) -> str | None:
    folded = _fold_text(text)
    patterns = (
        r"\bcod(?:igo)?\.?\s*(?:do\s+)?pedido\s*[:#-]?\s*([a-z0-9][a-z0-9-]*)",
        r"\bpedido\s+(?:n(?:umero|o)?|cod(?:igo)?)\.?\s*[:#-]?\s*([a-z0-9][a-z0-9-]*)",
        r"\bpedido\s*[:#-]\s*([a-z0-9][a-z0-9-]*)",
        # Keep digit-leading tokens (store codes like 0CC… / numeric ids).
        r"\bpedido\s+([0-9][a-z0-9-]*)",
    )
    for pattern in patterns:
        match = re.search(pattern, folded)
        if not match:
            continue
        raw = match.group(1).strip()
        if not _looks_like_order_token(raw):
            continue
        candidates = order_reference_candidates(raw)
        return candidates[0] if candidates else None
    return None


def is_order_lookup_request(text: str | None) -> bool:
    folded = _fold_text(text)
    if extract_order_reference(text):
        return True
    # Short follow-ups after an order was discussed in the same thread.
    followup_signals = (
        "como ficou",
        "como que ficou",
        "e ai ficou",
        "e ai como ficou",
    )
    if any(signal in folded for signal in followup_signals):
        return True
    if "pedido" not in folded:
        return False
    lookup_signals = (
        "status",
        "como esta",
        "como ficou",
        "acompanhar",
        "acompanhamento",
        "rastre",
        "andamento",
        "onde esta",
        "fiz um pedido",
        "meu pedido",
    )
    return any(signal in folded for signal in lookup_signals)


def _money(value: Any) -> str | None:
    try:
        return format(Decimal(str(value)).quantize(Decimal("0.01")), "f")
    except (InvalidOperation, TypeError, ValueError):
        return None


def cart_order_products(
    cart: dict[str, Any],
    factual_items: list[CommerceCartItem] | None = None,
) -> list[dict[str, Any]]:
    factual_prices = {
        (item.product_id, normalize_variant_identity(item.variant_id)): (
            item.unit_price,
            item.original_price,
        )
        for item in factual_items or []
    }
    products: list[dict[str, Any]] = []
    for item in cart.get("items") or []:
        if not isinstance(item, dict):
            continue
        product_id = item.get("product_id") or item.get("id")
        if product_id is None:
            continue
        try:
            variant_id = normalize_variant_identity(item.get("variant_id"))
            quantity = int(item.get("quantity"))
        except (TypeError, ValueError):
            continue
        persisted = factual_prices.get((str(product_id), variant_id), (None, None))
        price = item.get("unit_price")
        if price is None:
            price = item.get("price")
        if price is None:
            price = persisted[0]
        original_price = item.get("original_price")
        if original_price is None:
            original_price = persisted[1]
        normalized_price = _money(price)
        normalized_original_price = _money(original_price)
        if normalized_price is None or Decimal(normalized_price) <= 0 or quantity < 1:
            continue
        if normalized_original_price is not None and Decimal(normalized_original_price) <= 0:
            normalized_original_price = None
        products.append({
            "product_id": str(product_id),
            "variant_id": variant_id,
            "price": normalized_price,
            "original_price": normalized_original_price,
            "quantity": quantity,
        })
    return products

def _preconditions(state: CommerceConversationState) -> list[str]:
    missing: list[str] = []
    if not state.cart_session_id:
        missing.append("cart_session_id")
    if state.checkout_channel_preference != "whatsapp":
        missing.append("checkout_channel_whatsapp")
    if not state.selected_shipping:
        missing.append("selected_shipping")
    elif not any(
        quote.model_dump(mode="json") == state.selected_shipping.model_dump(mode="json")
        for quote in state.shipping_quotes
    ):
        missing.append("selected_shipping_not_in_active_quote")
    elif (
        state.shipping_quote_zipcode
        and state.checkout_draft.address.zip_code
        and state.shipping_quote_zipcode != state.checkout_draft.address.zip_code
    ):
        missing.append("shipping_zipcode_mismatch")
    missing.extend(checkout_missing_fields(state.checkout_draft))
    if not state.selected_payment_option or not state.selected_payment_option.name:
        missing.append("selected_payment_method")
    return missing


async def _current_order_facts(
    state: CommerceConversationState,
    execute: ToolExecutor,
    cart_snapshot: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    missing = _preconditions(state)
    if not state.cart_session_id:
        return None, missing
    if cart_snapshot is not None:
        cart = cart_snapshot
    else:
        try:
            cart = await execute("get_cart_complete", {"session_id": state.cart_session_id})
        except Exception:
            cart = {"error": "commerce_upstream_error"}
    if "error" in cart:
        return None, [*missing, "cart_unavailable"]
    products = cart_order_products(cart, state.cart_items)
    cart_items = cart.get("items") or []
    if not products or len(products) != len(cart_items):
        return None, [*missing, "cart_products"]
    if not cart_items:
        return None, [*missing, "cart_products"]
    if any(product.get("original_price") is None for product in products):
        return None, [*missing, "cart_original_price_missing"]

    draft = state.checkout_draft
    shipping = state.selected_shipping
    payment = state.selected_payment_option
    if missing or shipping is None or payment is None:
        return None, list(dict.fromkeys(missing))
    payload = {
        "session_id": state.cart_session_id,
        "shipping": {
            "shipping_id": shipping.shipping_id,
            "quotation_id": shipping.quotation_id,
            "name": shipping.name,
            "value": shipping.price,
            "min_period": shipping.min_period,
            "max_period": shipping.max_period,
        },
        "payment": {"method_id": payment.id, "name": payment.name},
        "customer": {
            "type": draft.customer.type,
            "name": draft.customer.name,
            "cpf": draft.customer.cpf,
            "email": draft.customer.email,
            "phone": draft.customer.phone,
            **({"rg": draft.customer.rg} if draft.customer.rg else {}),
            **({"gender": draft.customer.gender} if draft.customer.gender else {}),
        },
        "address": {
            "address": draft.address.address,
            "zip_code": draft.address.zip_code,
            "number": draft.address.number,
            "complement": draft.address.complement or "",
            "neighborhood": draft.address.neighborhood,
            "city": draft.address.city,
            "state": draft.address.state,
            "country": draft.address.country,
            "type": draft.address.type,
        },
        "products": products,
    }
    summary_products: list[dict[str, Any]] = []
    subtotal = Decimal("0")
    for raw, product in zip(cart_items, products):
        unit = Decimal(product["price"])
        item_subtotal = unit * product["quantity"]
        subtotal += item_subtotal
        summary_products.append({
            "product_id": product["product_id"],
            "variant_id": product["variant_id"],
            "name": raw.get("name"),
            "variant": raw.get("variant") or raw.get("variant_name"),
            "quantity": product["quantity"],
            "price": product["price"],
            "subtotal": format(item_subtotal.quantize(Decimal("0.01")), "f"),
        })
    cart_subtotal = _money(cart.get("subtotal")) or format(subtotal.quantize(Decimal("0.01")), "f")
    shipping_value = Decimal(shipping.price)
    display_total = Decimal(cart_subtotal) + shipping_value
    summary = {
        "order_ready": True,
        "order_confirmation_pending": True,
        "products": summary_products,
        "cart_subtotal": cart_subtotal,
        "shipping": {
            "name": shipping.name,
            "price": shipping.price,
            "min_period": shipping.min_period,
            "max_period": shipping.max_period,
            "estimated_delivery_date": shipping.estimated_delivery_date,
        },
        "payment": {"id": payment.id, "name": payment.name},
        "customer": {"name": draft.customer.name},
        "delivery": {
            "city": draft.address.city,
            "state": draft.address.state,
            "zipcode": draft.address.zip_code,
        },
        "display_total": format(display_total.quantize(Decimal("0.01")), "f"),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    version = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {"payload": payload, "summary": summary, "version": version}, []


async def prepare_order(
    *,
    state: CommerceConversationState,
    execute: ToolExecutor,
    cart_snapshot: dict[str, Any] | None = None,
) -> AgentResult:
    print("[sales.order.prepare]", {
        "session": _session_tag(state.cart_session_id),
        "has_shipping": bool(state.selected_shipping),
        "has_payment": bool(state.selected_payment_option),
    })
    facts, missing = await _current_order_facts(state, execute, cart_snapshot)
    if facts is None:
        print("[sales.checkout.missing_fields]", {
            "missing_count": len(missing), "missing_fields": missing,
        })
        return AgentResult(
            reply_text="O pedido ainda n\u00e3o est\u00e1 pronto para revis\u00e3o.",
            intent="commerce",
            safety_reason="order_not_ready",
            commercial_data={
                "order_ready": False,
                "required_fields": list(CHECKOUT_REQUIRED_FIELDS),
                "missing_fields": missing,
            },
            response_metadata={
                "domain": "commerce",
                "order_state": {
                    "order_confirmation_status": "not_ready",
                    "order_review_version": None,
                    "confirmed_order_review_version": None,
                },
                "pending_action": "awaiting_checkout_data",
                "pending_action_product_ids": [],
                "used_tray": bool(state.cart_session_id),
            },
        )
    print("[sales.order.confirmation.pending]", {
        "session": _session_tag(state.cart_session_id),
        "review_version": facts["version"][:10],
    })
    summary = facts["summary"] if isinstance(facts.get("summary"), dict) else {}
    total = summary.get("display_total")
    product_names = [
        str(item.get("name") or "").strip()
        for item in (summary.get("products") or [])
        if isinstance(item, dict) and item.get("name")
    ]
    product_bit = product_names[0] if len(product_names) == 1 else (
        f"{len(product_names)} itens" if product_names else "seus itens"
    )
    total_bit = f" Total: R$ {total}." if total else ""
    review_reply = (
        f"Separei o resumo do pedido ({product_bit}).{total_bit} "
        "Confirma para eu criar o pedido?"
    )
    return AgentResult(
        reply_text=review_reply,
        intent="commerce",
        commercial_data=facts["summary"],
        response_metadata={
            "domain": "commerce",
            "factual_fallback_text": review_reply,
            "order_state": {
                "order_confirmation_status": "pending",
                "order_review_version": facts["version"],
                "confirmed_order_review_version": None,
            },
            "purchase_stage": "order_review",
            "pending_action": "awaiting_order_confirmation",
            "pending_action_product_ids": [],
            "used_tray": True,
        },
    )


def confirm_prepared_order(state: CommerceConversationState) -> AgentResult:
    allowed = bool(
        state.order_confirmation_status == "pending"
        and state.order_review_version
    )
    if not allowed:
        return AgentResult(
            reply_text="N\u00e3o h\u00e1 resumo atual aguardando confirma\u00e7\u00e3o.",
            intent="commerce",
            safety_reason="order_confirmation_missing",
            commercial_data={"success": False, "stage": "order_confirmation"},
            response_metadata={"domain": "commerce", "used_tray": False},
        )
    print("[sales.order.confirmation.accepted]", {
        "session": _session_tag(state.cart_session_id),
        "review_version": state.order_review_version[:10],
    })
    return AgentResult(
        reply_text="Confirma\u00e7\u00e3o expl\u00edcita vinculada ao resumo atual.",
        intent="commerce",
        commercial_data={"success": True, "stage": "order_confirmation"},
        response_metadata={
            "domain": "commerce",
            "order_state": {
                "order_confirmation_status": "confirmed",
                "order_review_version": state.order_review_version,
                "confirmed_order_review_version": state.order_review_version,
            },
            "clear_pending_action": True,
            "used_tray": False,
        },
    )


def _existing_order(orders: dict[str, Any]) -> dict[str, Any] | None:
    values = orders.get("orders") if isinstance(orders.get("orders"), list) else []
    return next(
        (order for order in values if isinstance(order, dict) and (order.get("order_id") or order.get("id"))),
        None,
    )


async def create_order(
    *,
    state: CommerceConversationState,
    execute: ToolExecutor,
) -> AgentResult:
    if state.order_id:
        return AgentResult(
            reply_text="Pedido existente recuperado do estado.",
            intent="commerce",
            commercial_data={
                "success": True, "existing": True, "order_id": state.order_id,
                "status": state.order_status,
            },
            response_metadata={"domain": "commerce", "used_tray": False},
        )
    if (
        state.order_confirmation_status != "confirmed"
        or not state.order_review_version
        or state.confirmed_order_review_version != state.order_review_version
    ):
        return AgentResult(
            reply_text="A cria\u00e7\u00e3o do pedido est\u00e1 bloqueada sem confirma\u00e7\u00e3o do resumo atual.",
            intent="commerce",
            safety_reason="order_confirmation_required",
            commercial_data={"success": False, "stage": "order_creation"},
            response_metadata={"domain": "commerce", "used_tray": False},
        )
    facts, missing = await _current_order_facts(state, execute)
    if facts is None:
        return AgentResult(
            reply_text="Os fatos do pedido mudaram ap\u00f3s a confirma\u00e7\u00e3o.",
            intent="commerce",
            safety_reason="order_confirmation_stale",
            commercial_data={
                "success": False, "stage": "order_creation",
                "missing_fields": missing,
            },
            response_metadata={
                "domain": "commerce",
                "order_state": {
                    "order_confirmation_status": "not_ready",
                    "order_review_version": None,
                    "confirmed_order_review_version": None,
                },
                "pending_action": "awaiting_checkout_data",
                "pending_action_product_ids": [],
                "used_tray": True,
            },
        )
    if facts["version"] != state.confirmed_order_review_version:
        return AgentResult(
            reply_text="Os fatos do pedido mudaram ap\u00f3s a confirma\u00e7\u00e3o.",
            intent="commerce",
            safety_reason="order_confirmation_stale",
            commercial_data=facts["summary"],
            response_metadata={
                "domain": "commerce",
                "order_state": {
                    "order_confirmation_status": "pending",
                    "order_review_version": facts["version"],
                    "confirmed_order_review_version": None,
                },
                "purchase_stage": "order_review",
                "pending_action": "awaiting_order_confirmation",
                "pending_action_product_ids": [],
                "used_tray": True,
            },
        )
    reconciled = None
    if state.order_creation_ambiguous and state.cart_session_id:
        try:
            preflight = await execute(
                "list_orders", {"session_id": state.cart_session_id}
            )
        except Exception:
            preflight = {"error": "commerce_upstream_error"}
        if "error" in preflight:
            return AgentResult(
                reply_text="A cria\u00e7\u00e3o anterior ainda precisa ser reconciliada.",
                intent="commerce",
                safety_reason="order_creation_technical_failure",
                commercial_data={
                    "success": False, "stage": "order_creation", "recoverable": True,
                },
                response_metadata={
                    "domain": "commerce",
                    "order_state": {"order_creation_ambiguous": True},
                    "used_tray": True,
                },
            )
        reconciled = _existing_order(preflight)
        print("[sales.order.reconcile]", {
            "session": _session_tag(state.cart_session_id),
            "found": reconciled is not None,
            "preflight": True,
        })
    if reconciled is None:
        print("[sales.order.create.request]", {
            "session": _session_tag(state.cart_session_id),
            "product_count": len(facts["payload"]["products"]),
            "has_customer": True,
            "address_complete": True,
        })
        try:
            result = await execute("create_order", facts["payload"])
        except Exception as exc:
            result = {
                "error": "commerce_upstream_error",
                "status_code": None,
                "error_type": type(exc).__name__,
            }
    else:
        result = reconciled
    ambiguous = "error" in result and result.get("status_code") in {None, 502, 503, 504}
    if ambiguous and state.cart_session_id:
        try:
            lookup = await execute("list_orders", {"session_id": state.cart_session_id})
        except Exception:
            lookup = {"error": "commerce_upstream_error"}
        reconciled = None if "error" in lookup else _existing_order(lookup)
        print("[sales.order.reconcile]", {
            "session": _session_tag(state.cart_session_id),
            "found": reconciled is not None,
        })
    effective = reconciled or result
    order_id = effective.get("order_id") or effective.get("id")
    if "error" in effective or order_id is None:
        print("[sales.order.create.result]", {
            "session": _session_tag(state.cart_session_id),
            "success": False,
            "status_code": effective.get("status_code"),
            "tray_error_field": effective.get("tray_error_field"),
            "tray_error_fields": effective.get("tray_error_fields"),
            "tray_error_code": effective.get("tray_error_code"),
            "tray_error_message": effective.get("tray_error_message"),
        })
        return AgentResult(
            reply_text="A cria\u00e7\u00e3o do pedido n\u00e3o foi confirmada pela integra\u00e7\u00e3o.",
            intent="commerce",
            safety_reason="order_creation_technical_failure",
            commercial_data={
                "success": False,
                "stage": "order_creation",
                "recoverable": ambiguous,
            },
            response_metadata={
                "domain": "commerce",
                "order_state": {"order_creation_ambiguous": ambiguous},
                "used_tray": True,
                **_failure_metadata(effective),
            },
        )
    print("[sales.order.create.result]", {
        "session": _session_tag(state.cart_session_id),
        "success": True,
        "reconciled": reconciled is not None,
    })
    status = effective.get("status")
    return AgentResult(
        reply_text="Pedido criado e identificado pela integra\u00e7\u00e3o.",
        intent="commerce",
        commercial_data={
            "success": True,
            "order_id": str(order_id),
            "status": status,
            "status_group": effective.get("status_group"),
        },
        response_metadata={
            "domain": "commerce",
            "order_state": {
                "order_confirmation_status": "not_ready",
                "order_review_version": None,
                "confirmed_order_review_version": None,
                "order_id": str(order_id),
                "order_status": status,
                "order_status_group": effective.get("status_group"),
                "order_session_id": state.cart_session_id,
                "order_created_at": effective.get("created_at") or effective.get("order_created_at"),
                "order_creation_ambiguous": False,
            },
            "purchase_stage": "order_created",
            "clear_pending_action": True,
            "used_tray": True,
        },
    )


def _order_not_found_result(target: str) -> AgentResult:
    return AgentResult(
        reply_text=(
            "Não consegui confirmar esse código de pedido diretamente. "
            "Para procurar o mesmo pedido no cadastro correto, informe o CPF ou CNPJ do comprador."
        ),
        intent="commerce",
        safety_reason="order_not_found",
        commercial_data={"success": False, "stage": "order_status"},
        response_metadata={
            "domain": "commerce",
            "used_tray": True,
            "pending_action": "awaiting_order_customer_document",
            "order_state": {"order_lookup_id": target},
        },
    )


def invalid_tax_document_result() -> AgentResult:
    return AgentResult(
        reply_text="O CPF ou CNPJ informado não é válido. Confira os números e envie novamente.",
        intent="commerce",
        safety_reason="invalid_customer_document",
        commercial_data={"success": False, "stage": "order_customer_lookup"},
        response_metadata={
            "domain": "commerce",
            "used_tray": False,
            "pending_action": "awaiting_order_customer_document",
        },
    )


def _order_payload_exists(result: dict[str, Any]) -> bool:
    return bool(
        result.get("success") is not False
        and (
            result.get("order_id") is not None
            or result.get("id") is not None
            or result.get("status") is not None
            or result.get("status_group") is not None
        )
    )


def _order_facts_result(
    result: dict[str, Any],
    target: str,
    state: CommerceConversationState,
) -> AgentResult:
    shipment = result.get("shipment")
    shipment = shipment if isinstance(shipment, dict) else {}
    tracking = {
        key: result.get(key)
        for key in (
            "sending_code", "tracking_url", "sending_date",
            "estimated_delivery_date", "shipment",
        ) if result.get(key) is not None
    }
    for key in (
        "sending_code", "tracking_url", "sending_date",
        "estimated_delivery_date",
    ):
        if key not in tracking and shipment.get(key) is not None:
            tracking[key] = shipment[key]
    if shipment:
        tracking["shipment"] = shipment
    facts = {
        "success": True,
        "order_id": str(result.get("order_id") or result.get("id") or target),
        "status": result.get("status"),
        "status_group": result.get("status_group"),
        "tracking": tracking,
    }
    print("[sales.order.status]", {
        "order_id_present": True,
        "status": facts["status"],
        "status_group": facts["status_group"],
        "tracking_present": bool(tracking),
    })
    status_label = str(facts["status"] or "").strip() or "em processamento"
    status_group = str(facts["status_group"] or "").casefold()
    awaiting_payment = (
        state.order_payment_status == "pending"
        or "aguard" in status_label.casefold()
        or status_group in {"open", "pending", "unpaid", "awaiting"}
    )
    if awaiting_payment and state.order_payment_url:
        reply_text = (
            f'Seu pedido está com status "{status_label}". '
            f"Segue o link para pagamento: {state.order_payment_url}"
        )
    elif awaiting_payment:
        reply_text = (
            f'Seu pedido está com status "{status_label}". '
            "Posso enviar o link para você realizar o pagamento?"
        )
    else:
        reply_text = f'Seu pedido está com status "{status_label}".'
    tracking_url = tracking.get("tracking_url")
    if tracking_url:
        reply_text = f"{reply_text} Rastreio: {tracking_url}"
    return AgentResult(
        reply_text=reply_text,
        intent="commerce",
        commercial_data=facts,
        response_metadata={
            "domain": "commerce",
            "clear_pending_action": True,
            "factual_fallback_text": reply_text,
            "order_state": {
                "order_id": facts["order_id"],
                "order_status": facts["status"],
                "order_status_group": facts["status_group"],
                "order_lookup_id": None,
            },
            "purchase_stage": (
                "payment_confirmed"
                if state.order_payment_status == "confirmed"
                else "awaiting_payment"
                if state.order_payment_status == "pending" or awaiting_payment
                else "order_created"
            ),
            "pending_action": (
                "awaiting_payment" if awaiting_payment else None
            ),
            "used_tray": True,
        },
    )


async def resolve_order_id_via_customer_state(
    *,
    state: CommerceConversationState,
    execute: ToolExecutor,
    preferred_codes: list[str],
) -> str | None:
    from .order_context_recovery import recover_order_id_from_customer

    customer = state.checkout_draft.customer
    handles: dict[str, Any] = {
        "documents": [],
        "emails": [],
        "order_ids": list(preferred_codes),
    }
    cpf = re.sub(r"\D+", "", str(customer.cpf or ""))
    if cpf:
        handles["documents"].append(("cpf", cpf))
    email = str(customer.email or "").strip()
    if email:
        handles["emails"].append(email)
    if not handles["documents"] and not handles["emails"]:
        return None
    return await recover_order_id_from_customer(
        execute=execute,
        handles=handles,
        preferred_codes=preferred_codes,
    )


async def get_order_facts(
    *,
    state: CommerceConversationState,
    execute: ToolExecutor,
    order_id: str | None = None,
    allow_customer_recovery: bool = True,
) -> AgentResult:
    seed = str(order_id or state.order_id or state.order_lookup_id or "").strip()
    targets = order_reference_candidates(seed)
    if not targets and (state.order_session_id or state.cart_session_id):
        session_id = state.order_session_id or state.cart_session_id
        try:
            lookup = await execute("list_orders", {"session_id": session_id})
        except Exception:
            lookup = {"error": "commerce_upstream_error"}
        existing = None if "error" in lookup else _existing_order(lookup)
        if existing is not None:
            recovered = str(existing.get("order_id") or existing.get("id") or "").strip()
            targets = order_reference_candidates(recovered)
        print("[sales.order.reconcile]", {
            "session": _session_tag(session_id),
            "found": bool(targets),
            "status_lookup": True,
        })

    # Storefront hex codes often 422 on Tray; resolve numeric id via CPF/email first.
    if allow_customer_recovery and (
        not targets
        or not any(token.isdigit() for token in targets)
    ):
        resolved = await resolve_order_id_via_customer_state(
            state=state,
            execute=execute,
            preferred_codes=targets or order_reference_candidates(seed),
        )
        if resolved:
            for token in order_reference_candidates(resolved):
                if token not in targets:
                    targets.insert(0, token)

    if not targets:
        return AgentResult(
            reply_text="N\u00e3o h\u00e1 pedido identificado para consulta.",
            intent="commerce",
            safety_reason="order_id_required",
            commercial_data={"success": False, "stage": "order_status"},
            response_metadata={"domain": "commerce", "used_tray": False},
        )

    last_error: dict[str, Any] | None = None
    last_empty_target = targets[0]
    for target in targets:
        try:
            result = await execute("get_order_complete", {"order_id": target})
        except Exception:
            result = {"error": "commerce_upstream_error"}
        if "error" in result:
            last_error = result
            status_code = str(result.get("status_code") or "")
            # Glued/store codes often yield 422; try the next candidate before failing.
            if status_code in {"404", "422"} and target != targets[-1]:
                print("[sales.order.lookup.retry]", {
                    "failed_order_id": target,
                    "status_code": status_code,
                    "remaining_candidates": len(targets) - targets.index(target) - 1,
                })
                continue
            if status_code in {"404", "422"} and allow_customer_recovery:
                resolved = await resolve_order_id_via_customer_state(
                    state=state,
                    execute=execute,
                    preferred_codes=targets,
                )
                if resolved and resolved not in targets:
                    print("[sales.order.lookup.customer_resolve]", {
                        "from": target,
                        "resolved": resolved,
                        "status_code": status_code,
                    })
                    targets.append(resolved)
                    continue
            if allow_customer_recovery and status_code == "404":
                return _order_not_found_result(target)
            return AgentResult(
                reply_text="A consulta atual do pedido n\u00e3o p\u00f4de ser conclu\u00edda.",
                intent="commerce",
                safety_reason="order_status_technical_failure",
                commercial_data={"success": False, "stage": "order_status"},
                response_metadata={
                    "domain": "commerce",
                    "used_tray": True,
                    **_failure_metadata(result),
                },
            )
        if _order_payload_exists(result):
            if target != seed:
                print("[sales.order.lookup.normalized]", {
                    "requested": seed,
                    "resolved": target,
                })
            return _order_facts_result(result, target, state)
        last_empty_target = target

    if allow_customer_recovery:
        return _order_not_found_result(last_empty_target)
    if last_error is not None:
        return AgentResult(
            reply_text="A consulta atual do pedido n\u00e3o p\u00f4de ser conclu\u00edda.",
            intent="commerce",
            safety_reason="order_status_technical_failure",
            commercial_data={"success": False, "stage": "order_status"},
            response_metadata={
                "domain": "commerce",
                "used_tray": True,
                **_failure_metadata(last_error),
            },
        )
    return AgentResult(
        reply_text="Não consegui confirmar esse pedido no cadastro informado.",
        intent="commerce",
        safety_reason="order_not_found",
        commercial_data={"success": False, "stage": "order_status"},
        response_metadata={"domain": "commerce", "used_tray": True},
    )


async def find_order_by_customer_document(
    *,
    state: CommerceConversationState,
    execute: ToolExecutor,
    document_kind: str,
    document: str,
) -> AgentResult:
    target = str(state.order_lookup_id or state.order_id or "").strip()
    if not target:
        return AgentResult(
            reply_text="Informe também o código do pedido que deseja consultar.",
            intent="commerce",
            safety_reason="order_id_required",
            commercial_data={"success": False, "stage": "order_customer_lookup"},
            response_metadata={"domain": "commerce", "used_tray": False},
        )
    try:
        customer_result = await execute(
            "search_customer",
            {document_kind: document, "limit": 5},
        )
    except Exception:
        customer_result = {"error": "commerce_upstream_error"}
    if "error" in customer_result:
        return AgentResult(
            reply_text="Não consegui consultar o cadastro do comprador agora. Tente novamente em instantes.",
            intent="commerce",
            safety_reason="order_customer_lookup_technical_failure",
            commercial_data={"success": False, "stage": "order_customer_lookup"},
            response_metadata={"domain": "commerce", "used_tray": True},
        )
    customers = [
        customer
        for customer in customer_result.get("customers") or []
        if isinstance(customer, dict) and customer.get("id") is not None
    ]
    if len(customers) != 1:
        return AgentResult(
            reply_text=(
                "Não consegui confirmar um único cadastro com esses dados. "
                "Confira o CPF/CNPJ ou fale com a equipe de atendimento."
            ),
            intent="commerce",
            safety_reason="order_customer_not_confirmed",
            commercial_data={"success": False, "stage": "order_customer_lookup"},
            response_metadata={"domain": "commerce", "used_tray": True},
        )
    try:
        order_result = await execute(
            "list_orders",
            {"customer_id": str(customers[0]["id"])},
        )
    except Exception:
        order_result = {"error": "commerce_upstream_error"}
    if "error" in order_result:
        return AgentResult(
            reply_text="Não consegui consultar os pedidos desse cadastro agora. Tente novamente em instantes.",
            intent="commerce",
            safety_reason="customer_orders_lookup_technical_failure",
            commercial_data={"success": False, "stage": "order_customer_lookup"},
            response_metadata={"domain": "commerce", "used_tray": True},
        )
    confirmed_customer_id = str(customers[0]["id"])

    def belongs_to_confirmed_customer(order: dict[str, Any]) -> bool:
        order_customer = order.get("customer")
        nested_customer_id = (
            order_customer.get("id")
            if isinstance(order_customer, dict)
            else None
        )
        returned_customer_id = order.get("customer_id") or nested_customer_id
        return (
            returned_customer_id is not None
            and str(returned_customer_id) == confirmed_customer_id
        )

    target_key = target.casefold()
    matching_order = next(
        (
            order
            for order in order_result.get("orders") or []
            if isinstance(order, dict)
            and belongs_to_confirmed_customer(order)
            and target_key in {
                str(order.get(field)).strip().casefold()
                for field in ("order_id", "id", "code")
                if order.get(field) is not None
            }
        ),
        None,
    )
    if matching_order is None:
        return AgentResult(
            reply_text=(
                "O código informado não foi confirmado entre os pedidos desse cadastro. "
                "Confira o código ou fale com a equipe de atendimento."
            ),
            intent="commerce",
            safety_reason="order_customer_mismatch",
            commercial_data={"success": False, "stage": "order_customer_lookup"},
            response_metadata={"domain": "commerce", "used_tray": True},
        )
    canonical_id = str(
        matching_order.get("order_id")
        or matching_order.get("id")
        or target
    )
    try:
        complete = await execute("get_order_complete", {"order_id": canonical_id})
    except Exception:
        complete = {"error": "commerce_upstream_error"}
    if "error" not in complete and _order_payload_exists(complete):
        return _order_facts_result(complete, canonical_id, state)
    if _order_payload_exists(matching_order):
        return _order_facts_result(matching_order, canonical_id, state)
    return AgentResult(
        reply_text="O pedido foi identificado, mas o status não está disponível agora.",
        intent="commerce",
        safety_reason="order_status_technical_failure",
        commercial_data={"success": False, "stage": "order_status"},
        response_metadata={"domain": "commerce", "used_tray": True},
    )
