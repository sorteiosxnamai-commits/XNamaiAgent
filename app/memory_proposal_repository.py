"""Audit repository for model memory / extension proposals."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .db import get_conn, get_returning_id, to_jsonb


def insert_memory_proposal(
    *,
    tenant_id: str,
    proposal_type: str,
    target_scope: str,
    idempotency_key: str,
    conversation_key: str | None = None,
    sender_key: str | None = None,
    inbound_id: int | None = None,
    response_id: int | None = None,
    proposal_key: str | None = None,
    proposed_value: dict[str, Any] | list[Any] | Any | None = None,
    proposed_text: str | None = None,
    importance: float | None = None,
    confidence: float | None = None,
    reason_code: str | None = None,
    sensitive_detected: bool = False,
    status: str = "pending",
    rejection_codes: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> int | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.ai_memory_proposals (
                    tenant_id, conversation_key, sender_key,
                    inbound_id, response_id, proposal_type, target_scope,
                    proposal_key, proposed_value, proposed_text,
                    importance, confidence, reason_code, sensitive_detected,
                    status, idempotency_key, rejection_codes, metadata
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING id
                """,
                (
                    tenant_id,
                    conversation_key,
                    sender_key,
                    inbound_id,
                    response_id,
                    proposal_type,
                    target_scope,
                    proposal_key,
                    to_jsonb(proposed_value if proposed_value is not None else {}),
                    proposed_text,
                    importance,
                    confidence,
                    reason_code,
                    sensitive_detected,
                    status,
                    idempotency_key,
                    to_jsonb(rejection_codes or []),
                    to_jsonb(metadata or {}),
                ),
            )
            inserted = get_returning_id(cur.fetchone())
            if inserted is not None:
                return inserted
            cur.execute(
                """
                SELECT id FROM public.ai_memory_proposals
                WHERE idempotency_key = %s
                LIMIT 1
                """,
                (idempotency_key,),
            )
            return get_returning_id(cur.fetchone())


def mark_proposal_rejected(
    proposal_id: int,
    *,
    rejection_codes: list[str] | None = None,
    status: str = "rejected",
) -> None:
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.ai_memory_proposals
                SET status = %s,
                    rejection_codes = %s,
                    reviewed_at = %s
                WHERE id = %s
                """,
                (status, to_jsonb(rejection_codes or []), now, proposal_id),
            )


def mark_proposal_duplicate(proposal_id: int) -> None:
    mark_proposal_rejected(proposal_id, rejection_codes=["duplicate"], status="duplicate")


def mark_proposal_applied(
    proposal_id: int,
    *,
    applied_memory_id: int | None = None,
    applied_extension_id: int | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.ai_memory_proposals
                SET status = 'applied',
                    applied_memory_id = %s,
                    applied_extension_id = %s,
                    applied_at = %s,
                    reviewed_at = %s
                WHERE id = %s
                """,
                (
                    applied_memory_id,
                    applied_extension_id,
                    now,
                    now,
                    proposal_id,
                ),
            )


def mark_proposal_pending_review(proposal_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.ai_memory_proposals
                SET status = 'pending'
                WHERE id = %s
                """,
                (proposal_id,),
            )
