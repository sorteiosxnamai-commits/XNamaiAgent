"""Persistence for Mercado Pago PIX payments created by the agent."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .db import get_conn, get_returning_id, to_jsonb


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row_to_dict(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    return dict(row)


def upsert_pix_payment_created(
    *,
    mp_payment_id: str,
    status: str,
    amount_cents: int,
    description: str | None = None,
    payer_email: str | None = None,
    qr_code: str | None = None,
    qr_code_base64: str | None = None,
    external_reference: str | None = None,
    date_of_expiration: datetime | None = None,
    expires_at: datetime | None = None,
    conversation_id: str | None = None,
    sender_key: str | None = None,
    sender_phone: str | None = None,
    channel: str | None = None,
    cart_session_id: str | None = None,
    checkout_snapshot: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    raw_create: dict[str, Any] | None = None,
    currency: str = "BRL",
) -> int | None:
    now = _now()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.ai_pix_payments (
                    mp_payment_id, status, amount_cents, currency, description,
                    payer_email, qr_code, qr_code_base64, external_reference,
                    date_of_expiration, expires_at,
                    conversation_id, sender_key, sender_phone, channel,
                    cart_session_id, checkout_snapshot, metadata, raw_create,
                    created_at, updated_at
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s
                )
                ON CONFLICT (mp_payment_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    qr_code = COALESCE(EXCLUDED.qr_code, public.ai_pix_payments.qr_code),
                    qr_code_base64 = COALESCE(
                        EXCLUDED.qr_code_base64,
                        public.ai_pix_payments.qr_code_base64
                    ),
                    description = COALESCE(
                        EXCLUDED.description,
                        public.ai_pix_payments.description
                    ),
                    payer_email = COALESCE(
                        EXCLUDED.payer_email,
                        public.ai_pix_payments.payer_email
                    ),
                    external_reference = COALESCE(
                        EXCLUDED.external_reference,
                        public.ai_pix_payments.external_reference
                    ),
                    date_of_expiration = COALESCE(
                        EXCLUDED.date_of_expiration,
                        public.ai_pix_payments.date_of_expiration
                    ),
                    expires_at = COALESCE(
                        EXCLUDED.expires_at,
                        public.ai_pix_payments.expires_at
                    ),
                    conversation_id = COALESCE(
                        EXCLUDED.conversation_id,
                        public.ai_pix_payments.conversation_id
                    ),
                    sender_key = COALESCE(
                        EXCLUDED.sender_key,
                        public.ai_pix_payments.sender_key
                    ),
                    sender_phone = COALESCE(
                        EXCLUDED.sender_phone,
                        public.ai_pix_payments.sender_phone
                    ),
                    channel = COALESCE(EXCLUDED.channel, public.ai_pix_payments.channel),
                    cart_session_id = COALESCE(
                        EXCLUDED.cart_session_id,
                        public.ai_pix_payments.cart_session_id
                    ),
                    checkout_snapshot = CASE
                        WHEN EXCLUDED.checkout_snapshot = '{}'::jsonb
                        THEN public.ai_pix_payments.checkout_snapshot
                        ELSE EXCLUDED.checkout_snapshot
                    END,
                    metadata = CASE
                        WHEN EXCLUDED.metadata = '{}'::jsonb
                        THEN public.ai_pix_payments.metadata
                        ELSE EXCLUDED.metadata
                    END,
                    raw_create = CASE
                        WHEN EXCLUDED.raw_create = '{}'::jsonb
                        THEN public.ai_pix_payments.raw_create
                        ELSE EXCLUDED.raw_create
                    END,
                    updated_at = EXCLUDED.updated_at
                RETURNING id
                """,
                (
                    str(mp_payment_id),
                    str(status or "pending"),
                    int(amount_cents),
                    currency or "BRL",
                    description,
                    payer_email,
                    qr_code,
                    qr_code_base64,
                    external_reference,
                    date_of_expiration,
                    expires_at,
                    conversation_id,
                    sender_key,
                    sender_phone,
                    channel,
                    cart_session_id,
                    to_jsonb(checkout_snapshot or {}),
                    to_jsonb(metadata or {}),
                    to_jsonb(raw_create or {}),
                    now,
                    now,
                ),
            )
            return get_returning_id(cur.fetchone())


def get_pix_payment_by_mp_id(mp_payment_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM public.ai_pix_payments
                WHERE mp_payment_id = %s
                LIMIT 1
                """,
                (str(mp_payment_id),),
            )
            return _row_to_dict(cur.fetchone())


def apply_mp_status_update(
    *,
    mp_payment_id: str,
    status: str,
    raw_last_status: dict[str, Any] | None = None,
    paid_at: datetime | None = None,
    mark_settlement_pending_on_approved: bool = True,
) -> dict[str, Any] | None:
    """Update status from MP poll/webhook. Returns updated row or None if missing."""
    now = _now()
    status_norm = str(status or "").strip().lower()
    is_approved = status_norm == "approved"
    effective_paid_at = paid_at if is_approved else None
    if is_approved and effective_paid_at is None:
        effective_paid_at = now

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.ai_pix_payments
                SET status = %s,
                    paid_at = CASE
                        WHEN %s THEN COALESCE(paid_at, %s)
                        ELSE paid_at
                    END,
                    settlement_status = CASE
                        WHEN %s
                             AND %s
                             AND settlement_status = 'none'
                        THEN 'pending'
                        ELSE settlement_status
                    END,
                    last_webhook_at = %s,
                    raw_last_status = %s,
                    updated_at = %s
                WHERE mp_payment_id = %s
                RETURNING *
                """,
                (
                    status_norm or "pending",
                    is_approved,
                    effective_paid_at,
                    is_approved,
                    mark_settlement_pending_on_approved,
                    now,
                    to_jsonb(raw_last_status or {}),
                    now,
                    str(mp_payment_id),
                ),
            )
            return _row_to_dict(cur.fetchone())


def claim_pix_settlement(mp_payment_id: str) -> dict[str, Any] | None:
    """Atomically move pending → processing. Returns row if claim succeeded."""
    now = _now()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.ai_pix_payments
                SET settlement_status = 'processing',
                    settlement_error = NULL,
                    updated_at = %s
                WHERE mp_payment_id = %s
                  AND lower(status) = 'approved'
                  AND settlement_status = 'pending'
                RETURNING *
                """,
                (now, str(mp_payment_id)),
            )
            return _row_to_dict(cur.fetchone())


def mark_pix_settlement(
    mp_payment_id: str,
    *,
    settlement_status: str,
    tray_order_id: str | None = None,
    settlement_error: str | None = None,
) -> dict[str, Any] | None:
    now = _now()
    settled = settlement_status in {"completed", "skipped"}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.ai_pix_payments
                SET settlement_status = %s,
                    tray_order_id = COALESCE(%s, tray_order_id),
                    settlement_error = %s,
                    settled_at = CASE WHEN %s THEN COALESCE(settled_at, %s) ELSE settled_at END,
                    updated_at = %s
                WHERE mp_payment_id = %s
                RETURNING *
                """,
                (
                    settlement_status,
                    tray_order_id,
                    settlement_error,
                    settled,
                    now,
                    now,
                    str(mp_payment_id),
                ),
            )
            return _row_to_dict(cur.fetchone())
