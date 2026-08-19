from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .config import Settings, get_settings
from .http_resilience import with_retries

DEFAULT_MP_BASE_URL = "https://api.mercadopago.com"
DEFAULT_TIMEOUT_SECONDS = 15.0


class MercadoPagoError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response: dict[str, Any] | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = response or {}
        self.code = code or "mercadopago_error"


@dataclass(frozen=True)
class PixPaymentCreated:
    payment_id: str
    status: str
    qr_code: str
    qr_code_base64: str
    copy_paste_code: str
    expires_in_seconds: int
    transaction_amount: float | None
    date_of_expiration: str | None
    raw: dict[str, Any]


def _mp_base_url(settings: Settings) -> str:
    return (settings.mp_base_url or DEFAULT_MP_BASE_URL).rstrip("/")


def _ensure_token(settings: Settings) -> str:
    token = settings.resolved_mp_access_token()
    if not token:
        raise MercadoPagoError(
            "MP_ACCESS_TOKEN/MERCADOPAGO_ACCESS_TOKEN não configurado.",
            code="mp_token_missing",
        )
    return token


def _to_brl_amount(amount: float | int) -> float:
    value = float(amount)
    if value < 0:
        raise MercadoPagoError("transaction_amount inválido", code="invalid_amount")
    return round(value, 2)


def _normalize_qr_fields(payload: dict[str, Any]) -> tuple[str, str]:
    td = ((payload.get("point_of_interaction") or {}).get("transaction_data") or {})
    qr_code = td.get("qr_code") or payload.get("qr_code") or ""
    qr_b64 = td.get("qr_code_base64") or payload.get("qr_code_base64") or ""
    if isinstance(qr_code, str):
        qr_code = qr_code.strip()
    else:
        qr_code = ""
    if isinstance(qr_b64, str):
        qr_b64 = "".join(qr_b64.split())
    else:
        qr_b64 = ""
    return qr_code, qr_b64


def normalize_pix_payment(
    payload: dict[str, Any],
    *,
    expires_in_seconds: int,
) -> PixPaymentCreated:
    payment_id = payload.get("id")
    if payment_id is None:
        raise MercadoPagoError(
            "Resposta MP sem id de pagamento",
            code="mp_payment_id_missing",
            response=payload,
        )
    qr_code, qr_b64 = _normalize_qr_fields(payload)
    amount = payload.get("transaction_amount")
    try:
        amount_f = float(amount) if amount is not None else None
    except (TypeError, ValueError):
        amount_f = None
    return PixPaymentCreated(
        payment_id=str(payment_id),
        status=str(payload.get("status") or "pending"),
        qr_code=qr_code,
        qr_code_base64=qr_b64,
        copy_paste_code=qr_code,
        expires_in_seconds=expires_in_seconds,
        transaction_amount=amount_f,
        date_of_expiration=(
            str(payload["date_of_expiration"])
            if payload.get("date_of_expiration")
            else None
        ),
        raw=payload,
    )


async def _mp_request(
    method: str,
    path: str,
    *,
    settings: Settings | None = None,
    body: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    cfg = settings or get_settings()
    token = _ensure_token(cfg)
    url = f"{_mp_base_url(cfg)}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "ns-agent-for-sorteios/1.0",
    }
    if idempotency_key:
        headers["X-Idempotency-Key"] = idempotency_key

    async def _do() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.request(method, url, json=body, headers=headers)
        try:
            data = resp.json() if resp.content else {}
        except Exception:
            data = {"raw": (resp.text or "")[:500]}
        if not isinstance(data, dict):
            data = {"raw": data}
        if resp.status_code >= 400:
            cause_text = None
            causes = data.get("cause")
            if isinstance(causes, list) and causes:
                cause_text = " | ".join(
                    str(c.get("description") or c.get("message") or c.get("code") or "")
                    for c in causes
                    if isinstance(c, dict)
                ).strip(" |")
            message = (
                data.get("message")
                or (data.get("error") if isinstance(data.get("error"), str) else None)
                or (
                    (data.get("error") or {}).get("message")
                    if isinstance(data.get("error"), dict)
                    else None
                )
                or f"MercadoPago {method} {path} falhou ({resp.status_code})"
            )
            if cause_text:
                message = f"{message}: {cause_text}"
            raise MercadoPagoError(
                str(message),
                status_code=resp.status_code,
                response=data,
                code="mp_http_error",
            )
        return data

    return await with_retries(_do, max_attempts=2)


async def create_pix_payment(
    *,
    transaction_amount: float | int,
    description: str,
    payer_email: str,
    external_reference: str | None = None,
    metadata: dict[str, Any] | None = None,
    notification_url: str | None = None,
    date_of_expiration: datetime | str | None = None,
    idempotency_key: str | None = None,
    settings: Settings | None = None,
) -> PixPaymentCreated:
    """Cria pagamento PIX no Mercado Pago (POST /v1/payments)."""
    cfg = settings or get_settings()
    expires_min = int(cfg.pix_exp_min or 30)
    if date_of_expiration is None:
        exp_dt = datetime.now(timezone.utc) + timedelta(minutes=expires_min)
        exp_value = exp_dt.isoformat().replace("+00:00", "Z")
    elif isinstance(date_of_expiration, datetime):
        exp_dt = date_of_expiration
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
        exp_value = exp_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    else:
        exp_value = str(date_of_expiration)

    notify = notification_url
    if notify is None:
        notify = cfg.pix_notification_url()

    body: dict[str, Any] = {
        "transaction_amount": _to_brl_amount(transaction_amount),
        "description": description or "Pagamento PIX New Store",
        "payment_method_id": "pix",
        "payer": {"email": (payer_email or "").strip() or "comprador@example.com"},
        "date_of_expiration": exp_value,
    }
    if external_reference:
        body["external_reference"] = str(external_reference)
    if metadata:
        body["metadata"] = metadata
    if notify:
        body["notification_url"] = notify

    payload = await _mp_request(
        "POST",
        "/v1/payments",
        settings=cfg,
        body=body,
        idempotency_key=idempotency_key or str(uuid.uuid4()),
    )
    return normalize_pix_payment(payload, expires_in_seconds=expires_min * 60)


async def get_payment(
    payment_id: str,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Consulta pagamento no Mercado Pago (GET /v1/payments/{id})."""
    pid = str(payment_id or "").strip()
    if not pid:
        raise MercadoPagoError("payment_id obrigatório", code="invalid_payment_id")
    return await _mp_request(
        "GET",
        f"/v1/payments/{pid}",
        settings=settings or get_settings(),
    )
