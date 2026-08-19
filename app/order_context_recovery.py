from __future__ import annotations

import re
from typing import Any

from .commerce_context import CommerceConversationState
from .order_service import (
    ToolExecutor,
    extract_valid_tax_document,
    order_reference_candidates,
)


_EMAIL_RE = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", re.I)
_PAYMENT_URL_RE = re.compile(
    r"https?://[^\s<>\"]+pedido=([A-Za-z0-9]{6,})",
    re.I,
)
_ORDER_CODE_RE = re.compile(
    r"\b(?:pedido|order)[=:#\s-]*([A-F0-9]{10,}|[0-9]{5,})\b",
    re.I,
)


def _texts_from_turns(recent_turns: list[dict[str, Any]] | None) -> list[str]:
    texts: list[str] = []
    for turn in recent_turns or []:
        if not isinstance(turn, dict):
            continue
        content = turn.get("content")
        if isinstance(content, str) and content.strip():
            texts.append(content)
    return texts


def extract_handles_from_conversation(
    *,
    state: CommerceConversationState | None,
    recent_turns: list[dict[str, Any]] | None = None,
    message_text: str | None = None,
) -> dict[str, Any]:
    """Recover order/payment/customer handles from state + transcript."""
    order_ids: list[str] = []
    payment_urls: list[str] = []
    emails: list[str] = []
    documents: list[tuple[str, str]] = []

    if state is not None:
        if state.order_id:
            order_ids.append(str(state.order_id))
        if state.order_lookup_id:
            order_ids.append(str(state.order_lookup_id))
        if state.order_payment_url:
            payment_urls.append(str(state.order_payment_url))
        customer = state.checkout_draft.customer
        if customer.email:
            emails.append(str(customer.email).strip())
        if customer.cpf:
            digits = re.sub(r"\D+", "", str(customer.cpf))
            if digits:
                documents.append(("cpf", digits))

    blobs = list(_texts_from_turns(recent_turns))
    if message_text:
        blobs.append(message_text)

    for blob in blobs:
        for match in _PAYMENT_URL_RE.finditer(blob):
            order_ids.extend(order_reference_candidates(match.group(1)))
            payment_urls.append(match.group(0).rstrip(").,;"))
        for match in _ORDER_CODE_RE.finditer(blob):
            order_ids.extend(order_reference_candidates(match.group(1)))
        for match in _EMAIL_RE.finditer(blob):
            emails.append(match.group(0).strip().casefold())
        document = extract_valid_tax_document(blob)
        if document:
            documents.append(document)

    # Preserve first-seen order while deduping.
    def _unique(values: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for value in values:
            token = value.strip()
            key = token.casefold()
            if not token or key in seen:
                continue
            seen.add(key)
            ordered.append(token)
        return ordered

    unique_docs: list[tuple[str, str]] = []
    seen_docs: set[tuple[str, str]] = set()
    for kind, value in documents:
        item = (kind, value)
        if item in seen_docs:
            continue
        seen_docs.add(item)
        unique_docs.append(item)

    return {
        "order_ids": _unique(order_ids),
        "payment_urls": _unique(payment_urls),
        "emails": _unique(emails),
        "documents": unique_docs,
    }


def hydrate_state_from_handles(
    state: CommerceConversationState,
    handles: dict[str, Any],
) -> CommerceConversationState:
    """Fill missing order/payment/customer fields from recovered handles."""
    updated = state.model_copy(deep=True)
    order_ids = handles.get("order_ids") or []
    payment_urls = handles.get("payment_urls") or []
    emails = handles.get("emails") or []
    documents = handles.get("documents") or []

    if not updated.order_id and order_ids:
        updated.order_id = str(order_ids[0])
        updated.order_lookup_id = updated.order_lookup_id or str(order_ids[0])
    if not updated.order_payment_url and payment_urls:
        updated.order_payment_url = str(payment_urls[0])
        if updated.pending_action is None:
            updated.pending_action = "awaiting_payment"
        if not updated.purchase_stage:
            updated.purchase_stage = "awaiting_payment"
    if not updated.checkout_draft.customer.email and emails:
        updated.checkout_draft.customer.email = str(emails[0])
    if not updated.checkout_draft.customer.cpf and documents:
        for kind, value in documents:
            if kind == "cpf":
                updated.checkout_draft.customer.cpf = value
                break
    return updated


def _order_unpaid_score(order: dict[str, Any]) -> int:
    status = str(order.get("status") or "").casefold()
    group = str(order.get("status_group") or "").casefold()
    score = 0
    if any(token in status for token in ("aguard", "pending", "pagar", "open", "novo")):
        score += 50
    if any(token in group for token in ("open", "pending", "unpaid", "awaiting")):
        score += 40
    if order.get("payment_url") or (order.get("payment") or {}).get("payment_url"):
        score += 30
    return score


def _canonical_tray_order_id(order: dict[str, Any]) -> str | None:
    """Prefer numeric Tray internal ids over storefront hex codes."""
    values: list[str] = []
    for field in ("id", "order_id", "code", "number"):
        value = order.get(field)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            values.append(text)
    for text in values:
        if text.isdigit():
            return text
    return values[0] if values else None


def _order_matches_preferred_codes(
    order: dict[str, Any],
    preferred_codes: set[str],
) -> bool:
    if not preferred_codes:
        return False
    for field in ("id", "order_id", "code", "number"):
        value = str(order.get(field) or "").strip().casefold()
        if value and value in preferred_codes:
            return True
    return False


async def recover_order_id_from_customer(
    *,
    execute: ToolExecutor,
    handles: dict[str, Any],
    preferred_codes: list[str] | None = None,
) -> str | None:
    """Locate the most relevant Tray order using CPF/email recovered from context."""
    documents = list(handles.get("documents") or [])
    for email in handles.get("emails") or []:
        documents.append(("email", email))
    preferred = {
        str(code).strip().casefold()
        for code in (preferred_codes or handles.get("order_ids") or [])
        if str(code).strip()
    }

    for kind, value in documents:
        payload = {"limit": 5}
        if kind == "cpf":
            payload["cpf"] = value
        elif kind == "cnpj":
            payload["cnpj"] = value
        elif kind == "email":
            payload["email"] = value
        else:
            continue
        try:
            customer_result = await execute("search_customer", payload)
        except Exception:
            customer_result = {"error": "commerce_upstream_error"}
        if "error" in customer_result:
            continue
        customers = [
            customer
            for customer in customer_result.get("customers") or []
            if isinstance(customer, dict) and customer.get("id") is not None
        ]
        if len(customers) != 1:
            continue
        try:
            order_result = await execute(
                "list_orders",
                {"customer_id": str(customers[0]["id"])},
            )
        except Exception:
            order_result = {"error": "commerce_upstream_error"}
        if "error" in order_result:
            continue
        orders = [
            order
            for order in order_result.get("orders") or []
            if isinstance(order, dict)
            and _canonical_tray_order_id(order)
        ]
        if not orders:
            continue
        matched = [
            order
            for order in orders
            if _order_matches_preferred_codes(order, preferred)
        ]
        pool = matched or orders
        ranked = sorted(
            pool,
            key=lambda order: (
                1 if _order_matches_preferred_codes(order, preferred) else 0,
                _order_unpaid_score(order),
                str(order.get("created_at") or order.get("order_created_at") or ""),
            ),
            reverse=True,
        )
        best = ranked[0]
        order_id = _canonical_tray_order_id(best)
        if order_id:
            print("[sales.order.recover]", {
                "via": kind,
                "order_id_present": True,
                "matched_preferred_code": bool(matched),
                "candidates": len(orders),
                "resolved_numeric": order_id.isdigit(),
            })
            return order_id
    return None
