"""Persona-first lookup performed for every inbound turn.

This lookup is deliberately separate from answer generation.  It makes the
source order explicit and auditable even when a deterministic handler answers
without calling an LLM.  Volatile commerce facts still require an official
runtime source; finding them in persona text never makes them authoritative.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .persona_knowledge import retrieve_knowledge


class PersonaConsultation(BaseModel):
    checked: bool = True
    active_persona_found: bool = False
    relevant_information_found: bool = False
    persona_version_id: int | None = None
    source_ids: list[str] = Field(default_factory=list)
    fallback_to_auxiliary: bool = True
    reason: str | None = None
    relevant_knowledge: list[dict[str, Any]] = Field(default_factory=list, exclude=True)


def consult_active_persona(active: Any | None, query: str | None) -> PersonaConsultation:
    """Check the active persona before any auxiliary knowledge source."""
    if active is None:
        return PersonaConsultation(
            active_persona_found=False,
            reason="active_persona_missing",
        )

    persona_id = getattr(active, "id", None)
    documents: list[dict[str, Any]] = []
    instructions = str(getattr(active, "instructions", "") or "").strip()
    if instructions:
        documents.append(
            {
                "id": f"persona:{persona_id or 'active'}",
                "title": "Persona ativa",
                "status": "approved",
                "content": instructions,
            }
        )

    metadata = getattr(active, "metadata", None) or {}
    published = metadata.get("knowledge_documents")
    if isinstance(published, list):
        documents.extend(item for item in published if isinstance(item, dict))

    relevant = retrieve_knowledge(documents, str(query or ""))
    source_ids = list(dict.fromkeys(str(item["source"]) for item in relevant))
    found = bool(relevant)
    return PersonaConsultation(
        active_persona_found=True,
        relevant_information_found=found,
        persona_version_id=int(persona_id) if persona_id is not None else None,
        source_ids=source_ids,
        fallback_to_auxiliary=not found,
        reason=None if found else "persona_has_no_relevant_information",
        relevant_knowledge=relevant,
    )
