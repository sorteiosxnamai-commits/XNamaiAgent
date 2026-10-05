"""Published persona and delivered history, bounded by server-owned workspace."""
from __future__ import annotations

import hashlib
import json

from app.db import get_conn
from app.config import get_settings
from app.memory_scope import trusted_workspace
from app.persona_knowledge import approved_documents
from app.persona_repository import get_active_persona


def load_persona(settings, workspace_id):
    workspace = trusted_workspace(settings, workspace_id)
    if not workspace:
        raise ValueError("direct_workspace_required")
    persona = get_active_persona(settings.agent_persona_tenant_id,
                                 settings.agent_persona_key, workspace)
    if (persona is None or persona.workspace_id != workspace
            or persona.tenant_id != settings.agent_persona_tenant_id
            or persona.persona_key != settings.agent_persona_key
            or persona.status != "active" or not persona.instructions.strip()):
        raise ValueError("direct_persona_unavailable")
    from app.prompt_compiler import _has_legacy_store_identity
    if _has_legacy_store_identity(persona.instructions):
        raise ValueError("direct_persona_identity_mismatch")
    return workspace, persona


def persona_context(persona):
    # Never serialize operational settings, credentials or arbitrary metadata.
    content = json.dumps({"version": persona.id, "instructions": persona.instructions,
                          "name": persona.metadata.get("persona_display_name") or persona.name},
                         ensure_ascii=False)
    return {"content": content, "sha256": hashlib.sha256(content.encode()).hexdigest()}


def knowledge_documents(persona):
    return approved_documents(persona.metadata.get("knowledge_documents") or [])


def load_history(incoming, workspace_id, *, limit=30):
    identity = incoming.sender_key or incoming.sender_phone or incoming.visitor_id
    if not identity or not incoming.conversation_id:
        return [], {}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT i.text, r.reply_text,
                       r.provider_response->'_agent_metadata' AS metadata
                FROM public.ai_inbound_messages i
                JOIN LATERAL (
                    SELECT reply_text, provider_response
                    FROM public.ai_agent_responses
                    WHERE inbound_id=i.id AND workspace_id=%(workspace)s::uuid
                      AND provider_send_ok=true
                      AND provider_response->>'_agent_tenant_id'=%(tenant)s
                    ORDER BY id DESC LIMIT 1
                ) r ON true
                WHERE i.workspace_id=%(workspace)s::uuid
                  AND i.conversation_id=%(conversation)s
                  AND i.provider=%(provider)s AND i.channel=%(channel)s
                  AND COALESCE(i.sender_key, i.sender_phone, i.visitor_id)=%(identity)s
                  AND (%(before)s::bigint IS NULL OR i.id < %(before)s::bigint)
                ORDER BY i.id DESC LIMIT %(limit)s
            """, {"workspace": workspace_id, "conversation": incoming.conversation_id,
                  "provider": incoming.provider, "channel": incoming.channel,
                  "identity": identity, "before": incoming.raw.get("inbound_id"),
                  "tenant": get_settings().agent_persona_tenant_id,
                  "limit": max(1, min(limit, 30))})
            rows = list(reversed(cur.fetchall()))
    history = []
    for row in rows:
        metadata = row.get("metadata") or {}
        if metadata.get("customer_registration_state") or metadata.get("direct_registration"):
            # Keep registration data in operational state, outside model history.
            continue
        history.extend([{"role": "user", "content": row["text"] or "[Mídia recebida]"},
                        {"role": "assistant", "content": row["reply_text"]}])
    return history, (rows[-1].get("metadata") or {}) if rows else {}
