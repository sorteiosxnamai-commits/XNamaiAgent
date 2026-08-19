"""In-memory persona store for unit tests (no Postgres required)."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any


class InMemoryPersonaStore:
    def __init__(self) -> None:
        self.personas: list[dict[str, Any]] = []
        self.compilations: list[dict[str, Any]] = []
        self._next_id = 1
        self._next_compilation_id = 1

    def reset(self) -> None:
        self.personas.clear()
        self.compilations.clear()
        self._next_id = 1
        self._next_compilation_id = 1

    def install(self, monkeypatch) -> "InMemoryPersonaStore":
        store = self

        def get_active_persona(tenant_id="newstore", persona_key="newstore_commercial"):
            from app.persona_models import PersonaVersion

            for row in store.personas:
                if (
                    row["tenant_id"] == tenant_id
                    and row["persona_key"] == persona_key
                    and row["status"] == "active"
                ):
                    return PersonaVersion.model_validate(row)
            return None

        def list_persona_versions(tenant_id="newstore", persona_key="newstore_commercial"):
            from app.persona_models import PersonaVersion

            rows = [
                row
                for row in store.personas
                if row["tenant_id"] == tenant_id and row["persona_key"] == persona_key
            ]
            rows.sort(key=lambda item: item["version"], reverse=True)
            return [PersonaVersion.model_validate(row) for row in rows]

        def get_persona_version(persona_id, *, tenant_id="newstore"):
            from app.persona_models import PersonaVersion

            for row in store.personas:
                if row["id"] == persona_id and row["tenant_id"] == tenant_id:
                    return PersonaVersion.model_validate(row)
            return None

        def create_persona_version(
            *,
            instructions,
            name="NewStore Commercial",
            tenant_id="newstore",
            persona_key="newstore_commercial",
            source="user",
            created_by=None,
            status="draft",
            metadata=None,
        ):
            from app.persona_models import PersonaVersion
            from app.persona_repository import hash_instructions

            versions = [
                row["version"]
                for row in store.personas
                if row["tenant_id"] == tenant_id and row["persona_key"] == persona_key
            ]
            version = (max(versions) if versions else 0) + 1
            now = datetime.now(timezone.utc)
            row = {
                "id": store._next_id,
                "tenant_id": tenant_id,
                "persona_key": persona_key,
                "version": version,
                "name": name,
                "source": source,
                "instructions": instructions,
                "instructions_hash": hash_instructions(instructions),
                "status": status,
                "created_by": created_by,
                "activated_by": None,
                "created_at": now,
                "activated_at": None,
                "archived_at": None,
                "metadata": deepcopy(metadata or {}),
            }
            store._next_id += 1
            store.personas.append(row)
            return PersonaVersion.model_validate(row)

        def activate_persona_version(
            persona_id, *, tenant_id="newstore", activated_by=None
        ):
            from app.persona_models import PersonaVersion

            target = None
            for row in store.personas:
                if row["id"] == persona_id and row["tenant_id"] == tenant_id:
                    target = row
                    break
            if target is None:
                raise ValueError("persona_not_found")
            now = datetime.now(timezone.utc)
            for row in store.personas:
                if (
                    row["tenant_id"] == tenant_id
                    and row["persona_key"] == target["persona_key"]
                    and row["status"] == "active"
                    and row["id"] != persona_id
                ):
                    row["status"] = "archived"
                    row["archived_at"] = now
            target["status"] = "active"
            target["activated_by"] = activated_by
            target["activated_at"] = now
            target["archived_at"] = None
            return PersonaVersion.model_validate(target)

        def archive_persona_version(persona_id, *, tenant_id="newstore"):
            from app.persona_models import PersonaVersion

            for row in store.personas:
                if row["id"] == persona_id and row["tenant_id"] == tenant_id:
                    row["status"] = "archived"
                    row["archived_at"] = datetime.now(timezone.utc)
                    return PersonaVersion.model_validate(row)
            raise ValueError("persona_not_found")

        def rollback_persona_version(
            persona_id, *, tenant_id="newstore", activated_by=None
        ):
            return activate_persona_version(
                persona_id, tenant_id=tenant_id, activated_by=activated_by
            )

        def find_persona_by_hash(
            *, tenant_id, persona_key, instructions_hash, version=None
        ):
            from app.persona_models import PersonaVersion

            matches = [
                row
                for row in store.personas
                if row["tenant_id"] == tenant_id
                and row["persona_key"] == persona_key
                and row["instructions_hash"] == instructions_hash
                and (version is None or row["version"] == version)
            ]
            if not matches:
                return None
            matches.sort(key=lambda item: item["version"], reverse=True)
            return PersonaVersion.model_validate(matches[0])

        def insert_prompt_compilation(**kwargs):
            cid = store._next_compilation_id
            store._next_compilation_id += 1
            store.compilations.append({"id": cid, **kwargs})
            return cid

        import app.persona_admin_api as admin
        import app.persona_repository as repo
        import app.prompt_compiler as compiler

        bindings = {
            "get_active_persona": get_active_persona,
            "list_persona_versions": list_persona_versions,
            "get_persona_version": get_persona_version,
            "create_persona_version": create_persona_version,
            "activate_persona_version": activate_persona_version,
            "archive_persona_version": archive_persona_version,
            "rollback_persona_version": rollback_persona_version,
            "find_persona_by_hash": find_persona_by_hash,
            "insert_prompt_compilation": insert_prompt_compilation,
        }
        modules = [repo, compiler, admin]
        try:
            import scripts.seed_newstore_persona as seed

            modules.append(seed)
        except Exception:
            pass

        for mod in modules:
            for name, fn in bindings.items():
                if hasattr(mod, name):
                    monkeypatch.setattr(mod, name, fn)

        # Expose fakes for direct use in tests.
        self.fakes = bindings
        return self
