"""Status-only Mercos snapshot. No customer data, items, totals or payment claims.

Production denies GET by id. Only the documented incremental list is used.
The initial seven-day window is deliberately NOT a complete order history:
absence means unconfirmed, never a claim that the order does not exist.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from .client import MercosAdaptorError

FRESH_SECONDS = 300
STATUS = {"0": "Cancelado", "1": "Orçamento", "2": "Pedido gerado"}
BILLING = {"0": "Não faturado", "1": "Parcialmente faturado", "2": "Faturado"}


def normalize_status(row):
    def identifier(value):
        if isinstance(value, bool) or not str(value or "").isascii() or not str(value or "").isdigit():
            raise ValueError("invalid_order_identifier")
        return str(value)
    return {
        "mercos_id": identifier(row.get("id")),
        "order_number": identifier(row.get("numero")),
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
        return bool(row and row["completed_at"] and
                    datetime.now(timezone.utc) - row["completed_at"] < timedelta(seconds=FRESH_SECONDS))

    async def sync(self, client, *, max_pages=2):
        """One writer per tenant; commit pages and cursor together, bounded calls."""
        try:
            with self.connect() as conn:
                conn.execute("SET LOCAL lock_timeout='500ms'")
                start = datetime.now(timezone.utc) - timedelta(days=7)
                conn.execute("""INSERT INTO public.ai_mercos_order_sync(tenant_id,cursor_value,window_start)
                    VALUES (%s,%s,%s) ON CONFLICT (tenant_id) DO NOTHING""",
                    (self.tenant_id, start.strftime("%Y-%m-%dT%H:%M:%S"), start))
                state = conn.execute("SELECT cursor_value FROM public.ai_mercos_order_sync WHERE tenant_id=%s FOR UPDATE NOWAIT",
                                     (self.tenant_id,)).fetchone()
                cursor = state["cursor_value"]
                count = 0
                for page_index in range(max(1, min(max_pages, 5))):
                    page = await asyncio.wait_for(client.list_resource("orders", changed_after=cursor), timeout=12)
                    rows = [normalize_status(row) for row in page.data]
                    for row in rows:
                        conn.execute("""INSERT INTO public.ai_mercos_order_status
                            (tenant_id,mercos_id,order_number,status_code,billing_code,excluded)
                            VALUES (%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (tenant_id,mercos_id) DO UPDATE SET
                            order_number=EXCLUDED.order_number,status_code=EXCLUDED.status_code,
                            billing_code=EXCLUDED.billing_code,excluded=EXCLUDED.excluded""",
                            (self.tenant_id, row["mercos_id"], row["order_number"], row["status_code"], row["billing_code"], row["excluded"]))
                    next_cursor = page.next_cursor or page.page_cursor or cursor
                    if page.has_more and next_cursor == cursor:
                        raise ValueError("cursor_stalled")
                    cursor = next_cursor
                    count += len(rows)
                    conn.execute("UPDATE public.ai_mercos_order_sync SET cursor_value=%s, completed_at=NULL WHERE tenant_id=%s",
                                 (cursor, self.tenant_id))
                    if not page.has_more:
                        conn.execute("UPDATE public.ai_mercos_order_sync SET completed_at=now() WHERE tenant_id=%s", (self.tenant_id,))
                        return {"ok": True, "complete": True, "records": count, "pages": page_index + 1}
                return {"ok": True, "complete": False, "records": count, "pages": max_pages}
        except MercosAdaptorError as exc:
            return {"ok": False, "error": exc.code, "status_code": exc.status_code}
        except TimeoutError:
            return {"ok": False, "error": "order_sync_timeout"}
        except Exception as exc:
            return {"ok": False, "error": "order_index_unavailable", "error_type": type(exc).__name__}

    async def lookup(self, reference, client):
        reference = str(reference or "").strip()
        if not reference.isascii() or not reference.isdigit():
            return {"ok": False, "error": "invalid_order_reference"}
        try:
            if not self.ready():
                sync = await self.sync(client)
                if not sync.get("complete"):
                    return {"ok": False, "error": "order_index_not_ready", "code": sync.get("error") or "sync_in_progress"}
            with self.connect() as conn:
                rows = conn.execute("""SELECT mercos_id,order_number,status_code,billing_code,excluded
                    FROM public.ai_mercos_order_status WHERE tenant_id=%s AND (order_number=%s OR mercos_id=%s) LIMIT 2""",
                    (self.tenant_id, reference, reference)).fetchall()
            return status_result(rows, reference)
        except Exception:
            return {"ok": False, "error": "order_index_unavailable"}
