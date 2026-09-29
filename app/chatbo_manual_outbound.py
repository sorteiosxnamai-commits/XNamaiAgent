from __future__ import annotations

from typing import Any

from app.db import get_conn


def claim_message(message_id: str, workspace_id: str) -> dict[str, Any] | None:
    """Claim one trusted ChatBô row; the UUID is a single-use capability."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.mensagens AS m
                   SET provider_status = 'dispatching'
                  FROM public.conversas AS c
                 WHERE m.id = %s::uuid
                   AND c.id = m.conversa_id
                   AND c.workspace_id = %s::uuid
                   AND m.direction = 'outbound'
                   AND m.sender IN ('agent', 'ai')
                   AND m.status = 'sending'
                RETURNING m.id::text AS id, m.content, c.contact_phone,
                          c.workspace_id::text AS workspace_id
                """,
                (message_id, workspace_id),
            )
            row = cur.fetchone()
            if row:
                return dict(row)

            cur.execute(
                """
                SELECT m.id::text AS id, m.status, m.provider_status,
                       c.workspace_id::text AS workspace_id
                  FROM public.mensagens AS m
                  JOIN public.conversas AS c ON c.id = m.conversa_id
                 WHERE m.id = %s::uuid AND c.workspace_id = %s::uuid
                """,
                (message_id, workspace_id),
            )
            existing = cur.fetchone()
            if existing and (
                existing.get("status") == "sent"
                or existing.get("provider_status") == "sent"
            ):
                return {"id": message_id, "already_sent": True}
            return None


def finish_message(message_id: str, workspace_id: str, delivery: dict[str, Any]) -> None:
    sent = bool(delivery.get("ok"))
    provider_id = delivery.get("provider_message_id") or delivery.get("wamid")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.mensagens AS m
                   SET status = %s,
                       provider_status = %s,
                       external_id = COALESCE(%s, m.external_id)
                  FROM public.conversas AS c
                 WHERE m.id = %s::uuid
                   AND c.id = m.conversa_id
                   AND c.workspace_id = %s::uuid
                """,
                (
                    "sent" if sent else "failed",
                    "sent" if sent else "failed",
                    str(provider_id) if provider_id else None,
                    message_id,
                    workspace_id,
                ),
            )
