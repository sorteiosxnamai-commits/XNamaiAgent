"""Admin API for versioned agent personas (ADMIN_API_TOKEN)."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .config import get_settings
from .models import IncomingMessage
from .persona_models import PersonaVersion, PersonaVersionCreate
from .persona_repository import (
    DEFAULT_PERSONA_KEY,
    activate_persona_version,
    archive_persona_version,
    create_persona_version,
    get_active_persona,
    get_persona_version,
    list_persona_versions,
    rollback_persona_version,
)
from .prompt_compiler import FIXED_SAFETY_POLICY, compile_agent_prompt
from .security import verify_admin_token


router = APIRouter(
    prefix="/api/admin/agents",
    tags=["admin-personas"],
    dependencies=[Depends(verify_admin_token)],
)


@router.get("/{tenant_id}/readiness")
def admin_readiness(tenant_id: str, workspace_id: UUID, persona_key: str = DEFAULT_PERSONA_KEY):
    from .knowledge_indexing import readiness
    return readiness(get_active_persona(tenant_id, persona_key, str(workspace_id)), get_settings())


class KnowledgeDocument(BaseModel):
    model_config = {"extra": "allow"}
    id: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1, max_length=100000)
    status: Literal["approved", "draft"]
    valid_until: datetime | None = None


class KnowledgeDraft(BaseModel):
    knowledge_documents: list[KnowledgeDocument] = Field(min_length=1, max_length=100)


@router.post("/{tenant_id}/personas/{persona_id}/knowledge-draft")
def admin_knowledge_draft(tenant_id: str, persona_id: int, workspace_id: UUID, body: KnowledgeDraft):
    original = get_persona_version(persona_id, tenant_id=tenant_id)
    if original is None or original.workspace_id != str(workspace_id):
        raise HTTPException(404, "persona_not_found")
    documents = [document.model_dump(mode="json", exclude_none=True) for document in body.knowledge_documents]
    if len({d["id"] for d in documents}) != len(documents):
        raise HTTPException(400, "duplicate_document_id")
    from .published_knowledge import POLICY_TOPICS
    policy_topics = [d.get("topic") for d in documents if d.get("topic") in POLICY_TOPICS and d["status"] == "approved"]
    if len(set(policy_topics)) != len(policy_topics):
        raise HTTPException(400, "ambiguous_policy_topic")
    metadata = {**original.metadata, "knowledge_documents": documents}
    metadata.pop("knowledge_index", None)
    created = create_persona_version(instructions=original.instructions, name=original.name,
        tenant_id=tenant_id, persona_key=original.persona_key, workspace_id=str(workspace_id),
        metadata=metadata, status="draft", source="user", created_by="knowledge_admin")
    return {"persona_id": created.id, "status": "draft"}


@router.post("/{tenant_id}/personas/{persona_id}/knowledge-index")
def admin_index_knowledge(tenant_id: str, persona_id: int, workspace_id: UUID):
    from .openai_client import get_sync_openai_client
    from .persona_repository import index_draft_knowledge
    if not get_settings().openai_api_key:
        raise HTTPException(503, "openai_not_configured")
    try:
        return index_draft_knowledge(persona_id, tenant_id=tenant_id, workspace_id=str(workspace_id),
            client=get_sync_openai_client().with_options(max_retries=0, timeout=5))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503, "index_step_failed_retry_after_checking_readiness") from exc


class EvaluationCase(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    anonymized: Literal[True]
    required: list[str] = Field(default_factory=list, max_length=20)
    forbidden: list[str] = Field(default_factory=list, max_length=20)


@router.post("/{tenant_id}/quality-evaluation")
async def admin_quality_evaluation(tenant_id: str, workspace_id: UUID, body: EvaluationCase,
                                   persona_key: str = DEFAULT_PERSONA_KEY):
    from .quality_evaluation import compare_answer
    active = get_active_persona(tenant_id, persona_key, str(workspace_id))
    if active is None:
        raise HTTPException(404, "persona_active_missing")
    settings = get_settings()
    if not settings.openai_api_key:
        raise HTTPException(503, "openai_not_configured")
    return await compare_answer(active, body.question, settings=settings,
                                required=body.required, forbidden=body.forbidden)


class PersonaPublic(BaseModel):
    id: int | None = None
    tenant_id: str
    persona_key: str
    version: int
    name: str
    source: str
    instructions_hash: str
    status: str
    created_by: str | None = None
    activated_by: str | None = None
    created_at: Any = None
    activated_at: Any = None
    archived_at: Any = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    instructions: str | None = None


def _public(persona: PersonaVersion, *, include_instructions: bool = False) -> PersonaPublic:
    data = persona.model_dump()
    if not include_instructions:
        data["instructions"] = None
    return PersonaPublic.model_validate(data)


@router.get("/{tenant_id}/personas")
def admin_list_personas(
    tenant_id: str,
    persona_key: str = Query(default=DEFAULT_PERSONA_KEY),
    include_instructions: bool = Query(default=False),
) -> dict[str, Any]:
    items = list_persona_versions(tenant_id, persona_key)
    return {
        "ok": True,
        "tenant_id": tenant_id,
        "persona_key": persona_key,
        "items": [
            _public(item, include_instructions=include_instructions).model_dump()
            for item in items
        ],
    }


@router.get("/{tenant_id}/personas/active")
def admin_get_active_persona(
    tenant_id: str,
    persona_key: str = Query(default=DEFAULT_PERSONA_KEY),
    include_instructions: bool = Query(default=False),
) -> dict[str, Any]:
    active = get_active_persona(tenant_id, persona_key)
    if active is None:
        raise HTTPException(status_code=404, detail="persona_active_missing")
    return {
        "ok": True,
        "persona": _public(active, include_instructions=include_instructions).model_dump(),
    }


@router.post("/{tenant_id}/personas")
def admin_create_persona(
    tenant_id: str,
    body: PersonaVersionCreate,
    persona_key: str = Query(default=DEFAULT_PERSONA_KEY),
) -> dict[str, Any]:
    if not (body.instructions or "").strip():
        raise HTTPException(status_code=400, detail="instructions_required")
    created = create_persona_version(
        instructions=body.instructions,
        name=body.name,
        tenant_id=tenant_id,
        persona_key=persona_key,
        source="user",
        created_by=body.created_by or "admin_api",
        status="draft",
        metadata=body.metadata or {},
    )
    return {
        "ok": True,
        "persona": _public(created, include_instructions=True).model_dump(),
    }


@router.post("/{tenant_id}/personas/{persona_id}/activate")
def admin_activate_persona(
    tenant_id: str,
    persona_id: int,
    activated_by: str | None = Query(default="admin_api"),
) -> dict[str, Any]:
    try:
        persona = activate_persona_version(
            persona_id,
            tenant_id=tenant_id,
            activated_by=activated_by,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "ok": True,
        "persona": _public(persona, include_instructions=False).model_dump(),
    }


@router.post("/{tenant_id}/personas/{persona_id}/archive")
def admin_archive_persona(tenant_id: str, persona_id: int) -> dict[str, Any]:
    try:
        persona = archive_persona_version(persona_id, tenant_id=tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "ok": True,
        "persona": _public(persona, include_instructions=False).model_dump(),
    }


@router.post("/{tenant_id}/personas/{persona_id}/rollback")
def admin_rollback_persona(
    tenant_id: str,
    persona_id: int,
    activated_by: str | None = Query(default="admin_api"),
) -> dict[str, Any]:
    target = get_persona_version(persona_id, tenant_id=tenant_id)
    if target is None:
        raise HTTPException(status_code=404, detail="persona_not_found")
    try:
        persona = rollback_persona_version(
            persona_id,
            tenant_id=tenant_id,
            activated_by=activated_by,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "ok": True,
        "persona": _public(persona, include_instructions=False).model_dump(),
    }


@router.get("/{tenant_id}/prompt-preview")
def admin_prompt_preview(
    tenant_id: str,
    channel: str = Query(default="whatsapp"),
    persona_key: str = Query(default=DEFAULT_PERSONA_KEY),
    sender_key: str | None = Query(default=None),
    text: str = Query(default="Olá"),
) -> dict[str, Any]:
    """Safe debug preview of compiled instruction blocks (no secrets)."""
    settings = get_settings()
    masked_sender = None
    if sender_key:
        raw = str(sender_key)
        masked_sender = raw[:3] + "***" if len(raw) > 3 else "***"

    incoming = IncomingMessage(
        channel=channel,
        sender_key=masked_sender,
        text=(text or "")[:200],
    )
    compiled = compile_agent_prompt(
        incoming=incoming,
        tenant_id=tenant_id,
        persona_key=persona_key,
        fallback_instructions=None,
        audit=False,
    )
    return {
        "ok": True,
        "tenant_id": tenant_id,
        "persona_key": persona_key,
        "channel": channel,
        "sender_key_masked": masked_sender,
        "agent_db_persona_enabled": bool(
            getattr(settings, "agent_db_persona_enabled", False)
        ),
        "persona_version_id": compiled.persona_version_id,
        "used_db_persona": compiled.used_db_persona,
        "fallback_reason": compiled.fallback_reason,
        "compiled_instructions_hash": compiled.instructions_hash,
        "instruction_char_count": compiled.instruction_char_count,
        "approximate_input_tokens": compiled.approximate_input_tokens,
        "blocks": {
            "fixed_safety_policy": FIXED_SAFETY_POLICY[:500],
            "user_managed_persona_present": (
                "<user_managed_persona>" in compiled.instructions
            ),
            "channel_overlay_present": "<channel_overlay>" in compiled.instructions,
            "customer_memory_present": "<customer_memory>" in compiled.instructions,
        },
        "instructions_preview": compiled.instructions[:1500],
    }


class ExtensionCreateBody(BaseModel):
    extension_key: str
    instruction_text: str
    category: str = "tone"
    scope: str = "tenant"
    scope_key: str | None = None


@router.get("/{tenant_id}/instruction-extensions")
def admin_list_instruction_extensions(
    tenant_id: str,
    status: str = Query(default="pending_review"),
) -> dict[str, Any]:
    from .instruction_extension_repository import (
        list_active_extensions,
        list_pending_extensions,
    )

    if status == "active":
        items = list_active_extensions(tenant_id=tenant_id)
    else:
        items = list_pending_extensions(tenant_id=tenant_id)
    return {"ok": True, "tenant_id": tenant_id, "items": items}


@router.post("/{tenant_id}/instruction-extensions")
def admin_create_instruction_extension(
    tenant_id: str,
    body: ExtensionCreateBody,
) -> dict[str, Any]:
    from .instruction_extension_repository import create_extension_proposal

    created = create_extension_proposal(
        tenant_id=tenant_id,
        extension_key=body.extension_key,
        instruction_text=body.instruction_text,
        category=body.category,
        scope=body.scope,
        scope_key=body.scope_key,
        source="user",
    )
    return {"ok": True, "extension": created}


@router.post("/{tenant_id}/instruction-extensions/{extension_id}/approve")
def admin_approve_instruction_extension(
    tenant_id: str,
    extension_id: int,
    approved_by: str = Query(default="admin_api"),
) -> dict[str, Any]:
    from .instruction_extension_repository import approve_extension

    try:
        row = approve_extension(
            extension_id,
            tenant_id=tenant_id,
            approved_by=approved_by,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "extension": row}


@router.post("/{tenant_id}/instruction-extensions/{extension_id}/reject")
def admin_reject_instruction_extension(
    tenant_id: str,
    extension_id: int,
    rejected_by: str = Query(default="admin_api"),
    reason: str | None = Query(default=None),
) -> dict[str, Any]:
    from .instruction_extension_repository import reject_extension

    try:
        row = reject_extension(
            extension_id,
            tenant_id=tenant_id,
            rejected_by=rejected_by,
            rejection_reason=reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "extension": row}


@router.get("/{tenant_id}/contacts/{sender_key}/memories")
def admin_list_contact_memories(tenant_id: str, sender_key: str) -> dict[str, Any]:
    from .contact_memory_repository import get_active_contact_memories

    items = get_active_contact_memories(tenant_id=tenant_id, sender_key=sender_key)
    return {
        "ok": True,
        "tenant_id": tenant_id,
        "sender_key": sender_key,
        "items": [item.model_dump(mode="json") for item in items],
    }


class ContactMemoryCreateBody(BaseModel):
    memory_key: str
    memory_kind: str = "stable_customer_fact"
    value: Any
    safe_summary: str | None = None
    use_in_instructions: bool = True


@router.post("/{tenant_id}/contacts/{sender_key}/memories")
def admin_create_contact_memory(
    tenant_id: str,
    sender_key: str,
    body: ContactMemoryCreateBody,
) -> dict[str, Any]:
    from .contact_memory_repository import upsert_contact_memory

    created = upsert_contact_memory(
        tenant_id=tenant_id,
        sender_key=sender_key,
        memory_key=body.memory_key,
        memory_kind=body.memory_kind,
        value=body.value,
        safe_summary=body.safe_summary,
        source="admin",
        use_in_instructions=body.use_in_instructions,
        importance=1.0,
        confidence=1.0,
    )
    return {"ok": True, "memory": created.model_dump(mode="json")}


@router.delete("/{tenant_id}/contacts/{sender_key}/memories/{memory_key}")
def admin_forget_contact_memory(
    tenant_id: str,
    sender_key: str,
    memory_key: str,
) -> dict[str, Any]:
    from .contact_memory_repository import forget_contact_memory

    count = forget_contact_memory(
        tenant_id=tenant_id,
        sender_key=sender_key,
        memory_key=memory_key,
    )
    return {"ok": True, "forgotten": count}
