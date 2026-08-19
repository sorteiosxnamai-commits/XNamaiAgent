"""PostgreSQL repository for versioned user-managed agent personas."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from .db import get_conn, get_returning_id, to_jsonb
from .persona_models import PersonaVersion


DEFAULT_TENANT_ID = "newstore"
DEFAULT_PERSONA_KEY = "newstore_commercial"


def hash_instructions(instructions: str) -> str:
    return hashlib.sha256(instructions.encode("utf-8")).hexdigest()


def _row_to_persona(row: dict[str, Any]) -> PersonaVersion:
    return PersonaVersion.model_validate(row)


def get_active_persona(
    tenant_id: str = DEFAULT_TENANT_ID,
    persona_key: str = DEFAULT_PERSONA_KEY,
) -> PersonaVersion | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM public.ai_agent_persona_versions
                WHERE tenant_id = %s
                  AND persona_key = %s
                  AND status = 'active'
                LIMIT 1
                """,
                (tenant_id, persona_key),
            )
            row = cur.fetchone()
    return _row_to_persona(row) if row else None


def list_persona_versions(
    tenant_id: str = DEFAULT_TENANT_ID,
    persona_key: str = DEFAULT_PERSONA_KEY,
) -> list[PersonaVersion]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM public.ai_agent_persona_versions
                WHERE tenant_id = %s
                  AND persona_key = %s
                ORDER BY version DESC
                """,
                (tenant_id, persona_key),
            )
            rows = cur.fetchall() or []
    return [_row_to_persona(row) for row in rows]


def get_persona_version(
    persona_id: int,
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> PersonaVersion | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM public.ai_agent_persona_versions
                WHERE id = %s
                  AND tenant_id = %s
                LIMIT 1
                """,
                (persona_id, tenant_id),
            )
            row = cur.fetchone()
    return _row_to_persona(row) if row else None


def _next_version(cur: Any, tenant_id: str, persona_key: str) -> int:
    cur.execute(
        """
        SELECT COALESCE(MAX(version), 0) + 1 AS next_version
        FROM public.ai_agent_persona_versions
        WHERE tenant_id = %s
          AND persona_key = %s
        """,
        (tenant_id, persona_key),
    )
    row = cur.fetchone() or {}
    return int(row.get("next_version") or 1)


def create_persona_version(
    *,
    instructions: str,
    name: str = "NewStore Commercial",
    tenant_id: str = DEFAULT_TENANT_ID,
    persona_key: str = DEFAULT_PERSONA_KEY,
    source: str = "user",
    created_by: str | None = None,
    status: str = "draft",
    metadata: dict[str, Any] | None = None,
) -> PersonaVersion:
    from .persona_policy import assert_persona_instructions_safe

    assert_persona_instructions_safe(instructions)
    instructions_hash = hash_instructions(instructions)
    with get_conn() as conn:
        with conn.cursor() as cur:
            version = _next_version(cur, tenant_id, persona_key)
            cur.execute(
                """
                INSERT INTO public.ai_agent_persona_versions (
                    tenant_id, persona_key, version, name, source,
                    instructions, instructions_hash, status, created_by, metadata
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    tenant_id,
                    persona_key,
                    version,
                    name,
                    source,
                    instructions,
                    instructions_hash,
                    status,
                    created_by,
                    to_jsonb(metadata or {}),
                ),
            )
            persona_id = get_returning_id(cur.fetchone())
    persona = get_persona_version(int(persona_id), tenant_id=tenant_id)
    if persona is None:
        raise RuntimeError("persona_create_failed")
    return persona


def activate_persona_version(
    persona_id: int,
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
    activated_by: str | None = None,
) -> PersonaVersion:
    from .persona_policy import assert_persona_instructions_safe

    existing = get_persona_version(persona_id, tenant_id=tenant_id)
    if existing is None:
        raise ValueError("persona_not_found")
    assert_persona_instructions_safe(existing.instructions)

    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, persona_key, status
                FROM public.ai_agent_persona_versions
                WHERE id = %s AND tenant_id = %s
                LIMIT 1
                FOR UPDATE
                """,
                (persona_id, tenant_id),
            )
            target = cur.fetchone()
            if not target:
                raise ValueError("persona_not_found")
            persona_key = str(target["persona_key"])
            # Archive current active version(s).
            cur.execute(
                """
                UPDATE public.ai_agent_persona_versions
                SET status = 'archived',
                    archived_at = %s
                WHERE tenant_id = %s
                  AND persona_key = %s
                  AND status = 'active'
                  AND id <> %s
                """,
                (now, tenant_id, persona_key, persona_id),
            )
            cur.execute(
                """
                UPDATE public.ai_agent_persona_versions
                SET status = 'active',
                    activated_by = %s,
                    activated_at = %s,
                    archived_at = NULL
                WHERE id = %s
                  AND tenant_id = %s
                """,
                (activated_by, now, persona_id, tenant_id),
            )
    persona = get_persona_version(persona_id, tenant_id=tenant_id)
    if persona is None or persona.status != "active":
        raise RuntimeError("persona_activate_failed")
    return persona


