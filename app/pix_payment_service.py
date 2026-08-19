"""PIX payment orchestration: create+persist and Mercado Pago webhook handling."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Settings, get_settings
from .mercadopago_client import (
    MercadoPagoError,
    PixPaymentCreated,
    create_pix_payment,
    get_payment,
)
from . import pix_payment_repository as repo
from .pix_settlement import settle_approved_pix_payment


def extract_mp_payment_id(
    payload: dict[str, Any] | None,
    query: dict[str, Any] | None = None,
) -> str | None:
    body = payload if isinstance(payload, dict) else {}
    q = query if isinstance(query, dict) else {}

    candidates = [
        (body.get("data") or {}).get("id") if isinstance(body.get("data"), dict) else None,
        body.get("id"),
        q.get("data.id"),
        q.get("id"),
    ]
    for value in candidates:
        if value is None or value == "":
            continue
        return str(value)
    return None


def extract_mp_notification_type(
    payload: dict[str, Any] | None,
    query: dict[str, Any] | None = None,
) -> str | None:
    body = payload if isinstance(payload, dict) else {}
    q = query if isinstance(query, dict) else {}
    for value in (body.get("type"), body.get("action"), q.get("type"), q.get("topic")):
        if value is None or value == "":
            continue
        text = str(value).strip().lower()
        if text.startswith("payment."):
            return "payment"
        return text
    return None


def _parse_mp_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def amount_to_cents(amount: float | int | None) -> int:
    return int(round(float(amount or 0) * 100))


async def create_and_persist_pix_payment(
    *,
    transaction_amount: float | int,
    description: str,
    payer_email: str,
    external_reference: str | None = None,
    metadata: dict[str, Any] | None = None,
    conversation_id: str | None = None,
    sender_key: str | None = None,
    sender_phone: str | None = None,
    channel: str | None = None,
    cart_session_id: str | None = None,
    checkout_snapshot: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> tuple[PixPaymentCreated, int | None]:
    cfg = settings or get_settings()
    created = await create_pix_payment(
        transaction_amount=transaction_amount,
        description=description,
        payer_email=payer_email,
        external_reference=external_reference,
        metadata=metadata,
        settings=cfg,
    )
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=int(created.expires_in_seconds or cfg.pix_exp_min * 60)
    )
    amount_cents = amount_to_cents(
        created.transaction_amount
        if created.transaction_amount is not None
        else transaction_amount
    )
    snapshot = dict(checkout_snapshot or {})
    if snapshot.get("expected_amount_cents") is None:
        snapshot["expected_amount_cents"] = amount_cents
    row_id = repo.upsert_pix_payment_created(
        mp_payment_id=created.payment_id,
        status=created.status,
        amount_cents=amount_cents,
        description=description,
        payer_email=payer_email,
        qr_code=created.qr_code or None,
        qr_code_base64=created.qr_code_base64 or None,
        external_reference=external_reference,
        date_of_expiration=_parse_mp_datetime(created.date_of_expiration),
        expires_at=expires_at,
        conversation_id=conversation_id,
        sender_key=sender_key,
        sender_phone=sender_phone,
        channel=channel,
        cart_session_id=cart_session_id,
        checkout_snapshot=snapshot,
        metadata=metadata,
        raw_create=created.raw,
    )
    return created, row_id


async def refresh_pix_payment_status(
    payment_id: str,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    cfg = settings or get_settings()
    payload = await get_payment(payment_id, settings=cfg)
    status = str(payload.get("status") or "pending")
    row = repo.apply_mp_status_update(
        mp_payment_id=str(payload.get("id") or payment_id),
        status=status,
        raw_last_status=payload,
    )
    return {
        "payment_id": str(payload.get("id") or payment_id),
        "status": status,
        "row": row,
        "raw": payload,
    }


async def handle_mercadopago_webhook(
    payload: dict[str, Any] | None,
    query: dict[str, Any] | None = None,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Process MP notification. Always safe to acknowledge with HTTP 200."""
    cfg = settings or get_settings()
    notify_type = extract_mp_notification_type(payload, query)
    if notify_type and notify_type != "payment":
        print("[mp.webhook] skipped", {"reason": "ignored_type", "type": notify_type})
        return {"ok": True, "skipped": True, "reason": "ignored_type", "type": notify_type}

    payment_id = extract_mp_payment_id(payload, query)
    if not payment_id:
        print("[mp.webhook] skipped", {"reason": "missing_payment_id"})
        return {"ok": True, "skipped": True, "reason": "missing_payment_id"}

    if not cfg.resolved_mp_access_token():
        print("[mp.webhook] skipped", {"reason": "mp_token_missing", "payment_id": payment_id})
        return {"ok": True, "skipped": True, "reason": "mp_token_missing", "payment_id": payment_id}

    try:
        refreshed = await refresh_pix_payment_status(payment_id, settings=cfg)
    except MercadoPagoError as exc:
        print("[mp.webhook] mp_fetch_failed", {
            "payment_id": payment_id,
            "code": exc.code,
            "status_code": exc.status_code,
        })
        return {
            "ok": True,
            "skipped": True,
            "reason": "mp_fetch_failed",
            "payment_id": payment_id,
            "error": exc.code,
        }
    except Exception as exc:  # noqa: BLE001 — webhook must not 5xx to MP
        print("[mp.webhook] error", {
            "payment_id": payment_id,
            "error_type": type(exc).__name__,
        })
        return {
            "ok": True,
            "skipped": True,
            "reason": "internal_error",
            "payment_id": payment_id,
        }

    row = refreshed.get("row")
    status = refreshed.get("status")
    result: dict[str, Any] = {
        "ok": True,
        "payment_id": refreshed.get("payment_id"),
        "status": status,
        "persisted": bool(row),
        "settlement_status": (row or {}).get("settlement_status"),
    }

    if str(status).lower() == "approved":
        # Only creates Tray order when PIX is approved AND amounts match.
        try:
            settlement = await settle_approved_pix_payment(
                str(result["payment_id"]),
                mp_payload=refreshed.get("raw")
                if isinstance(refreshed.get("raw"), dict)
                else None,
            )
        except Exception as exc:  # noqa: BLE001 — never fail the MP webhook
            print("[mp.webhook] settle_error", {
                "payment_id": result["payment_id"],
                "error_type": type(exc).__name__,
            })
            settlement = {
                "ok": False,
                "action": "failed",
                "reason": "settle_exception",
            }
        result["settlement"] = settlement
        result["settlement_status"] = settlement.get("settlement_status") or (
            (row or {}).get("settlement_status")
        )
        print("[mp.webhook] approved", {
            "payment_id": result["payment_id"],
            "persisted": result["persisted"],
            "settlement_action": settlement.get("action"),
            "settlement_reason": settlement.get("reason"),
            "tray_order_id": settlement.get("tray_order_id"),
        })
    else:
        print("[mp.webhook] updated", {
            "payment_id": result["payment_id"],
            "status": status,
            "persisted": result["persisted"],
        })

    return result
