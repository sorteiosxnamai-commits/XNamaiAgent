#!/usr/bin/env python3
"""Publish the current XNamai persona as an idempotent active DB version."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.persona_policy import assert_persona_instructions_safe  # noqa: E402
from app.persona_repository import (  # noqa: E402
    DEFAULT_PERSONA_KEY,
    DEFAULT_TENANT_ID,
    DEFAULT_WORKSPACE_ID,
    activate_persona_version,
    create_persona_version,
    find_persona_by_hash,
    get_active_persona,
    hash_instructions,
    list_persona_versions,
)


PERSONA_PATH = ROOT / "persona_xnamai.txt"
PERSONA_NAME = "Mai — Consultora Digital XNamai"


def persona_metadata() -> dict:
    return {
        "content_schema_version": 2,
        "brand": "XNamai",
        "persona_display_name": "Mai",
        "role": "assistente comercial virtual",
        "archetype": "consultora de confiança",
        "audience": [
            "lojistas",
            "revendedores",
            "compradores empresariais",
            "clientes em busca de informações",
        ],
        "voice_traits": [
            "cordial",
            "acolhedora",
            "prática",
            "paciente",
            "segura",
            "resolutiva",
        ],
        "official_channels": {
            "institutional": "https://www.xnamai.com/",
            "catalog": "https://xnamai.meuspedidos.com.br/",
            "club": "https://www.clubxnamai.com.br/",
        },
        "channel_guidance": [
            "whatsapp",
            "instagram",
            "facebook",
            "widget",
            "catalog",
            "club",
        ],
        "source_file": PERSONA_PATH.name,
        "based_on": "análise dos canais oficiais e sugestão aprovada pelo usuário",
    }


def publish() -> dict:
    instructions = PERSONA_PATH.read_text(encoding="utf-8")
    assert_persona_instructions_safe(instructions)
    instructions_hash = hash_instructions(instructions)

    target = find_persona_by_hash(
        tenant_id=DEFAULT_TENANT_ID,
        persona_key=DEFAULT_PERSONA_KEY,
        instructions_hash=instructions_hash,
        workspace_id=DEFAULT_WORKSPACE_ID,
    )
    action = "reused_existing"
    if target is None:
        target = create_persona_version(
            instructions=instructions,
            name=PERSONA_NAME,
            tenant_id=DEFAULT_TENANT_ID,
            persona_key=DEFAULT_PERSONA_KEY,
            workspace_id=DEFAULT_WORKSPACE_ID,
            source="user",
            created_by="codex_user_request",
            status="draft",
            metadata=persona_metadata(),
        )
        action = "created"

    active_before = get_active_persona(
        DEFAULT_TENANT_ID, DEFAULT_PERSONA_KEY, DEFAULT_WORKSPACE_ID
    )
    activated = active_before is None or active_before.id != target.id
    if activated:
        target = activate_persona_version(
            target.id,
            tenant_id=DEFAULT_TENANT_ID,
            activated_by="codex_user_request",
        )

    versions = list_persona_versions(DEFAULT_TENANT_ID, DEFAULT_PERSONA_KEY)
    active_after = get_active_persona(
        DEFAULT_TENANT_ID, DEFAULT_PERSONA_KEY, DEFAULT_WORKSPACE_ID
    )
    if active_after is None or active_after.id != target.id:
        raise RuntimeError("persona_activation_verification_failed")

    return {
        "action": action,
        "activated": activated,
        "persona_id": target.id,
        "version": target.version,
        "status": target.status,
        "name": target.name,
        "hash": target.instructions_hash,
        "active_before_id": getattr(active_before, "id", None),
        "versions": [
            {"id": item.id, "version": item.version, "status": item.status}
            for item in versions
        ],
    }


if __name__ == "__main__":
    print(json.dumps(publish(), ensure_ascii=False))