def archive_persona_version(
    persona_id: int,
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> PersonaVersion:
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.ai_agent_persona_versions
                SET status = 'archived',
                    archived_at = %s
                WHERE id = %s
                  AND tenant_id = %s
                """,
                (now, persona_id, tenant_id),
            )
    persona = get_persona_version(persona_id, tenant_id=tenant_id)
    if persona is None:
        raise ValueError("persona_not_found")
    return persona


def rollback_persona_version(
    persona_id: int,
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
    activated_by: str | None = None,
) -> PersonaVersion:
    """Re-activate a previously archived/draft version (archives current active)."""
    return activate_persona_version(
        persona_id,
        tenant_id=tenant_id,
        activated_by=activated_by,
    )


def find_persona_by_hash(
    *,
    tenant_id: str,
    persona_key: str,
    instructions_hash: str,
    version: int | None = None,
) -> PersonaVersion | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            if version is None:
                cur.execute(
                    """
                    SELECT *
                    FROM public.ai_agent_persona_versions
                    WHERE tenant_id = %s
                      AND persona_key = %s
                      AND instructions_hash = %s
                    ORDER BY version DESC
                    LIMIT 1
                    """,
                    (tenant_id, persona_key, instructions_hash),
                )
            else:
                cur.execute(
                    """
                    SELECT *
                    FROM public.ai_agent_persona_versions
                    WHERE tenant_id = %s
                      AND persona_key = %s
                      AND instructions_hash = %s
                      AND version = %s
                    LIMIT 1
                    """,
                    (tenant_id, persona_key, instructions_hash, version),
                )
            row = cur.fetchone()
    return _row_to_persona(row) if row else None


def insert_prompt_compilation(
    *,
    tenant_id: str,
    compiled_instructions_hash: str,
    openai_api_mode: str,
    persona_version_id: int | None = None,
    instruction_extension_ids: list[int] | None = None,
    contact_memory_ids: list[int] | None = None,
    instructions_char_count: int = 0,
    input_char_count: int = 0,
    approximate_input_tokens: int = 0,
    conversation_key: str | None = None,
    sender_key: str | None = None,
    inbound_id: int | None = None,
    response_id: int | None = None,
    channel: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> int | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.ai_prompt_compilations (
                    tenant_id, conversation_key, sender_key,
                    inbound_id, response_id, persona_version_id,
                    instruction_extension_ids, contact_memory_ids,
                    compiled_instructions_hash,
                    instructions_char_count, input_char_count,
                    approximate_input_tokens, channel, openai_api_mode, metadata
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                RETURNING id
                """,
                (
                    tenant_id,
                    conversation_key,
                    sender_key,
                    inbound_id,
                    response_id,
                    persona_version_id,
                    to_jsonb(instruction_extension_ids or []),
                    to_jsonb(contact_memory_ids or []),
                    compiled_instructions_hash,
                    instructions_char_count,
                    input_char_count,
                    approximate_input_tokens,
                    channel,
                    openai_api_mode,
                    to_jsonb(metadata or {}),
                ),
            )
            return get_returning_id(cur.fetchone())
