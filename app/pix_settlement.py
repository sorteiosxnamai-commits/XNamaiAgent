"""Settle approved Mercado Pago PIX into a Tray order.

Hard rules:
- Only settle when MP payment status is exactly ``approved`` (fresh from API).
- PIX amount (MP) must equal stored PIX amount and expected order total (cents).
- Checkout snapshot must include a full ``order_payload`` for Tray create.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Any, Awaitable, Callable

from . import pix_payment_repository as repo
from .mercadopago_client import MercadoPagoError, get_payment
from .tray_adapter_client import TrayAdapterClient, TrayAdapterError

CreateOrderFn = Callable[[dict[str, Any]], Awaitable[Any]]


def brl_to_cents(amount: Any) -> int | None:
    if amount is None or amount == "":
        return None
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if value < 0:
        return None
    return int((value * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def cents_equal(*values: int | None) -> bool:
    if not values or any(v is None for v in values):
        return False
    first = values[0]
    return all(int(v) == int(first) for v in values)


def expected_amount_cents_from_snapshot(snapshot: dict[str, Any] | None) -> int | None:
    data = snapshot if isinstance(snapshot, dict) else {}
    direct = data.get("expected_amount_cents")
    if direct is not None:
        try:
            return int(direct)
        except (TypeError, ValueError):
            return None
    # Fallbacks from order summary fields stored at PIX creation time.
    for key in ("display_total", "order_total", "total"):
        cents = brl_to_cents(data.get(key))
        if cents is not None:
            return cents
    payload = data.get("order_payload")
    if isinstance(payload, dict):
        products = payload.get("products") or []
        shipping = payload.get("shipping") or {}
        try:
            subtotal = sum(
                Decimal(str(p.get("price"))) * int(p.get("quantity") or 0)
                for p in products
                if isinstance(p, dict)
            )
            ship = Decimal(str(shipping.get("value") or "0"))
            return brl_to_cents(subtotal + ship)
        except (InvalidOperation, ValueError, TypeError):
            return None
    return None


def validate_pix_settlement_amounts(
    *,
    mp_status: str | None,
    mp_amount_cents: int | None,
    stored_amount_cents: int | None,
    expected_amount_cents: int | None,
) -> str | None:
    """Return rejection reason code, or None when settlement is allowed."""
    if str(mp_status or "").strip().lower() != "approved":
        return "pix_not_approved"
    if stored_amount_cents is None or int(stored_amount_cents) <= 0:
        return "stored_amount_missing"
    if expected_amount_cents is None or int(expected_amount_cents) <= 0:
        return "expected_amount_missing"
    if mp_amount_cents is None or int(mp_amount_cents) <= 0:
        return "mp_amount_missing"
    if not cents_equal(mp_amount_cents, stored_amount_cents, expected_amount_cents):
        return "amount_mismatch"
    return None


def _order_id_from_result(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    if result.get("error"):
        return None
    order_id = result.get("order_id") or result.get("id")
    if order_id is None and isinstance(result.get("order"), dict):
        order_id = result["order"].get("order_id") or result["order"].get("id")
    return str(order_id) if order_id is not None else None


async def _default_create_order(payload: dict[str, Any]) -> Any:
    return await TrayAdapterClient().create_order(payload)


async def settle_approved_pix_payment(
    payment_id: str,
    *,
    mp_payload: dict[str, Any] | None = None,
    create_order: CreateOrderFn | None = None,
) -> dict[str, Any]:
    """Create Tray order only after approved PIX + exact amount match."""
    pid = str(payment_id or "").strip()
    if not pid:
        return {"ok": False, "action": "rejected", "reason": "invalid_payment_id"}

    row = repo.get_pix_payment_by_mp_id(pid)
    if not row:
        return {"ok": False, "action": "rejected", "reason": "pix_row_missing", "payment_id": pid}

    current_settlement = str(row.get("settlement_status") or "none")
    if current_settlement == "completed" and row.get("tray_order_id"):
        return {
            "ok": True,
            "action": "already_settled",
            "reason": "already_completed",
            "payment_id": pid,
            "tray_order_id": row.get("tray_order_id"),
            "settlement_status": "completed",
        }
    if current_settlement == "processing":
        return {
            "ok": True,
            "action": "in_progress",
            "reason": "settlement_in_progress",
            "payment_id": pid,
            "settlement_status": "processing",
        }

    if mp_payload is None:
        try:
            mp_payload = await get_payment(pid)
        except MercadoPagoError as exc:
            return {
                "ok": False,
                "action": "rejected",
                "reason": "mp_fetch_failed",
                "payment_id": pid,
                "error": exc.code,
            }

    mp_status = str(mp_payload.get("status") or "").strip().lower()
    mp_amount_cents = brl_to_cents(mp_payload.get("transaction_amount"))
    stored_amount_cents = row.get("amount_cents")
    try:
        stored_amount_cents = int(stored_amount_cents) if stored_amount_cents is not None else None
    except (TypeError, ValueError):
        stored_amount_cents = None

    snapshot = row.get("checkout_snapshot") if isinstance(row.get("checkout_snapshot"), dict) else {}
    expected_cents = expected_amount_cents_from_snapshot(snapshot)

    reject = validate_pix_settlement_amounts(
        mp_status=mp_status,
        mp_amount_cents=mp_amount_cents,
        stored_amount_cents=stored_amount_cents,
        expected_amount_cents=expected_cents,
    )
    if reject:
        # Keep pending when not approved yet; fail hard on amount/data problems after approval.
        if reject == "pix_not_approved":
            print("[mp.settle] skipped", {
                "payment_id": pid,
                "reason": reject,
                "mp_status": mp_status,
            })
            return {
                "ok": False,
                "action": "skipped",
                "reason": reject,
                "payment_id": pid,
                "mp_status": mp_status,
            }
        updated = repo.mark_pix_settlement(
            pid,
            settlement_status="failed",
            settlement_error=reject,
        )
        print("[mp.settle] rejected", {
            "payment_id": pid,
            "reason": reject,
            "mp_amount_cents": mp_amount_cents,
            "stored_amount_cents": stored_amount_cents,
            "expected_amount_cents": expected_cents,
        })
        return {
            "ok": False,
            "action": "rejected",
            "reason": reject,
            "payment_id": pid,
            "mp_amount_cents": mp_amount_cents,
            "stored_amount_cents": stored_amount_cents,
            "expected_amount_cents": expected_cents,
            "settlement_status": (updated or {}).get("settlement_status") or "failed",
        }

    order_payload = snapshot.get("order_payload")
    if not isinstance(order_payload, dict) or not order_payload.get("products"):
        updated = repo.mark_pix_settlement(
            pid,
            settlement_status="failed",
            settlement_error="checkout_snapshot_incomplete",
        )
        return {
            "ok": False,
            "action": "rejected",
            "reason": "checkout_snapshot_incomplete",
            "payment_id": pid,
            "settlement_status": (updated or {}).get("settlement_status") or "failed",
        }

    claimed = repo.claim_pix_settlement(pid)
    if not claimed:
        latest = repo.get_pix_payment_by_mp_id(pid) or {}
        return {
            "ok": True,
            "action": "already_settled" if latest.get("tray_order_id") else "in_progress",
            "reason": "claim_failed",
            "payment_id": pid,
            "tray_order_id": latest.get("tray_order_id"),
            "settlement_status": latest.get("settlement_status"),
        }

    create = create_order or _default_create_order
    try:
        created = await create(order_payload)
    except TrayAdapterError as exc:
        repo.mark_pix_settlement(
            pid,
            settlement_status="failed",
            settlement_error=f"tray_error:{exc.error or exc}",
        )
        print("[mp.settle] tray_failed", {
            "payment_id": pid,
            "status_code": exc.status_code,
            "error": exc.error,
        })
        return {
            "ok": False,
            "action": "failed",
            "reason": "tray_create_failed",
            "payment_id": pid,
            "status_code": exc.status_code,
        }
    except Exception as exc:  # noqa: BLE001
        repo.mark_pix_settlement(
            pid,
            settlement_status="failed",
            settlement_error=f"tray_exception:{type(exc).__name__}",
        )
        print("[mp.settle] tray_exception", {
            "payment_id": pid,
            "error_type": type(exc).__name__,
        })
        return {
            "ok": False,
            "action": "failed",
            "reason": "tray_create_failed",
            "payment_id": pid,
        }

    order_id = _order_id_from_result(created)
    if not order_id:
        repo.mark_pix_settlement(
            pid,
            settlement_status="failed",
            settlement_error="tray_order_id_missing",
        )
        return {
            "ok": False,
            "action": "failed",
            "reason": "tray_order_id_missing",
            "payment_id": pid,
            "tray_result": created if isinstance(created, dict) else None,
        }

    updated = repo.mark_pix_settlement(
        pid,
        settlement_status="completed",
        tray_order_id=order_id,
        settlement_error=None,
    )
    print("[mp.settle] completed", {
        "payment_id": pid,
        "tray_order_id": order_id,
        "amount_cents": stored_amount_cents,
        "created_after_pix_approval": True,
    })
    return {
        "ok": True,
        "action": "settled",
        "reason": "created_after_pix_approval",
        "payment_id": pid,
        "tray_order_id": order_id,
        "amount_cents": stored_amount_cents,
        "mp_amount_cents": mp_amount_cents,
        "expected_amount_cents": expected_cents,
        "settlement_status": (updated or {}).get("settlement_status") or "completed",
    }
