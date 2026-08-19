"""HTTP endpoints for Mercado Pago PIX webhook and status poll."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .config import get_settings
from .mercadopago_client import MercadoPagoError
from .pix_payment_repository import get_pix_payment_by_mp_id
from .pix_payment_service import handle_mercadopago_webhook, refresh_pix_payment_status

router = APIRouter(prefix="/api/payments", tags=["payments-pix"])


@router.post("/webhook")
async def mercadopago_pix_webhook(request: Request) -> JSONResponse:
    """Mercado Pago notification URL. Always returns 200 to avoid infinite retries."""
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            payload = {"value": payload}
    except Exception:
        payload = {}

    query: dict[str, Any] = dict(request.query_params)
    result = await handle_mercadopago_webhook(payload, query)
    # MP expects 200 even when skipped/errors are handled internally.
    return JSONResponse(result, status_code=200)


@router.get("/{payment_id}/status")
async def pix_payment_status(payment_id: str) -> JSONResponse:
    """Poll PIX status from MP and sync local row (frontend 'Já paguei' pattern)."""
    settings = get_settings()
    if not settings.resolved_mp_access_token():
        return JSONResponse(
            {"error": "mp_token_missing"},
            status_code=503,
        )
    try:
        refreshed = await refresh_pix_payment_status(payment_id, settings=settings)
    except MercadoPagoError as exc:
        return JSONResponse(
            {
                "error": exc.code or "mp_http_error",
                "message": str(exc),
                "status_code": exc.status_code,
            },
            status_code=502 if (exc.status_code or 500) >= 500 else 400,
        )

    row = refreshed.get("row") or get_pix_payment_by_mp_id(payment_id)
    return JSONResponse(
        {
            "paymentId": refreshed.get("payment_id"),
            "id": refreshed.get("payment_id"),
            "status": refreshed.get("status"),
            "settlement_status": (row or {}).get("settlement_status"),
            "paid_at": (
                row.get("paid_at").isoformat()
                if row and row.get("paid_at")
                else None
            ),
        }
    )
