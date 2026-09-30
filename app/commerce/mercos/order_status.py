"""Status-only Mercos snapshot. No customer data, items, totals or payment claims.

Production denies GET by id. Only the documented incremental list is used.
Recent changes and the historical backfill have independent cursors. Absence
means unconfirmed, never a claim that the order does not exist.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from .client import MercosAdaptorError

FRESH_SECONDS = 300
STATUS = {"0": "Cancelado", "1": "Orçamento", "2": "Pedido gerado"}
BILLING = {"0": "Não faturado", "1": "Parcialmente faturado", "2": "Faturado"}


def normalize_status(row):
    def identifier(value, *, allow_zero=False):
        text = str(value) if value is not None else ""
        if (isinstance(value, bool) or not text.isascii() or not text.isdigit()
                or (not allow_zero and int(text) == 0)):
            raise ValueError("invalid_order_identifier")
        return text
    return {
        "mercos_id": identifier(row.get("id")),
        # Legacy Mercos records can carry numero=0. Retain their internal ID;
        # never fail the entire history page or invent a customer-facing number.
        "order_number": identifier(row.get("numero"), allow_zero=True),
        "status_code": str(row["status"]) if row.get("status") is not None else None,
        "billing_code": str(row["status_faturamento"]) if row.get("status_faturamento") is not None else None,
        "excluded": row.get("excluido") is True,
    }


def status_result(rows, reference):
    matches = {row["mercos_id"]: row for row in rows
               if reference in (row["mercos_id"], row["order_number"])}
    if len(matches) != 1:
        return {"ok": False, "error": "order_reference_unconfirmed",
                "code": "ambiguous_reference" if matches else "outside_synced_window"}
    row = next(iter(matches.values()))
    label = STATUS.get(row["status_code"])
    if row["excluded"] or not label:
        return {"ok": False, "error": "order_status_unconfirmed"}
    # Billing is not payment or shipment; never infer either from these codes.
    if row["status_code"] == "2" and row["billing_code"] in BILLING:
        label += " — " + BILLING[row["billing_code"]].lower()
    return {"ok": True, "success": True, "order_id": row["mercos_id"],
            "order_number": row["order_number"], "status": label,
            "status_group": "cancelled" if row["status_code"] == "0" else "registered",
            "payment_supported": False, "source": "mercos_order_status_index"}


class OrderStatusIndex:
    def __init__(self, *, tenant_id, connect=None):
        from ...db import get_conn
        if not str(tenant_id or "").strip():
            raise ValueError("tenant_required")
        self.tenant_id = tenant_id
        self.connect = connect or get_conn

    def ready(self):
        with self.connect() as conn:
            row = conn.execute("SELECT completed_at FROM public.ai_mercos_order_sync WHERE tenant_id=%s",
                               (self.tenant_id,)).fetchone()
        return bool(row and self._fresh(row["completed_at"]))

    @staticmethod
    def _fresh(timestamp):
        return bool(timestamp and timedelta(0) <= datetime.now(timezone.utc) - timestamp
                    < timedelta(seconds=FRESH_SECONDS))

    async def sync(self, client, *, max_pages=2, historical=False, timeout_seconds=12):
        """One writer per tenant; commit pages and cursor together, bounded calls."""
        page_limit = max(1, min(max_pages, 5))
        cursor_column = "history_cursor" if historical else "cursor_value"
        completed_column = "history_completed_at" if historical else "completed_at"
        try:
            with self.connect() as conn:
                conn.execute("SET LOCAL lock_timeout='500ms'")
                start = datetime.now(timezone.utc) - timedelta(days=7)
                conn.execute("""INSERT INTO public.ai_mercos_order_sync(tenant_id,cursor_value,window_start)
                    VALUES (%s,%s,%s) ON CONFLICT (tenant_id) DO NOTHING""",
                    (self.tenant_id, start.strftime("%Y-%m-%dT%H:%M:%S"), start))
                state = conn.execute("SELECT cursor_value, history_cursor, history_completed_at FROM public.ai_mercos_order_sync WHERE tenant_id=%s FOR UPDATE NOWAIT",
                                     (self.tenant_id,)).fetchone()
                if historical and state["history_completed_at"]:
                    return {"ok": True, "complete": True, "records": 0, "pages": 0}
                cursor = state[cursor_column]
                count = 0
                for page_index in range(page_limit):
                    page = await asyncio.wait_for(client.list_resource("orders", changed_after=cursor), timeout=timeout_seconds)
                    rows = [normalize_status(row) for row in page.data]
                    # History may overlap the live cursor. It must never replace
                    # a newer status (or deletion) already seen by the incremental.
                    conflict = "DO NOTHING" if historical else """DO UPDATE SET
                        order_number=EXCLUDED.order_number,status_code=EXCLUDED.status_code,
                        billing_code=EXCLUDED.billing_code,excluded=EXCLUDED.excluded,
                        verified_at=EXCLUDED.verified_at"""
                    for row in rows:
                        conn.execute(f"""INSERT INTO public.ai_mercos_order_status
                            (tenant_id,mercos_id,order_number,status_code,billing_code,excluded,verified_at)
                            VALUES (%s,%s,%s,%s,%s,%s,clock_timestamp())
                            ON CONFLICT (tenant_id,mercos_id) {conflict}""",
                            (self.tenant_id, row["mercos_id"], row["order_number"], row["status_code"], row["billing_code"], row["excluded"]))
                    next_cursor = page.next_cursor or page.page_cursor or cursor
                    if page.has_more and next_cursor == cursor:
                        raise ValueError("cursor_stalled")
                    cursor = next_cursor
                    count += len(rows)
                    conn.execute(f"UPDATE public.ai_mercos_order_sync SET {cursor_column}=%s, {completed_column}=NULL WHERE tenant_id=%s",
                                 (cursor, self.tenant_id))
                    if not page.has_more:
                        conn.execute(f"UPDATE public.ai_mercos_order_sync SET {completed_column}=clock_timestamp() WHERE tenant_id=%s", (self.tenant_id,))
                        return {"ok": True, "complete": True, "records": count, "pages": page_index + 1}
                return {"ok": True, "complete": False, "records": count, "pages": page_limit}
        except MercosAdaptorError as exc:
            return {"ok": False, "error": exc.code, "status_code": exc.status_code}
        except TimeoutError:
            return {"ok": False, "error": "order_sync_timeout"}
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "55P03":
                return {"ok": False, "error": "sync_already_running"}
            return {"ok": False, "error": "order_index_unavailable", "error_type": type(exc).__name__}

    async def background_sync(self, client):
        """At most three HTTP reads per tick, prioritizing current changes."""
        # The adapter serializes calls and may wait 60s after an upstream 429.
        # Background work can wait; the customer's lookup keeps its 12s limit.
        incremental = await self.sync(client, max_pages=2, timeout_seconds=75)
        history = None
        if incremental.get("complete"):
            history = await self.sync(client, max_pages=1, historical=True, timeout_seconds=75)
        return {"ok": bool(incremental.get("ok") and (history is None or history.get("ok"))),
                "incremental": incremental, "history": history}

    def _snapshot(self, reference):
        with self.connect() as conn:
            rows = conn.execute("""SELECT o.mercos_id,o.order_number,o.status_code,o.billing_code,o.excluded,
                o.verified_at,s.completed_at FROM public.ai_mercos_order_status o
                LEFT JOIN public.ai_mercos_order_sync s ON s.tenant_id=o.tenant_id
                WHERE o.tenant_id=%s AND (o.order_number=%s OR o.mercos_id=%s) LIMIT 2""",
                (self.tenant_id, reference, reference)).fetchall()
        # Check ambiguity before freshness: a fresh row cannot hide a collision.
        if len(rows) > 1:
            return status_result(rows, reference)
        if rows and all(self._fresh(row.get("completed_at")) or self._fresh(row.get("verified_at")) for row in rows):
            return status_result(rows, reference)
        return None

    async def lookup(self, reference, client):
        reference = str(reference or "").strip()
        if not reference.isascii() or not reference.isdigit():
            return {"ok": False, "error": "invalid_order_reference"}
        try:
            result = self._snapshot(reference)
            if result is not None:
                return result
            sync = await self.sync(client)
            # A matching row verified in a committed partial batch is usable;
            # unrelated remaining pages must not block this customer's order.
            result = self._snapshot(reference)
            if result is not None:
                return result
            if not sync.get("complete"):
                return {"ok": False, "error": "order_index_not_ready", "code": sync.get("error") or "sync_in_progress"}
            return status_result([], reference)
        except Exception:
            return {"ok": False, "error": "order_index_unavailable"}
