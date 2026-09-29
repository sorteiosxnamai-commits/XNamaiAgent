"""Reconcile ambiguous outbound deliveries from provider status callbacks.

An ambiguous send (``delivery_unknown``, see ``delivery_policy``) is never
retried to "find out". Only EVIDENCE from the provider moves it:

* provider says the message was accepted/sent/delivered/read -> ``sent``;
* provider says it failed                                   -> stays ``dead``,
  ``last_error`` becomes ``delivery_failed_confirmed:<code>`` (terminal);
* anything else                                            -> unchanged.

Only YCloud echoes our correlation id (``externalId = outbox:<id>``). Meta and
Brevo give no correlation for a send whose response was lost, so their
``delivery_unknown`` rows can only be closed manually.

Trust: the callback is authenticated upstream (HMAC + timestamp age). Here the
payload is still validated — id format, row existence, provider, recipient —
and every transition is a guarded single UPDATE, so a repeated or replayed
callback is a no-op and a terminal state never moves backwards.
"""

from __future__ import annotations

import re
from typing import Any

from app.config import get_settings
from app.db import get_conn, to_jsonb
from app.observability import log_event

from .delivery_policy import DELIVERY_UNKNOWN_ERROR

EXTERNAL_ID_RE = re.compile(r"^outbox:([1-9][0-9]{0,17})$")
DELIVERED_STATUSES = frozenset({"accepted", "sent", "delivered", "read"})
FAILED_STATUSES = frozenset({"failed"})
DELIVERY_FAILED_ERROR = "delivery_failed_confirmed"


def parse_outbox_external_id(value: Any) -> int | None:
    match = EXTERNAL_ID_RE.match(str(value or "").strip())
    return int(match.group(1)) if match else None


def _digits(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _same_recipient(row: dict[str, Any], recipient: str | None) -> bool:
    """The callback must be about the customer this row was addressed to."""
    if not recipient:
        return True  # provider omitted it; id + provider + signature still bind the row
    payload = row.get("reply_payload") or {}
    incoming = payload.get("incoming") if isinstance(payload, dict) else None
    expected = _digits((incoming or {}).get("sender_phone")) if isinstance(incoming, dict) else ""
    if not expected:
        return True
    got = _digits(recipient)
    # Tolerate country-code/9th-digit formatting differences on the tail only.
    return got == expected or got.endswith(expected[-10:]) or expected.endswith(got[-10:])


def _result(outcome: str, **fields: Any) -> dict[str, Any]:
    info = {"outcome": outcome, **fields}
    log_event("outbox.reconciliation", info)
    return info


def reconcile_provider_status(
    *,
    provider: str,
    external_id: Any,
    provider_status: Any,
    event_id: Any = None,
    recipient: str | None = None,
    error_code: Any = None,
) -> dict[str, Any]:
    outbox_id = parse_outbox_external_id(external_id)
    if outbox_id is None:
        return _result("ignored_foreign_external_id")
    status = str(provider_status or "").strip().casefold()
    if status not in DELIVERED_STATUSES | FAILED_STATUSES:
        return _result("ignored_status", outbox_id=outbox_id, provider_status=status or None)
    if not get_settings().database_url:
        return _result("no_database", outbox_id=outbox_id)

    evidence = {
        "provider": provider,
        "provider_status": status,
        "event_id": str(event_id or "")[:120] or None,
        "error_code": str(error_code or "")[:60] or None,
    }
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, provider, status, last_error, reply_payload, provider_response
                FROM public.ai_outbound_outbox
                WHERE id = %s
                FOR UPDATE
                """,
                (outbox_id,),
            )
            row = cur.fetchone()
            if not row:
                return _result("unknown_outbox", outbox_id=outbox_id)
            if str(row.get("provider") or "").casefold() != provider:
                return _result("provider_mismatch", outbox_id=outbox_id)
            if not _same_recipient(row, recipient):
                return _result("recipient_mismatch", outbox_id=outbox_id)

            previous = row.get("provider_response") if isinstance(row.get("provider_response"), dict) else {}
            seen = (previous.get("reconciliation") or {}).get("event_ids") or []
            if evidence["event_id"] and evidence["event_id"] in seen:
                return _result("duplicate_event", outbox_id=outbox_id, status=row.get("status"))
            reconciliation = {
                **evidence,
                "event_ids": [*seen, evidence["event_id"]][-20:] if evidence["event_id"] else list(seen),
            }

            if status in DELIVERED_STATUSES:
                cur.execute(
                    """
                    UPDATE public.ai_outbound_outbox
                    SET status = 'sent',
                        sent_at = COALESCE(sent_at, now()),
                        last_error = NULL,
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        provider_response = COALESCE(provider_response, '{}'::jsonb)
                            || jsonb_build_object('reconciliation', %(rec)s::jsonb),
                        updated_at = now()
                    WHERE id = %(id)s
                      AND (
                        status IN ('pending', 'failed', 'leased')
                        OR (status = 'dead' AND last_error LIKE %(unknown)s)
                      )
                    """,
                    {"id": outbox_id, "rec": to_jsonb(reconciliation), "unknown": f"{DELIVERY_UNKNOWN_ERROR}%"},
                )
                if cur.rowcount == 1:
                    return _result("confirmed_sent", outbox_id=outbox_id, previous_status=row.get("status"))
                return _result("no_transition", outbox_id=outbox_id, status=row.get("status"))

            cur.execute(
                """
                UPDATE public.ai_outbound_outbox
                SET last_error = %(error)s,
                    provider_response = COALESCE(provider_response, '{}'::jsonb)
                        || jsonb_build_object('reconciliation', %(rec)s::jsonb),
                    updated_at = now()
                WHERE id = %(id)s AND status = 'dead' AND last_error LIKE %(unknown)s
                """,
                {
                    "id": outbox_id,
                    "rec": to_jsonb(reconciliation),
                    "unknown": f"{DELIVERY_UNKNOWN_ERROR}%",
                    "error": f"{DELIVERY_FAILED_ERROR}:{evidence['error_code'] or 'unspecified'}"[:500],
                },
            )
            if cur.rowcount == 1:
                return _result("confirmed_failed", outbox_id=outbox_id)
            # A failure report for a row that is still retrying or already
            # sent says nothing definitive about THIS row's final state.
            return _result("no_transition", outbox_id=outbox_id, status=row.get("status"))
