#!/usr/bin/env python3
"""Idempotent seed for NewStore commercial persona version 1."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.persona_repository import (  # noqa: E402
    DEFAULT_PERSONA_KEY,
    DEFAULT_TENANT_ID,
    activate_persona_version,
    create_persona_version,
    find_persona_by_hash,
    get_active_persona,
    hash_instructions,
    list_persona_versions,
)


PERSONA_CANDIDATES = (
    ROOT / "persona NS.txt",
    ROOT / "persona_NS.txt",
    ROOT / "assets" / "persona NS.txt",
)


def load_persona_text(path: Path | None = None) -> tuple[Path, str]:
    candidates = [path] if path else list(PERSONA_CANDIDATES)
    for candidate in candidates:
        if candidate is None:
            continue
        if candidate.is_file():
            raw = candidate.read_bytes()
            # Prefer UTF-8; fall back to latin-1 for legacy Windows exports.
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("latin-1")
            normalized = text.replace("\r\n", "\n").replace("\r", "\n")
            if not normalized.endswith("\n"):
                normalized += "\n"
            return candidate, normalized
    raise FileNotFoundError(
        "persona NS.txt não encontrado. Adicione o arquivo na raiz do projeto "
        "(conteúdo exato fornecido pelo administrador) e rode o seed novamente."
    )


def seed_persona(
    *,
    persona_path: Path | None = None,
    activate: bool = True,
    tenant_id: str = DEFAULT_TENANT_ID,
    persona_key: str = DEFAULT_PERSONA_KEY,
) -> dict:
    path, instructions = load_persona_text(persona_path)
    instructions_hash = hash_instructions(instructions)

    existing_v1 = find_persona_by_hash(
        tenant_id=tenant_id,
        persona_key=persona_key,
        instructions_hash=instructions_hash,
        version=1,
    )
    if existing_v1 is None:
        # Version may exist with different content — never overwrite.
        versions = list_persona_versions(tenant_id, persona_key)
        v1 = next((item for item in versions if item.version == 1), None)
        if v1 is not None:
            return {
                "action": "skipped_existing_different_v1",
                "path": str(path),
                "persona_id": v1.id,
                "hash": v1.instructions_hash,
                "active": v1.status == "active",
            }
        created = create_persona_version(
            instructions=instructions,
            name="NewStore Commercial",
            tenant_id=tenant_id,
            persona_key=persona_key,
            source="user",
            created_by="seed_newstore_persona",
            status="draft",
            metadata={"seed_file": path.name},
        )
        persona = created
        action = "inserted_v1"
    else:
        persona = existing_v1
        action = "already_present"

    active = get_active_persona(tenant_id, persona_key)
    activated = False
    if activate and active is None and persona.id is not None:
        activate_persona_version(
            persona.id,
            tenant_id=tenant_id,
            activated_by="seed_newstore_persona",
        )
        activated = True
    elif active is not None and active.id != persona.id:
        # Never overwrite a different active persona.
        return {
            "action": action,
            "path": str(path),
            "persona_id": persona.id,
            "hash": instructions_hash,
            "active": False,
            "note": "active_persona_preserved",
            "active_persona_id": active.id,
        }

    return {
        "action": action,
        "path": str(path),
        "persona_id": persona.id,
        "hash": instructions_hash,
        "activated": activated,
        "active": True if activated or (active and active.id == persona.id) else False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed NewStore persona v1")
    parser.add_argument("--file", type=Path, default=None, help="Path to persona NS.txt")
    parser.add_argument("--no-activate", action="store_true")
    args = parser.parse_args()
    result = seed_persona(persona_path=args.file, activate=not args.no_activate)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
